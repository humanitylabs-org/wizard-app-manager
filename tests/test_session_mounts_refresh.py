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


def test_an_agent_service_update_puts_the_timer_in_place(box, tmp_path, monkeypatch):
    """wizard-messenger#352: a box switched to alongside mode before the timer existed never got it (only `wam
    alongside` set it up), so stale -wal/-shm mounts needed a hand-made restart script. The next Agent Service update
    now sets the timer up; the update itself is cut short here, right after the override is refreshed."""
    c, hh, ovr, restarts = box
    units = tmp_path / "_systemd_user"
    for f in units.glob("wizard-session-mounts.*"):
        f.unlink()                                      # the old box: alongside on, no timer

    class RT:
        def app(self, urn): return {"app": {"status": "running"}}
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: RT())

    def stop(*a, **k):
        raise SystemExit("stop here")
    monkeypatch.setattr(cli, "host_access_risks", stop)
    with pytest.raises(SystemExit, match="stop here"):
        cli._app_update(c, "wizard-agent-service")
    assert "alongside --refresh-mounts" in (units / "wizard-session-mounts.service").read_text()
    assert (units / "wizard-session-mounts.timer").is_file()

    # A shell update leaves it alone; with session files no longer shared, the next service update takes it away.
    alongside.enable(c.home, hh, None, True)
    with pytest.raises(SystemExit, match="stop here"):
        cli._app_update(c, "wizard-ux")
    assert (units / "wizard-session-mounts.timer").is_file()
    with pytest.raises(SystemExit, match="stop here"):
        cli._app_update(c, "wizard-agent-service")
    assert not (units / "wizard-session-mounts.timer").exists()


def test_a_flag_first_agent_service_update_puts_the_timer_in_place_too(box, tmp_path, monkeypatch):
    """A flag-first or bulk form can reach runtipi-cli and skip app_update(): it must set the timer up as well
    (Codex P2 on the first version)."""
    c, hh, ovr, restarts = box
    units = tmp_path / "_systemd_user"
    for f in units.glob("wizard-session-mounts.*"):
        f.unlink()
    for name in ("backfill_owner_login", "keep_trusted_hosts"):
        monkeypatch.setattr(cli, name, lambda *a, **k: False)
    from wam import inbox_tokens
    monkeypatch.setattr(inbox_tokens, "before_start", lambda *a, **k: None)
    cli._backfill_owner_logins(c.runtipi_root, ["notifications"])
    assert not (units / "wizard-session-mounts.timer").exists()     # another app: nothing
    cli._backfill_owner_logins(c.runtipi_root, ["wizard-agent-service"])
    assert (units / "wizard-session-mounts.timer").is_file()


# WAM#1115: browser_boundary.write_overlay re-dumps the whole override, which drops the quotes around
# WIZARD_HERMES_STATE_PATH. The doctor row and the remount timer read the parsed YAML, so both keep working.

def _session_row(c):
    return next(r for r in cli.doctor(c) if r[0].startswith("alongside: session database"))


def _boundary_rewrite(ovr, add=True):
    from wam import browser_boundary as bb
    block = {"service": "wizard-agent-service", "environment": {"WIZARD_BROWSER_BOUNDARY": "1"}, "volumes": []}
    assert bb.write_overlay(ovr, block if add else None)


def test_doctor_and_timer_still_see_the_mount_after_the_boundary_block_rewrites_the_override(box):
    c, hh, ovr, restarts = box
    assert _session_row(c)[1] is True
    _boundary_rewrite(ovr)
    assert 'WIZARD_HERMES_STATE_PATH: "/hermes/state.db"' not in ovr.read_text()   # unquoted now
    assert _session_row(c)[1] is True
    assert cli.refresh_session_mounts(c)["state"] == "fresh"
    _boundary_rewrite(ovr, add=False)   # removing the block re-dumps it too
    assert _session_row(c)[1] is True
    _boundary_rewrite(ovr)
    _hermes_restart(hh)    # the inode stamp in the header survives the rewrite: a Hermes restart is still noticed
    assert _session_row(c)[1] is False
    assert cli.refresh_session_mounts(c)["state"] == "refreshed" and restarts == ["wizard-agent-service"]
    assert _session_row(c)[1] is True


def test_doctor_still_fails_when_the_state_db_is_really_not_mounted(box):
    c, hh, ovr, restarts = box
    ovr.write_text("\n".join(l for l in ovr.read_text().splitlines() if ":/hermes/state.db:ro" not in l) + "\n")
    assert _session_row(c)[1] is False and "not in the override yet" in _session_row(c)[2]
    assert cli.refresh_session_mounts(c)["state"] == "skipped"


@pytest.mark.parametrize("text,ok", [
    ('services:\n  a:\n    environment:\n      WIZARD_HERMES_STATE_PATH: "/hermes/state.db"\n'
     '    volumes:\n      - /h/state.db:/hermes/state.db:ro\n', True),
    ("services:\n  a:\n    environment:\n      WIZARD_HERMES_STATE_PATH: /hermes/state.db\n"
     "    volumes:\n      - /h/state.db:/hermes/state.db:ro\n", True),
    ("services:\n  a:\n    environment:\n      - WIZARD_HERMES_STATE_PATH=/hermes/state.db\n"
     "    volumes:\n      - {type: bind, source: /h/state.db, target: /hermes/state.db, read_only: true}\n", True),
    ('services:\n  a:\n    environment:\n      WIZARD_HERMES_STATE_PATH: ""\n'
     "    volumes:\n      - /h/state.db:/hermes/state.db:ro\n", False),
    ("services:\n  a:\n    environment:\n      WIZARD_HERMES_STATE_PATH: /hermes/state.db\n"
     "    volumes:\n      - /h/view:/hermes:ro\n", False),
    ("services:\n  b:\n    environment:\n      WIZARD_HERMES_STATE_PATH: /hermes/state.db\n"
     "    volumes:\n      - /h/state.db:/hermes/state.db:ro\n", False),   # another service
    ("not: [valid", False),
    ("", False),
])
def test_session_db_mounted_reads_the_yaml_not_the_text(text, ok):
    assert alongside.session_db_mounted(text, "a") is ok
