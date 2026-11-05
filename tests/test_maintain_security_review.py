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
