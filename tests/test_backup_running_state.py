"""Restore/Undo finish only after previously live apps finish asynchronous start."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_backup_flow import _cli, _export, isolated, world  # noqa: E402,F401


def delayed_starts(machine, monkeypatch):
    """Runtipi acknowledges start before its health checks mark it running."""
    pending = {}
    start, app = machine.rt.start, machine.rt.app

    def async_start(urn):
        result = start(urn)
        machine.rt.apps[urn]['status'] = 'starting'
        pending[urn] = 3
        return result

    def poll(urn):
        if urn in pending:
            pending[urn] -= 1
            if pending[urn] == 0:
                machine.rt.apps[urn]['status'] = 'running'
                del pending[urn]
        return app(urn)

    monkeypatch.setattr(machine.rt, 'start', async_start)
    monkeypatch.setattr(machine.rt, 'app', poll)
    return pending


@pytest.mark.parametrize('stop_notes', [False, True])
def test_restore_and_undo_leave_running_apps_running(world, monkeypatch, stop_notes):
    out, _ = _export(world)
    machine = world['old']
    # The snapshot had Files stopped. This box's current choice wins on restore,
    # while apps installed for the first time still use the snapshot's choice.
    machine.rt.start('files-app:wizardapps')
    if stop_notes:
        machine.rt.stop('notes-app:wizardapps')
    before = {urn: row['status'] for urn, row in machine.rt.apps.items()}
    pending = delayed_starts(machine, monkeypatch)
    regenerated = []
    box = machine.box(after_restore=lambda b: regenerated.append(
        {urn: row['status'] for urn, row in machine.rt.apps.items()}))
    code, got = _cli(box, 'import', str(out), '--yes')
    assert code == 0 and got['ok'], got
    assert not pending, 'restore returned while an app was still starting'
    assert {urn: row['status'] for urn, row in machine.rt.apps.items()} == before
    assert regenerated[-1] == before, 'registry must be regenerated after apps become healthy'

    code, got = _cli(box, 'undo')
    assert code == 0 and got['ok'] and got['digest_match'], got
    assert not pending, 'undo returned while an app was still starting'
    assert {urn: row['status'] for urn, row in machine.rt.apps.items()} == before
    assert regenerated[-1] == before


@pytest.mark.parametrize('phase', ['restore', 'undo'])
def test_restore_and_undo_do_not_report_success_when_running_wait_expires(world, monkeypatch, phase):
    out, _ = _export(world)
    machine = world['old']
    box = machine.box()
    if phase == 'undo':
        code, got = _cli(box, 'import', str(out), '--yes')
        assert code == 0, got
    pending = delayed_starts(machine, monkeypatch)
    wait = box.wait_status

    def expired(urn, want, timeout=900, **kwargs):
        if want == {'running'}:
            assert 0 < timeout <= 600
            return 'starting'
        return wait(urn, want, timeout, **kwargs)

    monkeypatch.setattr(box, 'wait_status', expired)
    argv = ('import', str(out), '--yes') if phase == 'restore' else ('undo',)
    code, got = _cli(box, *argv)
    assert code != 0 and not got.get('ok'), got
    assert pending
    assert 'running' in got['error'] and 'starting' in got['error']
