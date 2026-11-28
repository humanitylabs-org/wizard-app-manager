"""Human Handoff for the host agent behind `wizard/human-handoff` (humanitylabs-org/wizard-os#775)."""
import json
import subprocess
from pathlib import Path

import pytest

from wam import human_handoff as hh


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "user"
    (home / ".hermes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(hh, "host_agent", lambda: str(home / ".local" / "bin" / "hermes"))
    monkeypatch.setenv("WIZARD_OS_HOME", str(home / ".wizard-os"))
    return {"home": home, "wiz": home / ".wizard-os"}


class Fake:
    """Stands in for subprocess.run: git clone/rev-parse, install.sh, doctor, apt, the browser."""

    def __init__(self, commit=hh.COMMIT, ready=True, apt_ok=True, browser="/usr/bin/chromium", uninstall_ok=True):
        self.calls, self.commit, self.ready, self.apt_ok = [], commit, ready, apt_ok
        self.browser, self.uninstall_ok = browser, uninstall_ok

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        ok = subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["python3", "-c"]:
            return ok
        if argv[0] == "git" and "clone" in argv:
            dest = Path(argv[-1])
            (dest / "scripts").mkdir(parents=True)
            (dest / "scripts" / "install.sh").write_text("#!/bin/sh\n")
            (dest / "scripts" / "uninstall.sh").write_text("#!/bin/sh\n")
            return ok
        if argv[0] == "git" and "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, self.commit + "\n", "")
        if argv[0] == "bash" and argv[1].endswith("install.sh") and not argv[1].endswith("uninstall.sh"):
            p = hh.paths()
            p["bin"].parent.mkdir(parents=True, exist_ok=True)
            p["bin"].write_text("#!/bin/sh\n")
            p["skill"].parent.mkdir(parents=True, exist_ok=True)
            p["skill"].write_text("skill\n")
            return subprocess.CompletedProcess(argv, 0 if self.ready else 2, "", "")
        if argv[0] == "bash" and argv[1].endswith("uninstall.sh"):
            if not self.uninstall_ok:
                return subprocess.CompletedProcess(argv, 2, "", "An active or failed handoff remains.")
            p = hh.paths()
            p["bin"].unlink(missing_ok=True)
            p["skill"].unlink(missing_ok=True)
            return ok
        if argv[-1] == "doctor":
            doc = {"ready": self.ready, "missing": [] if self.ready else ["running-tailscale-with-serve-access"],
                   "browser": self.browser}
            return subprocess.CompletedProcess(argv, 0 if self.ready else 2, json.dumps(doc), "")
        if argv[:3] == ["sudo", "-n", "apt-get"]:
            return subprocess.CompletedProcess(argv, 0 if self.apt_ok else 1, "", "sudo: a password is required")
        return ok


def all_there(cmd):
    return "/usr/bin/" + cmd


def test_switch_off_installs_reads_and_writes_nothing(box):
    run = Fake()
    res = hh.ensure(on=False, run=run, which=all_there)
    assert res == {"state": "off", "switch": hh.SWITCH}
    assert run.calls == []
    assert not box["wiz"].exists()
    assert not hh.paths()["bin"].exists()


def test_switch_on_installs_the_pinned_release_and_doctor_passes(box):
    run = Fake()
    res = hh.ensure(on=True, run=run, which=all_there)
    assert res["state"] == "ready" and res["installed_by_wam"] is True
    assert res["release"] == "v0.1.0" and res["commit"] == hh.COMMIT
    clone = next(c for c in run.calls if "clone" in c)
    assert "--branch" in clone and "v0.1.0" in clone and clone[-2] == hh.REPO
    assert any(c[0] == "bash" and c[1].endswith("scripts/install.sh") for c in run.calls)
    assert hh.paths()["bin"].exists() and hh.paths()["skill"].is_file()
    assert json.loads((box["wiz"] / hh.STATE).read_text())["state"] == "ready"
    # the next hourly run reinstalls nothing
    again = Fake()
    assert hh.ensure(on=True, run=again, which=all_there)["state"] == "ready"
    assert not any("clone" in c for c in again.calls)


def test_a_moved_tag_is_refused(box):
    run = Fake(commit="0" * 40)
    res = hh.ensure(on=True, run=run, which=all_there)
    assert res["state"] == "failed" and "expected" in res["error"]
    assert not any(c[0] == "bash" for c in run.calls)
    assert not hh.paths()["bin"].exists()


def test_missing_system_packages_are_installed_with_sudo(box):
    run = Fake()
    res = hh.ensure(on=True, run=run, which=lambda c: None if c == "x11vnc" else all_there(c))
    assert res["state"] == "ready"
    install = next(c for c in run.calls if c[:3] == ["sudo", "-n", "apt-get"] and "install" in c)
    assert "x11vnc" in install


def test_without_sudo_it_says_what_to_run(box):
    run = Fake(apt_ok=False)
    res = hh.ensure(on=True, run=run, which=lambda c: None if c in ("Xvfb", "x11vnc") else all_there(c))
    assert res["state"] == "needs-packages"
    assert res["remedy"].startswith("sudo apt-get install -y xvfb x11vnc")
    assert not any("clone" in c for c in run.calls)


def test_not_ready_doctor_is_recorded(box):
    res = hh.ensure(on=True, run=Fake(ready=False), which=all_there)
    assert res["state"] == "not-ready"
    assert res["doctor"]["missing"] == ["running-tailscale-with-serve-access"]


def test_a_hand_installed_copy_is_left_alone(box):
    b = hh.paths()["bin"]
    b.parent.mkdir(parents=True)
    b.write_text("hand\n")
    run = Fake()
    res = hh.ensure(on=True, run=run, which=all_there)
    assert res["state"] == "hand-installed"
    assert not any("clone" in c or c[0] == "bash" for c in run.calls)
    assert b.read_text() == "hand\n"
    # turning the switch off never removes a copy this command did not install
    assert hh.ensure(on=False, run=Fake(), which=all_there)["state"] == "off"
    assert b.read_text() == "hand\n"


def test_fresh_box_container_agent_is_not_touched(box, monkeypatch):
    monkeypatch.setattr(hh, "host_agent", lambda: None)
    run = Fake()
    res = hh.ensure(on=True, run=run, which=all_there)
    assert res["state"] == "no-host-agent"
    assert run.calls == []


def test_losing_the_host_agent_never_orphans_the_install(box, monkeypatch):
    hh.ensure(on=True, run=Fake(), which=all_there)
    monkeypatch.setattr(hh, "host_agent", lambda: None)
    assert hh.ensure(on=True, run=Fake(), which=all_there)["state"] == "no-host-agent"
    res = hh.ensure(on=False, run=Fake(), which=all_there)
    assert res["removed"] is True and not hh.paths()["bin"].exists()


def test_switch_off_again_removes_what_it_installed(box):
    hh.ensure(on=True, run=Fake(), which=all_there)
    run = Fake()
    res = hh.ensure(on=False, run=run, which=all_there)
    assert res["removed"] is True
    assert any(c[0] == "bash" and c[1].endswith("uninstall.sh") for c in run.calls)
    assert not hh.paths()["bin"].exists() and not hh.paths()["src"].exists()
    assert not (box["wiz"] / hh.STATE).exists()


def test_removal_waits_while_a_handoff_is_active(box):
    hh.ensure(on=True, run=Fake(), which=all_there)
    res = hh.ensure(on=False, run=Fake(uninstall_ok=False), which=all_there)
    assert res["state"] == "remove-waiting"
    assert hh.paths()["bin"].exists()


def test_playwright_chromium_gets_its_libraries(box, monkeypatch):
    run = Fake(browser=str(box["home"] / ".cache" / "ms-playwright" / "chromium-1" / "chrome-linux64" / "chrome"))
    starts = iter([False, True])
    monkeypatch.setattr(hh, "_browser_starts", lambda b, r=None: next(starts))
    res = hh.ensure(on=True, run=run, which=all_there)
    assert res["browser_deps"] == "installed"
    assert any(c[:2] == ["sudo", "-n"] and c[-2:] == ["install-deps", "chromium"] for c in run.calls)


def test_an_unreadable_switch_never_uninstalls(box, monkeypatch):
    hh.ensure(on=True, run=Fake(), which=all_there)
    monkeypatch.setattr(hh, "switch_on", lambda home=None: None)
    run = Fake()
    res = hh.ensure(run=run, which=all_there)
    assert res["state"] == "ready" and "could not be read" in res["note"]
    assert run.calls == [] and hh.paths()["bin"].exists()


def test_switch_reads_look_json_and_is_off_on_for_everyone(box, monkeypatch):
    from wam import feature_switches as fs

    class Pages:
        def __init__(self, doc):
            self.doc = doc

        def get(self):
            return self.doc, 1

    doc = {"version": 1, "tweaks": {hh.SWITCH: {"on": True, "at": 3}}}
    monkeypatch.setattr(fs, "_channel", lambda home=None: "latest")
    monkeypatch.setattr(fs, "_pages", lambda home=None, timeout=10: Pages(doc))
    assert hh.switch_on() is True
    monkeypatch.setattr(fs, "_pages", lambda home=None, timeout=10: Pages({"version": 1, "tweaks": {}}))
    assert hh.switch_on() is False
    monkeypatch.setattr(fs, "_channel", lambda home=None: "stable")
    assert hh.switch_on() is False

    def down(home=None, timeout=10):
        raise OSError("connection refused")
    monkeypatch.setattr(fs, "_channel", lambda home=None: "latest")
    monkeypatch.setattr(fs, "_pages", down)
    assert hh.switch_on() is None


def test_tick_never_raises(box, monkeypatch):
    monkeypatch.setattr(hh, "switch_on", lambda home=None: (_ for _ in ()).throw(RuntimeError("no look.json")))
    assert hh.tick()["state"] == "off"


def test_flipping_the_switch_starts_the_install_at_once(monkeypatch):
    from wam import feature_switches as fs
    started = []
    monkeypatch.setattr(hh, "spawn", lambda home=None: started.append(home))
    fs._after_switch("wizard/update-bug-reports", None)
    assert started == []
    fs._after_switch(hh.SWITCH, None)
    assert started == [None]


def test_spawn_runs_wam_detached(tmp_path):
    seen = {}

    def popen(argv, **kw):
        seen.update(argv=argv, **kw)

    assert hh.spawn(tmp_path, popen=popen) == "started"
    assert seen["argv"][1:] == ["-m", "wam", "human-handoff", "--ensure"]
    assert seen["start_new_session"] is True


def test_the_hourly_tick_keeps_it_in_line(monkeypatch, capsys):
    from wam import app_updates, cli, switch_feedback
    ticks = []
    monkeypatch.setattr(switch_feedback, "tick", lambda *a, **k: {"sent": False})
    monkeypatch.setattr(hh, "tick", lambda *a, **k: ticks.append(1) or {"state": "off"})
    monkeypatch.setattr(app_updates, "apply_if_auto", lambda *a, **k: {"mode": "ask"})
    assert cli.main(["app-updates", "--apply-if-auto"]) == 0
    assert ticks == [1]


def test_cli_status_and_ensure(box, monkeypatch, capsys):
    from wam import cli
    monkeypatch.setattr(hh, "switch_on", lambda home=None: False)
    assert cli.main(["human-handoff"]) == 0
    assert "off (switch wizard/human-handoff)" in capsys.readouterr().out
    assert cli.main(["human-handoff", "--ensure", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "off"
