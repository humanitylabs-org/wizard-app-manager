"""Identical source-only regressions; sockets restricted to synthetic loopback fixtures."""
import ipaddress
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from wam import registry

GOOD = b'<svg viewBox="0 0 100 100"><path d="M10 10h20v20z"/></svg>'

@pytest.fixture(autouse=True)
def network_fence(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        assert ipaddress.ip_address(address[0]).is_loopback, 'nonfixture network forbidden'
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)

@pytest.mark.parametrize('base', [
    'http://192.0.2.1/x/', 'http://2130706433/x/', 'http://127.1/x/',
    'http://0177.0.0.1/x/', 'http://127.000.0.1/x/', 'http://0.0.0.0/x/',
    'http://localhost.evil/x/', 'http://localhost@192.0.2.1/x/',
    'http://user@127.0.0.1/x/', 'http://127.0.0.1:80@192.0.2.1/x/',
    'http://[::ffff:127.0.0.1]/x/', 'http://127.0.0.1:99999/x/',
])
def test_reject_before_probe(base, monkeypatch):
    calls = []
    monkeypatch.setattr(registry, '_probe_icon', lambda b, t: calls.append(b) or True)
    reg = {'apps': [{'id': 'a', 'view': {'local': base, 'url': '/a/'}}]}
    registry.resolve_icons(reg, {})
    assert calls == []
    assert '_icon' not in reg['apps'][0]

@pytest.mark.parametrize('base,canonical', [
    ('http://localhost:1234/a/', 'http://127.0.0.1:1234/a/'),
    ('http://127.0.0.1:1234/a/', 'http://127.0.0.1:1234/a/'),
    ('http://[::1]:1234/a/', 'http://[::1]:1234/a/'),
])
def test_canonical_probe(base, canonical, monkeypatch):
    calls = []
    monkeypatch.setattr(registry, '_probe_icon', lambda b, t: calls.append(b) or True)
    registry.resolve_icons({'apps': [{'id': 'a', 'view': {'local': base}}]}, {})
    assert calls == [canonical]

@pytest.mark.parametrize('vb', ['bad 0 100 100', '0 bad 100 100', 'nan 0 100 100', '0 inf 100 100',
    '0 0 nan 100', '0 0 inf 100', '0 0 -1 100', '0 0 100 0', '0 0 100', '0 0 1e309 100'])
def test_bad_viewbox_fails_closed(vb):
    assert registry.svg_silhouette(f'<svg viewBox="{vb}"><path d="M1 1h1"/></svg>'.encode()) is False

@pytest.mark.parametrize('shape', [
    '<rect x="bad" width="20" height="20"/>', '<rect x="nan" width="20" height="20"/>',
    '<rect width="inf" height="20"/>', '<rect width="-1" height="20"/>',
    '<circle cx="bad" r="5"/>', '<circle r="nan"/>', '<circle r="-1"/>',
    '<ellipse rx="5" ry="inf"/>', '<rect width="0" height="5"/>',
])
def test_bad_child_numbers_fail_closed(shape):
    assert registry.svg_silhouette(f'<svg viewBox="0 0 100 100">{shape}</svg>'.encode()) is False

def test_deep_child_does_not_abort_registry_probe():
    body = b'<svg viewBox="0 0 100 100">' + b'<g>' * 1200 + b'<path d="M1 1h1"/>' + b'</g>' * 1200 + b'</svg>'
    assert registry.svg_silhouette(body) is False

@pytest.fixture
def local_app():
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == '/app/icon.svg':
                self.send_response(302)
                self.send_header('Location', '/outside/icon.svg')
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'image/svg+xml')
            self.end_headers()
            if self.path != '/app/icon.svg': self.wfile.write(GOOD)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}', hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

def test_redirect_never_follows_outside_app(local_app):
    base, hits = local_app
    assert registry._probe_icon(base + '/app/', .5) is False
    assert hits == ['/app/icon.svg']

def test_proxy_environment_not_used(local_app, monkeypatch):
    base, hits = local_app
    monkeypatch.setenv('http_proxy', 'http://192.0.2.9:3128')
    monkeypatch.setenv('no_proxy', '')
    assert registry._probe_icon(base + '/ok/', .5) is True
    assert hits == ['/ok/icon.svg']

@pytest.mark.parametrize('body', [
    b'<svg viewBox="0 0 1_0 100"><path d="M1 1h1"/></svg>',
    b'<svg viewBox="0 0 100 100"><rect width="1_0" height="5"/></svg>',
    b'<?xml version="1.0" encoding="unknown-codec"?><svg viewBox="0 0 100 100"/>',
])
def test_invalid_numeric_grammar_and_encoding(body):
    assert registry.svg_silhouette(body) is False

@pytest.mark.parametrize('answer,expected', [(None, True), (False, False), (True, True)])
def test_previous_answer_semantics(answer, expected, monkeypatch):
    monkeypatch.setattr(registry, '_probe_icon', lambda *args: answer)
    old = {'apps': [{'id': 'a', 'icon': '/a/icon.svg'}]}
    reg = {'apps': [{'id': 'a', 'view': {'local': 'http://127.0.0.1:1234/a/', 'url': '/a/'}}]}
    registry.resolve_icons(reg, old)
    assert bool(reg['apps'][0].get('_icon')) is expected

@pytest.mark.parametrize('target', ['http://192.0.2.1/a/', 'http://2130706433/a/',
    'http://user@127.0.0.1/a/', 'http://[::ffff:127.0.0.1]/a/'])
def test_direct_probe_cannot_bypass_validation(target, monkeypatch):
    import urllib.request
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid destination reached HTTP client')
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(urllib.request, 'build_opener', forbidden)
    assert registry._probe_icon(target, .1) is False
