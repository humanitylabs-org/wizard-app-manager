import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from wam import maintain as m, os_patch as op, quiet_restart as qr


@pytest.fixture
def rig(tmp_path, monkeypatch):
    calls = []
    snapshot = {"state": "current", "services": ["first.service", "second.service"],
                "reboot_required": True, "boot_id": "before"}
    def helper(action, *args, **kwargs):
        calls.append(action)
        if action == ["restart-check"]:
            return dict(snapshot)
        if action[0] == "restart-service":
            return {"state": "restarted", "service": action[1]}
        return {"state": "scheduled" if action == ["reboot"] else "applied"}
    monkeypatch.setattr(m, "call_helper", helper)
    monkeypatch.setattr(m, "os_step", lambda *a, **kw: {"state": "current", "held": [{"name": "systemd"}]})
    monkeypatch.setattr(m, "backup_step", lambda *a: {"state": "not_set_up"})
    monkeypatch.setattr(m, "updates_step", lambda *a: {"state": "ask"})
    monkeypatch.setattr("wam.updates.installing", lambda *a: False)
    monkeypatch.setattr("wam.updates.boot_id", lambda: "before")
    return tmp_path, calls, snapshot, helper


def config(home, **kwargs):
    m._save(home / "maintenance.json", kwargs)


def run(home, busy=lambda: []):
    return m.run(home, wait=0, is_busy=busy, sleep=lambda s: None)


def test_defaults_report_only_changes_no_external_state(rig):
    home, calls, _, _ = rig
    res = run(home)
    assert calls == [["restart-check"]]
    assert all(m.settings(home)[k] == "off" for k in qr.OPTIONS)
    assert res["restarts"]["services"]["pending"] == ["first.service", "second.service"]
    assert not (home / "maintenance/reboot.json").exists()


@pytest.mark.parametrize("key,value", [("quiet_restarts", "auto"), ("held_platform", "patch"), ("quiet_reboot", "auto")])
def test_settings_are_explicit_and_invalid_values_fail_closed(rig, key, value):
    home, *_ = rig
    assert m.set_restart_option(key, value, home)[key] == value
    config(home, **{key: "yes"})
    assert m.settings(home)[key] == "off"
    assert m.set_restart_option(key, "yes", home).get("error")


def test_active_turn_defers_every_destructive_step_using_real_probe(rig, monkeypatch):
    home, calls, _, _ = rig
    hh = home / "hermes"
    hh.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hh))
    db = sqlite3.connect(hh / "state.db")
    db.execute("create table session_turn_leases(expires_at real)")
    db.execute("insert into session_turn_leases values (?)", (time.time() + 100,))
    db.commit(); db.close()
    config(home, os_security="patch", quiet_restarts="auto", held_platform="patch", quiet_reboot="auto")
    rec = run(home, lambda: m.busy_reasons(home))
    assert rec["state"] == "deferred"
    assert all(a == ["restart-check"] for a in calls)
    assert rec["restarts"]["services"]["results"][0]["state"] == "deferred"


def test_unreadable_busy_probe_does_not_mutate_and_is_reported(rig):
    home, calls, _, _ = rig
    config(home, quiet_restarts="auto", quiet_reboot="auto")
    def broken(): raise OSError("probe offline")
    res = run(home, broken)
    assert res["state"] == "error"
    assert all(a == ["restart-check"] for a in calls)
    assert any(i["status"] == "error" for i in m.health(home)["items"])


def test_rechecks_immediately_before_each_service(rig):
    home, calls, _, _ = rig
    config(home, quiet_restarts="auto")
    # Wait's two samples pass; the boundary sample notices incoming work.
    n = [0]
    def busy():
        n[0] += 1
        return ["turn arrived"] if n[0] >= 4 else []  # update probe consumes one
    rec = run(home, busy)
    assert not any(a[0] == "restart-service" for a in calls)
    assert rec["restarts"]["services"]["state"] == "deferred"


def test_failed_service_does_not_block_later_services_or_held_step(rig, monkeypatch):
    home, calls, _, helper = rig
    def failing(action, *a, **kw):
        if action == ["restart-service", "first.service"]:
            calls.append(action)
            raise subprocess.TimeoutExpired(action, 180)
        return helper(action, *a, **kw)
    monkeypatch.setattr(m, "call_helper", failing)
    config(home, os_security="patch", held_platform="patch", quiet_restarts="auto", quiet_reboot="auto")
    rec = run(home)
    assert ["apply-held"] in calls and ["restart-service", "second.service"] in calls
    assert ["reboot"] not in calls and rec["state"] == "error"
    row = next(i for i in m.health(home)["items"] if i["key"] == "maintenance-services")
    assert row["status"] == "error" and "first.service" in row["detail"]
    assert next(i for i in m.health(home)["items"] if i["key"] == "os-security")["status"] != "error"


def test_failed_backup_and_os_do_not_sink_other_steps(rig, monkeypatch):
    home, calls, _, _ = rig
    config(home, quiet_restarts="auto")
    def broken(*a, **kw): raise OSError("step failed")
    monkeypatch.setattr(m, "backup_step", broken)
    monkeypatch.setattr(m, "os_step", broken)
    rec = run(home)
    assert rec["state"] == "error" and ["restart-service", "second.service"] in calls
    assert rec["updates"]["state"] == "ask"


def test_installer_blocks_restarts(rig, monkeypatch):
    home, calls, _, _ = rig
    config(home, quiet_restarts="auto", quiet_reboot="auto", os_security="patch", held_platform="patch")
    monkeypatch.setattr(m, "updates_step", lambda *a: {"state": "started"})
    rec = run(home)
    assert rec["state"] == "deferred" and all(a == ["restart-check"] for a in calls)


def test_held_requires_security_patch_as_well_as_its_own_opt_in(rig):
    home, calls, _, _ = rig
    config(home, held_platform="patch")
    assert run(home)["restarts"]["held"]["state"] == "report_only"
    assert ["apply-held"] not in calls


def test_reboot_intent_precedes_helper_and_is_reconciled_after_new_boot(rig, monkeypatch):
    home, calls, snap, helper = rig
    config(home, quiet_reboot="auto")
    def reboot(action, *a, **kw):
        if action == ["reboot"]:
            assert m._load(home / "maintenance/reboot.json")["state"] == "intent"
        return helper(action, *a, **kw)
    monkeypatch.setattr(m, "call_helper", reboot)
    assert run(home)["restarts"]["reboot"]["state"] == "scheduled"
    monkeypatch.setattr("wam.updates.boot_id", lambda: "after")
    row = next(i for i in m.health(home)["items"] if i["key"] == "maintenance-reboot")
    assert row["value"] == "rebooted" and row["status"] == "ok"
    assert m._load(home / "maintenance/reboot.json")["new_boot_id"] == "after"
    snap.update(boot_id="after", reboot_required=True)
    calls.clear()
    assert run(home)["restarts"]["reboot"]["state"] == "error"
    assert ["reboot"] not in calls  # do not loop reboots


def test_reboot_not_observed_becomes_failure(rig, monkeypatch):
    home, *_ = rig
    config(home, quiet_reboot="auto")
    run(home)
    monkeypatch.setattr("wam.updates.boot_id", lambda: "before")
    rec = m._load(home / "maintenance/reboot.json")
    rec["requested_epoch"] -= 301
    m._save(home / "maintenance/reboot.json", rec)
    row = next(i for i in m.health(home)["items"] if i["key"] == "maintenance-reboot")
    assert row["status"] == "error" and row["value"] == "reboot failed"


@pytest.mark.parametrize("name", ["--bad.service", "x;reboot.service", "../../x.service", "a.service --no-block", "x\ny.service", "*.service"])
def test_root_helper_rejects_untrusted_service_names(name, monkeypatch):
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    monkeypatch.setattr(op, "restart_check", lambda: 1 / 0)
    assert op.restart_service(name)["state"] == "refused"


def test_root_helper_will_not_restart_unlisted_service(monkeypatch):
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    monkeypatch.setattr(op, "restart_check", lambda: {"state": "current", "services": []})
    monkeypatch.setattr(op, "run", lambda *a, **k: 1 / 0)
    assert op.restart_service("unlisted.service")["state"] == "current"


def test_service_failure_attempts_recovery_but_remains_an_error(monkeypatch):
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    monkeypatch.setattr(op, "restart_check", lambda: {"state": "current", "services": ["bad.service"]})
    calls = []
    def runner(argv, **kw):
        calls.append(argv)
        rc = 1 if "try-restart" in argv or ("is-active" in argv and len(calls) == 3) else 0
        return subprocess.CompletedProcess(argv, rc, "", "failed" if rc else "")
    monkeypatch.setattr(op, "run", runner)
    res = op.restart_service("bad.service")
    assert res["state"] == "error" and res["recovered"] is True
    assert ["systemctl", "start", "bad.service"] in calls


def test_held_transaction_is_pinned_no_remove_and_guarded(tmp_path, monkeypatch):
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    monkeypatch.setattr(op, "POLICY_RC", tmp_path / "policy")
    docs = iter([{"state": "pending", "held": [{"name": "systemd", "to": "1.2"}]},
                 {"state": "current", "held": []}])
    monkeypatch.setattr(op, "check", lambda **kw: next(docs))
    monkeypatch.setattr(op, "detect", lambda: op.Apt({}))
    def runner(argv, **kw):
        assert "systemd=1.2" in argv and "--no-remove" in argv
        assert op.POLICY_RC.exists() and kw["env"]["NEEDRESTART_SUSPEND"] == "1"
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(op, "run", runner)
    assert op.apply_held()["installed"] == ["systemd"]
    assert not op.POLICY_RC.exists()


def test_held_non_apt_is_explicitly_report_only(monkeypatch):
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    monkeypatch.setattr(op, "check", lambda **k: {"state": "pending", "held": [{"name": "systemd"}]})
    monkeypatch.setattr(op, "detect", lambda: op.Dnf({}))
    assert op.apply_held()["state"] == "report_only"


def test_needrestart_parser_dedupes_and_is_list_only(monkeypatch):
    monkeypatch.setattr(op, "detect", lambda: type("Backend", (), {"reboot_required": lambda s: False})())
    monkeypatch.setattr(op.shutil, "which", lambda n: "/usr/bin/" + n)
    def runner(argv, **kw):
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 0, "running\n", "")
        assert argv == ["needrestart", "-b", "-r", "l"]
        return subprocess.CompletedProcess(argv, 0, "NEEDRESTART-SVC: foo.service\nNEEDRESTART-SVC: foo.service\n", "")
    monkeypatch.setattr(op, "run", runner)
    assert op.restart_check()["services"] == ["foo.service"]


def test_needrestart_invalid_name_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(op, "detect", lambda: type("Backend", (), {"reboot_required": lambda s: False})())
    monkeypatch.setattr(op.shutil, "which", lambda n: "/usr/bin/" + n)
    out = "NEEDRESTART-SVC: foo.service\nNEEDRESTART-SVC: ../evil\nNEEDRESTART-SVC: user@1000.service\n"
    monkeypatch.setattr(op, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "running\n" if argv[0] == "systemctl" else out, ""))
    doc = op.restart_check()
    assert doc["state"] == "current"
    assert doc["services"] == ["foo.service", "user@1000.service"]
    assert doc["skipped"] == ["'../evil'"]
    monkeypatch.setattr(op.os, "geteuid", lambda: 0)
    assert op.restart_service("../evil")["state"] == "refused"


def test_needrestart_error_never_invents_an_empty_healthy_list(monkeypatch):
    monkeypatch.setattr(op, "detect", lambda: type("Backend", (), {"reboot_required": lambda s: False})())
    monkeypatch.setattr(op.shutil, "which", lambda n: n)
    monkeypatch.setattr(op, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "unreadable"))
    assert op.restart_check()["state"] == "error"


def test_cli_rejects_ambiguous_settings_and_negative_wait():
    with pytest.raises(SystemExit): m.main(["--quiet-restarts", "auto", "--quiet-reboot", "auto"])
    with pytest.raises(SystemExit): m.main(["run", "--wait-quiet", "-1"])
