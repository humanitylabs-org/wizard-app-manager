import json
import subprocess
import time
from pathlib import Path

import pytest
from wam import maintain, os_patch


@pytest.fixture(autouse=True)
def no_host_restart_state(monkeypatch):
    original = maintain.call_helper
    def helper(action, *args, **kwargs):
        if action == ["restart-check"]:
            return {"state": "current", "services": [], "boot_id": "test-boot", "reboot_required": False}
        return original(action, *args, **kwargs)
    monkeypatch.setattr(maintain, "call_helper", helper)


def _row(home, key, last=None, now=None):
    return next(i for i in maintain.health(home, last, now)["items"] if i["key"] == key)


@pytest.mark.parametrize("rel,family", [
    ({"ID": "debian"}, "apt"), ({"ID": "ubuntu", "ID_LIKE": "debian"}, "apt"), ({"ID": "pop", "ID_LIKE": "ubuntu debian"}, "apt"),
    ({"ID": "fedora"}, "dnf"), ({"ID": "rocky", "ID_LIKE": "rhel centos fedora"}, "dnf"), ({"ID": "amzn"}, "dnf"),
    ({"ID": "opensuse-leap", "ID_LIKE": "suse opensuse"}, "zypper"), ({"ID": "arch"}, "pacman"),
    ({"ID": "alpine"}, "apk"), ({"ID": "something-new", "ID_LIKE": "debian"}, "apt"),
])
def test_detects_family_from_os_release(rel, family):
    assert os_patch.detect(rel, require_tool=False).family == family


def test_os_release_parsing(tmp_path):
    f = tmp_path / "os-release"
    f.write_text('PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nID_LIKE=\'x y\'\n# c\n')
    assert os_patch.os_release(f) == {"PRETTY_NAME": "Debian GNU/Linux 12 (bookworm)", "ID": "debian", "ID_LIKE": "x y"}


def test_platform_packages_are_held():
    for n in ("docker-ce", "containerd.io", "tailscale", "systemd", "runc", "dbus"):
        assert os_patch.held(n), n
    for n in ("openssl", "libssl3", "zlib1g", "libc6", "openssh-server", "systemd-container-foo"):
        assert not os_patch.held(n) or n.startswith("systemd-container"), n


APT_SIM = """Reading package lists...
Inst libssl3 [3.0.11-1~deb12u1] (3.0.13-1~deb12u1 Debian-Security:12/stable-security [amd64])
Inst tzdata [2024a-0+deb12u1] (2025b-0+deb12u1 Debian:12.11/stable [all])
Inst openssl [3.0.2-0ubuntu1.10] (3.0.2-0ubuntu1.12 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Inst containerd.io [1.7.1] (1.7.2 Docker CE:jammy-security [amd64])
Inst linux-image-6.8.0-50 (6.8.0-50.51 Ubuntu:24.04/noble-security [amd64])
Conf libssl3 (3.0.13-1~deb12u1 Debian-Security:12/stable-security [amd64])
"""


def test_apt_pending_parses_only_security_origins(monkeypatch):
    monkeypatch.setattr(os_patch, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, APT_SIM, ""))
    got = os_patch.Apt({}).pending(refresh=False)
    assert [p["name"] for p in got] == ["libssl3", "openssl", "containerd.io", "linux-image-6.8.0-50"]
    assert got[0] == {"name": "libssl3", "from": "3.0.11-1~deb12u1", "to": "3.0.13-1~deb12u1"}
    assert got[3]["from"] is None


DNF4 = """FEDORA-2026-1a  Important/Sec.  openssl-libs-1:3.2.4-1.fc40.x86_64
FEDORA-2026-1a  Important/Sec.  openssl-1:3.2.4-1.fc40.x86_64
RLSA-2026:1     Moderate/Sec.   curl-7.76.1-31.el9_6.x86_64
RLSA-2026:2     Moderate/Sec.   curl-7.76.1-31.el9_6.x86_64
"""


def test_dnf_pending_parses_nevra_and_dedupes(monkeypatch):
    monkeypatch.setattr(os_patch.shutil, "which", lambda t: "/usr/bin/dnf" if t == "dnf" else None)
    monkeypatch.setattr(os_patch, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, DNF4, ""))
    got = os_patch.Dnf({}).pending(refresh=False)
    assert [p["name"] for p in got] == ["openssl-libs", "openssl", "curl"]
    assert got[0]["to"] == "3.2.4-1.fc40"


def test_apk_pending(monkeypatch):
    out = "Installed:                                Available:\nlibcrypto3-3.3.2-r0          < 3.3.3-r0\nmusl-1.2.5-r0 < 1.2.5-r1\n"
    monkeypatch.setattr(os_patch, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, out, ""))
    assert [p["name"] for p in os_patch.Apk({}).pending(False)] == ["libcrypto3", "musl"]


def test_policy_guard_is_temporary_and_never_overwrites(tmp_path, monkeypatch):
    rc = tmp_path / "policy-rc.d"
    monkeypatch.setattr(os_patch, "POLICY_RC", rc)
    with os_patch._no_service_restarts():
        assert rc.read_text().strip().endswith("exit 101")
    assert not rc.exists()
    rc.write_text("#!/bin/sh\nexit 0\n")           # the owner's own policy
    with os_patch._no_service_restarts():
        assert rc.read_text() == "#!/bin/sh\nexit 0\n"
    assert rc.exists()
    rc.write_text(f"#!/bin/sh\n{os_patch.POLICY_MARK}\nexit 101\n")   # left by a killed run
    assert os_patch.leftover_guard() and not rc.exists()


def test_dnf_apply_installs_unheld_and_excludes_held(monkeypatch):
    monkeypatch.setattr(os_patch.os, "geteuid", lambda: 0)
    seq = iter([{"state": "pending", "can_apply": True, "security": [{"name": "curl"}], "held": [{"name": "docker-ce"}]},
                {"state": "pending", "can_apply": True, "security": [], "held": [{"name": "docker-ce"}]}])
    monkeypatch.setattr(os_patch, "check", lambda **k: next(seq))
    monkeypatch.setattr(os_patch, "leftover_guard", lambda: False)
    monkeypatch.setattr(os_patch, "stale_processes", lambda: [])
    monkeypatch.setattr(os_patch, "detect", lambda rel=None: os_patch.Dnf({}))
    argvs = []
    monkeypatch.setattr(os_patch, "run", lambda argv, **k: argvs.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    res = os_patch.apply()
    assert res["state"] == "applied" and res["installed"] == ["curl"]
    assert "--exclude=docker*" in argvs[0] and argvs[0][-1] == "curl"


def test_apply_installs_only_unheld_with_restarts_suppressed(monkeypatch):
    monkeypatch.setattr(os_patch.os, "geteuid", lambda: 0)
    seq = iter([{"state": "pending", "can_apply": True, "security": [{"name": "openssl"}, {"name": "zlib1g"}],
                 "held": [{"name": "containerd.io"}]},
                {"state": "pending", "can_apply": True, "security": [], "held": [{"name": "containerd.io"}]}])
    monkeypatch.setattr(os_patch, "check", lambda **k: next(seq))
    monkeypatch.setattr(os_patch, "leftover_guard", lambda: False)
    monkeypatch.setattr(os_patch, "stale_processes", lambda: ["sshd"])
    monkeypatch.setattr(os_patch, "detect", lambda rel=None: os_patch.Apt({}))
    got = []
    monkeypatch.setattr(os_patch.Apt, "apply", lambda self, n: got.append(n) or subprocess.CompletedProcess([], 0, "", ""))
    res = os_patch.apply()
    assert got == [["openssl", "zlib1g"]]
    assert res["state"] == "applied" and res["installed"] == ["openssl", "zlib1g"] and res["restarts"] == "suppressed"
    assert res["needs_restart"] == ["sshd"] and res["held"] == [{"name": "containerd.io"}]


def test_rolling_and_unsupported_are_report_only(monkeypatch):
    monkeypatch.setattr(os_patch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(os_patch, "leftover_guard", lambda: False)
    monkeypatch.setattr(os_patch, "check", lambda **k: {"state": "pending", "can_apply": False, "security": [{"name": "x"}], "held": []})
    monkeypatch.setattr(os_patch, "detect", lambda rel=None: os_patch.Pacman({}))
    assert os_patch.apply()["state"] == "report_only"
    assert os_patch.check(rel={"ID": "plan9"})["state"] in ("unsupported", "pending", "current", "error")


def test_apply_refuses_without_root(monkeypatch):
    monkeypatch.setattr(os_patch.os, "geteuid", lambda: 1000)
    assert os_patch.apply()["state"] == "refused"


# ---- maintain ----------------------------------------------------------------------------------------------------

def test_default_is_check_and_installer_never_writes_settings(tmp_path):
    assert maintain.settings(tmp_path)["os_security"] == "check"
    maintain.ensure_timer(tmp_path, tmp_path / "units", systemctl=False)
    assert not (tmp_path / "maintenance.json").exists()
    timer = (tmp_path / "units" / "wizard-os-maintain.timer").read_text()
    assert "OnCalendar=*-*-* 02:00" in timer and "Persistent=true" in timer


def test_off_skips_os_but_keeps_the_nightly_timer(tmp_path, monkeypatch):
    monkeypatch.setattr(maintain, "call_helper", lambda a, r=None: {"changed": False})
    maintain.set_os_security("off", tmp_path, unit_dir=tmp_path / "u", systemctl=False)
    assert (tmp_path / "u" / "wizard-os-maintain.timer").exists()
    assert json.loads((tmp_path / "maintenance.json").read_text())["os_security"] == "off"
    rec = maintain.run(tmp_path, wait=0, is_busy=lambda: [], with_updates=False)
    assert rec["os"] == {"state": "off"} and not [i for i in maintain.health(tmp_path)["items"] if i["key"] == "os-security"]


def test_maintenance_timer_retires_the_update_timer_and_updates_defers(tmp_path, monkeypatch):
    from wam import updates
    ud = tmp_path / "u"
    monkeypatch.setattr(updates, "mode", lambda home=None: "auto")
    updates.ensure_timer(tmp_path, ud, systemctl=False)
    assert (ud / "wizard-os-update.timer").exists()
    assert maintain.ensure_timer(tmp_path, ud, systemctl=False)["retired_update_timer"]
    assert not (ud / "wizard-os-update.timer").exists() and (ud / "wizard-os-maintain.timer").exists()
    assert updates.ensure_timer(tmp_path, ud, systemctl=False)["nightly"] == "wizard-os-maintain"
    assert not (ud / "wizard-os-update.timer").exists()


def test_patch_needs_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(maintain, "call_helper", lambda a, r=None: {"state": "refused", "reason": "x"})
    assert "error" in maintain.set_os_security("patch", tmp_path, systemctl=False)
    assert maintain.settings(tmp_path)["os_security"] == "check"


def test_patch_mode_hands_native_updater_over(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(maintain, "call_helper", lambda a, r=None: calls.append(a) or {"changed": True})
    maintain.set_os_security("patch", tmp_path, unit_dir=tmp_path / "u", systemctl=False)
    maintain.set_os_security("check", tmp_path, unit_dir=tmp_path / "u", systemctl=False)
    assert calls == [["native-off"], ["native-on"]]


def test_wait_quiet_needs_two_quiet_probes_and_gives_up_at_deadline():
    t = [0.0]
    clock = lambda: t[0]
    def sleep(s): t[0] += s
    seq = iter([["busy"], [], ["busy"], [], []])
    assert maintain.wait_quiet(Path("/x"), 600, lambda: next(seq), sleep, clock) == []
    t[0] = 0.0
    assert maintain.wait_quiet(Path("/x"), 120, lambda: ["Agent Service busy"], sleep, clock) == ["Agent Service busy"]


def test_busy_box_defers_patch_and_changes_nothing(tmp_path, monkeypatch):
    (tmp_path / "maintenance.json").write_text('{"os_security": "patch"}')
    called = []
    monkeypatch.setattr(maintain, "call_helper", lambda a, r=None: called.append(a) or {"state": "applied"})
    rec = maintain.run(tmp_path, wait=0, is_busy=lambda: ["A chat run is active."], sleep=lambda s: None)
    assert rec["state"] == "deferred" and rec["os"]["state"] == "deferred" and called == [["restart-check"]]
    h = json.loads((tmp_path / "maintenance" / "health.json").read_text())
    assert next(i for i in h["items"] if i["key"] == "os-security")["value"] == "waiting for quiet"


def test_quiet_box_applies_and_records_history(tmp_path, monkeypatch):
    (tmp_path / "maintenance.json").write_text('{"os_security": "patch"}')
    monkeypatch.setattr(maintain, "call_helper", lambda a, r=None: {"state": "applied", "installed": ["openssl"],
                                                                      "security": [], "held": [], "reboot_required": True})
    rec = maintain.run(tmp_path, wait=0, is_busy=lambda: [], sleep=lambda s: None)
    assert rec["state"] == "ok"
    item = _row(tmp_path, "os-security")
    assert item["value"] == "1 installed" and item["status"] == "warn" and "restart" in item["detail"]
    assert len(maintain.history(tmp_path)) == 1


def test_lease_blocks_a_second_run(tmp_path):
    with maintain._lease(tmp_path):
        rec = maintain.run(tmp_path, wait=0, is_busy=lambda: [])
    assert rec["state"] == "busy"


def test_hermes_update_lock_blocks_the_run(tmp_path):
    import fcntl, os
    fd = os.open(tmp_path / "hermes-update.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert maintain.run(tmp_path, wait=0, is_busy=lambda: [])["state"] == "busy"
    finally:
        os.close(fd)


def test_health_is_stale_after_three_days(tmp_path):
    old = {"finished_at": "2026-10-01T00:00:00Z", "state": "ok", "os": {"state": "current"}}
    now = time.mktime(time.strptime("2026-10-23", "%Y-%m-%d"))
    assert _row(tmp_path, "os-security", old, now)["status"] == "error"


def test_history_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(maintain, "HISTORY_KEEP", 3)
    for _ in range(5):
        maintain._record(tmp_path, {"state": "ok", "finished_at": maintain._now(), "os": {"state": "current"}})
    assert len(maintain.history(tmp_path, 10)) == 3


def test_check_mode_without_helper_uses_unprivileged_check(tmp_path, monkeypatch):
    monkeypatch.setattr(maintain, "_helper", lambda a: None)
    monkeypatch.setattr(maintain.os_patch, "check", lambda refresh=False: {"state": "current", "refresh": refresh})
    assert maintain.os_step("check", tmp_path) == {"state": "current", "refresh": False}


def test_sudoers_rule_allows_only_the_helper(monkeypatch, tmp_path):
    seen = {}
    def runner(argv, **k):
        seen["argv"] = argv
        seen["rule"] = next(Path(p).read_text() for p in argv[-1].split() if p.endswith("/sudoers"))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(maintain.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(maintain, "HELPER", tmp_path / "missing-helper")
    res = maintain.setup(runner, user="wizard")
    assert seen["argv"][:2] == ["sudo", "-n"] and res["changed"]
    rule = seen["rule"]
    assert rule.startswith("wizard ALL=(root) NOPASSWD: ") and "ALL\n" not in rule.split("NOPASSWD:")[1]
    assert all(part.strip().startswith(str(tmp_path / "missing-helper")) for part in rule.split("NOPASSWD:")[1].split(","))


def test_apt_drops_packages_that_would_pull_a_held_dependency(monkeypatch):
    deps = {"openssl": ["openssl"], "udev": ["udev", "systemd", "libsystemd0"], "zlib1g": ["zlib1g"]}
    a = os_patch.Apt({})
    monkeypatch.setattr(a, "would_install", lambda names: sorted({x for n in names for x in deps[n]}))
    assert a.safe_set(["openssl", "udev", "zlib1g"]) == ["openssl", "zlib1g"]
    assert a.safe_set(["openssl"]) == ["openssl"]


def test_dnf_excludes_held_packages():
    argv = os_patch.Dnf({}).apply_argv(["curl"])
    assert "--exclude=docker*" in argv and "--exclude=systemd" in argv and argv[-1] == "curl"


def test_zypper_lists_only_needed_security_patches(monkeypatch):
    xml = ('<update kind="patch" name="SLE-1" edition="1" status="needed" category="recommended" severity="moderate">'
           '<update kind="patch" name="SLE-2" edition="1" status="needed" category="security" severity="important">'
           '<update kind="patch" name="SLE-3" edition="2" status="applied" category="security" severity="low">')
    monkeypatch.setattr(os_patch, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, xml, ""))
    assert [p["name"] for p in os_patch.Zypper({}).pending(False)] == ["SLE-2"]


# ---- slice 2: backups ---------------------------------------------------------------------------------------------

NOW = 1_800_000_000


def _bk(monkeypatch, state, status=None, every="daily"):
    monkeypatch.setattr(maintain, "backup_state", lambda h: {"state": state, "status": status or {}, "every": every})


def test_fresh_backup_is_not_rerun(tmp_path, monkeypatch):
    _bk(monkeypatch, "on", {"ok": True, "last_success_at": NOW - 3600})
    called = []
    res = maintain.backup_step(tmp_path, lambda *a, **k: called.append(a), now=NOW)
    assert res["ran"] is False and not called


def test_missed_backup_is_caught_up_through_wam_backup(tmp_path, monkeypatch):
    _bk(monkeypatch, "on", {"ok": True, "last_success_at": NOW - 30 * 3600})
    called = []
    def runner(argv, **k):
        called.append(argv); return subprocess.CompletedProcess(argv, 0, "{}", "")
    res = maintain.backup_step(tmp_path, runner, now=NOW)
    assert res["ran"] and called[0][1:] == ["backup", "run", "--scheduled", "--json"]


def test_backup_not_configured_does_nothing(tmp_path, monkeypatch):
    _bk(monkeypatch, "not_set_up")
    assert maintain.backup_step(tmp_path, lambda *a, **k: 1 / 0, now=NOW)["state"] == "not_set_up"


@pytest.mark.parametrize("b,status,value", [
    ({"state": "not_set_up"}, "warn", "not set up"),
    ({"state": "on", "status": {"ok": True, "last_success_at": NOW - 7200,
                                 "read_data": {"ok": True, "at": NOW - 86400}}}, "ok", "verified, 2h ago"),
    ({"state": "on", "status": {"ok": True, "last_success_at": NOW - 7200}}, "warn", "backed up 2h ago"),
    ({"state": "on", "status": {"ok": False, "error": "R2 down", "last_success_at": NOW - 20 * 3600,
                                 "read_data": {"ok": True, "at": NOW}}}, "warn", "last run failed"),
    ({"state": "on", "status": {"ok": False, "error": "R2 down", "last_success_at": NOW - 4 * 86400}}, "error", "last 4 days ago"),
    ({"state": "on", "status": {"ok": False, "error": "no key"}}, "error", "no backup yet"),
])
def test_backup_health_row(b, status, value):
    item = maintain._backup_item(b, NOW)
    assert (item["status"], item["value"]) == (status, value)


def test_backup_row_absent_without_backup_engine():
    assert maintain._backup_item({"state": "not_installed"}, NOW) is None


# ---- slice 3: Wizard OS updates -------------------------------------------------------------------------------

def test_updates_step_ask_does_nothing(tmp_path, monkeypatch):
    from wam import updates
    monkeypatch.setattr(updates, "mode", lambda h=None: "ask")
    monkeypatch.setattr(updates, "apply_if_auto", lambda h=None: 1 / 0)
    assert maintain.updates_step(tmp_path, True)["state"] == "ask"


def test_updates_step_auto_applies_only_when_quiet(tmp_path, monkeypatch):
    from wam import updates
    monkeypatch.setattr(updates, "mode", lambda h=None: "auto")
    monkeypatch.setattr(updates, "apply_if_auto", lambda h=None: {"started": True, "latest": "0.4.0", "installed": "0.3.9"})
    assert maintain.updates_step(tmp_path, False)["state"] == "deferred"
    assert maintain.updates_step(tmp_path, True) == {"state": "started", "to": "0.4.0", "from": "0.3.9"}


def test_updates_failure_never_sinks_the_run(tmp_path, monkeypatch):
    from wam import updates
    monkeypatch.setattr(updates, "mode", lambda h=None: "auto")
    def boom(h=None): raise OSError("network")
    monkeypatch.setattr(updates, "apply_if_auto", boom)
    _bk(monkeypatch, "not_set_up")
    monkeypatch.setattr(maintain, "os_step", lambda *a, **k: {"state": "current", "security": [], "held": []})
    rec = maintain.run(tmp_path, wait=0, is_busy=lambda: [])
    assert rec["updates"]["state"] == "error" and rec["os"]["state"] == "current" and rec["state"] == "error"
    keys = [i["key"] for i in maintain.health(tmp_path)["items"]]
    assert keys == ["backups", "os-security", "wizard-updates", "maintenance-held", "maintenance-services", "maintenance-reboot"]


def test_failed_catch_up_backup_marks_the_run(tmp_path, monkeypatch):
    calls = []
    def state(h):
        calls.append(1)
        return ({"state": "on", "status": {"last_success_at": None}} if len(calls) == 1
                else {"state": "on", "status": {"ok": False, "error": "no runtipi"}})
    monkeypatch.setattr(maintain, "backup_state", state)
    monkeypatch.setattr(maintain, "os_step", lambda *a, **k: {"state": "current", "security": [], "held": []})
    rec = maintain.run(tmp_path, runner=lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", ""), wait=0,
                       is_busy=lambda: [], with_updates=False)
    assert rec["backup"]["result"] == "failed" and rec["state"] == "error"
