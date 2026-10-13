"""Custom app views (wizard-os #30): customize a real app's screen, use its API through the door,
survive app updates (merge, conflict, failed build), revert, and keep routes across a door restart."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from wam import builder, views


def _port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Box:
    """A test Wizard home with one staged Hello Wizard app running from its staged source."""

    def __init__(self, tmp):
        self.home = tmp / 'home'
        self.dev = builder.new_app('habit-tracker', tmp / 'dev', 'track habits')
        self.app_port = _port()
        builder.install_local(self.dev, self.home, stage_only=True, port=self.app_port)
        self.data = tmp / 'data'
        self.app = None
        self.door = None
        self.start_app()
        self.start_door()

    def start_app(self):
        if self.app:
            self.app.terminate(); self.app.wait(5)
        src = self.home / 'staged-apps/habit-tracker/source'
        env = dict(os.environ, PORT=str(self.app_port), DATA_DIR=str(self.data), BIND='127.0.0.1')
        self.app = subprocess.Popen([sys.executable, str(src / 'app/server.py')], env=env)
        self._wait(f'http://127.0.0.1:{self.app_port}/healthz')

    def start_door(self):
        if self.door:
            self.door.shutdown(); self.door.server_close()
        self.door_port = getattr(self, 'door_port', None) or _port()
        self.door = views.serve(self.home, self.door_port)
        threading.Thread(target=self.door.serve_forever, daemon=True).start()
        self._wait(f'http://127.0.0.1:{self.door_port}/views/_index.json')

    @staticmethod
    def _wait(url):
        for _ in range(100):
            try:
                urllib.request.urlopen(url, timeout=1); return
            except Exception:
                time.sleep(.05)
        raise AssertionError('not up: ' + url)

    def get(self, path, **headers):
        req = urllib.request.Request(f'http://127.0.0.1:{self.door_port}{path}', headers=headers)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            r = opener.open(req, timeout=5)
            return r.status, dict(r.headers), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def post(self, path, body, **headers):
        req = urllib.request.Request(f'http://127.0.0.1:{self.door_port}{path}', data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', **headers}, method='POST')
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def upstream_update(self, edit):
        """The app's author ships a new version: edit the dev source, re-stage it, restart the app."""
        edit(self.dev / 'web')
        out = builder.update_local(self.dev, self.home)
        assert out['update'] == 'updated'
        self.start_app()
        return out

    def close(self):
        self.door.shutdown(); self.door.server_close()
        self.app.terminate(); self.app.wait(5)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture
def box(tmp_path, monkeypatch):
    # Unit tests run the screen build on the host; the Thales e2e runs it in the app's container.
    monkeypatch.setenv('WIZARD_VIEWS_BUILD', 'host')
    b = Box(tmp_path)
    yield b
    b.close()


def _edit(path, old, new):
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new, 1))


def test_no_views_folder_means_the_apps_own_screen(box):
    assert views.open_url('habit-tracker', box.home) == '/apps/habit-tracker/'
    assert not (box.home / 'views').exists()
    code, _, html = box.get('/apps/habit-tracker/')
    assert code == 200 and 'data-wz-overlay' not in html and '<h1>Habits</h1>' in html
    code, headers, _ = box.get('/views/habit-tracker/')
    assert code == 307 and headers['Location'] == '/apps/habit-tracker/'
    assert box.get('/apps/habit-tracker/mcp')[0] == 404  # MCP stays behind the agent's door
    assert box.post('/apps/habit-tracker/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})[0] == 404
    assert box.post('/apps/habit-tracker/api/habits', {'name': 'Via app route', 'day': time.strftime('%Y-%m-%d')})[0] in (200, 201)


def test_own_version_uses_the_api_survives_updates_reverts_and_restarts(box):
    d = views.customize('habit-tracker', 'own', box.home)
    web = Path(d['edit'])
    assert views.open_url('habit-tracker', box.home) == '/apps/habit-tracker/'  # nothing switches before a build passes
    _edit(web / 'index.html', '<h1>Habits</h1>', '<h1>My rituals</h1>')
    _edit(web / 'styles.css', '.habit-progress', '.owner-card { border-radius: 18px; }\n.habit-progress')
    v = views.apply('habit-tracker', box.home)
    assert v['active'] and v['serving'] and views.open_url('habit-tracker', box.home) == '/views/habit-tracker/'
    code, headers, html = box.get('/views/habit-tracker/')
    assert code == 200 and '<h1>My rituals</h1>' in html and "default-src 'self'" in headers['Content-Security-Policy']
    assert 'owner-card' in box.get('/views/habit-tracker/styles.css')[2]

    # The custom screen talks to the real app through its own door, and only to the app's API.
    day = time.strftime('%Y-%m-%d')
    code, body = box.post('/views/habit-tracker/api/habits', {'name': 'Stretch', 'day': day})
    assert code in (200, 201), body
    code, _, listing = box.get(f'/views/habit-tracker/api/habits?day={day}')
    assert code == 200 and 'Stretch' in listing
    assert box.get('/views/habit-tracker/api/../mcp')[0] == 404
    assert box.post('/views/habit-tracker/api/habits', {'name': 'x', 'day': day}, Origin='https://evil.example')[0] == 403
    assert box.get('/views/habit-tracker/', Host='evil.example')[0] == 403
    assert box.get('/views/habit-tracker/.ok')[0] == 404

    # Upstream update that touches other lines: merged in, rebuilt, owner change kept.
    first = v['serving']
    box.upstream_update(lambda w: _edit(w / 'index.html', 'Add one below, then tap it each day you do it.',
                                         'Add one below. Tap it each day you do it.'))
    v = views.update('habit-tracker', box.home)
    assert v['state'] == 'active' and v['serving'] != first and v['note'] is None
    html = box.get('/views/habit-tracker/')[2]
    assert '<h1>My rituals</h1>' in html and 'Add one below. Tap it each day you do it.' in html

    # Upstream update on the same line as the owner's change: owner's version stays on screen, a decision is recorded.
    kept = v['serving']
    box.upstream_update(lambda w: _edit(w / 'index.html', '<h1>Habits</h1>', '<h1>Daily habits</h1>'))
    v = views.update('habit-tracker', box.home)
    assert v['state'] == 'update_conflict' and v['conflicts'] == ['web/index.html'] and v['serving'] == kept
    assert 'Your version stays on screen' in v['note']
    assert '<h1>My rituals</h1>' in box.get('/views/habit-tracker/')[2]
    with pytest.raises(views.ViewError, match='resolve'):
        views.apply('habit-tracker', box.home)
    v = views.resolve('habit-tracker', 'mine', box.home)
    assert v['state'] == 'active' and v['pending'] is None
    assert '<h1>My rituals</h1>' in box.get('/views/habit-tracker/')[2]

    # An update that breaks the owner's build: last working version stays, note explains.
    kept = v['serving']
    box.upstream_update(lambda w: (w / 'boot.js').unlink())
    v = views.update('habit-tracker', box.home)
    assert v['state'] == 'update_build_failed' and v['serving'] == kept and 'no longer builds' in v['note']
    assert box.get('/views/habit-tracker/')[0] == 200
    # The owner (agent) fixes the build; the update completes.
    (box.home / 'views/habit-tracker/src/app/build_view.py').write_text(
        (box.home / 'views/habit-tracker/src/app/build_view.py').read_text().replace("'boot.js', ", ''))
    with pytest.raises(views.ViewError, match='Only the screen source'):
        views.apply('habit-tracker', box.home)  # build script lives outside the screen folder: refused
    subprocess.run(['git', '-C', str(box.home / 'views/habit-tracker/src'), 'checkout', '--', 'app'], check=True)
    (box.home / 'views/habit-tracker/src/web/boot.js').write_text('/* owner restored */\n')
    fixed = views.apply('habit-tracker', box.home)
    assert fixed['state'] == 'active' and fixed['pending'] is None and fixed['base'] == views.app_record('habit-tracker', box.home)['version']

    # Door restart: routes come back from disk.
    box.start_door()
    idx = json.loads(box.get('/views/_index.json')[2])['apps']['habit-tracker']
    assert idx['open'] == '/views/habit-tracker/' and idx['state'] == 'active' and idx['note'] is None
    assert '<h1>My rituals</h1>' in box.get('/views/habit-tracker/')[2]

    # Put it back to normal; history stays and it can come back.
    v = views.revert('habit-tracker', box.home)
    assert not v['active'] and views.open_url('habit-tracker', box.home) == '/apps/habit-tracker/'
    assert box.get('/views/habit-tracker/')[0] == 307
    assert '<h1>Daily habits</h1>' in box.get('/apps/habit-tracker/')[2]
    log = subprocess.run(['git', '-C', str(box.home / 'views/habit-tracker/src'), 'log', '--oneline', 'main'],
                         capture_output=True, text=True).stdout
    assert 'Owner changes' in log
    views.reactivate('habit-tracker', box.home)
    assert '<h1>My rituals</h1>' in box.get('/views/habit-tracker/')[2]


def test_resolve_upstream_takes_the_apps_line_and_keeps_other_owner_changes(box):
    web = Path(views.customize('habit-tracker', 'own', box.home)['edit'])
    _edit(web / 'index.html', '<h1>Habits</h1>', '<h1>My rituals</h1>')
    _edit(web / 'index.html', 'placeholder="New habit, e.g. Drink water"', 'placeholder="What will you do daily?"')
    views.apply('habit-tracker', box.home)
    box.upstream_update(lambda w: _edit(w / 'index.html', '<h1>Habits</h1>', '<h1>Daily habits</h1>'))
    assert views.update('habit-tracker', box.home)['state'] == 'update_conflict'
    assert views.resolve('habit-tracker', 'upstream', box.home)['state'] == 'active'
    html = box.get('/views/habit-tracker/')[2]
    assert '<h1>Daily habits</h1>' in html and 'What will you do daily?' in html


def test_own_version_only_changes_the_screen_and_a_failed_first_build_switches_nothing(box):
    d = views.customize('habit-tracker', 'own', box.home)
    src = box.home / 'views/habit-tracker/src'
    (src / 'app/server.py').write_text('# planted\n')
    with pytest.raises(views.ViewError, match='Only the screen source'):
        views.apply('habit-tracker', box.home)
    subprocess.run(['git', '-C', str(src), 'checkout', '--', 'app/server.py'], check=True)
    (Path(d['edit']) / 'app.js').unlink()
    with pytest.raises(views.ViewError, match='Build failed'):
        views.apply('habit-tracker', box.home)
    assert views.open_url('habit-tracker', box.home) == '/apps/habit-tracker/'
    with pytest.raises(views.ViewError, match='already customized'):
        views.customize('habit-tracker', 'own', box.home)


def test_overlay_applies_and_switches_itself_off_when_an_update_removes_its_target(box):
    d = Path(views.customize('habit-tracker', 'overlay', box.home)['edit'])
    (d / 'overlay.css').write_text('#add-form { order: -1; }\n')
    (d / 'overlay.json').write_text(json.dumps({'requires': ['#add-form', '.wz-list']}))
    views.apply('habit-tracker', box.home)
    html = box.get('/apps/habit-tracker/')[2]
    assert html.count('data-wz-overlay') == 2 and html.index('data-wz-overlay') < html.index('</head>')
    assert '#add-form' in box.get('/views/habit-tracker/_overlay.css')[2]
    assert '"#add-form"' in box.get('/views/habit-tracker/_overlay.js')[2]
    assert box.get('/views/habit-tracker/')[0] == 307  # overlays open the app itself

    box.upstream_update(lambda w: _edit(w / 'index.html', 'id="add-form"', 'id="new-habit-form"'))
    v = views.update('habit-tracker', box.home)
    assert v['suspended'] and '#add-form' in v['note']
    assert 'data-wz-overlay' not in box.get('/apps/habit-tracker/')[2]

    # The browser-side loader can switch it off too (script error / selector gone at runtime).
    (d / 'overlay.json').write_text(json.dumps({'requires': ['#new-habit-form']}))
    (d / 'overlay.css').write_text('#new-habit-form { order: -1; }\n')
    assert views.apply('habit-tracker', box.home)['active']
    assert box.post('/views/habit-tracker/_overlay-report', {'error': 'boom'}, Origin='https://evil.example')[0] == 403
    assert box.post('/views/habit-tracker/_overlay-report', {'error': 'boom'})[0] == 204
    assert views.load('habit-tracker', box.home)['suspended']
    with pytest.raises(views.ViewError, match="doesn't have"):
        (d / 'overlay.json').write_text(json.dumps({'requires': ['#gone']}))
        views.apply('habit-tracker', box.home)


def test_app_build_commands_never_run_on_the_host_by_default(box, monkeypatch):
    monkeypatch.delenv('WIZARD_VIEWS_BUILD')
    web = Path(views.customize('habit-tracker', 'own', box.home)['edit'])
    (web.parent / 'app' / 'build_view.py').write_text('open("/tmp/wz-host-pwned", "w").write("x")\n')
    rec = views.app_record('habit-tracker', box.home)
    monkeypatch.setattr(views, 'app_record', lambda *a, **k: {**rec, 'image': None})
    with pytest.raises(views.ViewError):
        views.apply('habit-tracker', box.home)  # planted build outside web/ is refused, and nothing ran on the host
    assert not Path('/tmp/wz-host-pwned').exists()
    subprocess.run(['git', '-C', str(box.home / 'views/habit-tracker/src'), 'checkout', '--', '.'], check=True)
    with pytest.raises(views.ViewError, match='No isolated build image'):
        views.apply('habit-tracker', box.home)


def test_modify_delete_conflict_is_resolved_explicitly(box):
    web = Path(views.customize('habit-tracker', 'own', box.home)['edit'])
    _edit(web / 'styles.css', '.habit-progress', '.mine { color: red; }\n.habit-progress')
    views.apply('habit-tracker', box.home)
    box.upstream_update(lambda w: (w / 'styles.css').unlink())
    v = views.update('habit-tracker', box.home)
    assert v['state'] == 'update_conflict' and v['conflicts'] == ['web/styles.css']
    v = views.resolve('habit-tracker', 'mine', box.home)
    assert v['state'] == 'active' and '.mine' in box.get('/views/habit-tracker/styles.css')[2]
    src = box.home / 'views/habit-tracker/src'
    assert subprocess.run(['git', '-C', str(src), 'merge-base', '--is-ancestor', 'upstream', 'main']).returncode == 0


def test_owner_merge_by_hand_is_kept_not_aborted(box):
    web = Path(views.customize('habit-tracker', 'own', box.home)['edit'])
    _edit(web / 'index.html', '<h1>Habits</h1>', '<h1>My rituals</h1>')
    views.apply('habit-tracker', box.home)
    box.upstream_update(lambda w: _edit(w / 'index.html', '<h1>Habits</h1>', '<h1>Daily habits</h1>'))
    assert views.update('habit-tracker', box.home)['state'] == 'update_conflict'
    src = box.home / 'views/habit-tracker/src'
    git = lambda *a: subprocess.run(['git', '-C', str(src), '-c', 'user.name=x', '-c', 'user.email=x@x', *a], capture_output=True, text=True)
    git('merge', 'upstream')
    with pytest.raises(views.ViewError, match='unfinished'):
        views.apply('habit-tracker', box.home)  # conflict markers still there: never silently aborted
    html = (src / 'web/index.html').read_text()
    start, end = html.index('<<<<<<<'), html.index('>>>>>>>')
    end = html.index('\n', end) + 1
    (src / 'web/index.html').write_text(html[:start] + '    <header class="wz-head"><h1>My daily rituals</h1><p id="day"></p></header>\n' + html[end:])
    git('add', '-A')
    v = views.apply('habit-tracker', box.home)
    assert v['state'] == 'active' and v['pending'] is None
    assert '<h1>My daily rituals</h1>' in box.get('/views/habit-tracker/')[2]


def test_interrupted_operations_heal_back_to_main(box, monkeypatch):
    web = Path(views.customize('habit-tracker', 'own', box.home)['edit'])
    _edit(web / 'index.html', '<h1>Habits</h1>', '<h1>Mine</h1>')
    views.apply('habit-tracker', box.home)
    src = box.home / 'views/habit-tracker/src'
    branch = lambda: subprocess.run(['git', '-C', str(src), 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True, text=True).stdout.strip()
    rec = views.app_record('habit-tracker', box.home)
    monkeypatch.setattr(views, 'app_record', lambda *a, **k: {**rec, 'version': 'new', 'source': str(box.home / 'gone')})
    with pytest.raises(views.ViewError, match='source is missing'):
        views.update('habit-tracker', box.home)
    assert branch() == 'main'
    monkeypatch.setattr(views, 'app_record', lambda *a, **k: rec)
    # A crash left the repo on upstream with a half-written tree: the next operation recovers.
    subprocess.run(['git', '-C', str(src), 'checkout', '-q', 'upstream'], check=True)
    (src / 'web/index.html').write_text('half')
    assert views.apply('habit-tracker', box.home)['state'] == 'active' and branch() == 'main'
    assert '<h1>Mine</h1>' in box.get('/views/habit-tracker/')[2]
    # Direct commits outside the screen folder are refused too, not only uncommitted ones.
    (src / 'app/server.py').write_text('# planted\n')
    subprocess.run(['git', '-C', str(src), '-c', 'user.name=x', '-c', 'user.email=x@x', 'commit', '-qam', 'x'], check=True)
    with pytest.raises(views.ViewError, match='Only the screen source'):
        views.apply('habit-tracker', box.home)


def test_overlay_files_are_checked_on_every_request_and_owner_js_loads_separately(box, tmp_path):
    d = Path(views.customize('habit-tracker', 'overlay', box.home)['edit'])
    (d / 'overlay.css').write_text('#add-form { order: -1; }\n')
    (d / 'overlay.js').write_text('document.body.dataset.owner = "1";\n')
    views.apply('habit-tracker', box.home)
    loader = box.get('/views/habit-tracker/_overlay.js')[2]
    assert 'dataset.owner' not in loader and '_overlay-owner.js' in loader
    assert 'dataset.owner' in box.get('/views/habit-tracker/_overlay-owner.js')[2]
    secret = tmp_path / 'secret.json'; secret.write_text('{"token": "s3cret"}')
    (d / 'overlay.css').unlink(); (d / 'overlay.css').symlink_to(secret)
    code, _, body = box.get('/views/habit-tracker/_overlay.css')
    assert code == 404 and 's3cret' not in body


def test_cross_site_reads_refused_and_uncustomized_apps_get_no_folder(box):
    assert box.get('/apps/habit-tracker/api/habits', **{'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'cors'})[0] == 403
    assert box.get('/apps/habit-tracker/', **{'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate'})[0] == 200
    with pytest.raises(views.ViewError, match='not customized'):
        views.revert('habit-tracker', box.home)
    assert not (box.home / 'views/habit-tracker').exists()


def test_cli_routes_views_and_update_local():
    src = (Path(builder.REPO) / 'wam/cli.py').read_text()
    assert 'argv[0] == "views"' in src and '"update-local"' in src
