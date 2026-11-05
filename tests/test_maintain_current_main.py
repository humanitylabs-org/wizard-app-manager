"""Current-main integration gates; all subprocesses are fake, no host maintenance."""
import fcntl
import json
import os
from pathlib import Path

import pytest
from wam import maintain, os_patch, quick_update, updates


def test_first_maintenance_run_creates_and_holds_peer_locks(tmp_path):
    with maintain._lease(tmp_path):
        for name in ("hermes-update.lock", "app-updates.lock", "install.start.lock", "install.lock"):
            with (tmp_path / name).open("a+") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_quick_update_cannot_start_under_maintenance(tmp_path):
    with maintain._lease(tmp_path):
        fds = quick_update._lock(tmp_path)
        try:
            assert fds is None
        finally:
            for fd in fds or []:
                os.close(fd)


def test_maintenance_cannot_start_under_quick_update(tmp_path):
    fds = quick_update._lock(tmp_path)
    assert fds
    try:
        assert maintain.run(tmp_path, wait=0, is_busy=lambda: [])["state"] == "busy"
    finally:
        for fd in fds:
            os.close(fd)


def test_unactivated_restore_defers_every_step(tmp_path, monkeypatch):
    root = tmp_path / "restores"
    (root / "fixture").mkdir(parents=True)
    (root / "last.json").write_text(json.dumps({"stamp": "fixture"}))
    (root / "fixture" / "journal.json").write_text(json.dumps({"status": "restored"}))
    def forbidden(*a, **kw):
        pytest.fail("restored copy must remain paused")
    for name in ("backup_step", "os_step", "updates_step"):
        monkeypatch.setattr(maintain, name, forbidden)
    rec = maintain.run(tmp_path, wait=0, is_busy=lambda: [])
    assert rec["state"] == "deferred" and "restore" in rec["reason"].lower()


def test_unset_update_choice_remains_unset(tmp_path):
    assert maintain.updates_step(tmp_path, True)["state"] == "ask"
    assert updates._choice(tmp_path) is None
    assert not (tmp_path / "updates.json").exists()
    assert not (tmp_path / "maintenance.json").exists()


@pytest.mark.parametrize("user", ["wizard\nALL ALL=(ALL) NOPASSWD: ALL", "wizard;touch /oops", "-root", "#bad"])
def test_setup_rejects_untrusted_account_before_privilege(user):
    called = []
    result = maintain.setup(lambda *a, **kw: called.append(a), user=user)
    assert result.get("state") == "refused"
    assert not called


def test_writable_or_symlink_helper_is_not_ready(tmp_path, monkeypatch):
    helper = tmp_path / "os-patch"
    helper.write_bytes(Path(os_patch.__file__).read_bytes())
    helper.chmod(0o777)
    monkeypatch.setattr(maintain.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(maintain, "HELPER", helper)
    assert maintain.helper_state() not in ("ready", "stale")
    assert maintain._helper(["apply"]) is None
    helper.unlink()
    helper.symlink_to(os_patch.__file__)
    assert maintain._helper(["apply"]) is None


def test_stale_helper_cannot_execute_privileged_actions(monkeypatch):
    monkeypatch.setattr(maintain, "helper_state", lambda: "stale")
    assert maintain._helper(["apply"]) is None


def test_root_helper_uses_isolated_fixed_interpreter():
    assert Path(os_patch.__file__).read_text().splitlines()[0] == "#!/usr/bin/python3 -I"


def test_helper_rejects_repeated_or_extra_check_arguments(monkeypatch):
    monkeypatch.setattr(os_patch, "check", lambda **kw: pytest.fail("invalid args reached check"))
    assert os_patch.main(["check", "--refresh", "--refresh"]) == 2
    assert os_patch.main(["check", "--refresh", "--anything"]) == 2
