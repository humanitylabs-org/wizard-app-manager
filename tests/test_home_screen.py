import json
import shutil
from pathlib import Path

import pytest
from wam import registry, wizardmd

TEMPLATE = Path(__file__).resolve().parents[1] / 'templates' / 'hello-wizard'
GOOD = {'offline': True, 'position_keys': ['reader:*:pos:*'], 'route_state': 'reader'}


def test_absent_and_empty_declare_nothing():
    assert wizardmd.home_screen({}) == {}
    assert wizardmd.home_screen({'home_screen': {'offline': False, 'position_keys': []}}) == {}


def test_valid_is_normalized():
    assert wizardmd.home_screen({'home_screen': GOOD}) == GOOD
    assert wizardmd.home_screen({'home_screen': {'offline': True}}) == {'offline': True}


@pytest.mark.parametrize('value', [None, 'offline', [], {'offline': 'true'}, {'offline': 1}, {'x': True}, {'position_keys': 'a'},
                                   {'position_keys': ['*']}, {'position_keys': ['ab*']}, {'position_keys': ['abc'] * 9},
                                   {'position_keys': ['x' * 121]}, {'position_keys': ['abc\n']}, {'route_state': '1a'}, {'route_state': 3}])
def test_invalid(value):
    with pytest.raises(ValueError, match='home_screen'):
        wizardmd.home_screen({'home_screen': value})


def _app(tmp_path, declaration):
    app = tmp_path / 'repos' / 'wizardapps' / 'apps' / 'hello-wizard'
    shutil.copytree(TEMPLATE, app)
    shutil.copy2(app / 'runtipi' / 'config.json', app / 'config.json')
    wm = app / 'wizard.md'
    wm.write_text(wm.read_text().replace('wizard: 0.2', 'home_screen: ' + json.dumps(declaration) + '\nwizard: 0.2'))
    return app


def test_declaration_survives_installed_apps_json(tmp_path):
    app = _app(tmp_path, GOOD)
    assert ('home_screen valid', True, '') in wizardmd.check(app)
    rows = [{'app': {'status': 'running', 'port': 8799}, 'info': {'urn': 'hello-wizard:wizardapps'}}]
    reg = registry.build(rows, 'wizardapps', '127.0.0.1', tmp_path)
    assert json.loads(registry.write(reg, tmp_path / 'owner').read_text())['apps'][0]['home_screen'] == GOOD


def test_bad_declaration_drops_only_the_capability(tmp_path):
    app = _app(tmp_path, {'offline': 'yes'})
    assert 'home_screen valid' in wizardmd.failures(wizardmd.check(app))
    rows = [{'app': {'status': 'running', 'port': 8799}, 'info': {'urn': 'hello-wizard:wizardapps'}}]
    row = json.loads(registry.write(registry.build(rows, 'wizardapps', '127.0.0.1', tmp_path), tmp_path / 'owner2').read_text())['apps'][0]
    assert row['id'] == 'hello-wizard' and 'home_screen' not in row


def _renamed(tmp_path, line):
    app = tmp_path / 'repos' / 'wizardapps' / 'apps' / 'hello-wizard'
    shutil.copytree(TEMPLATE, app)
    shutil.copy2(app / 'runtipi' / 'config.json', app / 'config.json')
    wm = app / 'wizard.md'
    wm.write_text(wm.read_text().replace('wizard: 0.2', line + '\nwizard: 0.2'))
    rows = [{'app': {'status': 'running', 'port': 8799}, 'info': {'urn': 'hello-wizard:wizardapps'}}]
    return json.loads(registry.write(registry.build(rows, 'wizardapps', '127.0.0.1', tmp_path), tmp_path / 'owner').read_text())['apps'][0]


def test_declared_rename_reaches_apps_json(tmp_path):
    # wizard-ux issue 1471: the shell maps a pin made under an old id to the renamed app only if apps.json says so.
    row = _renamed(tmp_path, 'renamed_from: [hello-preview, hello-wizard, "Bad Id", hello-preview]')
    assert row['renamed_from'] == ['hello-preview']


def test_no_rename_declared_leaves_apps_json_unchanged(tmp_path):
    assert 'renamed_from' not in _renamed(tmp_path, '# no rename')


def test_malformed_rename_entries_drop_out_without_breaking_the_registry(tmp_path):
    row = _renamed(tmp_path, 'renamed_from: [{old: id}, [nested], hello-preview]')
    assert row['id'] == 'hello-wizard' and row['renamed_from'] == ['hello-preview']
