"""A failed Store install must not pin its broken release, and apps must open in every shell alias."""
import json
from types import SimpleNamespace
import pytest
from wam import store_selection as ss, cli, private, links

PIN = 'a' * 40
ENTRY = {'id': 'demo', 'repo': 'org/demo', 'commit': PIN, 'uses': []}
BODY = {'source': 'standard', 'repo': 'org/demo', 'commit': PIN, 'operation': 'install'}


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(ss.index, 'resolve', lambda index, ref: {**ENTRY, 'install': {'commit': PIN}})
    monkeypatch.setattr(ss.index, 'search', lambda *a, **k: [])
    return SimpleNamespace(home=tmp_path, runtipi_root=tmp_path, index='https://index.test', runtipi=lambda: None)


def staged(root):
    folder = root / 'repos/wizardapps/apps/demo'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / '.wizard-source').write_text(json.dumps({'repo': 'org/demo', 'source': 'standard'}))
    (folder / '.wizard-commit').write_text(PIN + '\n')
    return folder


def test_failed_store_install_leaves_no_pin(c, monkeypatch):
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: set())
    monkeypatch.setattr(private, 'prepare', lambda root, store, repo, **k: (staged(root), dict(ENTRY))[1])
    def broken(*a, **k):
        raise SystemExit('demo: install failed (the App Manager removed it)')
    monkeypatch.setattr(cli, 'install', broken)
    with pytest.raises(SystemExit):
        ss.execute(c, 'demo', ss.resolve(c, 'demo', BODY))
    assert not (c.runtipi_root / 'repos/wizardapps/apps/demo/.wizard-selection').exists()


def test_stale_pin_dropped_only_for_uninstalled_other_commit(c):
    folder = staged(c.runtipi_root)
    pin = folder / '.wizard-selection'
    pin.write_text(json.dumps({'repo': 'org/demo', 'commit': PIN}))
    assert cli.drop_stale_selection(c, 'demo', PIN) is False      # same release: the Store flow itself
    assert pin.exists()
    assert cli.drop_stale_selection(c, 'demo', 'b' * 40) is True  # the index moved on: refetch
    assert not pin.exists()
    assert cli.drop_stale_selection(c, 'demo', 'b' * 40) is False


def test_trusted_hosts_cover_every_shell_alias(c, monkeypatch):
    app = c.runtipi_root / 'repos/wizardapps/apps/demo'
    app.mkdir(parents=True)
    (app / 'docker-compose.yml').write_text('services: {demo: {environment: {WIZARD_TRUSTED_HOSTS: x}}}\n')
    (c.home / 'links.json').write_text(json.dumps({'host': 'box.tail.ts.net'}))
    monkeypatch.setattr(cli.alongside, 'load', lambda home: {'shell': {'port': 8796}})
    monkeypatch.setattr(links, 'serve_status', lambda: {})
    monkeypatch.setattr(links, 'gateway_shell_ports', lambda cfg, host, port: [8444, port])
    env = cli.trusted_origin_env(c, 'demo')
    assert env['WIZARD_TRUSTED_HOSTS'].split(',') == ['box.tail.ts.net:8796', 'box.tail.ts.net', 'box.tail.ts.net:8444']
    assert 'https://box.tail.ts.net:8444' in env['WIZARD_TRUSTED_ORIGINS'].split(',')
    # No tailscale on the box: the single shell port still works.
    monkeypatch.setattr(links, 'serve_status', lambda: (_ for _ in ()).throw(OSError('no tailscale')))
    assert cli.trusted_origin_env(c, 'demo')['WIZARD_TRUSTED_HOSTS'] == 'box.tail.ts.net:8796,box.tail.ts.net'
