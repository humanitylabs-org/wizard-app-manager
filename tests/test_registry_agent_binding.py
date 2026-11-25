"""apps.json carries an app's wizard.md `agent_binding`, so the Agent Service's binding and delivery routes accept it (#1054)."""
import json
from pathlib import Path

import pytest

from wam import registry


def _build(tmp_path: Path, field: str) -> dict:
    tipi = tmp_path / 'tipi'
    app = tipi / 'apps' / 'wizardapps' / 'goals'
    app.mkdir(parents=True, exist_ok=True)
    (app / 'wizard.md').write_text('---\nid: goals\nname: Goals\nversion: 0.1.0\ntype: view\n'
                                   f'view: {{path: /}}\n{field}---\n')
    row = {'info': {'urn': 'goals:wizardapps'}, 'app': {'status': 'running', 'port': 9300}}
    return registry.build([row], 'wizardapps', '127.0.0.1', tipi)


def test_map_form_reaches_apps_json(tmp_path):
    reg = _build(tmp_path, 'agent_binding: {required: false, purpose: "Where your weekly goal check-ins go"}\n')
    registry.write(reg, tmp_path / 'home')
    entry = json.loads((tmp_path / 'home' / 'apps.json').read_text())['apps'][0]
    assert entry['agent_binding'] == {'required': False, 'purpose': 'Where your weekly goal check-ins go'}


def test_true_form_stays_true(tmp_path):
    assert _build(tmp_path, 'agent_binding: true\n')['apps'][0]['agent_binding'] is True


def test_required_and_purpose_are_normalised(tmp_path):
    entry = _build(tmp_path, 'agent_binding: {required: true, purpose: "' + 'x' * 200 + '", extra: 1}\n')['apps'][0]
    assert entry['agent_binding'] == {'required': True, 'purpose': 'x' * 120}


@pytest.mark.parametrize('field', ['', 'agent_binding: false\n', 'agent_binding:\n', 'agent_binding: [a]\n',
                                   'agent_binding: yes please\n'])
def test_absent_or_malformed_gives_none_and_keeps_the_app(tmp_path, field):
    apps = _build(tmp_path, field)['apps']
    assert [a['id'] for a in apps] == ['goals']
    assert 'agent_binding' not in apps[0]


def test_removing_the_declaration_removes_it_on_the_next_write(tmp_path):
    home = tmp_path / 'home'
    registry.write(_build(tmp_path, 'agent_binding: true\n'), home)
    registry.write(_build(tmp_path, ''), home)
    assert 'agent_binding' not in json.loads((home / 'apps.json').read_text())['apps'][0]
