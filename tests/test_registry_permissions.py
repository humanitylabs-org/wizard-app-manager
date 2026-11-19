"""apps.json carries an app's declared wizard.md `permissions`, today only geolocation (WAM#810, wizard-ux#1629)."""
import json
from pathlib import Path

import pytest

from wam import registry


def _build(tmp_path: Path, app_id: str, field: str) -> dict:
    tipi = tmp_path / 'tipi'
    app = tipi / 'apps' / 'wizardapps' / app_id
    app.mkdir(parents=True, exist_ok=True)
    (app / 'wizard.md').write_text(f'---\nid: {app_id}\nname: {app_id}\nversion: 0.1.0\ntype: view\n'
                                   f'view: {{path: /}}\n{field}---\n')
    row = {'info': {'urn': f'{app_id}:wizardapps'}, 'app': {'status': 'running', 'port': 9300}}
    return registry.build([row], 'wizardapps', '127.0.0.1', tipi)


def test_maps_with_geolocation_gets_it_in_apps_json(tmp_path):
    reg = _build(tmp_path, 'wizard-maps', 'permissions: [geolocation]\n')
    registry.write(reg, tmp_path / 'home')
    entry = json.loads((tmp_path / 'home' / 'apps.json').read_text())['apps'][0]
    assert entry['permissions'] == ['geolocation']
    assert registry.public_projection(reg)['apps'][0]['permissions'] == ['geolocation']


@pytest.mark.parametrize('field', ['', 'permissions: []\n', 'permissions: [camera]\n', 'permissions: geolocation\n',
                                   'permissions: [Geolocation]\n', 'permissions:\n', 'permissions: {geolocation: true}\n',
                                   'permissions: [1]\n'])
def test_other_apps_get_none(tmp_path, field):
    entry = _build(tmp_path, 'wizard-notes', field)['apps'][0]
    assert 'permissions' not in entry


def test_unknown_values_are_dropped_and_duplicates_collapse(tmp_path):
    entry = _build(tmp_path, 'wizard-maps', 'permissions: [microphone, geolocation, geolocation, camera]\n')['apps'][0]
    assert entry['permissions'] == ['geolocation']


def test_removing_the_declaration_removes_it_on_the_next_write(tmp_path):
    home = tmp_path / 'home'
    registry.write(_build(tmp_path, 'wizard-maps', 'permissions: [geolocation]\n'), home)
    registry.write(_build(tmp_path, 'wizard-maps', ''), home)
    assert 'permissions' not in json.loads((home / 'apps.json').read_text())['apps'][0]
