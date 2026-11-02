"""Per-installation gateway key for box-only MCP services (wizard-os #386, card 5): Transcriber, Browser and Voice.

WAM mints it per installation, hands it to the app only through its own Runtipi user env (WIZARD_GATEWAY_KEY) and to
the Agent Service gateway through grants/agent-service.json `outgoing` (which the gateway already sends as
X-Wizard-Grant). The value never reaches apps.json; reinstall rotates it, uninstall removes it, unmanaged rows stay.
"""
import json
import stat
from types import SimpleNamespace

import pytest

from wam import browser_admission as ba
from wam import cli

GATED = ("services:\n  {app}:\n    environment:\n      WIZARD_BROWSER_BOUNDARY: ${{WIZARD_BROWSER_BOUNDARY:-0}}\n"
         "      WIZARD_GATEWAY_KEY: ${{WIZARD_GATEWAY_KEY:-}}\n")


def install(root, app_id='transcriber', serial=7, gated=True, port=8791, mcp=True):
    d = root / 'apps' / 'wizardapps' / app_id
    d.mkdir(parents=True, exist_ok=True)
    (d / 'wizard.md').write_text(f'---\nid: {app_id}\nname: {app_id}\ntype: service\nversion: 1.0.0\n'
                                 f'provides: [{app_id}-job]\n' + ('mcp: {path: /mcp, port: 8000}\n' if mcp else '') + '---\n')
    (d / 'config.json').write_text(json.dumps({'port': port}))
    (d / '.wizard-source').write_text(json.dumps({'repo': f'oscarhenrycollins/{app_id}'}))
    (d / '.wizard-commit').write_text('b' * 40 + '\n')
    (d / 'docker-compose.yml').write_text(GATED.format(app=app_id) if gated else f'services:\n  {app_id}:\n    image: x\n')
    return {'app': {'id': serial, 'createdAt': 1761955200 + serial, 'status': 'running', 'port': port},
            'info': {'urn': f'{app_id}:wizardapps'}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv('WIZARD_BROWSER_BOUNDARY', '1')
    root, rows = tmp_path / 'runtipi', []
    c = SimpleNamespace(home=tmp_path / 'home', runtipi_root=root, host_ip=lambda: '127.0.0.1',
                        runtipi=lambda: SimpleNamespace(installed=lambda: list(rows), req=lambda *a, **k: []))
    monkeypatch.setattr(cli, 'Config', lambda: c)
    return SimpleNamespace(c=c, root=root, rows=rows, write=lambda: cli.write_registry(c),
                           env=lambda app='transcriber': root / 'user-config' / 'wizardapps' / app / 'app.env',
                           gw=lambda: json.loads(ba.gateway_file(c.home).read_text()),
                           b=lambda reg, app='transcriber': next(a for a in reg['apps'] if a['id'] == app)['browser'])


def env_of(path):
    return dict(l.split('=', 1) for l in path.read_text().splitlines() if '=' in l)


def test_minted_per_installation_wired_to_app_and_gateway_never_in_registry(box):
    box.rows.append(install(box.root))
    reg = box.write()
    b = box.b(reg)
    assert b['gateway_key'] == {'header': 'X-Wizard-Grant', 'ref': f"browser-gateway-keys/{b['installation_id']}.key",
                                'env': 'WIZARD_GATEWAY_KEY', 'state': 'written'}
    key_file = box.c.home / b['gateway_key']['ref']
    key = key_file.read_text().strip()
    assert len(key) == 64 and int(key, 16) >= 0
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert env_of(box.env()) == {'WIZARD_BROWSER_BOUNDARY': '1', 'WIZARD_GATEWAY_KEY': key}
    gw = ba.gateway_file(box.c.home)
    assert box.gw() == {'outgoing': {'transcriber': key}}
    assert stat.S_IMODE(gw.stat().st_mode) == 0o600 and stat.S_IMODE(gw.parent.stat().st_mode) == 0o700
    assert key not in json.dumps(reg) and key not in (box.c.home / 'apps.json').read_text()
    box.write()   # stable across registry rewrites: the running app keeps its key
    assert key_file.read_text().strip() == key and box.gw()['outgoing']['transcriber'] == key


def test_three_box_only_services_each_get_their_own_key(box):
    for i, app in enumerate(('transcriber', 'browser', 'wizard-voice-service')):
        box.rows.append(install(box.root, app, serial=10 + i, port=8791 + i))
    box.write()
    out = box.gw()['outgoing']
    assert sorted(out) == ['browser', 'transcriber', 'wizard-voice-service'] and len(set(out.values())) == 3
    for app in out:
        assert env_of(box.env(app))['WIZARD_GATEWAY_KEY'] == out[app]


def test_reinstall_rotates_and_uninstall_removes(box):
    box.rows.append(install(box.root, serial=7))
    first = box.b(box.write())
    old = (box.c.home / first['gateway_key']['ref']).read_text().strip()
    box.rows.clear()
    box.write()
    assert box.gw() == {'outgoing': {}}
    assert not (box.c.home / first['gateway_key']['ref']).exists()
    box.rows.append(install(box.root, serial=8))   # Runtipi row serials are never reused
    second = box.b(box.write())
    new = box.gw()['outgoing']['transcriber']
    assert second['installation_id'] != first['installation_id'] and new != old
    assert env_of(box.env())['WIZARD_GATEWAY_KEY'] == new


def test_unmanaged_gateway_rows_and_fields_are_kept(box):
    f = ba.gateway_file(box.c.home)
    f.parent.mkdir(parents=True)
    hand = 'a' * 64
    f.write_text(json.dumps({'outgoing': {'wizard-connection-router': hand}, 'note': 'operator'}))
    box.rows.append(install(box.root))
    box.write()
    doc = box.gw()
    assert doc['note'] == 'operator' and doc['outgoing']['wizard-connection-router'] == hand
    assert set(doc['outgoing']) == {'wizard-connection-router', 'transcriber'}
    box.rows.clear()
    box.write()
    assert box.gw()['outgoing'] == {'wizard-connection-router': hand}


def test_malformed_gateway_file_is_never_clobbered(box):
    f = ba.gateway_file(box.c.home)
    f.parent.mkdir(parents=True)
    f.write_text('{not json')
    box.rows.append(install(box.root))
    box.write()
    assert f.read_text() == '{not json'


def test_apps_without_the_variable_or_without_mcp_are_untouched(box):
    box.rows.append(install(box.root, 'files', serial=3, gated=False))
    box.rows.append(install(box.root, 'pages', serial=4, mcp=False))
    reg = box.write()
    assert 'gateway_key' not in box.b(reg, 'files') and 'gateway_key' not in box.b(reg, 'pages')
    assert not box.env('files').exists() and not ba.gateway_file(box.c.home).exists()
    assert not ba.gateway_keys_dir(box.c.home).exists()


def test_default_off_writes_nothing(box, monkeypatch):
    monkeypatch.delenv('WIZARD_BROWSER_BOUNDARY')
    box.rows.append(install(box.root))
    assert 'browser' not in box.write()['apps'][0]
    assert not box.env().exists() and not ba.gateway_file(box.c.home).exists()
    assert not ba.gateway_keys_dir(box.c.home).exists()


def test_bad_installation_ids_refused(box):
    with pytest.raises(ValueError):
        ba.gateway_key_path(box.c.home, '../../etc/passwd')
