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
