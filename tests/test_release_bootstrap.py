"""Release pins enter the real installer/dependency path; no Docker/network required."""
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from wam import cli, private, release_pins, alongside
from test_install_docker import sandbox
from test_release_pins import box

REPO = Path(__file__).resolve().parents[1]


def manifest():
    return json.loads((REPO / 'releases/0.2.2.json').read_text())


@pytest.fixture
def pins(tmp_path, monkeypatch):
    p = tmp_path / 'pins.json'
    p.write_text(json.dumps(manifest()))
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(p))
    return p


@pytest.mark.parametrize('field,value', [('commit', 'main'), ('repo', '../app'), ('app', '../escape'),
                                         ('commit', None), ('repo', 'https://github.com/org/app')])
def test_invalid_pin_stops_before_runtipi_or_files(pins, tmp_path, field, value):
    doc = json.loads(pins.read_text())
    doc['default_apps']['files'][field] = value
    pins.write_text(json.dumps(doc))
    c = SimpleNamespace(runtipi=Mock(side_effect=AssertionError('must not contact Runtipi')))
    with pytest.raises(ValueError):
        cli.install(c, 'wizard-ux')
    with pytest.raises(ValueError):
        cli.bootstrap(c)
    p, home, root, calls, _ = sandbox(tmp_path / 'sandbox', WIZARD_RELEASE_PINS=str(pins))
    assert p.returncode != 0
    assert not calls.exists()  # not even private access/service preparation
    assert not (home / '.wizard-os').exists()


def test_get_rejects_invalid_source_pin_before_checkout(box):
    p = box['rel'] / '9.9.9.json'
    doc = json.loads(p.read_text())
    doc['default_apps']['files'].update(repo='org/files', commit='main')
    p.write_text(json.dumps(doc))
    result, receipt = box['run']()
    assert result.returncode == 2 and receipt is None
    assert not (box['home'] / 'wam').exists()


def fixture_fetch(repo, commit, src, tok):
    """Source fixtures only; exercise the real prepare/write/safety/image code."""
    app = {'oscarhenrycollins/ai-accounts': 'wizard-ai-accounts'}.get(repo, repo.split('/')[-1])
    uses = {'wizard-ux': ['agent-service'], 'wizard-agent-service': ['agent']}.get(app, [])
    provides = {'wizard-ux': ['shell'], 'wizard-agent-service': ['agent-service'], 'hermes-agent': ['agent']}.get(app, [])
    src.mkdir(parents=True)
    (src / 'wizard.md').write_text('---\n' + yaml.safe_dump(dict(id=app, name=app, version='fixture',
        uses=uses, provides=provides, runtime={'config': 'config.json', 'compose': 'compose.yml'})) + '---\n')
    (src / 'config.json').write_text(json.dumps({'port': 0, 'version': 'fixture'}))
    (src / 'compose.yml').write_text(yaml.safe_dump({'services': {app: {'image': f'ghcr.io/example/{app}:catalog',
        'x-runtipi': {'is_main': True, 'internal_port': 8000}}}}))


@pytest.fixture
def install_box(tmp_path, monkeypatch, pins):
    root = tmp_path / 'runtipi'
    home = tmp_path / 'home'
    home.mkdir()
    installed, calls, fetched, pulled = set(), [], [], []
    def fetch(*args):
        fetched.append(args[:2])
        fixture_fetch(*args)
    monkeypatch.setattr(private, 'fetch', fetch)
    monkeypatch.setattr(private, 'token', lambda: '')  # public release needs no GitHub login
    monkeypatch.setattr(private, 'pull', lambda image, tok: pulled.append(image))
    monkeypatch.setattr(cli.index, 'resolve', Mock(side_effect=AssertionError('release used live index')))
    monkeypatch.setattr(cli, 'ensure_store', lambda *a: None)
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: set(installed))
    monkeypatch.setattr(cli, 'trusted_origin_env', lambda *a: {})
    monkeypatch.setattr(cli, 'port_free', lambda *a: True)
    monkeypatch.setattr(cli, 'host_dashboard_consumers', lambda *a: {})
    monkeypatch.setattr(cli, 'store_data_cmd', lambda *a: {})
    from wam import builder_bundle
    monkeypatch.setattr(builder_bundle, 'fresh', lambda *a: None)
    # Docker media ownership is exercised by its dedicated alongside tests,
    # not a real daemon from this fake release installer fixture.
    monkeypatch.setattr(alongside, 'adopt_media', lambda *a, **kw: True)

    def reg(*args):
        apps = []
        for app in sorted(installed):
            d = yaml.safe_load((root / 'apps/wizardapps' / app / 'wizard.md').read_text().split('---')[1])
            apps.append({'id': app, 'provides': d['provides']})
        return {'apps': apps, 'external': alongside.external_slots(home)}

    class RT:
        def app(self, urn):
            return {'info': json.loads((root / 'repos/wizardapps/apps' / urn.split(':')[0] / 'config.json').read_text())}
        def install(self, urn, form):
            app = urn.split(':')[0]
            source = root / 'repos/wizardapps/apps' / app
            dest = root / 'apps/wizardapps' / app
            shutil.copytree(source, dest)
            compose = yaml.safe_load((dest / 'docker-compose.yml').read_text())
            calls.append((app, compose['services'][app]['image'], form))
            installed.add(app)
        def wait_status(self, *a):
            return 'running'
    rt = RT()
    c = SimpleNamespace(home=home, runtipi_root=root, secrets=home / 'secrets', runtipi=lambda: rt,
                        private_apps={}, index='https://unused.invalid')
    monkeypatch.setattr(cli, 'write_registry', reg)
    return c, calls, fetched, pulled


def test_fresh_first_recursive_dependency_uses_source_and_image_pins(install_box):
    c, calls, fetched, pulled = install_box
    cli.install(c, 'wizard-ux')
    assert [app for app, _, _ in calls] == ['hermes-agent', 'wizard-agent-service', 'wizard-ux']
    doc = manifest()
    for app, image, _ in calls:
        row = next(r for r in doc['default_apps'].values() if r and r['app'] == app)
        assert (row['repo'], row['commit']) in fetched
        if app in doc['images']:
            assert image == release_pins.image_ref(doc['images'][app])
    assert pulled == []  # public release pulls use Runtipi, not the fresh user's Docker socket


def test_missing_index_default_is_materialized_without_credentials(install_box):
    c, calls, fetched, _ = install_box
    cli.install(c, 'wizard-ai-accounts')
    assert calls[0][0] == 'wizard-ai-accounts'
    assert fetched == [('oscarhenrycollins/ai-accounts', manifest()['default_apps']['ai-accounts']['commit'])]


def test_explicit_image_override_applies_on_initial_fresh_dependency(install_box, monkeypatch):
    c, calls, _, _ = install_box
    override = 'ghcr.io/example/service@sha256:' + 'a' * 64
    monkeypatch.setenv('WIZARD_AGENT_SERVICE_IMAGE', override)
    cli.install(c, 'wizard-ux')
    assert next(image for app, image, _ in calls if app == 'wizard-agent-service') == override
    # get.sh independently reads running digests; an override is never exact-release proof.


def test_alongside_skips_external_hermes_and_keeps_host_override(install_box, monkeypatch):
    c, calls, fetched, _ = install_box
    st = {'mode': 'alongside', 'agent': {'id': 'host-hermes', 'name': 'host Hermes', 'hermes_home': str(c.home / 'hermes'),
          'origin': 'http://127.0.0.1:8642', 'uid': 1000, 'gid': 1000},
          'shell': {'port': 8798}, 'service': {'port': 8797},
          'images': {app: release_pins.image_ref(im) for app, im in manifest()['images'].items()}}
    alongside.save(c.home, st)
    (c.home / 'hermes').mkdir()
    sentinel = c.home / 'hermes/config.yaml'
    sentinel.write_bytes(b'owner sentinel\n')
    cli.install(c, 'wizard-ux')
    assert [a for a, _, _ in calls] == ['wizard-agent-service', 'wizard-ux']
    assert not any(repo.endswith('/hermes-agent') for repo, _ in fetched)
    assert sentinel.read_bytes() == b'owner sentinel\n'
    for app in ('wizard-agent-service', 'wizard-ux'):
        text = (c.runtipi_root / 'user-config/wizardapps' / app / 'docker-compose.yml').read_text()
        assert 'network_mode: host' in text and '127.0.0.1' in text
        assert st['images'][app] in text


def test_unpinned_resolution_unchanged_with_and_without_release(pins, monkeypatch, tmp_path):
    c = SimpleNamespace(runtipi_root=tmp_path, private_apps={}, index='index')
    index = Mock(return_value={'id': 'other'})
    monkeypatch.setattr(cli.index, 'resolve', index)
    assert cli.resolve_any(c, 'other') == {'id': 'other'}
    monkeypatch.delenv('WIZARD_RELEASE_PINS')
    assert cli.resolve_any(c, 'wizard-ux') == {'id': 'other'}
    assert index.call_count == 2


def test_installer_keeps_manager_digest_and_starts_pin_view_first(pins, tmp_path):
    image = release_pins.image_ref(manifest()['images']['wizard-app-manager'])
    p, home, root, calls, _ = sandbox(tmp_path / 'box', WIZARD_RELEASE_PINS=str(pins), WAM_IMAGE=image)
    assert p.returncode == 0, p.stdout + p.stderr
    text = (root / 'user-config/tipi-compose.yml').read_text()
    assert text.count('image: ' + image) == 2  # manager + tiny Node index view, no new image
    assert '!override' in text and '127.0.0.1:8480:80' in text
    assert 'ports:' not in text.split('  wizard-release-index:')[1]
    log = calls.read_text()
    assert log.index('up -d --force-recreate --wait wizard-release-index') < log.index('up -d --force-recreate --no-deps runtipi')
    assert log.index('up -d --force-recreate --no-deps runtipi') < log.index('wam -m wam bootstrap')
    assert f'WIZARD_RELEASE_PINS={pins}' in (home / '.config/systemd/user/wizard-app-manager-api.service').read_text()


def test_compose_roundtrip_preserves_tags_owner_settings_and_unpinned_mode(pins, tmp_path):
    path = tmp_path / 'compose.yml'
    path.write_text('services:\n  runtipi:\n    environment:\n      OWNER: keep\n  proxy:\n    ports: !override ["127.0.0.1:80:80"]\n    networks: !reset []\n')
    release_pins.configure_manager(path, 'example/manager:1', str(pins))
    first = path.read_text()
    release_pins.configure_manager(path, 'example/manager:1', str(pins))
    assert path.read_text() == first
    release_pins.configure_manager(path, 'example/manager:2', None)
    text = path.read_text()
    assert 'WIZARD_INDEX_URL' not in text and 'wizard-release-index' not in text
    assert '!override' in text and '!reset' in text and 'OWNER: keep' in text


@pytest.mark.parametrize('drift', [False, True])
def test_candidate_helper_preserves_original_and_never_greens_source_exception(box, tmp_path, drift):
    import importlib.util
    from test_release_pins import OTHER
    spec = importlib.util.spec_from_file_location('candidate_qa', REPO / 'scripts/qa-release-candidate.py')
    qa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qa)
    original = json.loads((box['rel'] / '9.9.9.json').read_text())
    original['components']['wizard-app-manager']['commit'] = '1' * 40
    path = tmp_path / 'original.json'
    raw = json.dumps(original).encode()
    path.write_bytes(raw)
    rel, record, releases = qa.stage(path, box['pinned'], tmp_path / 'evidence')
    assert path.read_bytes() == raw
    assert (releases.parent / 'original-release.json').read_bytes() == raw
    rel['components']['wizard-app-manager']['commit'] = '1' * 40
    assert rel == original  # exactly one selector changes, not the image or default pins
    env = {'WIZARD_RELEASE_BASE': releases.as_uri(), 'WIZARD_RELEASE': '9.9.9', 'WIZARD_STRICT': '1'}
    if drift: env['SHELL_DIGEST'] = OTHER
    result, receipt = box['run'](**env)
    assert result.returncode == (3 if drift else 0)
    final = qa.record_readback(record, receipt, box['pinned'])
    assert final['status'] == 'mismatch' and final['release_exact'] is False
    source = next(c for c in final['checks'] if c['name'] == 'wizard-app-manager commit')
    assert source == {'name': 'wizard-app-manager commit', 'expected': '1' * 40, 'observed': box['pinned'], 'ok': False, 'core': True}
    assert record['candidate_checks_passed'] is (not drift)


def test_real_node_index_pin_lookup_and_unrelated_fallback(pins, tmp_path):
    # Exercise real HTTP, not an invented proxy response. Upstream is a local fixture.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    hits = []
    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200); self.end_headers()
            if self.path == '/v1/search?q=wizard':
                self.wfile.write(b'{"apps":[{"id":"wizard-ux","name":"Wizard","summary":"Keep this description","install":{"commit":"live"}}]}')
                return
            self.wfile.write(b'{"apps":[{"id":"unrelated","install":{"commit":"live"}}],"next_offset":null}')
        def log_message(self, *a): pass
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    env = {**os.environ, 'PORT': str(port), 'HOST': '127.0.0.1',
           'WIZARD_RELEASE_INDEX_UPSTREAM': f'http://127.0.0.1:{upstream.server_port}'}
    proc = subprocess.Popen(['node', str(REPO / 'wam/release-index.mjs')], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def get(path):
        with urllib.request.urlopen(f'http://127.0.0.1:{port}' + path, timeout=2) as r:
            return json.load(r)
    try:
        for _ in range(200):  # node startup on a busy CI runner can take seconds, not 1s
            try:
                get('/healthz'); break
            except OSError:
                time.sleep(.05)
        for row in manifest()['default_apps'].values():
            if not row: continue
            rec = get('/v1/search?id=' + row['app'])['apps'][0]
            assert rec['install']['commit'] == row['commit']
            assert rec['install']['git'] == 'https://github.com/' + row['repo']
        assert hits == []  # removed app and every release pin bypass live catalog entirely
        assert get('/v1/search?id=unrelated')['apps'][0]['install']['commit'] == 'live'
        assert hits == ['/v1/search?id=unrelated']
        browse = get('/v1/search?q=wizard')['apps'][0]
        assert browse['name'] == 'Wizard' and browse['summary'] == 'Keep this description'
        assert browse['install']['commit'] == manifest()['default_apps']['shell']['commit']
    finally:
        proc.terminate(); proc.communicate(timeout=5)
        upstream.shutdown(); upstream.server_close()
