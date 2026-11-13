"""wizard-os#425: a restored `helper_removed` flag and the sudoers rule against the documented commands."""
import json
import os
import pwd
import subprocess
from pathlib import Path

from wam import maintain, os_patch

# wizard-os spec/MAINTENANCE.md "Root helper": the main sudoers rule allows exactly these seven argument lists, and
# the attended restart lane exactly these three. Change the spec first, then this list and maintain.setup() together.
DOCUMENTED_MAIN = ["check", "check --refresh", "apply", "native-off", "native-on", "restart-check", "remove"]
DOCUMENTED_LANE = ["restart-service *", "apply-held", "reboot"]


def _rows(home):
    return {i["key"]: i for i in maintain.health(home, last={})["items"]}


def test_restored_removed_flag_with_a_helper_here_shows_a_health_choice(tmp_path, monkeypatch):
    helper = tmp_path / "os-patch"
    monkeypatch.setattr(maintain, "HELPER", helper)
    home = tmp_path / "home"
    home.mkdir()
    (home / "maintenance.json").write_text(json.dumps({"helper_removed": True, "os_security": "check"}))
    # Source box after `wam maintain remove`: flag set, no helper. Nothing to say.
    assert "maintenance-helper" not in _rows(home)
    # Restored onto a host whose installer put the helper in place: the flag no longer matches this machine.
    helper.write_text("x")
    row = _rows(home)["maintenance-helper"]
    assert row["status"] == "warn" and "wam maintain setup" in row["detail"] and "wam maintain remove" in row["detail"]
    # Choosing setup clears the flag, and the row goes away; nothing here touched the helper.
    (home / "maintenance.json").write_text(json.dumps({"os_security": "check"}))
    assert "maintenance-helper" not in _rows(home)
    assert helper.read_text() == "x"


def test_installer_still_respects_a_restored_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path))
    (tmp_path / "maintenance.json").write_text(json.dumps({"helper_removed": True}))
    monkeypatch.setattr(maintain.os, "geteuid", lambda: 1000)
    called = []
    res = maintain.setup(lambda *a, **k: called.append(a), installer=True)
    assert res["state"] == "removed_by_owner" and not called


def _written_rules(monkeypatch, tmp_path, lane):
    seen = {}

    def runner(argv, **k):
        script = argv[-1]
        for p in script.split():
            p = p.strip("'\"")
            if p.endswith("/sudoers"):
                seen["main"] = Path(p).read_text()
            if p.endswith("/lane"):
                seen["lane"] = Path(p).read_text()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "owner"))
    monkeypatch.setattr(maintain.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(maintain, "HELPER", tmp_path / "missing-helper")
    monkeypatch.setattr(maintain, "_install_paths_safe", lambda: True)
    maintain.setup(runner, user=pwd.getpwuid(os.getuid()).pw_name, lane=lane)
    return seen


def _commands(rule, helper):
    body = rule.split("NOPASSWD:", 1)[1].strip()
    out = []
    for part in body.split(","):
        part = part.strip()
        assert part.startswith(str(helper) + " "), part
        out.append(part[len(str(helper)) + 1:])
    return out


def test_sudoers_rule_matches_the_documented_commands(tmp_path, monkeypatch):
    seen = _written_rules(monkeypatch, tmp_path, lane=True)
    helper = tmp_path / "missing-helper"
    assert _commands(seen["main"], helper) == DOCUMENTED_MAIN
    assert _commands(seen["lane"], helper) == DOCUMENTED_LANE


def test_helper_accepts_every_documented_command_and_nothing_else(monkeypatch):
    called = []
    for name in ("check", "apply", "restart_check", "native", "restart_service", "apply_held", "schedule_reboot"):
        monkeypatch.setattr(os_patch, name, lambda *a, _n=name, **k: called.append(_n) or {"state": "ok"})
    monkeypatch.setattr(os_patch, "lane_enabled", lambda: True)
    monkeypatch.setattr(os_patch.os, "geteuid", lambda: 1000)   # `remove` refuses without root: still parsed
    for cmd in DOCUMENTED_MAIN + [c.replace("*", "wizard-os.service") for c in DOCUMENTED_LANE]:
        assert os_patch._main(cmd.split()) in (0, 1), cmd
    assert os_patch._main(["install", "curl"]) == 2
    assert os_patch._main(["check", "--refresh", "x"]) == 2


def test_restore_recomputes_the_carried_health_projection(tmp_path, monkeypatch):
    from wam.backup import restore
    helper = tmp_path / "os-patch"
    helper.write_text("x")
    monkeypatch.setattr(maintain, "HELPER", helper)
    home = tmp_path / "home"
    (home / "maintenance").mkdir(parents=True)
    (home / "maintenance.json").write_text(json.dumps({"helper_removed": True}))
    # health.json as written on the source box, where the helper was gone
    (home / "maintenance" / "health.json").write_text(json.dumps({"items": []}))

    class FakeBox:
        pass
    box = FakeBox()
    box.home = home
    restore._maintenance_health_after_restore(box)
    keys = [i["key"] for i in json.loads((home / "maintenance" / "health.json").read_text())["items"]]
    assert "maintenance-helper" in keys
