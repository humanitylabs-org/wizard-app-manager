import fcntl
import hashlib
import os
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from wam import cli, phone_sync as ps


@pytest.fixture
def mirror(tmp_path):
    home, root = tmp_path / 'home', tmp_path / 'runtipi'
    home.mkdir()
    (home / 'release.json').write_text(json.dumps({'status': 'pinned', 'release': '0.3.40'}))
    state = {'apk': b'fixture APK bytes', 'hits': [], 'status': 200, 'redirect': False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state['hits'].append(self.path)
            if self.path == '/latest.json':
                body = json.dumps(state['pointer']).encode()
                self.send_response(state['status'])
            elif self.path == '/redirect.apk':
                self.send_response(302); self.send_header('Location', '/phone.apk'); self.end_headers(); return
            else:
                body = state['apk']
                self.send_response(200)
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    state['pointer'] = {'app': 'ai.wizards.os', 'versionCode': 78, 'versionName': '0.10.14',
                        'sha256': hashlib.sha256(state['apk']).hexdigest(), 'bytes': len(state['apk']),
                        'signer': ps.SIGNER, 'asset_url': origin + '/phone.apk', 'notes': 'Fixture release',
                        'min_box_snapshot': '0.3.40', 'source_commit': 'a' * 40}
    state.update(home=home, root=root, feed=root / 'app-data/wizard-os/phone', url=origin + '/latest.json')
    state['run'] = lambda: ps.sync(home, root, pointer_url=state['url'], fixture=True)
    yield SimpleNamespace(**state, state=state)
    server.shutdown(); server.server_close(); thread.join()


def test_mirror_idempotent_and_relative_feed(mirror):
    m = mirror
    assert m.run() == 'updated:versionCode=78'
    feed = json.loads((m.feed / 'latest.json').read_text())
    assert feed == {k: m.pointer[k] for k in ('app', 'versionCode', 'versionName', 'bytes', 'sha256', 'signer', 'notes')} | {
        'version': '0.10.14', 'url': 'wizard-os-0.10.14.apk'}
    apk = m.feed / feed['url']
    assert apk.read_bytes() == m.apk
    assert apk.stat().st_mode & 0o777 == 0o644
    assert m.feed.stat().st_mode & 0o777 == 0o755
    assert m.run() == 'unchanged'
    assert m.state['hits'].count('/phone.apk') == 1


def test_tampered_sha_rejected(mirror):
    mirror.pointer['sha256'] = '0' * 64
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert not (mirror.feed / 'latest.json').exists()
    assert list(mirror.feed.glob('*.apk')) == []
    assert list(mirror.feed.glob('.phone-*')) == []


def test_pointer_404_noop(mirror):
    mirror.state['status'] = 404
    assert mirror.run() == 'skipped:pointer_404'
    assert not mirror.feed.exists()


@pytest.mark.parametrize('installed', ['0.3.39', '0.3.40-rc.1', None])
def test_min_snapshot_gate(mirror, installed):
    (mirror.home / 'release.json').write_text(json.dumps({'status': 'pinned', 'release': installed}))
    assert mirror.run() == 'skipped:min_box_snapshot'
    assert not mirror.feed.exists()
    assert mirror.state['hits'] == ['/latest.json']


def test_never_downgrades(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    before = (mirror.feed / 'latest.json').read_bytes()
    mirror.pointer['versionCode'] = 77
    assert mirror.run() == 'skipped:no_downgrade'
    assert (mirror.feed / 'latest.json').read_bytes() == before
    assert mirror.state['hits'].count('/phone.apk') == 1


def test_invalid_new_release_preserves_old(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    before = (mirror.feed / 'latest.json').read_bytes()
    mirror.pointer.update(versionCode=79, versionName='0.10.15', sha256='0' * 64)
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert (mirror.feed / 'latest.json').read_bytes() == before
    assert list(p.name for p in mirror.feed.glob('*.apk')) == ['wizard-os-0.10.14.apk']


def test_upgrade_cleans_only_superseded_apks(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    (mirror.feed / 'keep.txt').write_text('owner file')
    mirror.pointer.update(versionCode=79, versionName='0.10.15')
    assert mirror.run() == 'updated:versionCode=79'
    assert list(p.name for p in mirror.feed.glob('*.apk')) == ['wizard-os-0.10.15.apk']
    assert (mirror.feed / 'keep.txt').exists()


def test_corrupt_local_apk_repaired(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    (mirror.feed / 'wizard-os-0.10.14.apk').write_bytes(b'corrupt')
    assert mirror.run() == 'updated:versionCode=78'
    assert (mirror.feed / 'wizard-os-0.10.14.apk').read_bytes() == mirror.apk


def test_immutable_apk_collision_rejected(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    mirror.pointer.update(versionCode=79, sha256='0' * 64)
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert (mirror.feed / 'wizard-os-0.10.14.apk').read_bytes() == mirror.apk


def test_github_style_redirect_followed(mirror):
    mirror.pointer['asset_url'] = mirror.url.replace('/latest.json', '/redirect.apk')
    assert mirror.run() == 'updated:versionCode=78'
    assert '/redirect.apk' in mirror.state['hits'] and '/phone.apk' in mirror.state['hits']


@pytest.mark.parametrize('key,value', [('app', 'other.app'), ('versionCode', True), ('versionCode', -1),
                                      ('bytes', ps.MAX_APK + 1), ('sha256', 'bad'), ('signer', '0' * 64),
                                      ('source_commit', 'bad'), ('versionName', '../bad'),
                                      ('min_box_snapshot', 'bad'), ('notes', []),
                                      ('asset_url', 'https://evil.test/app.apk')])
def test_pointer_field_validation(mirror, key, value):
    mirror.pointer[key] = value
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert not mirror.feed.exists()


def test_small_pointer_cap(mirror, monkeypatch):
    monkeypatch.setattr(ps, 'MAX_POINTER', 16)
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert not mirror.feed.exists()


def test_actual_size_mismatch(mirror):
    mirror.pointer['bytes'] += 1
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert not (mirror.feed / 'latest.json').exists()
    assert list(mirror.feed.glob('*.apk')) == []


def test_offline_is_successful_noop(mirror):
    assert ps.sync(mirror.home, mirror.root, pointer_url='http://127.0.0.1:1/latest.json', fixture=True) == 'skipped:unavailable_or_invalid'


def test_timer_always_enabled_even_check_mode(mirror, tmp_path):
    (mirror.home / 'maintain.json').write_text('{"mode":"check"}')
    (mirror.home / 'updates.json').write_text('{"mode":"ask"}')
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)
    units = tmp_path / 'units'
    assert ps.ensure_timer(mirror.home, mirror.root, unit_dir=units, runner=runner) == 'timer:wizard-phone-sync.timer'
    timer = (units / 'wizard-phone-sync.timer').read_text()
    service = (units / 'wizard-phone-sync.service').read_text()
    assert 'OnCalendar=hourly' in timer and 'Persistent=true' in timer
    assert ' phone-sync\n' in service and 'maintain' not in service and '--apply' not in service
    assert f'RUNTIPI_ROOT={mirror.root}' in service and f'WIZARD_OS_HOME={mirror.home}' in service
    assert calls == [['systemctl', '--user', 'daemon-reload'], ['systemctl', '--user', 'enable', '--now', 'wizard-phone-sync.timer']]
    assert json.loads((mirror.home / 'updates.json').read_text()) == {'mode': 'ask'}


def test_cli_one_line_exit_zero_on_noop(mirror, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'Config', lambda: SimpleNamespace(home=mirror.home, runtipi_root=mirror.root))
    monkeypatch.setattr(ps, 'sync', lambda *a: 'skipped:pointer_404')
    assert cli.main(['phone-sync']) == 0
    assert capsys.readouterr().out == 'phone-sync skipped:pointer_404\n'


def test_install_wires_timer_independently():
    text = (Path(__file__).parents[1] / 'install.sh').read_text()
    assert 'RUNTIPI_ROOT="$RUNTIPI_ROOT" "$HOME/.local/bin/wam" phone-sync --ensure-timer' in text


def test_publication_orders_apk_before_feed(mirror, monkeypatch):
    original = ps._atomic_json
    def publish(path, value):
        assert (path.parent / value['url']).read_bytes() == mirror.apk
        assert not path.exists()
        return original(path, value)
    monkeypatch.setattr(ps, '_atomic_json', publish)
    assert mirror.run() == 'updated:versionCode=78'


def test_overlapping_runs_skip(mirror):
    mirror.feed.mkdir(parents=True)
    with (mirror.feed / '.sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert mirror.run() == 'skipped:busy'
    assert mirror.state['hits'] == ['/latest.json']


def test_restrictive_umask_still_publishes_readable_files(mirror):
    mask = os.umask(0o077)
    try:
        assert mirror.run() == 'updated:versionCode=78'
    finally:
        os.umask(mask)
    assert mirror.feed.parent.stat().st_mode & 0o777 == 0o755
    assert mirror.feed.stat().st_mode & 0o777 == 0o755
    for name in ['latest.json', 'wizard-os-0.10.14.apk']:
        assert (mirror.feed / name).stat().st_mode & 0o777 == 0o644


def test_production_asset_contract(mirror):
    pointer = dict(mirror.pointer, asset_url='https://github.com/humanitylabs-org/wizard-ux/releases/download/android-v0.10.14/wizard-os-0.10.14.apk')
    assert ps.validate(pointer) == pointer
    with pytest.raises(ValueError):
        ps.validate(mirror.pointer)


def test_default_pointer_is_public_mirror_beside_box_snapshots():
    # wizard-os is private: a pointer there 404s on every box (regression, 31 Oct 2026).
    from wam import updates
    assert ps.POINTER == updates.RELEASE_BASE + '/phone/latest.json'


def test_published_mirror_pointer_validates():
    import json
    pointer = json.loads((Path(__file__).resolve().parents[1] / 'releases/phone/latest.json').read_text())
    assert ps.validate(pointer)['app'] == 'ai.wizards.os'
