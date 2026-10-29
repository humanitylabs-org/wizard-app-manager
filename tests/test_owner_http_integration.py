"""Real loopback HTTP adapter; synthetic HOME/data/auth only, no detached jobs."""
import json
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from wam import app_updates as au, cli, owner, updates


def test_real_store_http_authority_local_status_manual_and_one_shot(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    cfg = SimpleNamespace(home=home, runtipi_root=tmp_path / 'rt',
                          runtipi=lambda: SimpleNamespace(installed=lambda: []))
    allowed = False
    checks = []
    def authorize(handler, post=False):
        checks.append((handler.path, post))
        return None if allowed else 'synthetic owner denied'
    monkeypatch.setattr(owner, 'check', authorize)
    monkeypatch.setattr(au, '_release_managed', lambda: set())
    monkeypatch.setattr(updates, 'status', lambda: {'mode': 'ask', 'update_available': False})
    timer = au.ensure_timer
    monkeypatch.setattr(au, 'ensure_timer', lambda h, *a, **kw: timer(h, tmp_path / 'units', False))
    starts = []
    monkeypatch.setattr(au, 'start_detached', lambda c: starts.append(c) or {'started': True})
    class Handler(cli.StoreHandler):
        pass
    Handler.cfg = cfg
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/_updates'
    def request(body=None):
        req = Request(url, data=None if body is None else json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as e:
            return e.code, json.load(e)
    try:
        assert request()[0] == 403
        assert not home.exists()
        allowed = True
        code, initial = request()
        assert code == 200 and initial['apps']['app_mode'] == 'auto' and not initial['apps']['chosen']
        assert not home.exists(), 'read-only HTTP status created a home'
        code, saved = request({'app_mode': 'ask'})
        assert code == 200 and saved['apps']['app_mode'] == 'ask' and saved['apps']['chosen']
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
        assert request()[1]['apps']['app_mode'] == 'ask'
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
        assert before == after
        assert request({'action': 'apply-apps'}) == (200, {'started': True})
        assert starts == [cfg]
        assert au.mode(home) == 'ask'
        assert not list((tmp_path / 'units').glob('*.timer'))
        assert checks == [('/_updates', False), ('/_updates', False), ('/_updates', True), ('/_updates', False), ('/_updates', True)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive(), 'synthetic HTTP server leaked'
