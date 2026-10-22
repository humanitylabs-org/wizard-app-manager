"""Own-screen publication and the existing privileged Settings owner gate."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from wam import cli, store_install, view_share, views


def fixture(tmp_path):
    home, shared = tmp_path / 'owner', tmp_path / 'tipi/app-data/wizard-os'
    home.mkdir()
    (home / 'app-manager.json').write_text(json.dumps({'runtipi_root': str(tmp_path / 'tipi')}))
    view = {'app': 'habit-tracker', 'mode': 'own', 'active': True, 'suspended': False,
            'serving': 'a' * 64, 'source_dir': '/private/source', 'build_error': '/private/log'}
    build = views.view_dir('habit-tracker', home) / 'builds' / view['serving']
    build.mkdir(parents=True)
    (build / 'index.html').write_text('<h1>Your version</h1>')
    (build / '.ok').write_text('{"private": "log path"}')
    views._save('habit-tracker', home, view)
    return home, shared


def test_only_active_build_and_small_metadata_are_shared(tmp_path):
    home, shared = fixture(tmp_path)
    dest = shared / 'views/habit-tracker'
    assert json.loads((dest / 'view.json').read_text()) == {
        'mode': 'own', 'active': True, 'suspended': False, 'serving': 'a' * 64, 'api_path': '/api'}
    assert (dest / ('builds/' + 'a' * 64 + '/index.html')).read_text() == '<h1>Your version</h1>'
    assert (dest / ('builds/' + 'a' * 64 + '/.ok')).read_text() == '{}'
    assert not (dest / 'src').exists()
    views.revert('habit-tracker', home)
    assert json.loads((dest / 'view.json').read_text())['active'] is False
    assert (home / 'views/habit-tracker/builds').exists()


def test_no_manager_config_means_no_implicit_other_install(tmp_path):
    assert view_share.shared_home(tmp_path) is None


def test_publication_rejects_symlinks(tmp_path):
    home, shared = fixture(tmp_path)
    v = views.load('habit-tracker', home)
    v['serving'] = 'b' * 64
    build = home / 'views/habit-tracker/builds' / v['serving']
    build.mkdir()
    (build / '.ok').write_text('{}')
    (build / 'leak.js').symlink_to(home / 'app-manager.json')
    with pytest.raises(views.ViewError, match='symlinks'):
        views._save('habit-tracker', home, v)
    assert json.loads((shared / 'views/habit-tracker/view.json').read_text())['serving'] == 'a' * 64


def test_revert_through_settings_owner_listener_and_readback(tmp_path, monkeypatch):
    home, shared = fixture(tmp_path)
    monkeypatch.setattr(cli.StoreHandler, 'cfg', SimpleNamespace(home=home), raising=False)
    (home / 'links.json').write_text('{"host":"box.test"}')
    monkeypatch.setattr(store_install, 'peer_uid', lambda p: 0)
    monkeypatch.setattr(store_install, 'owner_login', lambda: 'owner@example.test')
    monkeypatch.setattr(store_install, 'served_origins', lambda h: {'https://box.test'})
    srv = ThreadingHTTPServer(('127.0.0.1', 0), cli.StoreHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
    def request(method, data=None, **extra):
        c = http.client.HTTPConnection('127.0.0.1', srv.server_port)
        headers = {'Tailscale-User-Login': 'owner@example.test', 'Origin': 'https://box.test',
                   'X-Wizard-Store': '1', 'Content-Type': 'application/json', **extra}
        c.request(method, '/_views/habit-tracker', json.dumps(data) if data is not None else None, headers)
        r = c.getresponse(); out = r.status, json.loads(r.read()); c.close(); return out
    try:
        assert request('POST', {'action': 'revert'}, Origin='https://foreign.test')[0] == 403
        assert request('POST', {'action': 'revert'}, **{'Tailscale-User-Login': 'guest@example.test'})[0] == 403
        assert request('POST', {'action': 'delete'})[0] == 400
        assert views.load('habit-tracker', home)['active'] is True
        assert request('POST', {'action': 'revert'}) == (200, {'app': 'habit-tracker', 'active': False})
        assert request('GET') == (200, {'app': 'habit-tracker', 'active': False})
        assert json.loads((shared / 'views/habit-tracker/view.json').read_text())['active'] is False
    finally:
        srv.shutdown(); srv.server_close(); thread.join()
