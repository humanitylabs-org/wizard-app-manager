"""Final Docker gate token rules (Python writer side) and migration no-sudo regression."""
import json
from pathlib import Path

from wam import final_host_tokens as fht

FIX = json.loads((Path(__file__).parent / 'fixtures' / 'final-host-tokens.json').read_text())


def _tokens(config):
    ctx = FIX['context']
    return fht.tokens(config, app_id=ctx['app_id'], store=ctx['store'], app_data_dir=ctx['app_data_dir'],
                      files_root=ctx['files_root'], source_root=ctx['source_root'], env_dirs=ctx['env_dirs'],
                      app_data_root=ctx['app_data_root'])


def test_shared_vectors():
    for case in FIX['cases']:
        assert _tokens(case['config']) == sorted(case['tokens']), case['name']


def test_grant_injected_bind_needs_app_data_root():
    ctx = FIX['context']
    cfg = {'services': {'web': {'volumes': ['/srv/rt/app-data/wizardapps/files/data/share:/share',
                                            '/etc:/share2']}}}
    got = fht.tokens(cfg, app_id='demo', store='wizardapps', app_data_dir=ctx['app_data_dir'],
                     files_root=ctx['files_root'], source_root=ctx['source_root'],
                     app_data_root=ctx['app_data_root'], injected=frozenset((fht.bind_identity('web', fht.volume_of(cfg['services']['web']['volumes'][0])),
                                                                           fht.bind_identity('web', fht.volume_of(cfg['services']['web']['volumes'][1])))))
    assert got == ['web|bind:/etc:/share2:rw']


def test_record_and_deny_round_trip(tmp_path):
    c = type('C', (), {'runtipi_root': tmp_path})()
    assert fht.record(c, 'demo', 'wizardapps', ['web|privileged'])
    doc = json.loads((tmp_path / 'user-config' / fht.FILE).read_text())
    assert doc == {'schema': 1, 'apps': {'wizardapps/demo': ['web|privileged']}}
    assert fht.record(c, 'demo', 'wizardapps', [])
    assert json.loads((tmp_path / 'user-config' / fht.FILE).read_text())['apps'] == {}


def test_migration_has_no_grant_step():
    from wam import privilege_migration as pm
    src = Path(pm.__file__).read_text()
    assert "'grant'" not in src.split('STEPS', 1)[1].split(')', 1)[0]
    from wam import developer
    assert developer.migration_grant(None, True)['active'] is False


def test_server_access_helper_has_no_migration_grant():
    text = (Path(__file__).resolve().parents[1] / 'scripts/root-helpers/server-access').read_text()
    assert "'migration'}" not in text and 'migration-used' not in text
