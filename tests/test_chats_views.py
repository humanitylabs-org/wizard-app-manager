"""Chats packages: disposable source -> pinned recipe -> owner/share registry, never live state."""
import http.client
import json
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from wam import chats_views as cv, cli, private, registry, slots, store_install

MANIFEST = {'id': 'mizel-chats', 'name': 'Mizel Chats', 'provides': ['chats'],
            'chats_view': {'contract': cv.CONTRACT, 'module': 'web/chats.js'},
            'runtime': {'config': 'config.json', 'compose': 'docker-compose.yml'}}
MODULE = "export default { contract: 'wizard.chats-view/1', id: 'mizel-chats', name: 'Mizel Chats', mount(el, host) { return { update() { el.replaceChildren(host.element('div')); }, unmount() { el.replaceChildren(); } }; } };\n"
COMMIT = 'a' * 40


def package(root):
    root.mkdir(parents=True)
    (root / 'web').mkdir()
    (root / 'web/chats.js').write_text(MODULE)
    (root / 'wizard.md').write_text('---\n' + yaml.safe_dump(MANIFEST) + '---\n# Chats\n')
    (root / 'config.json').write_text('{"id":"mizel-chats", "port":8799}')
    (root / 'docker-compose.yml').write_text('services: {web: {image: "fixture:1"}}')
    return root


def admitted(tmp_path, monkeypatch):
    src = package(tmp_path / 'source')
    tipi, home = tmp_path / 'tipi', tmp_path / 'owner'
    monkeypatch.setenv('WIZARD_OS_HOME', str(home))
    monkeypatch.setattr(private, 'fetch', lambda repo, commit, dst, tok: shutil.copytree(src, dst))
    monkeypatch.setattr(private, 'head_commit', lambda *a: COMMIT)
    monkeypatch.setattr(private, 'pull', lambda *a: None)
    private.prepare(tipi, 'wizardapps', 'fixture/chats', tok='fixture-token')
    row = {'info': {'urn': 'mizel-chats:wizardapps'}, 'app': {'status': 'running'}}
    reg = registry.build([row], 'wizardapps', '127.0.0.1', tipi)
    registry.write(reg, home)
    slots.merge_into_registry(reg)
    registry.write(reg, home)
    return src, tipi, home, row, reg


def test_pinned_install_share_selection_and_uninstall(tmp_path, monkeypatch):
    src, tipi, home, row, reg = admitted(tmp_path, monkeypatch)
    public = json.loads((home / 'apps.json').read_text())
    view = public['apps'][0]['chats_view']
    assert public['apps'][0]['provides'] == ['chats']
    assert view['module'] == f"/wizard/v1/chats-views/mizel-chats/{view['sha256']}.js"
    assert view['commit'] == COMMIT
    # Serialized registries are rewritten by link publication too, without private source fields.
    registry.write(public, home)
    assert json.loads((home / 'apps.json').read_text())['apps'][0]['chats_view'] == view
    assert public['slots']['chats'] == {'default': 'wizard-chats', 'candidates': ['wizard-chats', 'mizel-chats']}
    assert cv.choose(home, 'mizel-chats')['default'] == 'mizel-chats'
    reg = registry.build([row], 'wizardapps', '', tipi)
    registry.write(reg, home)
    assert reg['slots']['chats']['default'] == 'mizel-chats'
    assert registry.collisions(reg) == []
    cfg = SimpleNamespace(home=home, runtipi_root=tipi)
    assert cli.share_registry(cfg)
    shared = tipi / 'app-data/wizard-os'
    assert json.loads((shared / 'apps.json').read_text())['slots']['chats']['default'] == 'mizel-chats'
    assert (shared / f"chats-views/mizel-chats/{view['sha256']}.js").read_text() == MODULE
    registry.write(registry.build([], 'wizardapps', '', tipi), home)
    cli.share_registry(cfg)
    assert not (shared / 'chats-views/mizel-chats').exists()
    assert json.loads((home / 'apps.json').read_text())['slots']['chats']['default'] == 'wizard-chats'
    with pytest.raises(ValueError, match='installed'):
        cv.choose(home, 'mizel-chats')


def test_recipe_tampering_is_not_resealed_by_registry_refresh(tmp_path, monkeypatch):
    _, tipi, _, row, _ = admitted(tmp_path, monkeypatch)
    (tipi / 'repos/wizardapps/apps/mizel-chats/web/chats.js').write_text(MODULE + '// changed')
    with pytest.raises(ValueError, match='integrity'):
        registry.build([row], 'wizardapps', '', tipi)


def test_several_chats_providers_are_additive_and_owner_choice_survives(tmp_path, monkeypatch):
    src, tipi, home, row, _ = admitted(tmp_path, monkeypatch)
    second = {**MANIFEST, 'id': 'other-chats'}
    dest = tipi / 'repos/wizardapps/apps/other-chats'
    dest.mkdir()
    (src / 'web/chats.js').write_text(MODULE.replace('mizel-chats', 'other-chats'))
    cv.prepare(src, dest, second, 'b' * 40)
    (dest / 'wizard.md').write_text('---\n' + yaml.safe_dump(second) + '---\n# Chats\n')
    (dest / '.wizard-commit').write_text('b' * 40)
    rows = [row, {'info': {'urn': 'other-chats:wizardapps'}, 'app': {'status': 'running'}}]
    registry.write(registry.build(rows, 'wizardapps', '', tipi), home)
    assert cv.choose(home, 'other-chats')['candidates'] == ['wizard-chats', 'mizel-chats', 'other-chats']
    reg = registry.build(rows, 'wizardapps', '', tipi)
    registry.write(reg, home)
    assert reg['slots']['chats']['default'] == 'other-chats'
    assert registry.collisions(reg) == []


@pytest.mark.parametrize('token', ['fetch', 'addEventListener', 'setTimeout', 'localStorage', 'innerHTML',
                                  'history', 'eval', 'Function', 'import', 'WebSocket', 'document.cookie'])
def test_lint_rejects_banned_apis(tmp_path, token):
    src = package(tmp_path / 'source')
    (src / 'web/chats.js').write_text(MODULE + token)
    with pytest.raises(ValueError, match='banned'):
        cv.prepare(src, tmp_path / 'recipe', MANIFEST, COMMIT)


@pytest.mark.parametrize('module', ['../chats.js', '/chats.js', 'https://foreign.test/chats.js', 'web/chats.js?x', 'web/chats.mjs'])
def test_bad_module_paths(tmp_path, module):
    src = package(tmp_path / 'source')
    manifest = {**MANIFEST, 'chats_view': {'contract': cv.CONTRACT, 'module': module}}
    with pytest.raises(ValueError):
        cv.validate(src, manifest)


def test_symlink_and_size_limits(tmp_path):
    src = package(tmp_path / 'source')
    file = src / 'web/chats.js'
    file.unlink(); file.symlink_to(src / 'config.json')
    with pytest.raises(ValueError, match='symlinks'):
        cv.validate(src, MANIFEST)
    file.unlink(); file.write_bytes(b'x' * (cv.MAX_BYTES + 1))
    with pytest.raises(ValueError, match='128 KiB'):
        cv.validate(src, MANIFEST)


def test_typescript_public_index_path_has_identical_seal(tmp_path):
    src = package(tmp_path / 'source')
    dest = tmp_path / 'public'
    helper = Path(__file__).resolve().parents[1] / 'packages/backend/src/modules/app-stores/chats-module.ts'
    code = f"import {{prepareChatsModule}} from {json.dumps(helper.as_uri())}; await prepareChatsModule(process.argv[1], process.argv[2], JSON.parse(process.argv[3]), process.argv[4]);"
    subprocess.run(['node', '--experimental-strip-types', '--input-type=module', '-e', code,
                    str(src), str(dest), json.dumps(MANIFEST), COMMIT], check=True, capture_output=True)
    assert cv.installed(dest, MANIFEST, COMMIT)['bytes'] == len(MODULE.encode())


def test_only_existing_settings_owner_listener_can_choose_default(tmp_path, monkeypatch):
    _, tipi, home, _, _ = admitted(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.StoreHandler, 'cfg', SimpleNamespace(home=home, runtipi_root=tipi), raising=False)
    (home / 'links.json').write_text('{"host":"box.test"}')
    monkeypatch.setattr(store_install, 'peer_uid', lambda p: 0)
    monkeypatch.setattr(store_install, 'owner_login', lambda: 'owner@example.test')
    monkeypatch.setattr(store_install, 'served_origins', lambda h: {'https://box.test'})
    server = ThreadingHTTPServer(('127.0.0.1', 0), cli.StoreHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def request(body=None, **extra):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
        headers = {'X-Forwarded-For': '100.64.0.9', 'Tailscale-User-Login': 'owner@example.test', 'Origin': 'https://box.test',
                   'X-Wizard-Store': '1', 'Content-Type': 'application/json', **extra}
        conn.request('POST' if body else 'GET', '/_slots/chats', json.dumps(body) if body else None, headers)
        res = conn.getresponse(); result = res.status, json.loads(res.read()); conn.close(); return result
    try:
        for headers in [{'Origin': 'https://app.test'}, {'Tailscale-User-Login': 'app@example.test'}, {'X-Wizard-Store': ''}]:
            assert request({'default': 'mizel-chats'}, **headers)[0] == 403
        assert request({'default': 'unknown'})[0] == 400
        assert request({'default': 'mizel-chats'})[0] == 200
        assert request()[1]['default'] == 'mizel-chats'
        assert request({'default': 'wizard-chats'})[0] == 200
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_optional_privacy_tag_reaches_the_registry(tmp_path, monkeypatch):
    """James, 23 Oct 2026: an optional tag marks an app private; untagged apps carry no privacy field."""
    _, tipi, home, row, reg = admitted(tmp_path, monkeypatch)
    assert 'privacy' not in next(a for a in reg['apps'] if a['id'] == 'mizel-chats')
    manifest = next(tipi.glob('repos/*/apps/mizel-chats/wizard.md'))
    text = manifest.read_text()
    manifest.write_text(text.replace('---\n', '---\nprivacy: private\n', 1))
    tagged = registry.build([row], 'wizardapps', '127.0.0.1', tipi)
    assert next(a for a in tagged['apps'] if a['id'] == 'mizel-chats')['privacy'] == 'private'
