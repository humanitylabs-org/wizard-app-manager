"""Link selections use pinned, checked source, never browser-provided install policy."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from wam import cli, private, registry, store_selection as ss

TEMPLATE = Path(__file__).resolve().parents[1] / 'templates/hello-wizard'
REPO = 'fixture/shared-app'
APP = 'hello-wizard'


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


@pytest.fixture
def linked(tmp_path, monkeypatch):
    """A real bare origin; advance HEAD so a floating fetch cannot pass the test."""
    src = tmp_path / 'origin'
    shutil.copytree(TEMPLATE, src)
    git(src, 'init', '-q')
    git(src, 'config', 'user.name', 'Test')
    git(src, 'config', 'user.email', 'test@example.invalid')
    git(src, 'add', '.')
    git(src, 'commit', '-qm', 'pinned app')
    commit = git(src, 'rev-parse', 'HEAD')
    wm = src / 'wizard.md'
    wm.write_text(wm.read_text().replace('id: hello-wizard', 'id: changed-app'))
    git(src, 'commit', '-qam', 'different HEAD identity')
    bare = tmp_path / 'shared.git'
    git(tmp_path, 'clone', '-q', '--bare', str(src), str(bare))
    config = tmp_path / 'gitconfig'
    config.write_text(f'[url "{bare.as_uri()}"]\n    insteadOf = https://github.com/{REPO}\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setattr(private, 'token', lambda: '')
    monkeypatch.setattr(private, 'pull', lambda *a: pytest.fail('no Docker pull in fixture'))
    monkeypatch.setattr(ss.index, 'resolve', lambda *a: pytest.fail('link is not a catalog lookup'))
    monkeypatch.setattr(ss.test_catalog, 'resolve', lambda *a: pytest.fail('link is not a private catalog lookup'))
    c = SimpleNamespace(home=tmp_path / 'home', runtipi_root=tmp_path / 'runtipi', runtipi=lambda: None)
    body = {'source': 'link', 'repo': REPO, 'commit': commit, 'operation': 'install'}
    return c, body


def test_link_resolves_checked_exact_commit_without_catalog(linked):
    c, body = linked
    selection = ss.resolve(c, APP, body)
    assert selection == {**body, 'entry': {'id': APP, 'repo': REPO, 'commit': body['commit'],
                                         'uses': [], 'source': 'link'}}
    assert not c.runtipi_root.exists()  # resolving cannot prepare, pull or install


def test_link_wrong_manifest_id(linked):
    c, body = linked
    with pytest.raises(ValueError, match='identity mismatch'):
        ss.resolve(c, 'another-app', body)


@pytest.mark.parametrize('changes', [
    {'commit': ''}, {'commit': 'main'}, {'commit': 'a' * 39},
    {'repo': 'https://github.com/fixture/shared-app'}, {'repo': 'github.com/fixture/shared-app'},
    {'repo': '../shared-app'}, {'repo': 'fixture/shared-app;command'},
    {'repo': 'fixture/shared-app\n'}, {'url': 'https://evil.invalid/app'},
])
def test_link_bad_selection_fails_before_fetch(linked, monkeypatch, changes):
    c, body = linked
    monkeypatch.setattr(private, 'fetch', lambda *a: pytest.fail('invalid identity must not fetch'))
    with pytest.raises(ValueError):
        ss.resolve(c, APP, {**body, **changes})


def test_link_missing_commit_key(linked, monkeypatch):
    c, body = linked
    body.pop('commit')
    monkeypatch.setattr(private, 'fetch', lambda *a: pytest.fail('no fetch'))
    with pytest.raises(ValueError, match='exact source'):
        ss.resolve(c, APP, body)


@pytest.mark.parametrize('bad', ['missing', 'invalid', 'unsafe'])
def test_link_uses_same_checker_and_rejects_invalid_source(linked, monkeypatch, bad):
    c, body = linked
    def fetch(repo, commit, dst, tok):
        shutil.copytree(TEMPLATE, dst)
        if bad == 'missing':
            (dst / 'wizard.md').unlink()
        elif bad == 'invalid':
            wm = dst / 'wizard.md'
            wm.write_text(wm.read_text().replace('type: service', 'type: unknown'))
        else:
            compose = dst / 'runtipi/docker-compose.yml'
            compose.write_text(compose.read_text().replace('    read_only: true', '    privileged: true'))
    monkeypatch.setattr(private, 'fetch', fetch)
    with pytest.raises(ValueError, match='wizard.md check failed'):
        ss.resolve(c, APP, body)
    assert not c.runtipi_root.exists()


@pytest.mark.parametrize('operation', ['update', 'switch'])
@pytest.mark.parametrize('existing,reason', [
    ({'repo': 'foreign/shared-app', 'source': 'link'}, 'repository conflict'),
    ({'repo': REPO, 'source': 'standard'}, 'source is not link'),
    ({'repo': REPO}, 'source is not link'),
])
def test_link_update_identity_must_match_before_prepare(linked, monkeypatch, operation, existing, reason):
    c, body = linked
    selection = ss.resolve(c, APP, {**body, 'operation': operation})
    installed = c.runtipi_root / 'apps' / cli.STORE / APP
    installed.mkdir(parents=True)
    (installed / '.wizard-source').write_text(json.dumps(existing))
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {APP})
    monkeypatch.setattr(private, 'prepare', lambda *a, **kw: pytest.fail('must not prepare'))
    with pytest.raises(ValueError, match=reason):
        ss.execute(c, APP, selection)


@pytest.mark.parametrize('operation', ['install', 'update', 'switch'])
def test_link_native_lifecycle_records_source_and_registry(linked, monkeypatch, operation):
    c, body = linked
    selection = ss.resolve(c, APP, {**body, 'operation': operation})
    if operation != 'install':
        installed = c.runtipi_root / 'apps' / cli.STORE / APP
        installed.mkdir(parents=True)
        (installed / '.wizard-source').write_text(json.dumps({'repo': REPO, 'source': 'link'}))
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: set() if operation == 'install' else {APP})
    calls = []
    def finish(config, app, **kw):
        calls.append(kw)
        src = c.runtipi_root / 'repos' / cli.STORE / 'apps' / APP
        dst = c.runtipi_root / 'apps' / cli.STORE / APP
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return {'status': 'running'}
    monkeypatch.setattr(cli, 'install', finish)
    monkeypatch.setattr(cli, 'app_update', finish)
    monkeypatch.setattr(cli, 'write_registry', lambda c: None)
    assert ss.execute(c, APP, selection)['status'] == 'running'
    installed = c.runtipi_root / 'apps' / cli.STORE / APP
    assert json.loads((installed / '.wizard-source').read_text())['source'] == 'link'
    assert (installed / '.wizard-commit').read_text().strip() == body['commit']
    assert json.loads((installed / '.wizard-selection').read_text()) == {'repo': REPO, 'commit': body['commit']}
    if operation != 'install':
        assert calls == [{}]  # native backup defaults; no uninstall
    rows = [{'app': {'status': 'running'}, 'info': {'urn': f'{APP}:{cli.STORE}'}}]
    reg = registry.build(rows, cli.STORE, '127.0.0.1', c.runtipi_root)
    saved = json.loads(registry.write(reg, c.home).read_text())['apps'][0]
    assert saved['source'] == 'link' and saved['repo'] == REPO and saved['commit'] == body['commit']


@pytest.mark.parametrize('source', ['standard', 'owner-private', 'alpha-test'])
def test_prepare_records_every_install_source(linked, source):
    c, body = linked
    private.prepare(c.runtipi_root, cli.STORE, REPO,
                    source_pin={'id': APP, 'repo': REPO, 'commit': body['commit']}, source=source)
    data = json.loads((c.runtipi_root / 'repos' / cli.STORE / 'apps' / APP / '.wizard-source').read_text())
    assert data['source'] == source


def test_prepare_link_rechecks_before_any_preparation(linked, monkeypatch):
    c, body = linked
    monkeypatch.setattr(private, '_check_link', lambda *a: (_ for _ in ()).throw(private.PrivateError('check failed')))
    with pytest.raises(private.PrivateError, match='check failed'):
        private.prepare(c.runtipi_root, cli.STORE, REPO, source='link',
                        source_pin={'id': APP, 'repo': REPO, 'commit': body['commit']})
    assert not c.runtipi_root.exists()


@pytest.mark.parametrize('identity,expected', [
    ({'repo': REPO, 'source': 'standard'}, 'standard'),
    ({'repo': REPO, 'source': 'owner-private'}, 'owner-private'),
    ({'repo': REPO, 'source': 'alpha-test'}, 'alpha-test'),
    ({'repo': REPO, 'source': 'link'}, 'link'),
    ({'repo': 'local/shared-app'}, 'local'),
    ({'local': '/fixture/developer/app'}, 'local'),
    ({'repo': REPO, 'source': 'local'}, 'local'),
    ({'repo': REPO}, 'standard'),
])
def test_registry_records_source_in_apps_json(tmp_path, identity, expected):
    app = tmp_path / 'apps' / cli.STORE / APP
    app.mkdir(parents=True)
    shutil.copy2(TEMPLATE / 'wizard.md', app / 'wizard.md')
    (app / '.wizard-source').write_text(json.dumps(identity))
    (app / '.wizard-commit').write_text('a' * 40)
    rows = [{'app': {'status': 'running'}, 'info': {'urn': f'{APP}:{cli.STORE}'}}]
    reg = registry.build(rows, cli.STORE, '127.0.0.1', tmp_path)
    stored = json.loads(registry.write(reg, tmp_path / 'home').read_text())['apps'][0]
    assert stored['source'] == expected
    assert '/fixture/developer/app' not in json.dumps(stored)


def test_registry_installed_source_wins_over_prospective_catalog_recipe(linked, monkeypatch):
    c, body = linked
    test_link_native_lifecycle_records_source_and_registry(linked, monkeypatch, 'install')
    recipe = c.runtipi_root / 'repos' / cli.STORE / 'apps' / APP
    (recipe / '.wizard-source').write_text(json.dumps({'repo': 'catalog/other', 'source': 'standard'}))
    (recipe / '.wizard-commit').write_text('b' * 40)
    meta = registry.app_meta(c.runtipi_root, cli.STORE, APP)
    assert (meta['repo'], meta['source'], meta['commit']) == (REPO, 'link', body['commit'])


def test_saved_github_token_is_used_without_persisting_auth(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    monkeypatch.delenv('GH_TOKEN', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    (tmp_path / '.wizard-os').mkdir()
    (tmp_path / '.wizard-os/github-token').write_text('fixture-only-token\n')
    assert private.token() == 'fixture-only-token'
    env = private._git_env(private.token())
    assert env['GIT_CONFIG_KEY_0'] == 'http.https://github.com/.extraheader'
    assert env['GIT_CONFIG_VALUE_0'].startswith('Authorization: Basic ')
    monkeypatch.setenv('GITHUB_TOKEN', 'environment-fixture')
    assert private.token() == 'environment-fixture'


def test_alpha_child_preparation_records_owner_local_source_id(linked, monkeypatch):
    c, body = linked
    monkeypatch.setenv('WAM_INSTALL_SOURCE', 'alpha-test')
    private.prepare(c.runtipi_root, cli.STORE, REPO,
                    source_pin={'id': APP, 'repo': REPO, 'commit': body['commit']})
    recipe = c.runtipi_root / 'repos' / cli.STORE / 'apps' / APP
    assert json.loads((recipe / '.wizard-source').read_text())['source'] == 'alpha-test'


def test_authenticated_git_fetch_does_not_persist_header(linked, tmp_path):
    c, body = linked
    dest = tmp_path / 'authenticated-fetch'
    private.fetch(REPO, body['commit'], dest, 'fixture-only-token')
    assert git(dest, 'rev-parse', 'HEAD') == body['commit']
    config = (dest / '.git/config').read_text()
    assert 'fixture-only-token' not in config
    assert 'extraheader' not in config.lower()
    assert 'Authorization' not in config


def test_link_preloads_private_images_with_server_login(linked, monkeypatch):
    c, body = linked
    pulled = []
    monkeypatch.setattr(private, 'token', lambda: 'server-token')
    monkeypatch.setattr(private, 'images', lambda compose: ['ghcr.io/owner/private-app:1'])
    monkeypatch.setattr(private, 'pull', lambda img, tok: pulled.append((img, tok)))
    private.prepare(c.runtipi_root, cli.STORE, REPO, source='link',
                    source_pin={'id': APP, 'repo': REPO, 'commit': body['commit']})
    assert pulled == [('ghcr.io/owner/private-app:1', 'server-token')]


def test_link_image_preload_is_best_effort_and_standard_pins_never_pull(linked, monkeypatch):
    c, body = linked
    monkeypatch.setattr(private, 'token', lambda: 'server-token')
    monkeypatch.setattr(private, 'images', lambda compose: ['ghcr.io/owner/app:1'])
    monkeypatch.setattr(private, 'pull', lambda *a: (_ for _ in ()).throw(private.PrivateError('no docker access')))
    private.prepare(c.runtipi_root, cli.STORE, REPO, source='link',
                    source_pin={'id': APP, 'repo': REPO, 'commit': body['commit']})
    assert (c.runtipi_root / 'repos' / cli.STORE / 'apps' / APP / '.wizard-source').is_file()
    monkeypatch.setattr(private, 'pull', lambda *a: pytest.fail('standard pins use Runtipi pulls'))
    private.prepare(c.runtipi_root, cli.STORE, REPO, source='standard',
                    source_pin={'id': APP, 'repo': REPO, 'commit': body['commit']})
