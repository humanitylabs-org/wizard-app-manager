"""Per-installation listener credential for apps whose own listener must stop trusting network identity
(wizard-os #386). WAM mints it, hands it to the app only through its own Runtipi user env, keeps the value out of
apps.json, rotates it with the installation and removes it on uninstall."""
import json
import os
import stat
from types import SimpleNamespace

import pytest

from wam import browser_admission as ba
from wam import cli

CONTRACT = """browser_contract:
  version: 1
  isolation: opaque
  operations: [notifications.list, notifications.mark_read]
"""
GATED_COMPOSE = "services:\n  notifications:\n    environment:\n      WIZARD_APP_CREDENTIAL: ${WIZARD_APP_CREDENTIAL:-}\n"


def install(root, app_id='notifications', serial=7, compose=GATED_COMPOSE, port=8810):
    d = root / 'apps' / 'wizardapps' / app_id
    d.mkdir(parents=True, exist_ok=True)
    (d / 'wizard.md').write_text(f'---\nid: {app_id}\nname: {app_id}\ntype: service\nversion: 1.0.0\n'
                                 f'provides: [notifications]\nview: {{path: /{app_id}/, port: 8080}}\n{CONTRACT}---\n')
    (d / 'config.json').write_text(json.dumps({'port': port}))
    (d / '.wizard-source').write_text(json.dumps({'repo': 'oscarhenrycollins/notifications'}))
    (d / '.wizard-commit').write_text('c' * 40 + '\n')
    if compose is not None:
        (d / 'docker-compose.yml').write_text(compose)
    return {'app': {'id': serial, 'createdAt': 1761955200 + serial, 'status': 'running', 'port': port},
            'info': {'urn': f'{app_id}:wizardapps'}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv('WIZARD_BROWSER_BOUNDARY', '1')
    root, rows = tmp_path / 'runtipi', []
    c = SimpleNamespace(home=tmp_path / 'home', runtipi_root=root, host_ip=lambda: '127.0.0.1',
                        runtipi=lambda: SimpleNamespace(installed=lambda: list(rows), req=lambda *a, **k: []))
    monkeypatch.setattr(cli, 'Config', lambda: c)
    env = root / 'user-config' / 'wizardapps' / 'notifications' / 'app.env'
    return SimpleNamespace(c=c, root=root, rows=rows, env=env, write=lambda: cli.write_registry(c),
                           b=lambda reg: next(a for a in reg['apps'] if a['id'] == 'notifications')['browser'])


def env_of(path):
    return dict(l.split('=', 1) for l in path.read_text().splitlines() if '=' in l)


def test_minted_per_installation_and_never_in_registry(box):
    box.rows.append(install(box.root))
    reg = box.write()
    b = box.b(reg)
    cred = b['listener_credential']
    assert cred == {'header': 'X-Wizard-App-Credential', 'ref': f"browser-app-credentials/{b['installation_id']}.key",
                    'env': 'WIZARD_APP_CREDENTIAL', 'state': 'written'}
    key_file = box.c.home / cred['ref']
    key = key_file.read_text().strip()
    assert len(key) == 64 and int(key, 16) >= 0
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600 and stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700
    assert key not in json.dumps(reg) and key not in (box.c.home / 'apps.json').read_text()
    assert env_of(box.env) == {'WIZARD_BROWSER_BOUNDARY': '1', 'WIZARD_APP_CREDENTIAL': key}
    assert stat.S_IMODE(box.env.stat().st_mode) == 0o600
    # Stable across registry rewrites (no rotation churn for the running app).
    box.write()
    assert key_file.read_text().strip() == key


def test_reinstall_rotates_and_uninstall_removes(box):
    box.rows.append(install(box.root, serial=7))
    first = box.b(box.write())
    old_file = box.c.home / first['listener_credential']['ref']
    old = old_file.read_text().strip()
    box.rows.clear()
    box.write()   # uninstalled: no installation, no credential
    assert not old_file.exists()
    box.rows.append(install(box.root, serial=8))   # Runtipi row serials are never reused
    second = box.b(box.write())
    assert second['installation_id'] != first['installation_id']
    new = (box.c.home / second['listener_credential']['ref']).read_text().strip()
    assert new != old and env_of(box.env)['WIZARD_APP_CREDENTIAL'] == new


def test_apps_without_the_gate_are_untouched(box):
    box.rows.append(install(box.root, compose='services:\n  notifications:\n    image: x\n'))
    b = box.b(box.write())
    assert 'listener_credential' not in b
    assert not box.env.exists() and not ba.credentials_dir(box.c.home).exists()


def test_default_off_writes_nothing(box, monkeypatch):
    monkeypatch.delenv('WIZARD_BROWSER_BOUNDARY')
    box.rows.append(install(box.root))
    assert 'browser' not in box.write()['apps'][0]
    assert not box.env.exists() and not ba.credentials_dir(box.c.home).exists()


def test_existing_user_env_kept_and_bad_ids_refused(box):
    box.env.parent.mkdir(parents=True)
    box.env.write_text('NOTIFY_TOPIC=owner-topic\n')
    box.rows.append(install(box.root))
    box.write()
    assert env_of(box.env)['NOTIFY_TOPIC'] == 'owner-topic'
    with pytest.raises(ValueError):
        ba.credential_path(box.c.home, '../../etc/passwd')


def test_unwritable_home_fails_closed_without_a_key(box):
    box.rows.append(install(box.root))
    box.c.home.mkdir(parents=True)
    (box.c.home / 'browser-app-credentials').write_text('not a directory')
    b = box.b(box.write())
    assert b['listener_credential']['state'] == 'unavailable' and not box.env.exists()
    assert os.path.isfile(box.c.home / 'browser-app-credentials')
