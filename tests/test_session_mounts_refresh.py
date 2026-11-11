"""wizard-app-manager#577: --share-sessions mounts state.db/-wal/-shm as single files, which pin their inode. After a
Hermes restart SQLite replaces -wal/-shm, and the Agent Service kept reading a frozen snapshot (archive did nothing,
new threads missing) until someone restarted it. `wam alongside --refresh-mounts` (run every minute by a timer) now
remounts them, but only once all three files exist again."""
import json
import os

import pytest

from wam import alongside, cli, session_mounts


@pytest.fixture
def box(tmp_path, monkeypatch):
    hh = tmp_path / "hermes"; hh.mkdir()
    (hh / "config.yaml").write_text("{}\n")
    for n in alongside.SESSION_STATE_FILES:
        (hh / n).write_bytes(b"x")
    wh = tmp_path / "wos"; wh.mkdir()
    rt = tmp_path / "runtipi"; rt.mkdir()
    monkeypatch.setenv("WIZARD_OS_HOME", str(wh)); monkeypatch.setenv("RUNTIPI_ROOT", str(rt))
    monkeypatch.setattr(alongside, "hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(alongside, "api_up", lambda origin, timeout=3.0: False)
    monkeypatch.setattr(alongside, "docker_rootless", lambda: True)
    monkeypatch.setattr(alongside, "transcriber_origin", lambda root, store="wizardapps": None)
    alongside.enable(wh, hh, 18796, None, True)
    alongside.set_service_active(wh, True)
    monkeypatch.setattr(cli, "_alongside_apps", lambda c, rt=None: {"wizard-agent-service": "service", "wizard-ux": "shell"})
    alongside.write_shell_override(rt, "wizardapps", "wizard-agent-service", wh, "service")
    restarts = []
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: object())
    monkeypatch.setattr(cli, "recreate_apps", lambda c, r, ids: restarts.extend(ids) or {i: "recreated" for i in ids})
    ovr = rt / "user-config" / "wizardapps" / "wizard-agent-service" / "docker-compose.yml"
    return cli.Config(), hh, ovr, restarts


_HELD = []   # the container's bind mount keeps the old inodes alive, so the new files can't reuse their numbers


def _hermes_restart(hh, recreate=True):
    """What SQLite does on a Hermes restart: delete -wal/-shm on close, create new files (new inodes) on reopen."""
    for n in ("state.db-wal", "state.db-shm"):
        _HELD.append(open(hh / n, "rb"))
        (hh / n).unlink()
        if recreate:
            (hh / n).write_bytes(b"new")


def test_fresh_mounts_change_nothing(box):
    c, hh, ovr, restarts = box
    before = ovr.read_text()
    assert cli.refresh_session_mounts(c)["state"] == "fresh"
    assert restarts == [] and ovr.read_text() == before


def test_hermes_restart_remounts_and_restarts_the_agent_service(box):
    c, hh, ovr, restarts = box
    _hermes_restart(hh)
    out = cli.refresh_session_mounts(c)
    assert out["state"] == "refreshed" and out["stale"] == ["state.db-shm", "state.db-wal"]
    assert restarts == ["wizard-agent-service"]
    assert alongside.stale_session_mounts(ovr.read_text(), str(hh)) == []    # new inodes recorded
    assert cli.refresh_session_mounts(c)["state"] == "fresh" and restarts == ["wizard-agent-service"]   # once


def test_a_failed_restart_is_retried(box, monkeypatch):
    """The App Manager was down: the container still holds the old files, so the next run must try again."""
    c, hh, ovr, restarts = box
    before = ovr.read_text()
    _hermes_restart(hh)
    monkeypatch.setattr(cli, "recreate_apps", lambda c, r, ids: {i: "error: App Manager not reachable" for i in ids})
    assert cli.refresh_session_mounts(c)["state"] == "error"
    assert ovr.read_text() == before and session_mounts.plan(before, str(hh))["state"] == "stale"
    assert cli.main(["alongside", "--refresh-mounts"]) == 1


def test_waits_while_hermes_has_its_wal_files_removed(box):
    """Between close and reopen -wal/-shm are missing: a restart then would make Docker create a directory in the
    Hermes home (or fail), so nothing happens until they are back."""
    c, hh, ovr, restarts = box
    before = ovr.read_text()
    _hermes_restart(hh, recreate=False)
    assert cli.refresh_session_mounts(c)["state"] == "waiting"
    assert restarts == [] and ovr.read_text() == before
    assert not any((hh / n).exists() for n in ("state.db-wal", "state.db-shm"))   # no directory made
    for n in ("state.db-wal", "state.db-shm"):
        (hh / n).write_bytes(b"back")
    assert cli.refresh_session_mounts(c)["state"] == "refreshed" and restarts == ["wizard-agent-service"]


def test_skips_when_sessions_are_not_shared(box):
    c, hh, ovr, restarts = box
    alongside.enable(c.home, hh, None, True)           # whole home shared: the directory mount follows new files
    _hermes_restart(hh)
    assert cli.refresh_session_mounts(c)["state"] == "skipped" and restarts == []


def test_timer_follows_the_share_sessions_choice(tmp_path):
    units = tmp_path / "units"
    assert session_mounts.ensure_timer(tmp_path, tmp_path / "rt", True, units) == "timer:wizard-session-mounts.timer"
    svc = (units / "wizard-session-mounts.service").read_text()
    assert "alongside --refresh-mounts" in svc and f"RUNTIPI_ROOT={tmp_path / 'rt'}" in svc
    assert "OnUnitActiveSec=1min" in (units / "wizard-session-mounts.timer").read_text()
    assert session_mounts.ensure_timer(tmp_path, tmp_path / "rt", False, units) == "removed"
    assert not os.listdir(units)
    assert session_mounts.ensure_timer(tmp_path, tmp_path / "rt", False, units) == "off"


def test_cli_entry_point(box, capsys):
    c, hh, ovr, restarts = box
    _hermes_restart(hh)
    assert cli.main(["alongside", "--refresh-mounts"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "refreshed"
