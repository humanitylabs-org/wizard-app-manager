"""Regressions from independent Opus privileged-source review (no root actions)."""
import json
import time
from pathlib import Path
from wam import maintain as m, os_patch as op


def test_rpm_patch_mode_is_report_only():
    assert op.Dnf.can_apply is False


def test_existing_owner_restart_policy_refuses_apt_apply(tmp_path, monkeypatch):
    policy = tmp_path / "policy-rc.d"
    policy.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(op, "POLICY_RC", policy)
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    monkeypatch.setattr(op, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must refuse before dpkg")))
    res = op.apply()
    assert res["state"] == "refused"


def test_reboot_scheduled_never_launches_update(tmp_path, monkeypatch):
    from wam import quiet_restart as qr
    monkeypatch.setattr(m, "backup_step", lambda *a: {"state": "not_set_up"})
    monkeypatch.setattr(m, "os_step", lambda *a, **kw: {"state": "current"})
    monkeypatch.setattr(qr, "reconcile", lambda *a: {"state": "none"})
    monkeypatch.setattr(qr, "run", lambda *a, **kw: {"reboot": {"state": "scheduled"}, "services": {"state": "off"}})
    monkeypatch.setattr(m, "updates_step", lambda *a: (_ for _ in ()).throw(AssertionError("must not launch update before reboot")))
    rec = m.run(tmp_path, wait=0, is_busy=lambda: [])
    assert rec["updates"]["state"] == "deferred"


def test_platform_units_cannot_be_restarted_even_if_needrestart_lists_them(monkeypatch):
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    monkeypatch.setattr(op, "restart_check", lambda: {"state": "current", "services": ["docker.service"]})
    monkeypatch.setattr(op, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not restart platform units")))
    assert op.restart_service("docker.service")["state"] == "report_only"


def test_default_sudoers_excludes_restart_reboot_and_held(tmp_path, monkeypatch):
    import os, pwd, subprocess
    seen = {}
    def runner(argv, **k):
        seen["script"] = argv[-1]
        seen["rule"] = Path(next(x for x in argv[-1].split() if x.endswith("/sudoers"))).read_text()
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(m.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(m, "HELPER", tmp_path / "os-patch")
    monkeypatch.setattr(m, "_install_paths_safe", lambda: True)
    m.setup(runner, user=pwd.getpwuid(os.getuid()).pw_name)
    for word in ("restart-service", "apply-held", "reboot"):
        assert word not in seen["rule"]
    assert "remove" in seen["rule"] and "wizard-os-lane" not in seen["script"]


def test_restart_opt_in_requires_attended_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "lane_ready", lambda: False)
    assert m.set_restart_option("quiet_reboot", "auto", tmp_path).get("error")
    assert m.settings(tmp_path)["quiet_reboot"] == "off"


def test_root_helper_refuses_lane_actions_without_root_owned_lane(monkeypatch, capsys):
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    monkeypatch.setattr(op, "lane_enabled", lambda: False)
    monkeypatch.setattr(op, "schedule_reboot", lambda: (_ for _ in ()).throw(AssertionError("must not reboot")))
    assert op._main(["reboot"]) == 1


def test_direct_helper_calls_share_one_non_blocking_root_lease(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    monkeypatch.setattr(op, "ROOT_LOCK", tmp_path / "root.lock")
    monkeypatch.setattr(op, "_main", lambda argv=None: 0)
    monkeypatch.setattr(op, "_unsafe_lock", lambda s: False)
    with op._root_lease():
        assert op.main(["apply"]) == 1
    assert "busy" in capsys.readouterr().out


def test_owner_removal_survives_installer_rerun(tmp_path, monkeypatch):
    m._save(tmp_path / "maintenance.json", {"helper_removed": True})
    monkeypatch.setattr(m, "_home", lambda home=None: tmp_path)
    res = m.setup(lambda *a, **k: (_ for _ in ()).throw(AssertionError("no privilege")), user="x", installer=True)
    assert res["state"] == "removed_by_owner"


def test_busy_health_is_not_up_to_date(tmp_path):
    m._save(tmp_path / "maintenance/last.json", {"state": "busy", "reason": "another updater"})
    row = m._os_item("check", {"state": "busy", "finished_at": m._now()}, time.time())
    assert row["status"] == "warn" and row["value"] != "up to date"


def test_stale_helper_keeps_mode_and_reports_native_restore_failure(tmp_path, monkeypatch):
    m._save(tmp_path / "maintenance.json", {"os_security": "patch"})
    monkeypatch.setattr(m, "call_helper", lambda *a, **kw: {"state": "needs_root_once", "reason": "stale helper"})
    res = m.set_os_security("check", tmp_path, unit_dir=tmp_path / "units", systemctl=False)
    assert res.get("error")
    assert m.settings(tmp_path)["os_security"] == "patch"


def test_helper_busy_is_deferred_not_up_to_date(monkeypatch):
    import subprocess
    monkeypatch.setattr(m, "helper_state", lambda: "ready")
    done = subprocess.CompletedProcess([], 1, json.dumps({"state": "busy", "reason": "other helper"}), "")
    assert m.call_helper(["check"], lambda *a, **k: done) == {"state": "deferred", "reason": "other helper"}


def test_remove_resets_patch_mode_and_records_owner_choice_even_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_home", lambda home=None: tmp_path)
    monkeypatch.setattr(m, "HELPER", tmp_path / "none")
    monkeypatch.setattr(m, "SUDOERS", tmp_path / "none-sudoers")
    monkeypatch.setattr(m, "LANE", tmp_path / "none-lane")
    m._save(tmp_path / "maintenance.json", {"os_security": "patch"})
    m.remove()
    cfg = m.settings(tmp_path)
    assert cfg["os_security"] == "check" and cfg["helper_removed"] is True


def test_sudo_setup_and_remove_use_invoking_accounts_state(tmp_path, monkeypatch):
    import pwd
    me = pwd.getpwuid(__import__("os").getuid())
    monkeypatch.setattr(m.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", me.pw_name)
    assert m._owner_home() == Path(me.pw_dir) / ".wizard-os"


def test_root_lane_requires_lane_file_even_for_root(monkeypatch):
    monkeypatch.setattr(m.os, "geteuid", lambda: 0)
    monkeypatch.setattr(m, "_protected", lambda path, mode=None: False)
    assert m.lane_ready() is False


def test_unprotected_network_and_remote_access_units_are_report_only(monkeypatch):
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    for unit in ("wg-quick@wg0.service", "cloudflared.service", "zerotier-one.service", "k3s.service", "libvirtd.service", "polkit.service"):
        monkeypatch.setattr(op, "restart_check", lambda unit=unit: {"state": "current", "services": [unit]})
        monkeypatch.setattr(op, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("restart attempted")))
        assert op.restart_service(unit)["state"] == "report_only"


def test_old_needrestart_is_not_run_as_root(monkeypatch):
    class P:  # noqa
        returncode, stdout, stderr = 0, "needrestart 3.6 - Restart daemons after library updates.", ""
    monkeypatch.setattr(op.shutil, "which", lambda name: "/usr/sbin/" + name)
    monkeypatch.setattr(op, "detect", lambda: type("B", (), {"reboot_required": lambda self: False})())
    def fake(argv, **kw):
        if argv[:2] == ["needrestart", "--version"]:
            return P()
        if argv == ["systemctl", "is-system-running"]:
            P2 = type("P2", (), {"returncode": 0, "stdout": "running", "stderr": ""})
            return P2()
        raise AssertionError("old needrestart executed")
    monkeypatch.setattr(op, "run", fake)
    assert op.restart_check()["state"] == "report_only"


def test_native_updater_pause_is_visible_when_not_patching(tmp_path, monkeypatch):
    marker = tmp_path / "52wizard-os-maintenance"
    marker.write_text("x")
    monkeypatch.setattr(m.os_patch, "APT_NATIVE_OFF", marker)
    m._save(tmp_path / "maintenance.json", {"os_security": "check"})
    rows = m.health(tmp_path, {"finished_at": m._now(), "os": {"state": "current"}})["items"]
    assert any(r["key"] == "native-updater" and r["status"] == "warn" for r in rows)


def test_restore_started_during_wait_blocks_os_apply(tmp_path, monkeypatch):
    root = tmp_path / "restores"; (root / "x").mkdir(parents=True)
    state = {"restored": False}
    def probe():
        if not state["restored"]:
            (root / "last.json").write_text(json.dumps({"stamp": "x"}))
            (root / "x" / "journal.json").write_text(json.dumps({"status": "restored"}))
            state["restored"] = True
        return []
    monkeypatch.setattr(m, "call_helper", lambda *a, **k: (_ for _ in ()).throw(AssertionError("apply after restore")))
    assert m.os_step("patch", tmp_path, wait=0, is_busy=probe, sleep=lambda s: None)["state"] == "deferred"


def test_busy_native_off_does_not_save_patch_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "call_helper", lambda *a, **k: {"state": "deferred", "reason": "other helper"})
    assert m.set_os_security("patch", tmp_path, unit_dir=tmp_path / "units", systemctl=False).get("error")
    assert m.settings(tmp_path)["os_security"] == "check"


def test_restart_lane_deferred_snapshot_is_not_current(tmp_path, monkeypatch):
    from wam import quiet_restart as qr
    monkeypatch.setattr(m, "restore_paused", lambda h: False)
    monkeypatch.setattr(m, "wait_quiet", lambda *a, **k: [])
    m._save(tmp_path / "maintenance.json", {"quiet_restarts": "auto"})
    monkeypatch.setattr(m, "call_helper", lambda *a, **k: {"state": "deferred", "reason": "helper busy", "services": []})
    monkeypatch.setattr(qr, "reconcile", lambda *a: {"state": "none"})
    out = qr.run(tmp_path, {"state": "current"}, lambda *a, **k: None, 0, lambda: [], lambda s: None)
    assert out["services"]["state"] == "deferred"


def test_sudo_owner_settings_run_in_dropped_child_and_parent_waits(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(m.os, "geteuid", lambda: 0)
    monkeypatch.setattr(m.pwd, "getpwnam", lambda n: type("A", (), {"pw_uid": 1234, "pw_gid": 2345, "pw_dir": str(tmp_path)})())
    monkeypatch.setattr(m.os, "fork", lambda: 0)
    monkeypatch.setattr(m.os, "initgroups", lambda u, g: calls.append("groups"))
    monkeypatch.setattr(m.os, "setgid", lambda g: calls.append("gid"))
    monkeypatch.setattr(m.os, "setuid", lambda u: calls.append("uid"))
    monkeypatch.setattr(m, "_load", lambda path: calls.append("load") or {})
    monkeypatch.setattr(m, "_save", lambda path, cfg: calls.append("save"))
    monkeypatch.setattr(m.os, "write", lambda fd, data: calls.append("report"))
    monkeypatch.setattr(m.os, "_exit", lambda c: (_ for _ in ()).throw(SystemExit(c)))
    try:
        m._owner_settings(lambda cfg: cfg.update(x=1))
    except SystemExit as e:
        assert e.code == 0
    assert calls == ["groups", "gid", "uid", "load", "save", "report"]


def test_sudo_owner_settings_failure_is_not_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(m.os, "geteuid", lambda: 0)
    monkeypatch.setattr(m.pwd, "getpwnam", lambda n: type("A", (), {"pw_gid": 2345, "pw_dir": str(tmp_path)})())
    monkeypatch.setattr(m.os, "fork", lambda: 4242)
    monkeypatch.setattr(m.os, "waitpid", lambda pid, flags: (pid, 256))
    assert m._owner_settings(lambda cfg: None) is None


def test_deferred_snapshot_never_requests_reboot(tmp_path, monkeypatch):
    from wam import quiet_restart as qr
    m._save(tmp_path / "maintenance.json", {"quiet_reboot": "auto"})
    monkeypatch.setattr(m, "call_helper", lambda *a, **k: {"state": "deferred", "reason": "helper busy"})
    monkeypatch.setattr(qr, "reconcile", lambda *a: {"state": "none"})
    out = qr.run(tmp_path, {"state": "current", "reboot_required": True}, lambda *a, **k: None, 0, lambda: [], lambda s: None)
    assert out["reboot"]["state"] == "deferred"
