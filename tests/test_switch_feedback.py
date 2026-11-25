"""Try new features (wizard-app-manager#701): the templates keep `tried`/`vote`, the hello-wizard view mounts the shared
section last in its Settings, the App Manager catalog passes the two fields through, and the box switch
`wizard/try-feature-counts` sends only anonymous counts to the Feedback relay."""
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from wam import app_switches, builder, switch_feedback as sf

REPO = Path(builder.REPO)
OWNER = {'Host': 'box.tail0.ts.net', 'X-Wizard-Switch': '1', 'Tailscale-User-Login': 'owner@example.com',
         'Origin': 'https://box.tail0.ts.net', 'Content-Type': 'application/json'}


def _serve(template, tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WIZARD_SWITCHES_DIR', str(tmp_path))
    spec = importlib.util.spec_from_file_location(f'tf_{template.replace("-", "_")}', REPO / 'templates' / template / 'app/server.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SWITCHES.append({'id': 'a', 'name': 'A', 'description': 'one line', 'default': False,
                         'issues': ['humanitylabs-org/demo#7']})
    srv = ThreadingHTTPServer(('127.0.0.1', 0), mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return mod, srv, srv.server_address[1]


def _call(port, method='GET', body=None, headers=OWNER):
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/switches', method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


def test_template_switches_keep_tried_and_vote(tmp_path, monkeypatch):
    for template in ('hello-wizard', 'list'):
        mod, srv, port = _serve(template, tmp_path / template, monkeypatch)
        try:
            code, doc = _call(port)
            assert code == 200 and doc['switches'][0] | {} == {**doc['switches'][0], 'on': False, 'tried': False, 'vote': None}
            assert _call(port, 'POST', {'id': 'a', 'vote': 'up'})[1]['switches'][0]['vote'] == 'up'
            row = _call(port, 'POST', {'id': 'a', 'on': True})[1]['switches'][0]
            assert row['on'] is True and row['tried'] is True and row['vote'] == 'up'
            row = _call(port, 'POST', {'id': 'a', 'on': False})[1]['switches'][0]
            assert row['on'] is False and row['tried'] is True, 'tried is never reset by turning it off'
            assert _call(port, 'POST', {'id': 'a', 'vote': 'down'})[1]['switches'][0]['vote'] == 'down'
            assert _call(port, 'POST', {'id': 'a', 'vote': None})[1]['switches'][0]['vote'] is None
            # any other vote value, or a flip and a vote in one body, is 400; votes are owner only like flips
            for bad in ({'id': 'a', 'vote': 'meh'}, {'id': 'a', 'vote': 1}, {'id': 'a', 'on': True, 'vote': 'up'},
                        {'id': 'a'}, {'id': 'a', 'on': 'yes'}):
                assert _call(port, 'POST', bad)[0] == 400, bad
            assert _call(port, 'POST', {'id': 'b', 'vote': 'up'})[0] == 404
            assert _call(port, 'POST', {'id': 'a', 'vote': 'up'}, {**OWNER, 'Origin': 'https://evil.example'})[0] == 403
            assert _call(port, 'POST', {'id': 'a', 'vote': 'up'}, {k: v for k, v in OWNER.items()
                                                                  if k != 'Tailscale-User-Login'})[0] == 403
            # state persists on the box (DATA/switches.json), across a restart of the app
            saved = json.loads((tmp_path / template / 'switches.json').read_text())
            assert saved == {'a': False, '_tried': {'a': True}}
            assert mod.switches_doc()['switches'][0]['tried'] is True and mod.switch_on('a') is False
        finally:
            srv.shutdown()


def test_the_apps_own_page_reads_without_the_switch_header(tmp_path, monkeypatch):
    # The shared section (wizard-try-features.js) GETs the listing without X-Wizard-Switch; its flips and votes send it.
    page = {k: v for k, v in OWNER.items() if k not in ('X-Wizard-Switch', 'Origin')}
    for template in ('hello-wizard', 'list'):
        mod, srv, port = _serve(template, tmp_path / template, monkeypatch)
        try:
            assert _call(port, headers={**page, 'Sec-Fetch-Site': 'same-origin'})[0] == 200
            assert _call(port, headers=page)[0] == 403
            assert _call(port, headers={**page, 'Sec-Fetch-Site': 'cross-site'})[0] == 403
            assert _call(port, 'POST', {'id': 'a', 'vote': 'up'}, {**page, 'Sec-Fetch-Site': 'same-origin',
                                                                  'Origin': OWNER['Origin']})[0] == 403
        finally:
            srv.shutdown()


def test_switch_state_from_before_votes_still_reads(tmp_path, monkeypatch):
    (tmp_path / 'switches.json').write_text('{"a": true}')
    mod, srv, port = _serve('hello-wizard', tmp_path, monkeypatch)
    try:
        row = _call(port)[1]['switches'][0]
        assert row['on'] is True and row['tried'] is True and row['vote'] is None
    finally:
        srv.shutdown()


def test_hello_wizard_settings_end_with_try_new_features():
    web = REPO / 'templates/hello-wizard/web'
    html = (web / 'index.html').read_text()
    assert '<script defer src="wizard-try-features.js"></script>' in html
    settings = html[html.index('<dialog class="wz-sheet" id="settings"'):html.index('</dialog>')]
    assert settings.rstrip().endswith('<section class="wz-sec" id="try-features"></section>'), 'last in Settings'
    assert "WzTryFeatures.mount(el('try-features'), { path: 'api/switches' })" in (web / 'app.js').read_text()
    lock = json.loads((REPO / 'templates/hello-wizard/appearance-provenance.json').read_text())
    assert lock['files']['style/try-features.js']['destination'] == 'web/wizard-try-features.js'
    assert 'wizard-try-features.js' in json.loads((REPO / 'wam/style-policy.json').read_text())['assets']


def test_catalog_passes_tried_and_vote_only_from_apps_that_keep_them():
    app = {'id': 'demo', 'name': 'Demo'}
    rows = app_switches._rows(app, {'switches': [
        {'id': 'a', 'on': False, 'tried': True, 'vote': 'up', 'issues': ['o/r#1']},
        {'id': 'b', 'on': True, 'vote': 'sideways'},
        {'id': 'c', 'on': True}]})
    by = {r['switch']: r for r in rows}
    assert by['a']['tried'] is True and by['a']['vote'] == 'up'
    assert by['b']['vote'] is None and 'tried' not in by['b']
    assert 'tried' not in by['c'] and 'vote' not in by['c']


ROWS = [
    {'id': 'demo:a', 'kind': 'app', 'name': 'Secret name', 'description': 'd', 'app_name': 'Demo', 'issues': ['o/r#1'],
     'on': False, 'tried': True, 'vote': 'up'},
    {'id': 'demo:b', 'kind': 'app', 'issues': [], 'on': True, 'tried': False, 'vote': None},
    {'id': 'demo:c', 'kind': 'app', 'issues': ['o/r#3'], 'on': False},
    {'id': 'wizard/x', 'kind': 'tweak', 'issues': ['o/r#4'], 'on': True},
]


class _Relay(BaseHTTPRequestHandler):
    got: list = []
    code = 200

    def do_POST(self):
        _Relay.got.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
        self.send_response(_Relay.code)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, *a):
        pass


def test_off_reads_and_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, 'switch_on', lambda home=None: False)
    monkeypatch.setattr(app_switches, 'listing', lambda *a, **k: (_ for _ in ()).throw(AssertionError('read apps')))
    sent = []
    assert sf.tick(tmp_path, post=lambda u, d: sent.append(d)) == {'switch': sf.SWITCH, 'on': False, 'sent': False}
    assert sent == [] and not (tmp_path / sf.STATE).exists()


def test_on_sends_only_anonymous_counts_once_per_change(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _Relay)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv('FEEDBACK_RELAY_URL', f'http://127.0.0.1:{srv.server_address[1]}')
    _Relay.got, _Relay.code = [], 200
    rows = [dict(r) for r in ROWS]
    try:
        dry = sf.tick(tmp_path, on=True, dry_run=True, app_rows=rows)
        assert dry['would_send']['switches'] == [{'id': 'demo:a', 'issues': ['o/r#1'], 'tried': True, 'vote': 'up'}]
        assert _Relay.got == []
        assert sf.tick(tmp_path, on=True, app_rows=rows, now=1000)['sent'] is True
        path, doc = _Relay.got[0]
        assert path == '/v1/switch-feedback'
        assert set(doc) == {'contract', 'box', 'switches'} and sf.BOX.fullmatch(doc['box'])
        assert doc['switches'] == dry['would_send']['switches'] and 'Secret name' not in json.dumps(doc)
        # unchanged: nothing sent until a day has passed
        assert sf.tick(tmp_path, on=True, app_rows=rows, now=2000)['unchanged'] is True and len(_Relay.got) == 1
        assert sf.tick(tmp_path, on=True, app_rows=rows, now=1000 + sf.RESEND_S + 1)['sent'] is True
        # a new thumbs down is sent at the next tick, with the same box id
        rows[1]['vote'] = 'down'
        assert sf.tick(tmp_path, on=True, app_rows=rows, now=1000 + sf.RESEND_S + 2)['sent'] is True
        assert _Relay.got[-1][1]['box'] == doc['box'] and len(_Relay.got[-1][1]['switches']) == 2
        # a refused send is kept as an error and retried next time
        _Relay.code = 404
        rows[1]['vote'] = None
        out = sf.tick(tmp_path, on=True, app_rows=rows, now=1000 + sf.RESEND_S + 3)
        assert out['sent'] is False and 'HTTP 404' in out['error']
        _Relay.code = 200
        assert sf.tick(tmp_path, on=True, app_rows=rows, now=1000 + sf.RESEND_S + 4)['sent'] is True
        assert oct((tmp_path / sf.STATE).stat().st_mode & 0o777) == '0o600'
    finally:
        srv.shutdown()


def test_relay_must_be_https_or_local(monkeypatch):
    monkeypatch.setenv('FEEDBACK_RELAY_URL', 'http://relay.example.com')
    out = sf.tick(None, on=True, app_rows=[])
    assert out['sent'] is False and 'https' in out['error']
    monkeypatch.delenv('FEEDBACK_RELAY_URL')
    assert sf.relay_url() == sf.DEFAULT_RELAY


def test_hourly_tick_calls_the_sender(monkeypatch):
    from wam import cli, app_updates
    called = []
    monkeypatch.setattr(sf, 'tick', lambda *a, **k: called.append(1) or {})
    monkeypatch.setattr(app_updates, 'apply_if_auto', lambda *a, **k: {'started': False})
    assert cli.main(['app-updates', '--apply-if-auto']) == 0
    assert called == [1]
