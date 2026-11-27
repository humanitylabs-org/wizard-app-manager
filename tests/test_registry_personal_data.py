"""apps.json carries an app's wizard.md `personal_data` label, so the Agent Service's Privacy check and the install
sheet's private AI card find the apps that hold personal data (wizard-os#929)."""
import json
from pathlib import Path

import pytest

from wam import registry


def _build(tmp_path: Path, field: str) -> dict:
    tipi = tmp_path / 'tipi'
    app = tipi / 'apps' / 'wizardapps' / 'mail'
    app.mkdir(parents=True, exist_ok=True)
    (app / 'wizard.md').write_text('---\nid: mail\nname: Mail\nversion: 0.1.0\ntype: view\n'
                                   f'view: {{path: /}}\n{field}---\n')
    row = {'info': {'urn': 'mail:wizardapps'}, 'app': {'status': 'running', 'port': 9300}}
    return registry.build([row], 'wizardapps', '127.0.0.1', tipi)


def test_line_reaches_apps_json(tmp_path):
    reg = _build(tmp_path, 'personal_data: reads your email\n')
    registry.write(reg, tmp_path / 'home')
    entry = json.loads((tmp_path / 'home' / 'apps.json').read_text())['apps'][0]
    assert entry['personal_data'] == 'reads your email'
    assert 'privacy' not in entry   # a label only: the app stays a standard app


def test_true_form_stays_true(tmp_path):
    assert _build(tmp_path, 'personal_data: true\n')['apps'][0]['personal_data'] is True


def test_line_is_one_trimmed_line_of_at_most_80(tmp_path):
    assert _build(tmp_path, 'personal_data: "  reads   your\\n email.  "\n')['apps'][0]['personal_data'] == 'reads your email'
    assert len(_build(tmp_path, f'personal_data: "{"x" * 200}"\n')['apps'][0]['personal_data']) == 80


def test_kept_beside_the_private_tag(tmp_path):
    entry = _build(tmp_path, 'privacy: private\npersonal_data: reads your notes\n')['apps'][0]
    assert entry['privacy'] == 'private' and entry['personal_data'] == 'reads your notes'


@pytest.mark.parametrize('field', ['', 'personal_data: false\n', 'personal_data:\n', 'personal_data: ""\n',
                                   'personal_data: [mail]\n', 'personal_data: {a: 1}\n', 'personal_data: 3\n'])
def test_absent_or_malformed_gives_none_and_keeps_the_app(tmp_path, field):
    apps = _build(tmp_path, field)['apps']
    assert [a['id'] for a in apps] == ['mail']
    assert 'personal_data' not in apps[0]
