"""Portable evidence for one IES Optimiser solve.

The bundle is a consistency-checked record, not a signature or an independent
verification of the optimisation equations. No solver is run by read_bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator, Mapping, cast

from ies_optimiser import api, fcn
from ies_optimiser.errors import InputError
from ies_optimiser.results import Result


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: Any) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def _case_digest(document: Mapping[str, Any]) -> str:
    return _digest(json.dumps(api.to_canonical(document), sort_keys=True, separators=(',', ':'),
                              allow_nan=False).encode('utf-8'))


def _profile_fields(case: dict[str, Any]) -> Iterator[tuple[list[str | int], dict[str, Any]]]:
    for group in ('generator', 'p2x'):
        for i, item in enumerate(case[group]):
            yield [group, i, 'profile'], item
    for i, item in enumerate(case['flex']):
        yield ['flex', i, 'inflow_profile'], item
    yield ['demand', 'e', 'profile'], case['demand']['e']
    for i, item in enumerate(case['demand']['x']):
        yield ['demand', 'x', i, 'profile'], item


def _at(case: dict[str, Any], pointer: list[str | int]) -> dict[str, Any]:
    node: Any = case
    for key in pointer[:-1]:
        node = node[key]
    return cast(dict[str, Any], node)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError('cannot read ' + str(path) + ': ' + str(exc)) from exc


def _safe_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise ValueError('bundle path must be text')
    parts = Path(relative).parts
    if not parts or any(p in ('', '.', '..') for p in parts) or Path(relative).is_absolute() or '\\' in relative:
        raise ValueError('unsafe bundle path: ' + relative)
    path = root.joinpath(*parts)
    if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root and root in parent.parents):
        raise ValueError('linked bundle path: ' + relative)
    if path.is_symlink() or not path.is_file():
        raise ValueError('missing or linked bundle file: ' + relative)
    return path


def _write(root: Path, relative: str, data: bytes, files: dict[str, dict[str, Any]]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    files[relative] = {'sha256': _digest(data), 'size_bytes': len(data)}


def _profile_name(digest: str, original: str) -> str:
    suffix = ''.join(Path(original).suffixes).lower()
    if not re.fullmatch(r'(\.[a-z0-9]{1,8}){1,3}', suffix or ''):
        suffix = '.csv'
    return 'inputs/profiles/' + digest + suffix


def save_bundle(case_path: str | os.PathLike[str], directory: str | os.PathLike[str], *,
                options: Mapping[str, Any] | None = None,
                config: fcn.RunConfig | None = None) -> dict[str, Any]:
    """Solve a path-based case once into a new, checked directory.

    The destination must not exist. An invalid case raises InputError, leaving
    no bundle. A non-optimal solve is retained with its status and provenance.
    The packaged thermodynamics executable is required for portable replay.
    """
    source = Path(case_path).absolute()
    destination = Path(directory).absolute()
    cfg = config if config is not None else fcn.RunConfig()
    if cfg.thermo_bin != fcn.Thermo_bin:
        raise InputError('bundles require the packaged thermodynamics executable',
                         entity='thermo_bin', code='bundle.thermo_override', layer='configuration')
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('bundle destination exists: ' + str(destination))
    if not destination.parent.is_dir():
        raise FileNotFoundError('bundle parent directory does not exist: ' + str(destination.parent))

    # Keep the source bytes before the solve, then require provenance to name
    # those bytes. Input changes during a solve are refused, not hidden.
    source_case = api.load_input(source)
    source_bytes = source.read_bytes()
    canonical = api.to_canonical(source_case)
    opts = api.check_options(options)
    base = Path(os.path.abspath(str(cfg.profile_base))) if cfg.profile_base is not None else source.parent
    # Snapshot every external input before the solve. The engine's provenance
    # hashes files after solving, so comparing only with those hashes could
    # otherwise retain bytes that changed while the solver was running.
    before: dict[Path, bytes] = {}
    snapshot_error: OSError | None = None
    try:
        for pointer, item in _profile_fields(canonical):
            declared = item[cast(str, pointer[-1])]
            if isinstance(declared, str) and declared:
                path = Path(declared)
                resolved = Path(os.path.abspath(str(path if path.is_absolute() else base / path)))
                before[resolved] = resolved.read_bytes()
        floor_option = opts.get('hourly-coverage-floor')
        if isinstance(floor_option, str):
            path = Path(floor_option)
            resolved = Path(os.path.abspath(str(path if path.is_absolute() else base / path)))
            before[resolved] = resolved.read_bytes()
    except OSError as exc:
        # Let solve report its normal structured file diagnostic. If the file
        # becomes available later, still refuse a bundle without a pre-solve
        # snapshot.
        snapshot_error = exc
    result = api.solve(source, options=opts, config=cfg)
    if snapshot_error is not None:
        raise ValueError('external input could not be snapshotted before the run: ' + str(snapshot_error))
    provenance = result.document['provenance']
    if _digest(source_bytes) != provenance['input_sha256'] or source.read_bytes() != source_bytes:
        raise ValueError('source case changed during the run')
    if _case_digest(source_case) != provenance['case_sha256']:
        raise ValueError('source case does not match the solved case identity')

    staging = Path(tempfile.mkdtemp(prefix='.' + destination.name + '.staging-', dir=destination.parent))
    try:
        files: dict[str, dict[str, Any]] = {}
        _write(staging, 'inputs/source-case.json', source_bytes, files)
        replay = json.loads(json.dumps(canonical))
        references: list[dict[str, Any]] = []
        profile_digests = provenance['profiles_sha256']
        for pointer, item in _profile_fields(replay):
            field = cast(str, pointer[-1])
            declared = item[field]
            if not isinstance(declared, str) or not declared:
                continue
            original = Path(declared)
            resolved = Path(os.path.abspath(str(original if original.is_absolute() else base / original)))
            data = before[resolved]
            if resolved.read_bytes() != data:
                raise ValueError('profile changed during the run: ' + declared)
            digest = _digest(data)
            if profile_digests.get(declared) != digest:
                raise ValueError('profile changed during the run: ' + declared)
            relative = _profile_name(digest, declared)
            if relative not in files:
                _write(staging, relative, data, files)
            item[field] = relative.removeprefix('inputs/')
            references.append({'pointer': pointer, 'declared': declared,
                               'bundle_path': relative, 'sha256': digest})

        floor = provenance.get('hourly_coverage_floor')
        if floor and floor.get('form') == 'csv':
            resolved = floor['resolved']
            data = before[Path(resolved)]
            if Path(resolved).read_bytes() != data:
                raise ValueError('hourly coverage floor file changed during the run')
            if _digest(data) != floor['file_sha256']:
                raise ValueError('hourly coverage floor file changed during the run')
            _write(staging, 'inputs/coverage-floor-source.csv', data, files)

        _write(staging, 'inputs/case.json', _json(replay), files)
        _write(staging, 'inputs/options.json', _json(provenance['options']), files)
        _write(staging, 'inputs/config.json', _json({
            'hours': cfg.hours, 'storage_closes_the_year': cfg.storage_closes_the_year,
            'profile_base': None, 'thermo_timeout': cfg.thermo_timeout}), files)
        _write(staging, 'result.json', _json(result.document), files)
        outcome = ('non_optimal' if not result.optimal else
                   'success' if result.accounting_ok else 'accounting_failure')
        manifest = {'format_version': 1, 'outcome': outcome, 'status': result.status,
                    'source': str(source), 'profile_references': references,
                    'files': files}
        (staging / 'manifest.json').write_bytes(_json(manifest))
        read_bundle(staging)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('bundle destination exists: ' + str(destination))
        staging.rename(destination)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _read_bundle(directory: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify saved bytes, replay references, provenance and result shape.

    This does not solve the model, attest data sources or authenticate the
    manifest. Returns the manifest or raises ValueError.
    """
    root = Path(directory).absolute()
    manifest = _load_json(_safe_file(root, 'manifest.json'))
    if not isinstance(manifest, dict) or manifest.get('format_version') != 1:
        raise ValueError('unsupported bundle manifest')
    files = manifest.get('files')
    if not isinstance(files, dict):
        raise ValueError('bundle file index is missing')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    if actual != set(files) | {'manifest.json'}:
        raise ValueError('bundle file index does not match directory contents')
    for relative, record in files.items():
        if not isinstance(record, dict):
            raise ValueError('invalid bundle file record')
        path = _safe_file(root, relative)
        data = path.read_bytes()
        if _digest(data) != record.get('sha256') or len(data) != record.get('size_bytes'):
            raise ValueError('bundle file changed: ' + relative)

    source = _load_json(root / 'inputs/source-case.json')
    replay = _load_json(root / 'inputs/case.json')
    options = _load_json(root / 'inputs/options.json')
    cfg = _load_json(root / 'inputs/config.json')
    result = _load_json(root / 'result.json')
    try:
        Result.model_validate(result)
    except Exception as exc:
        raise ValueError('invalid result document: ' + str(exc)) from exc
    provenance = result['provenance']
    if _digest((root / 'inputs/source-case.json').read_bytes()) != provenance['input_sha256']:
        raise ValueError('source file does not match result provenance')
    if _case_digest(source) != provenance['case_sha256']:
        raise ValueError('source case does not match result provenance')
    if options != provenance['options']:
        raise ValueError('replay options differ from the result')
    if cfg['hours'] != provenance['hours'] or cfg['storage_closes_the_year'] != provenance['storage_closes_the_year']:
        raise ValueError('replay configuration differs from the result')
    if cfg['profile_base'] is not None:
        raise ValueError('replay profile base must be the bundle case directory')
    floor = provenance.get('hourly_coverage_floor')
    if floor is not None:
        values = options.get('hourly-coverage-floor')
        if not isinstance(values, list) or len(values) != cfg['hours'] or fcn.floor_digest(values) != floor['values_sha256']:
            raise ValueError('effective coverage floor differs from provenance')
    if manifest['status'] != result['solver']['stat_status']:
        raise ValueError('run status differs from result')
    expected_outcome = ('non_optimal' if result['solver']['stat_succ'] == 0 else
                        'success' if result['system']['accounting_ok'] else 'accounting_failure')
    if manifest['outcome'] != expected_outcome:
        raise ValueError('run outcome differs from result')
    if result['solver']['stat_succ'] == 1:
        checks = result['system']['checks']
        if result['system']['accounting_ok'] != all(check['ok'] for check in checks.values()):
            raise ValueError('accounting outcome differs from recorded checks')

    restored = json.loads(json.dumps(replay))
    for ref in manifest['profile_references']:
        pointer = ref['pointer']
        relative = ref['bundle_path']
        path = _safe_file(root, relative)
        if _digest(path.read_bytes()) != ref['sha256']:
            raise ValueError('profile reference has wrong bytes')
        if _at(replay, pointer)[pointer[-1]] != relative.removeprefix('inputs/'):
            raise ValueError('replay case has changed a profile reference')
        if provenance['profiles_sha256'].get(ref['declared']) != ref['sha256']:
            raise ValueError('profile differs from result provenance')
        _at(restored, pointer)[pointer[-1]] = ref['declared']
    if restored != api.to_canonical(source):
        raise ValueError('replay case does not reconstruct the source case')
    if floor is not None and floor.get('form') == 'csv':
        data = _safe_file(root, 'inputs/coverage-floor-source.csv').read_bytes()
        if _digest(data) != floor['file_sha256']:
            raise ValueError('coverage floor source differs from provenance')
        restored_floor, _ = fcn.resolve_coverage_floor('inputs/coverage-floor-source.csv',
                                                       fcn.RunConfig(hours=cfg['hours'], profile_base=str(root)))
        if restored_floor != values:
            raise ValueError('coverage floor source differs from effective options')
    return manifest


def read_bundle(directory: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify a bundle without solving; return its manifest or raise ValueError."""
    try:
        return _read_bundle(directory)
    except (KeyError, IndexError, TypeError, AttributeError, InputError) as exc:
        raise ValueError('malformed bundle: ' + str(exc)) from exc
