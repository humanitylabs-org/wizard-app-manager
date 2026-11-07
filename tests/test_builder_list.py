"""List kind, the stricter static rules and the `wam verify` round trip (no Docker or browser needed here)."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wam import builder, verify


def _port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Server:
    """The app's own server.py as a process: stands in for the container, restart included."""

    def __init__(self, source, data):
        self.source, self.data, self.port, self.proc = source, data, _port(), None

    def start(self):
        env = {**os.environ, 'PORT': str(self.port), 'DATA_DIR': str(self.data), 'BIND': '127.0.0.1'}
        self.proc = subprocess.Popen([sys.executable, str(self.source / 'app/server.py')], env=env)
        verify.Client(self.port).wait(seconds=10)

    def restart(self):
        self.stop()
        self.start()

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)


@pytest.fixture
def list_app(tmp_path):
    return builder.new_app('grocery-list', tmp_path / 'dev', 'a grocery list', kind='list')


def test_list_kind_generates_a_checked_app(list_app):
    meta = json.loads((list_app / '.builder.json').read_text())
    assert meta['kind'] == 'list' and meta['template'] == 'list'
    assert json.loads((list_app / 'web/list.json').read_text())['title'] == 'Grocery List'
    for kit in ('boot.js', 'wizard-ui.css', 'wizard-sheet.js', 'wizard-tokens.css'):
        assert (list_app / 'web' / kit).read_bytes() == (builder.REPO / 'templates/hello-wizard/web' / kit).read_bytes()
    text = '\n'.join(p.read_text() for p in (list_app / 'app/server.py', list_app / 'skill/SKILL.md', list_app / 'web/index.html'))
    assert 'wizard-list' not in text and 'wizard_list' not in text and 'grocery_list_change' in text
    assert 'WizardApp.back.register' in (list_app / 'web/app.js').read_text()
    report = builder.check_app(list_app, proposal=True)
    assert report['ok'], [r for r in report['rules'] if not r['ok']]
    assert not [r for r in report['rules'] if not r['ok']], 'the list template is clean, not just passing'


def test_hello_stays_the_default_kind(tmp_path):
    root = builder.new_app('plain-app', tmp_path, 'hello')
    assert json.loads((root / '.builder.json').read_text())['template'] == 'hello-wizard'
    assert (root / 'verify.json').is_file() and not (root / 'web/list.json').exists()
    with pytest.raises(ValueError):
        builder.new_app('other-app', tmp_path, 'x', kind='kanban')


def test_list_round_trip_survives_restart_and_is_idempotent(list_app, tmp_path):
    server = Server(list_app, tmp_path / 'data')
    server.start()
    try:
        name, steps = verify.round_trip(list_app, {})
        assert name.startswith('list kind')
        done = verify.run_steps(steps, verify.Client(server.port), server.restart)
        assert 'restart' in done and done[-1] == 'read back after restart'
        assert verify.mcp_tools(verify.Client(server.port)) == 3
        client = verify.Client(server.port)
        status, body = client.call('POST', '/api/items/change', {'operation_id': '00000000-0000-4000-8000-0000000000aa',
                                                                  'id': '00000000-0000-4000-8000-0000000000bb', 'create': True,
                                                                  'title': 'Milk', 'fields': {'note': '2 litres'}})
        assert status == 200 and body['item']['fields'] == {'note': '2 litres'}
        status, body = client.call('POST', '/api/items/change', {'operation_id': '00000000-0000-4000-8000-0000000000ac',
                                                                  'id': '00000000-0000-4000-8000-0000000000bb', 'fields': {'colour': 'x'}})
        assert status == 400 and 'note' in body['error']
    finally:
        server.stop()


def test_hello_declared_round_trip(tmp_path):
    root = builder.new_app('plain-app', tmp_path / 'dev', 'hello')
    server = Server(root, tmp_path / 'data')
    server.start()
    try:
        name, steps = verify.round_trip(root, {})
        assert name == 'verify.json'
        assert verify.run_steps(steps, verify.Client(server.port), server.restart)[-1] == 'read back after restart'
    finally:
        server.stop()


def test_round_trip_must_be_declared_and_restart(tmp_path):
    (tmp_path / 'web').mkdir()
    with pytest.raises(verify.Failed, match='No API round trip'):
        verify.round_trip(tmp_path, {})
    with pytest.raises(verify.Failed, match='restart'):
        verify.run_steps([{'do': 'GET /healthz'}], None, None)


def test_fill_and_dig():
    names = {'run': 'r1', 'n': 7}
    assert verify._fill({'a': 'x {{run}}', 'b': '{{n}}', 'c': ['{{uuid:k}}']}, names)['b'] == 7
    assert names['uuid:k'] == verify._fill('{{uuid:k}}', names)
    assert verify.dig({'a': [{'b': 1}, {'b': 2}]}, 'a.-1.b') == 2


# Each new rule: a positive and a negative fixture. Builder-made apps (strict) fail; older apps get a warning.
FIXTURES = [
    ('back: one Back, WizardApp.back', 'web/app.js', "window.addEventListener('popstate', () => close());",
     "WizardApp.back.register({canGoBack: () => open, back: close});"),
    ('back: one Back, WizardApp.back', 'web/app.js', "history.pushState({sheet: 1}, '');",
     "const s = 'history.pushState'; // text, not a call\nhistory.back;"),
    ('design: pop-ups are the shared sheet', 'web/index.html', '<div class="modal"><p>Sure?</p></div>',
     '<dialog class="wz-sheet"><h2>Sure?</h2></dialog>'),
    ('design: pop-ups are the shared sheet', 'web/app.js', "box.innerHTML = '<dialog id=\"d\">Hi</dialog>'; d.showModal();",
     "const d = node('dialog', 'wz-sheet'); WzSheet.open(d);"),
    ('design: pop-ups are the shared sheet', 'web/styles.css', '.overlay { position: fixed; inset: 0; }',
     '.sheet-form { display: grid; } dialog.wz-sheet::backdrop { opacity: .5; }'),
    ('back: one Back, WizardApp.back', 'web/app.js', "window.onpopstate = e => show(e.state);",
     "WizardApp.back.useHistory({isRoot: () => !location.hash}); history.pushState({r: 1}, '', '#/read');"),
    ('design: one pop-up, the sheet', 'web/app.js', "if (confirm('Delete?')) remove();",
     "function confirmDelete() { WzSheet.open(d); }"),
]


@pytest.mark.parametrize('rule,path,bad,good', FIXTURES)
def test_rule_fixtures(list_app, rule, path, bad, good):
    target = list_app / path
    original = target.read_text()

    def rows(snippet):
        target.write_text(original + '\n' + snippet + '\n')
        return [r for r in builder.design_rows(list_app) if r['rule'] == rule]
    hit = rows(bad)
    assert hit and hit[0]['severity'] == 'error', hit
    assert not rows(good)
    for legacy in ('{"template": "legacy"}', '{"template": "hello-wizard"}'):  # older builder apps: rules v1
        (list_app / '.builder.json').write_text(legacy)
        assert rows(bad)[0]['severity'] == 'warning'


def test_strict_rule_fails_check(list_app):
    (list_app / 'web/app.js').write_text((list_app / 'web/app.js').read_text() + "\nwindow.onpopstate = () => {};\n")
    report = builder.check_app(list_app, proposal=True)
    assert not report['ok']


def test_verify_cli_reports_failure_without_docker(tmp_path, capsys):
    assert verify.main([str(tmp_path / 'missing')]) == 1
    assert 'FAIL' in capsys.readouterr().out


def test_skins_and_chrome_lookup(list_app, monkeypatch, tmp_path):
    assert {'grove', 'tide', 'ember', 'signal'} <= set(verify.skins(list_app))
    monkeypatch.setenv('WAM_CHROMIUM', '/x/chrome')
    assert verify.find_chrome() == '/x/chrome'


def test_test_fixtures_are_not_view_code(list_app):
    (list_app / 'tests').mkdir()
    (list_app / 'tests/ui.test.js').write_text("page.on('dialog', d => d.accept()); alert(1); history.pushState({}, '');")
    assert not [r for r in builder.design_rows(list_app) if r['rule'].startswith(('back:', 'design: one pop-up'))]
