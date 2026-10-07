"""Hourly floor contract, including public discovery, diagnostics and numerical derivatives."""
import builtins
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import jsonschema
import numpy as np
from pydantic import ValidationError
import pytest

from ies_optimiser import api, RunConfig, InputError, schemas
from ies_optimiser import fcn as u
from ies_optimiser.models import SolveOptions
from ies_optimiser.results import Result
from conftest import generator, system, demand_x, p2x

KEY = 'hourly-coverage-floor'


def case(dm=(10., 10.), high=1e6, low=0, profile=(1., .5)):
    return system(generators=[generator('gen', fix=3., profile=list(profile), cf=np.mean(profile))],
                  e_total=sum(dm), e_kwargs={'profile': list(dm), 'var_cost_ns': 0., 'l_ns': [low, high]})


def solve(c=None, value=.6, **kw):
    c = case() if c is None else c
    return api.solve(c, options={KEY: value}, config=RunConfig(hours=2 if isinstance(c['demand']['e']['profile'], str) else len(c['demand']['e']['profile'])), **kw)


def numeric(result):
    d = copy.deepcopy(result.document)
    d.pop('provenance')
    d['solver'].pop('stat_time')
    return d


def test_capacity_and_derivatives():
    r = solve()
    d = r.document['demand']['e']
    assert r.optimal and r.accounting_ok
    assert r.document['generator'][0]['c_prod'] == pytest.approx(12.)
    assert r.objective == pytest.approx(36.)
    assert d['output_ns'][1] == pytest.approx(4.)
    assert d['shadow_prices']['demand_match'][1] == pytest.approx(6.)
    marginal = d['shadow_prices']['demand_marginal'][1]
    assert marginal == pytest.approx(3.6)
    eps = 1e-4
    # Set total and profile to literal MW so only the tested hour changes.
    derivative = (solve(case(dm=(10., 10. + eps))).objective -
                  solve(case(dm=(10., 10. - eps))).objective) / (2 * eps)
    assert derivative == pytest.approx(marginal)
    local = (solve(value=[.6, .6 + eps]).objective - solve(value=[.6, .6 - eps]).objective) / (2 * eps)
    uniform = (solve(value=.6 + eps).objective - solve(value=.6 - eps).objective) / (2 * eps)
    assert local == pytest.approx(60.)
    assert uniform == pytest.approx(60.)
    assert -10. * (marginal - 6.) / .4 == pytest.approx(local)


def test_equivalent_forms_and_replay(tmp_path):
    path = tmp_path / 'a,b.csv'
    raw = b'0.9\n0.9\n'
    path.write_bytes(raw)
    forms = [.9, [.9, .9], u.parse_options([KEY + '=0.9,0.9'])[KEY], str(path)]
    results = [solve(value=v) for v in forms]
    assert all(numeric(r) == numeric(results[0]) for r in results)
    origins = [r.document['provenance']['hourly_coverage_floor'] for r in results]
    assert {o['values_sha256'] for o in origins} == {u.floor_digest([.9, .9])}
    assert [o['form'] for o in origins] == ['scalar', 'list', 'list', 'csv']
    assert origins[0]['declared'] == .9 and origins[1]['declared'] is None
    assert origins[-1]['file_sha256'] == hashlib.sha256(raw).hexdigest()
    assert origins[-1]['resolved'] == str(path)
    names = [api.output_path('case.json', {KEY: v}, floor_sha256=o['values_sha256'])
             for v, o in zip(forms, origins)]
    assert names[1] == names[2] == names[3]
    assert names[0].endswith('hourly-coverage-floor_0.9.json')
    path.unlink()
    for r in results:
        assert r.document['provenance']['options'][KEY] == [.9, .9]
        replay = api.solve(case(), options=r.document['provenance']['options'], config=RunConfig(hours=2))
        assert numeric(replay) == numeric(r)
        Result.model_validate(r.document)
        jsonschema.validate(r.document, schemas.result_schema())


@pytest.mark.parametrize('dm,floor', [((5., 15.), .8), ((0., 20.), 1.), ((10., 10.), 1.), ((0., 0.), .9)])
def test_boundary_demand(dm, floor):
    c = case(dm=dm)
    # An all-zero demand profile is not a profile: use flat for the zero-total case.
    if not sum(dm):
        c['demand']['e']['profile'] = ''
    r = api.solve(c, options={KEY: floor}, config=RunConfig(hours=2))
    assert r.optimal and r.accounting_ok
    assert np.all(np.array(r.document['demand']['e']['output_ns']) <= (1-floor)*np.array(dm)+1e-8)
    assert r.document['system']['checks']['coverage_floor']['ok']


def test_zero_and_absence_have_same_numbers():
    absent = api.solve(case(), config=RunConfig(hours=2))
    zero = solve(value=[0., 0.])
    a, z = numeric(absent), numeric(zero)
    assert 'coverage_floor' not in a['system']['checks']
    z['system']['checks'].pop('coverage_floor')
    assert a == z
    assert 'hourly_coverage_floor' not in absent.document['provenance']
    for key in ('stat_capa', 'stat_outp', 'stat_cons'):
        assert absent.document['solver'][key] == zero.document['solver'][key]


@pytest.mark.parametrize('value,code,hour', [
    (None, 'value.type', None), (True, 'value.type', None), ((.9,.9), 'value.type', None),
    (np.array([.9,.9]), 'value.type', None), (Path('f.csv'), 'value.type', None),
    ([[.9]], 'value.type', 0), ([False], 'value.type', 0), (['.9'], 'value.type', 0),
    ('', 'value.type', None), (float('nan'), 'option.floor_range', None),
    (float('inf'), 'option.floor_range', None), (-.1, 'option.floor_range', None),
    (1.1, 'option.floor_range', None), ([.9, float('inf')], 'option.floor_range', 1),
    ([.9, float('nan')], 'option.floor_range', 1), ([.9, 1.1], 'option.floor_range', 1)])
def test_structural_refusals(value, code, hour):
    with pytest.raises(InputError) as e:
        api.check_options({KEY: value})
    d = e.value.diagnostics[0]
    assert (d.code, d.layer, d.entity, d.field, d.path, d.hour) == (code, 'options', 'command line', KEY, None, hour)
    assert api.validate(case(), options={KEY:value}, config=RunConfig(hours=2)).diagnostics == e.value.diagnostics


@pytest.mark.parametrize('raw,code,hour,layer', [
    (b'.9\n', 'option.floor_length', None, 'semantics'),
    (b'.9 .8\n', 'profile.dimensions', None, 'profiles'),
    (b'.9 .8\n.9 .8\n', 'profile.dimensions', None, 'profiles'),
    (b'hello\n', 'profile.unreadable', None, 'profiles'),
    (b'.9,.8\n', 'profile.unreadable', None, 'profiles'),
    (b'.9\nnan\n', 'option.floor_range', 1, 'semantics'),
    (b'.9\ninf\n', 'option.floor_range', 1, 'semantics'),
    (b'.9\n1.1\n', 'option.floor_range', 1, 'semantics')])
def test_csv_refusals_agree(tmp_path, raw, code, hour, layer):
    p = tmp_path / 'floor.csv'; p.write_bytes(raw)
    assert_refusal(str(p), code, hour, layer)


def assert_refusal(value, code, hour=None, layer='semantics', c=None, path=None, field=KEY):
    c = case() if c is None else c
    report = api.validate(c, options={KEY:value}, config=RunConfig(hours=2))
    with pytest.raises(InputError) as e:
        solve(c, value)
    assert not report.valid and report.stages['semantics'] == 'failed'
    a, b = report.diagnostics[0], e.value.diagnostics[0]
    assert a == b
    assert (a.code, a.layer, a.field, a.path, a.hour) == (code, layer, field, path, hour)


def test_semantic_refusals(tmp_path):
    assert_refusal([.9], 'option.floor_length')
    assert_refusal('relative.csv', 'profile.unresolvable', layer='profiles')
    assert_refusal(str(tmp_path/'missing.csv'), 'profile.not_found', layer='profiles')
    assert_refusal([0., .9], 'shortfall.exceeds_floor', 1, c=case(low=2.), path='/demand/e/l_ns', field='l_ns')
    assert_refusal(.9, 'shortfall.exceeds_demand', 0, c=case(low=11.), path='/demand/e/l_ns', field='l_ns')


@pytest.mark.parametrize('text,expected', [('0.9', .9), ('0.9,0.8', [.9,.8]), ('a,b.csv','a,b.csv'),
    ('a,b.CSV','a,b.CSV'), ('folder\\floors', 'folder\\floors'), ('./floors','./floors')])
def test_cli_classification(text, expected):
    assert u.parse_options([KEY+'='+text]) == {KEY:expected}


@pytest.mark.parametrize('text,pos', [('0.9,,0.8', 1), ('0.9,no',1), ('',0)])
def test_cli_malformed(text,pos):
    with pytest.raises(InputError) as e:
        u.parse_options([KEY+'='+text])
    d = e.value.diagnostics[0]
    assert d.code == 'value.type' and d.hour is None and d.field == KEY
    assert 'position '+str(pos) in d.message


def test_repeated_and_digest():
    with pytest.raises(InputError) as e:
        u.parse_options([KEY+'=.9', KEY+'=.8'])
    assert e.value.diagnostics[0].code == 'option.repeated'
    assert u.floor_digest([0,1]) == u.floor_digest([-0.,1.])
    with pytest.raises(ValueError):
        api.output_path('case.json', {KEY:[.9,.9]})
    assert api.check_options({KEY:'0.9'})[KEY] == '0.9'


def test_direct_option_schema():
    schema = SolveOptions.model_json_schema(by_alias=True, mode='validation')
    entry = schema['properties'][KEY]
    assert 'default' not in entry and len(entry['anyOf']) == 3
    assert entry['anyOf'][0]['minimum'] == 0 and entry['anyOf'][0]['maximum'] == 1
    assert entry['anyOf'][1]['minLength'] == 1
    assert entry['anyOf'][2]['items']['maximum'] == 1
    for value in (.9, 'floor.csv', [.9,.8]):
        SolveOptions.model_validate({KEY:value})
        jsonschema.validate({KEY:value}, schema)
    for value in (None, True, 1.1):
        with pytest.raises(ValidationError):
            SolveOptions.model_validate({KEY:value})
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({KEY:value}, schema)
    SolveOptions.model_validate({})


@pytest.mark.parametrize('operation', ['solve','validate'])
def test_read_once_paths_and_no_mutation(tmp_path, monkeypatch, operation):
    base = tmp_path/'base'; base.mkdir()
    p = base/'floor.csv'; raw=b'.6\n.8\n'; p.write_bytes(raw)
    c = case(); before = copy.deepcopy(c); opts={KEY:'floor.csv'}; original=copy.deepcopy(opts)
    real_open = builtins.open; reads=[]
    def opened(file, *args, **kw):
        f = real_open(file, *args, **kw)
        if str(file) == str(p):
            reads.append(args[0] if args else kw.get('mode','r'))
            class Reader:
                def __enter__(self): return self
                def __exit__(self,*a): f.close()
                def read(self):
                    data=f.read()
                    # Change the file after reading; checks and provenance must still use original bytes.
                    p.write_bytes(b'.1\n.1\n')
                    return data
            return Reader()
        return f
    monkeypatch.setattr(builtins,'open',opened)
    monkeypatch.chdir(tmp_path)
    result = getattr(api,operation)(c, options=opts, source=base/'case.json', config=RunConfig(hours=2))
    assert reads == ['rb']
    assert c == before and opts == original
    if operation == 'solve':
        assert result.accounting_ok
        pv=result.document['provenance']
        assert pv['options'][KEY] == [.6,.8]
        assert pv['hourly_coverage_floor']['file_sha256'] == hashlib.sha256(raw).hexdigest()
        assert result.document['demand']['e']['output_ns'][1] == pytest.approx(2.)
    else:
        assert result.valid


def test_explicit_base_and_one_hour(tmp_path):
    (tmp_path/'floor.csv').write_text('.9\n')
    c=case(dm=(10.,), profile=(1.,))
    r=api.solve(c, options={KEY:'floor.csv'}, source='/different/case.json',
                config=RunConfig(hours=1, profile_base=tmp_path))
    assert r.optimal and r.accounting_ok
    assert r.document['provenance']['hourly_coverage_floor']['resolved'] == str(tmp_path/'floor.csv')
    assert api.solve(c, options={KEY:[.9]}, config=RunConfig(hours=1)).optimal


@pytest.mark.parametrize('opts', [{'non-served-power-constraint':.1}, {'carbon-constraint':0.}])
def test_coexistence(opts):
    r=api.solve(case(), options={KEY:.6, **opts}, config=RunConfig(hours=2))
    assert r.accounting_ok
    checks=r.document['system']['checks']
    assert checks['coverage_floor']['ok']
    assert checks['reliability_cap' if 'non-served-power-constraint' in opts else 'carbon_cap']['ok']


@pytest.mark.parametrize('high,dm,floor', [(2.,(10.,10.),.6), (4.,(10.,10.),.6),
    (0.,(10.,10.),1.), (1e6,(0.,10.),.6), (4.,(5.,10.),.6)])
def test_marginal_bounds_and_subgradients(high,dm,floor):
    r=solve(case(dm=dm,high=high),floor)
    marginal=r.document['demand']['e']['shadow_prices']['demand_marginal']
    eps=1e-4
    for i,d in enumerate(dm):
        plus=list(dm); plus[i]+=eps
        right=(solve(case(dm=plus,high=high),floor).objective-r.objective)/eps
        if d>0:
            minus=list(dm);minus[i]-=eps
            left=(r.objective-solve(case(dm=minus,high=high),floor).objective)/eps
            assert min(left,right)-1e-6 <= marginal[i] <= max(left,right)+1e-6
        else:
            assert marginal[i] <= right+1e-6


def test_commodity_rule_unchanged(monkeypatch):
    from ies_optimiser import pos_dmd
    original=pos_dmd.demand_marginal; calls=[]
    def traced(rows,ns,dm,bounds,floor=None):
        calls.append(floor)
        return original(rows,ns,dm,bounds,floor)
    monkeypatch.setattr(pos_dmd,'demand_marginal',traced)
    c=case(); c['p2x']=[p2x('ro',elec_use=.1)]
    c['demand']['x']=[demand_x('water',2.,['ro'])]
    r=solve(c)
    assert r.accounting_ok and calls == [[.6,.6],None]


@pytest.mark.parametrize('text,code,hour,path,field', [
    ('0.9,,0.8','value.type',None,None,KEY), ('1.1','option.floor_range',None,None,KEY),
    ('0.9,1.1','option.floor_range',1,None,KEY), ('missing.csv','profile.not_found',None,None,KEY),
    ('short.csv','option.floor_length',None,None,KEY),
    ('0.99','shortfall.exceeds_floor',0,'/demand/e/l_ns','l_ns')])
def test_validate_json_route(tmp_path,text,code,hour,path,field):
    c=system(generators=[generator('gen')], e_total=87600., e_kwargs={'l_ns':[2.,1e6]})
    p=tmp_path/'case.json';p.write_text(json.dumps(c))
    (tmp_path/'short.csv').write_text('.9\n')
    out=subprocess.run([sys.executable,'-m','ies_optimiser','validate',str(p),KEY+'='+text,'--json'],
                       capture_output=True,text=True)
    assert out.returncode == 1
    doc=json.loads(out.stdout)
    d=doc['diagnostics'][0]
    assert (d['code'],d['hour'],d['path'],d['field']) == (code,hour,path,field)
    if code in ('value.type','option.floor_range'):
        assert d['entity']=='command line'


def test_help_discovers_all_options():
    out=subprocess.run([sys.executable,'-m','ies_optimiser','--help'],capture_output=True,text=True)
    assert out.returncode==0
    assert all(name in out.stdout for name in SolveOptions.names())


def test_unreadable_floor_keeps_path_context(tmp_path, monkeypatch):
    p=tmp_path/'floor.csv'; p.write_text('.9\n.9\n')
    real_open=builtins.open
    def denied(file,*a,**kw):
        if str(file)==str(p):
            raise PermissionError('test refusal')
        return real_open(file,*a,**kw)
    monkeypatch.setattr(builtins,'open',denied)
    assert_refusal(str(p),'profile.unreadable',layer='profiles')
    d=api.validate(case(),options={KEY:str(p)},config=RunConfig(hours=2)).diagnostics[0]
    assert str(p) in d.message and 'RunConfig.profile_base' in d.message


def test_zero_total_still_validates_floor():
    c=case();c['demand']['e'].update(total=0.,profile='')
    assert_refusal([.9],'option.floor_length',c=c)


def test_nonoptimal_result_still_has_floor_origin():
    c=case();c['generator'][0]['c_prod']=0
    r=solve(c,1.)
    assert not r.optimal
    assert r.document['provenance']['options'][KEY] == [1.,1.]
    assert r.document['provenance']['hourly_coverage_floor']['values_sha256'] == u.floor_digest([1.,1.])
    Result.model_validate(r.document)
    jsonschema.validate(r.document,schemas.result_schema())


def test_floor_bounds_do_not_reach_commodities(monkeypatch):
    original=u.shortfall_bounds;calls=[]
    def traced(dm,bounds,who,path=None,floor=None):
        calls.append((who,floor))
        return original(dm,bounds,who,path=path,floor=floor)
    monkeypatch.setattr(u,'shortfall_bounds',traced)
    c=case();c['p2x']=[p2x('ro',elec_use=.1)]
    c['demand']['x']=[demand_x('water',2.,['ro'])]
    assert solve(c).accounting_ok
    commodity=[floor for who,floor in calls if who!='demand.e']
    assert commodity and all(f is None for f in commodity)


def test_floor_zero_respects_existing_lower_bound():
    absent=api.solve(case(low=2.),config=RunConfig(hours=2))
    zero=solve(case(low=2.),0.)
    a,z=numeric(absent),numeric(zero)
    z['system']['checks'].pop('coverage_floor')
    assert a==z


def test_tighter_l_ns_and_floor_each_control_an_hour():
    c=case(dm=(5.,10.),high=3.,profile=(1.,1.))
    c['generator'][0].update(c_prod=100.,fix_cost_prod=0.,var_cost_prod=100.)
    r=solve(c,.6)
    assert r.document['demand']['e']['output_ns'] == pytest.approx([2.,3.])
    marginal=r.document['demand']['e']['shadow_prices']['demand_marginal']
    assert marginal == pytest.approx([60.,100.])
    eps=1e-4
    for i in range(2):
        dm=[5.,10.];dm[i]+=eps
        changed=copy.deepcopy(c); changed['demand']['e'].update(total=sum(dm),profile=dm)
        assert (solve(changed,.6).objective-r.objective)/eps == pytest.approx(marginal[i])


def test_all_zero_csv_and_origin_fields(tmp_path):
    p=tmp_path/'zeros.csv';p.write_bytes(b'0\n-0\n')
    r=solve(value=str(p))
    zero=solve(value=0.)
    assert numeric(r)==numeric(zero)
    for value,form,declared in [(0.,'scalar',0.),([0.,0.],'list',None)]:
        origin=solve(value=value).document['provenance']['hourly_coverage_floor']
        assert origin == {'form':form,'declared':declared,'resolved':None,'file_sha256':None,
                          'values_sha256':u.floor_digest([0.,0.])}
    assert r.document['provenance']['hourly_coverage_floor']['values_sha256'] == u.floor_digest([0.,0.])


def test_option_order_is_preserved_in_names():
    supplied={'carbon-constraint':2,KEY:[.6,.8],'non-served-power-constraint':.5}
    normalised=api.check_options(supplied)
    assert list(normalised)==list(supplied)
    assert api.output_path('case.json',normalised,floor_sha256=u.floor_digest([.6,.8])).endswith(
        '.carbon-constraint_2.0.hourly-coverage-floor_'+u.floor_digest([.6,.8])[:12]+'.non-served-power-constraint_0.5.json')
