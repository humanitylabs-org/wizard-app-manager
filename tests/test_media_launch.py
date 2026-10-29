import subprocess
from types import SimpleNamespace
import pytest
from wam import alongside, cli


def test_service_keeps_files_group_without_host_capabilities(tmp_path):
    st = {'agent': {'uid': 0, 'gid': 0, 'hermes_home': str(tmp_path / 'hermes'), 'origin': 'http://127.0.0.1:8642'},
          'shell': {'port': 8796}, 'service': {'port': 8797}}
    for rootless in (True, False):
        text = alongside.shell_override(st, 'wizard-agent-service', tmp_path / 'wizard', 'service', rootless=rootless)
        assert 'group_add:\n      - "1000"' in text
        assert 'cap_add' not in text
        assert 'WIZARD_SETUP_MODE: alongside' in text
    assert 'group_add' not in alongside.ux_override(st, 'wizard-ux')


def test_adoption_prepares_new_install_at_actual_media_root(tmp_path, monkeypatch):
    rt = tmp_path / 'rt'; rt.mkdir()
    actual = tmp_path / 'host'
    (rt / '.env').write_text(f'ROOT_FOLDER_HOST={actual}\n')
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, '', ''))
    assert alongside.adopt_media(rt, 'image:pin', '1001:1001') is True
    cmd = calls[-1]
    assert f'{actual}/media:/media' in cmd
    assert 'chgrp 1000 /media && chmod g+rwx /media' in cmd[-1]
    assert 'chmod -R 777' not in cmd[-1]
    assert 'chown -R 1001:1001 /media/Inbox /media/.wizard-uploads' in cmd[-1]
    assert alongside.adopt_media(rt, 'image:pin', '1001:1001') is True
    assert len(calls) == 2


@pytest.mark.parametrize('role', ['service', 'shell'])
def test_initial_install_prepares_upload_storage_before_container_start(tmp_path, monkeypatch, role):
    events = []
    st = {'agent': {'uid': 1001, 'gid': 1001}, 'shell': {'port': 8796}, 'service': {'port': 8797}, 'images': {}}
    app = 'wizard-agent-service' if role == 'service' else 'wizard-ux'
    class Queued(Exception): pass
    def queue(*args):
        events.append('install')
        raise Queued()
    rt = SimpleNamespace(app=lambda urn: {'info': {'port': 8797}}, install=queue)
    c = SimpleNamespace(home=tmp_path, runtipi_root=tmp_path / 'rt', runtipi=lambda: rt)
    monkeypatch.setattr(cli.release_pins, 'load', lambda: {})
    monkeypatch.setattr(cli, 'ensure_store', lambda *a: None)
    monkeypatch.setattr(cli, 'installed_ids', lambda *a: set())
    monkeypatch.setattr(cli.slots, 'load_defaults', lambda: {})
    monkeypatch.setattr(alongside, 'external_slots', lambda *a: set())
    monkeypatch.setattr(alongside, 'load', lambda *a: st)
    monkeypatch.setattr(alongside, 'docker_rootless', lambda: False)
    monkeypatch.setattr(cli, 'build_form', lambda *a, **kw: ({}, []))
    monkeypatch.setattr(cli, 'alongside_role', lambda *a: role)
    def override(*args):
        events.append('override'); st['images'][app] = 'image:new-pin'
    monkeypatch.setattr(alongside, 'write_shell_override', override)
    def adopt(root, image, user):
        assert image == 'image:new-pin' and user == '1001:1001'
        events.append('adopt')
    monkeypatch.setattr(alongside, 'adopt_media', adopt)
    monkeypatch.setattr(cli, 'trusted_origin_env', lambda *a: '')
    monkeypatch.setattr(cli, 'claude_login_cmd', lambda *a: {'apps': {}})
    with pytest.raises(Queued):
        cli.install(c, app, _record={'id': app, 'uses': [], 'provides': []})
    assert events == (['override', 'adopt', 'install'] if role == 'service' else ['override', 'install'])
