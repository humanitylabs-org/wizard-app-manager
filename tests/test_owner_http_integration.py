"""Real loopback HTTP adapter; synthetic HOME/data/auth only, no detached jobs."""
import json
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from wam import app_updates as au, cli, store_install, updates


def test_real_store_http_authority_local_status_manual_and_one_shot(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('WIZARD_OS_HOME', str(home / '.wizard-os'))
    monkeypatch.setenv('HERMES_HOME', str(home / '.hermes'))
    monkeypatch.setenv('RUNTIPI_ROOT', str(tmp_path / 'rt'))
    monkeypatch.delenv('WIZARD_RELEASE_PINS', raising=False)
    cfg = SimpleNamespace(home=home, runtipi_root=tmp_path / 'rt',
                          runtipi=lambda: SimpleNamespace(installed=lambda: []))
    home.mkdir()
    (home / 'links.json').write_text(json.dumps({'host': 'synthetic.invalid'}))
    peer = {'uid': 1000}
    monkeypatch.setattr(store_install, 'peer_uid', lambda *a: peer['uid'])
    monkeypatch.setattr(store_install, 'owner_login', lambda: 'owner@example.invalid')
    monkeypatch.setattr(store_install, 'served_origins', lambda host: {'https://synthetic.invalid'})
    monkeypatch.setattr(au, '_release_managed', lambda: set())
    monkeypatch.setattr(updates, 'status', lambda: {'mode': 'ask', 'update_available': False})
    timer = au.ensure_timer
    monkeypatch.setattr(au, 'ensure_timer', lambda h, *a, **kw: timer(h, tmp_path / 'units', False))
    starts = []
    real_start_detached = au.start_detached
    def record_start(argv, **kwargs):
        starts.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    def start_detached():
        # The HTTP adapter passes no Config. Exercise the real HOME-based command builder,
        # replacing only its injected runner so no systemd job or process can start.
        return real_start_detached(runner=record_start)
    monkeypatch.setattr(au, 'start_detached', start_detached)
    class Handler(cli.StoreHandler):
        pass
    Handler.cfg = cfg
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/_updates'
    def request(body=None, extra=None):
        req = Request(url, data=None if body is None else json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json', 'Tailscale-User-Login': 'owner@example.invalid', 'Origin': 'https://synthetic.invalid', 'X-Wizard-Store': '1', **(extra or {})})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as e:
            return e.code, json.load(e)
    try:
        assert request()[0] == 403
        peer['uid'] = 0
        assert request(extra={'Tailscale-User-Login': 'other@example.invalid'})[0] == 403
        assert request({'app_mode': 'ask'}, {'Origin': 'https://untrusted.invalid'})[0] == 403
        assert request({'app_mode': 'ask'}, {'X-Wizard-Store': '0'})[0] == 403
        code, initial = request()
        assert code == 200 and initial['apps']['app_mode'] == 'auto' and not initial['apps']['chosen']
        assert sorted(p.name for p in home.iterdir()) == ['links.json']
        code, saved = request({'app_mode': 'ask'})
        assert code == 200 and saved['apps']['app_mode'] == 'ask' and saved['apps']['chosen']
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
        assert request()[1]['apps']['app_mode'] == 'ask'
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
        assert before == after
        assert request({'action': 'apply-apps'}) == (200, {'started': True})
        assert len(starts) == 1
        argv, kwargs = starts[0]
        assert argv[:3] == ['systemd-run', '--user', '--collect']
        assert argv[3].startswith(f'--unit={au.UNIT}-now-')
        assert argv[4:] == [str(home / '.local/bin/wam'), 'app-updates', '--apply']
        assert kwargs == {'capture_output': True, 'text': True, 'timeout': 30}
        assert au.mode(home) == 'ask'
        assert not list((tmp_path / 'units').glob('*.timer'))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive(), 'synthetic HTTP server leaked'
