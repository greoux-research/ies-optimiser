"""The portable run contract, using a short real solve."""

import json

import pytest

from ies_optimiser import RunConfig, api, read_bundle, save_bundle
from ies_optimiser import bundle, cli, fcn, schemas
from conftest import generator, system


def _case(tmp_path):
    profile = tmp_path / 'sun.csv'
    profile.write_text('\n'.join(['1'] * 24), encoding='utf-8')
    floors = tmp_path / 'floors.csv'
    floors.write_text('\n'.join(['0.9'] * 24), encoding='utf-8')
    doc = api.to_canonical(system(generators=[generator('solar', fix=1, var=10, c_prod=1,
                                                        profile='sun.csv')], e_total=24))
    case = tmp_path / 'case.json'
    case.write_text(json.dumps(doc), encoding='utf-8')
    return case


def test_bundle_is_portable_and_replayable(tmp_path):
    case = _case(tmp_path)
    directory = tmp_path / 'run'
    manifest = save_bundle(case, directory, options={'hourly-coverage-floor': 'floors.csv'},
                           config=RunConfig(hours=24))
    assert manifest['outcome'] == 'success'
    assert read_bundle(directory) == manifest
    replay = api.solve(directory / 'inputs/case.json', options=json.loads(
        (directory / 'inputs/options.json').read_text()), config=RunConfig(hours=24))
    original = json.loads((directory / 'result.json').read_text())
    assert replay.optimal and replay.accounting_ok
    assert replay.document['system']['cost'] == pytest.approx(original['system']['cost'])
    assert manifest['profile_references'][0]['declared'] == 'sun.csv'
    with pytest.raises(FileExistsError):
        save_bundle(case, directory, config=RunConfig(hours=24))


def test_bundle_detects_changed_bytes(tmp_path):
    case = _case(tmp_path)
    directory = tmp_path / 'run'
    save_bundle(case, directory, config=RunConfig(hours=24))
    next((directory / 'inputs/profiles').glob('*.csv')).write_text('0\n', encoding='utf-8')
    with pytest.raises(ValueError, match='bundle file changed'):
        read_bundle(directory)


def test_bundle_refuses_profile_changed_during_solve(tmp_path, monkeypatch):
    case = _case(tmp_path)
    real_provenance = fcn.provenance

    def provenance_after_change(*args, **kwargs):
        (tmp_path / 'sun.csv').write_text('\n'.join(['0.5'] * 24), encoding='utf-8')
        return real_provenance(*args, **kwargs)

    monkeypatch.setattr(fcn, 'provenance', provenance_after_change)
    with pytest.raises(ValueError, match='profile changed during the run'):
        save_bundle(case, tmp_path / 'run', config=RunConfig(hours=24))
    assert not (tmp_path / 'run').exists()


@pytest.mark.parametrize('floor', [0.9, [0.9] * 24])
def test_inline_coverage_floor_replays(tmp_path, floor):
    case = _case(tmp_path)
    directory = tmp_path / 'run'
    save_bundle(case, directory, options={'hourly-coverage-floor': floor}, config=RunConfig(hours=24))
    assert read_bundle(directory)['outcome'] == 'success'
    assert not (directory / 'inputs/coverage-floor-source.csv').exists()


def test_non_optimal_bundle_has_no_numerical_result(tmp_path):
    doc = api.to_canonical(system(generators=[generator('small', c_prod=0.5, l_prod=[0, 0.5])],
                                  e_total=24, e_kwargs={'l_ns': [0, 0]}))
    case = tmp_path / 'case.json'
    case.write_text(json.dumps(doc), encoding='utf-8')
    directory = tmp_path / 'run'
    manifest = save_bundle(case, directory, config=RunConfig(hours=24))
    assert manifest['outcome'] == 'non_optimal'
    result = json.loads((directory / 'result.json').read_text())
    assert result['solver']['stat_succ'] == 0 and 'system' not in result
    assert read_bundle(directory)['outcome'] == 'non_optimal'


def test_guide_options_schema_and_json_refusal(tmp_path, capsys):
    assert cli.main(['guide']) == 0
    assert 'Validate, run and keep evidence' in capsys.readouterr().out
    assert cli.main(['schema', 'options']) == 0
    schema = json.loads(capsys.readouterr().out)
    assert 'hourly-coverage-floor' in schema['properties']
    assert schema == json.loads(schemas.generate('options'))
    assert cli.main(['run', str(tmp_path / 'missing.json'), '--output', str(tmp_path / 'run'), '--json']) == 1
    refusal = json.loads(capsys.readouterr().out)
    assert refusal['outcome'] == 'input_invalid' and refusal['diagnostics'][0]['code'] == 'input.unreadable'
    assert not (tmp_path / 'run').exists()


def test_run_command_reports_the_bundle_outcome_as_json(tmp_path, capsys, monkeypatch):
    captured = {}

    def save(case, directory, *, options, config):
        captured.update(case=case, directory=directory, options=options, config=config)
        return {'outcome': 'success', 'status': 'optimal'}

    monkeypatch.setattr(bundle, 'save_bundle', save)
    directory = tmp_path / 'run'
    assert cli.main(['run', 'case.json', '--output', str(directory), 'hourly-coverage-floor=0.9', '--json']) == 0
    document = json.loads(capsys.readouterr().out)
    assert document == {'command': 'run', 'outcome': 'success', 'status': 'optimal',
                        'bundle': str(directory), 'diagnostics': []}
    assert captured['options'] == {'hourly-coverage-floor': 0.9}
    assert captured['config'].hours == 8760
