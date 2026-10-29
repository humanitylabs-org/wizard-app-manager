"""Owner approval laundering: risk-raising owner actions need the separate approval origin and a
short-lived, single-use, intent-bound confirmation. Synthetic only; no sudo, no Tailscale."""
import io
import json
from types import SimpleNamespace

import pytest

from wam import owner, owner_confirm as oc

LOGIN = 'owner@example.com'
ORIGIN = 'https://box.example.ts.net:8446'
SERVE = {'Web': {'box.example.ts.net:8446': {'Handlers': {'/': {'Proxy': f'http://127.0.0.1:{oc.PORT}'}}},
                 'box.example.ts.net:443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9000'},
                                                         '/wizard-os-home/store-install': {'Proxy': 'http://127.0.0.1:7791'}}}}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(oc, '_serve_status', lambda: SERVE)
    monkeypatch.setattr(oc, 'listener_owned', lambda home: True)  # identity/route tests; ownership has separate kernel-table fixtures
    monkeypatch.setattr(owner, 'execute', lambda c, path, body: calls.append((path, body)) or {'ok': path})
    (tmp_path / oc.CONFIG).write_text(json.dumps({'origin': ORIGIN}))
    return SimpleNamespace(home=tmp_path), calls


def test_origin_must_be_a_whole_dedicated_site(box, monkeypatch):
    c, _ = box
    assert oc.verified_origin(c.home) == ORIGIN
    shared = {'Web': {'box.example.ts.net:8446': {'Handlers': {'/': {'Proxy': f'http://127.0.0.1:{oc.PORT}'},
                                                               '/app/': {'Proxy': 'http://127.0.0.1:9100'}}}}}
    assert oc.verified_origin(c.home, shared) is None
    (c.home / oc.CONFIG).write_text(json.dumps({'origin': 'https://box.example.ts.net:443'}))
    assert oc.verified_origin(c.home) is None  # the shell site is not an approval site


@pytest.mark.parametrize('path,body', [
    ('/api/developer', {'enabled': True, 'repo': 'https://example.com/r.git', 'ref': 'main'}),
    ('/api/developer/server-access', {'action': 'grant', 'seconds': 604800}),
    ('/api/developer/apply', {}),
    ('/api/network', {'action': 'allow', 'app': 'x', 'sha256': 'a' * 64}),
    ('/api/network', {'action': 'off'}),
    ('/api/grants', {'action': 'grant', 'app': 'x', 'to': 'files'}),
    ('/api/backup/import', {'file': '/tmp/b'}),
    ('/api/updates', {'channel': 'alpha'}),
])
def test_shell_origin_never_executes_risk_raising(box, path, body):
    c, calls = box
    with pytest.raises(oc.NeedsConfirmation) as e:
        oc.gate(c, path, body, LOGIN)
    assert calls == []
    assert e.value.payload['confirm_url'].startswith(ORIGIN + '/confirm/')


@pytest.mark.parametrize('path,body', [
    ('/api/developer/server-access', {'action': 'revoke'}),
    ('/api/developer', {'enabled': False}),
    ('/api/network', {'action': 'deny', 'app': 'x'}),
    ('/api/grants', {'action': 'revoke', 'app': 'x', 'to': 'files'}),
    ('/api/updates', {'mode': 'ask'}),
])
def test_backing_out_stays_one_click(box, path, body):
    c, calls = box
    assert oc.gate(c, path, body, LOGIN) == {'ok': path}


def test_fail_closed_without_approval_origin(box):
    c, calls = box
    (c.home / oc.CONFIG).unlink()
    with pytest.raises(oc.NeedsConfirmation) as e:
        oc.gate(c, '/api/developer/server-access', {'action': 'grant', 'seconds': 60}, LOGIN)
    assert e.value.payload == {'error': 'approval_origin_missing', 'setup': oc.SETUP,
                               'message': e.value.payload['message']}
    assert calls == [] and not (c.home / oc.INTENTS).exists()


def test_intent_single_use_bound_and_short_lived(box):
    c, _ = box
    doc = oc.create(c.home, 'owner', '/api/developer/server-access', {'action': 'grant', 'seconds': 60}, LOGIN, now=1000)
    with pytest.raises(ValueError):
        oc.consume(c.home, doc['id'], '0' * 64, LOGIN, now=1001)       # wrong digest
    with pytest.raises(ValueError):
        oc.consume(c.home, doc['id'], doc['sha'], 'other@example.com', now=1001)  # other identity
    with pytest.raises(ValueError):
        oc.consume(c.home, doc['id'], doc['sha'], LOGIN, now=1000 + oc.TTL + 1)  # expired
    assert oc.consume(c.home, doc['id'], doc['sha'], LOGIN, now=1001)['path'] == '/api/developer/server-access'
    with pytest.raises(ValueError):
        oc.consume(c.home, doc['id'], doc['sha'], LOGIN, now=1002)       # replay


class FakeHandler:
    def __init__(self, c, method, path, headers, body=b''):
        self.cfg, self.path, self.headers = c, path, headers
        self.rfile, self.wfile = io.BytesIO(body), io.BytesIO()
        self.status, self.sent = None, {}

    def send_response(self, code): self.status = code
    def send_header(self, k, v): self.sent[k] = v
    def end_headers(self): pass


def _post(c, path, body, **headers):
    raw = json.dumps(body).encode()
    h = {'Origin': ORIGIN, 'Sec-Fetch-Site': 'same-origin', 'X-Wizard-Store': '1',
         'Content-Type': 'application/json', 'Content-Length': str(len(raw)), **headers}
    handler = FakeHandler(c, 'POST', path, h, raw)
    oc.respond(handler, 'POST')
    return handler


def test_approval_listener_exact_origin_and_human_confirm(box, monkeypatch):
    c, calls = box
    monkeypatch.setattr(owner, 'check', lambda handler, post=False: None)
    from wam import store_install
    monkeypatch.setattr(store_install, 'owner_login', lambda: LOGIN)
    doc = oc.create(c.home, 'owner', '/api/developer/server-access', {'action': 'grant', 'seconds': 60}, LOGIN)
    # A shell-origin document (app or agent HTML) cannot spend it, even with the digest.
    for bad in ({'Origin': 'https://box.example.ts.net'}, {'Origin': 'https://box.example.ts.net:443'},
                {'Sec-Fetch-Site': 'same-site'}, {'Sec-Fetch-Site': ''}, {'X-Wizard-Store': ''}):
        assert _post(c, f'/confirm/{doc["id"]}', {'sha': doc['sha']}, **bad).status == 403
    assert calls == []
    page = FakeHandler(c, 'GET', f'/confirm/{doc["id"]}', {})
    oc.respond(page, 'GET')
    assert page.status == 200 and page.sent['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none'" in page.sent['Content-Security-Policy']
    assert page.sent['Cross-Origin-Opener-Policy'] == 'same-origin'
    ok = _post(c, f'/confirm/{doc["id"]}', {'sha': doc['sha']})
    assert ok.status == 200 and calls == [('/api/developer/server-access', {'action': 'grant', 'seconds': 60})]
    assert _post(c, f'/confirm/{doc["id"]}', {'sha': doc['sha']}).status == 409  # single use


def test_listener_source_requires_exact_manager_uid_and_loopback(tmp_path):
    proc = tmp_path / 'tcp'
    uid = tmp_path.stat().st_uid
    def table(addr='0100007F', owner=uid):
        proc.write_text('header\n' + f'0: {addr}:{oc.PORT:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000 {owner} 0 123\n')
    table()
    assert oc.listener_owned(tmp_path, proc)
    table(owner=uid + 1)
    assert not oc.listener_owned(tmp_path, proc)
    table(addr='00000000')
    assert not oc.listener_owned(tmp_path, proc)
    proc.unlink()
    assert not oc.listener_owned(tmp_path, proc)


def test_enable_never_publishes_a_route_to_an_unowned_listener(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(oc, 'listener_owned', lambda home: False)
    with pytest.raises(ValueError, match='not manager-owned'):
        oc.enable(tmp_path, 'box.example.ts.net', run=lambda argv: calls.append(argv))
    assert calls == [] and not (tmp_path / oc.CONFIG).exists()
