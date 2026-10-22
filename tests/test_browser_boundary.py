import http.client
from http.server import ThreadingHTTPServer
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from wam.browser_boundary import BrowserBoundary, from_env
from wam import cli, store_install, recommended

TOKEN = 'a1' * 32


def test_disabled_is_inert():
    assert from_env({'WIZARD_SHELL_TOKEN_FILE': '/missing'}).allows({'Origin': 'null'})


def test_enabled_validates_file_and_constant_time_ascii(tmp_path):
    with pytest.raises(ValueError, match='readable'):
        from_env({'WIZARD_BROWSER_BOUNDARY': '1'})
    for value in ['', 'short', TOKEN.upper(), TOKEN + 'a']:
        with pytest.raises(ValueError, match='32 random bytes'):
            BrowserBoundary(True, value)
    path = tmp_path / 'token'
    path.write_text(TOKEN + '\n')
    policy = from_env({'WIZARD_BROWSER_BOUNDARY': '1', 'WIZARD_SHELL_TOKEN_FILE': str(path)})
    assert policy.allows({'X-Wizard-Shell-Token': TOKEN})
    for headers in [{}, {'Tailscale-User-Login': 'owner@example.test'}, {'X-Wizard-Shell-Token': 'é'},
                    {'X-Wizard-Shell-Token': TOKEN + ', ' + TOKEN},
                    {'X-Wizard-Shell-Token': TOKEN, 'Origin': 'null'},
                    {'X-Wizard-Shell-Token': TOKEN, 'Sec-Fetch-Site': 'cross-site'}]:
        assert not policy.allows(headers)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.StoreHandler, 'cfg', SimpleNamespace(home=tmp_path), raising=False)
    monkeypatch.setattr(store_install, 'peer_uid', lambda _: 0)
    monkeypatch.setattr(store_install, 'owner_login', lambda: 'owner@example.test')
    monkeypatch.setattr(store_install, 'served_origins', lambda _: {'https://box.example.test'})
    (tmp_path / 'links.json').write_text('{"host":"box.example.test"}')
    calls = []
    monkeypatch.setattr(recommended, 'listing', lambda _: {'apps': []})
    monkeypatch.setattr(recommended, 'start', lambda _: calls.append('start') or {'state': 'installing'})
    server = ThreadingHTTPServer(('127.0.0.1', 0), cli.StoreHandler)
    setattr(server, 'browser_boundary', BrowserBoundary(True, TOKEN))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, extra=None):
        h = http.client.HTTPConnection('127.0.0.1', server.server_port)
        headers = {'Tailscale-User-Login': 'owner@example.test', 'Origin': 'https://box.example.test',
                   'X-Wizard-Store': '1', 'Content-Type': 'application/json'}
        headers.update(extra or {})
        h.request(method, path, '{}' if method == 'POST' else None, headers)
        r = h.getresponse()
        code, body = r.status, r.read()
        h.close()
        return code, body
    yield server, request, calls
    server.shutdown()
    server.server_close()
    thread.join()


def test_all_store_owner_routes_are_gated_before_side_effects(store):
    _, request, calls = store
    for method in ('GET', 'POST'):
        for path in ('/_recommended', '/_updates', '/_slots/chats', '/_views/reader', '/wisdom'):
            code, body = request(method, path)
            assert code == 401, (method, path, code, body)
            assert b'shell_token_required' in body
    assert calls == []
    assert request('POST', '/_recommended', {'X-Wizard-Shell-Token': TOKEN})[0] == 202
    assert calls == ['start']
    assert request('GET', '/_recommended', {'X-Wizard-Shell-Token': TOKEN})[0] == 200
    assert request('POST', '/_recommended', {'X-Wizard-Shell-Token': TOKEN, 'Origin': 'null'})[0] == 401
    assert calls == ['start']


def test_token_does_not_replace_owner_or_peer_gate(store, monkeypatch):
    _, request, calls = store
    assert request('POST', '/_recommended', {'X-Wizard-Shell-Token': TOKEN, 'Tailscale-User-Login': 'other@example.test'})[0] == 403
    monkeypatch.setattr(store_install, 'peer_uid', lambda _: 1000)
    assert request('POST', '/_recommended', {'X-Wizard-Shell-Token': TOKEN})[0] == 403
    assert calls == []


def test_disabled_listener_keeps_legacy_install_contract(store):
    server, request, calls = store
    setattr(server, 'browser_boundary', BrowserBoundary())
    assert request('POST', '/_recommended')[0] == 202
    assert calls == ['start']
