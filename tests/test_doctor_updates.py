"""Doctor must not call an interrupted update healthy (wizard-os#90)."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import builder_bundle, cli, updates


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / 'wizard-os'
    home.mkdir()
    token = home / 'api-token'
    token.write_text('test-only')
    token.chmod(0o600)
    (home / 'apps.json').write_text('{"apps": []}')
    rt = SimpleNamespace(base='http://127.0.0.1:1', installed=lambda: [],
                         stores=lambda: [], req=lambda *a: {'data': [{}]})
    c = SimpleNamespace(home=home, runtipi_root=tmp_path / 'runtipi', index='test-index',
                        bind='127.0.0.1', port=1, token_file=token, runtipi=lambda: rt,
                        api_token=lambda: 'test-only')
    monkeypatch.setattr(builder_bundle, 'health', lambda *a: [])
    monkeypatch.setattr(builder_bundle, 'runtime_health', lambda *a: [])
    monkeypatch.setattr(builder_bundle, 'install', lambda *a: None)
    monkeypatch.setattr(builder_bundle, 'fresh', lambda *a: None)
    monkeypatch.setattr(cli, 'store_ok', lambda *a: True)
    monkeypatch.setattr(cli.index, 'search', lambda *a: [{}])
    monkeypatch.setattr(updates, 'boot_id', lambda *a: 'this-boot')
    monkeypatch.setattr(updates, '_booted_at', lambda *a: 1792512809)
    monkeypatch.setattr(cli, 'Config', lambda: c)
    return c


def state(box, **kw):
    p = subprocess.Popen(['true'])
    p.wait()
    st = {'state': 'running', 'pid': p.pid, 'boot_id': 'this-boot',
          'started_at': '2026-10-20T16:13:30Z', **kw}
    (box.home / 'install-status.json').write_text(json.dumps(st))
    (box.home / 'install.lock').touch()
    return st


@pytest.mark.parametrize('kind', ['dead-pid', 'changed-boot', 'old-status', 'already-interrupted'])
def test_doctor_cli_fails_on_an_interrupted_update(box, monkeypatch, capsys, kind):
    kw = {
        'dead-pid': {},
        'changed-boot': {'boot_id': 'old-boot', 'pid': os.getpid()},
        'old-status': {'boot_id': None, 'pid': os.getpid(), 'started_at': '2026-10-20T16:13:16Z'},
        'already-interrupted': {'state': 'interrupted'},
    }[kind]
    state(box, **kw)
    monkeypatch.setattr(sys, 'argv', ['wam', 'doctor'])
    assert cli.main() == 1
    text = capsys.readouterr().out
    assert 'FAIL  Wizard OS update completed' in text
    assert 'interrupted' in text and 'https://aiwizards.com/os.sh' in text
    assert json.loads((box.home / 'install-status.json').read_text())['state'] == 'interrupted'
    if kind != 'already-interrupted':
        assert not (box.home / 'install.lock').exists()


@pytest.mark.parametrize('lock_name', ['install.lock', 'install.start.lock'])
def test_doctor_respects_a_held_runner_or_start_lock(box, lock_name):
    before = state(box, boot_id='old-boot')
    with (box.home / lock_name).open('a') as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        checks = cli.doctor(box)
        assert all(ok for _, ok, _ in checks)
        assert json.loads((box.home / 'install-status.json').read_text()) == before
        assert (box.home / 'install.lock').exists()


@pytest.mark.parametrize('kind', ['no-status', 'done', 'live-runner', 'unknown-pid'])
def test_doctor_does_not_invent_an_interruption(box, kind):
    if kind != 'no-status':
        state(box, **{'done': {'state': 'done'}, 'live-runner': {'pid': os.getpid()},
                      'unknown-pid': {'pid': None}}[kind])
    assert all(ok for _, ok, _ in cli.doctor(box))


def test_doctor_fix_does_not_hide_the_unfinished_update(box):
    state(box)
    checks = cli.doctor(box, fix=True)
    assert any(n == 'Wizard OS update completed' and not ok for n, ok, _ in checks)
    state(box, state='done')
    assert all(ok for _, ok, _ in cli.doctor(box))
