"""Quick update (wam/quick_update.py): a release that moves only the Wizard UX shell is swapped in place."""
import copy
import json
import subprocess
from pathlib import Path

import pytest

from wam import quick_update, updates

WAM = "a" * 40
D = lambda c: "sha256:" + c * 64   # noqa: E731


def _rel(version, ux="1", ux_commit="b", agent="2", wam_commit=WAM, files_commit="d"):
    return {
        "version": version, "name": f"Wizard OS {version}", "date": "2026-10-28", "about": "words", "notes": "words",
        "install": {"command": "curl -fsSL aiwizards.com/os.sh | bash"},
        "components": {"wizard-app-manager": {"repo": "humanitylabs-org/wizard-app-manager", "commit": wam_commit},
                       "wizard-ux": {"repo": "humanitylabs-org/wizard-ux", "commit": ux_commit * 40},
                       "wizard-agent-service": {"repo": "humanitylabs-org/wizard-agent-service", "commit": "c" * 40}},
        "images": {"wizard-app-manager": {"ref": "ghcr.io/humanitylabs-org/wizard-app-manager:v1", "digest": D("e")},
                   "wizard-ux": {"ref": f"ghcr.io/humanitylabs-org/wizard-ux:sha-{ux_commit * 40}", "digest": D(ux)},
                   "wizard-agent-service": {"ref": "ghcr.io/humanitylabs-org/wizard-agent-service:sha-x", "digest": D(agent)}},
        "default_apps": {
            "shell": {"app": "wizard-ux", "repo": "humanitylabs-org/wizard-ux", "index_version": "0.3.0",
                      "commit": ux_commit * 40, "note": "n"},
            "agent-service": {"app": "wizard-agent-service", "repo": "humanitylabs-org/wizard-agent-service",
                              "commit": "c" * 40},
            "files": {"app": "files", "repo": "oscarhenrycollins/files", "commit": files_commit * 40}},
    }


def _apps(h, rel, **moved):
    """apps.json as the installer's projection writes it: each default at its release source commit."""
    apps = [{"id": r["app"], "commit": moved.get(r["app"], r["commit"])} for r in rel["default_apps"].values()]
    (h / "apps.json").write_text(json.dumps({"apps": apps}))


@pytest.fixture(autouse=True)
def _clean():
    updates._latest.clear(); quick_update._docs.clear()
    yield
    updates._latest.clear(); quick_update._docs.clear()


@pytest.fixture
def box(tmp_path, monkeypatch):
    """An alongside box on 0.9.1 (clean read-back), a mirror with 0.9.2 = 0.9.1 + a new Wizard UX image."""
    rel = tmp_path / "rel"; rel.mkdir()
    old, new = _rel("0.9.1"), _rel("0.9.2", ux="9", ux_commit="f")
    for d in (old, new):
        (rel / f"{d['version']}.json").write_text(json.dumps(d, indent=2) + "\n")
    (rel / "stable").write_text("0.9.2\n")
    h = tmp_path / "wos"; h.mkdir()
    (h / "release.json").write_text(json.dumps({"release": "0.9.1", "mode": "alongside", "status": "pinned",
                                                "install_rc": 0, "checks": []}))
    (h / "release-pins.json").write_bytes((rel / "0.9.1.json").read_bytes())
    (h / "alongside.json").write_text(json.dumps({"mode": "alongside", "shell": {"port": 8796}}))
    _apps(h, old)
    monkeypatch.setattr(quick_update, "checkout_state", lambda: (WAM, True))
    monkeypatch.delenv("WIZARD_RELEASE_BASE", raising=False)
    return h, rel


def test_only_shell_pins_are_a_quick_change():
    a = _rel("0.9.1")
    assert quick_update.changes(a, _rel("0.9.2", ux="9", ux_commit="f")) == ["wizard-ux"]
    assert quick_update.changes(a, {**copy.deepcopy(a), "version": "0.9.2", "about": "x"}) == []   # words only
    assert quick_update.changes(a, _rel("0.9.2", agent="7")) is None          # Agent Service: installer only
    assert quick_update.changes(a, _rel("0.9.2", wam_commit="0" * 40)) is None  # App Manager
    assert quick_update.changes(a, _rel("0.9.2", files_commit="7")) is None   # any other default app
    b = _rel("0.9.2", ux="9", ux_commit="f"); b["hermes"] = {"tag": "v1"}
    assert quick_update.changes(a, b) is None                                 # a new top-level pin
    b = _rel("0.9.2", ux="9", ux_commit="f"); b["default_apps"]["shell"]["alongside"] = "x"
    assert quick_update.changes(a, b) is None                                 # the slot's install flags
    b = _rel("0.9.2", ux="9", ux_commit="f"); b["images"]["wizard-ux"]["ref"] = "ghcr.io/evil/wizard-ux:sha-f"
    assert quick_update.changes(a, b) is None                                 # another image repository
    b = _rel("0.9.2", ux="9", ux_commit="f"); b["default_apps"]["shell"]["repo"] = "evil/wizard-ux"
    assert quick_update.changes(a, b) is None


def test_status_offers_the_quick_path_only_when_every_condition_holds(box, monkeypatch):
    h, rel = box
    st = updates.status(h, rel.as_uri())
    assert st["update_available"] is True and st["quick"] is True
    assert quick_update.plan(h, "0.9.2", rel.as_uri())["apps"] == ["wizard-ux"]

    def reason(**kw):
        quick_update._docs.clear(); updates._latest.clear()
        return quick_update.plan(h, "0.9.2", rel.as_uri(), **kw)

    assert reason(checkout=lambda: (WAM, False))["quick"] is False            # hand-edited checkout
    assert reason(checkout=lambda: ("0" * 40, True))["quick"] is False        # not the release's App Manager
    receipt = json.loads((h / "release.json").read_text())
    for bad in ({"status": "drift"}, {"status": "mismatch"}, {"mode": "fresh"}):
        (h / "release.json").write_text(json.dumps({**receipt, **bad}))
        assert reason()["quick"] is False, bad
    (h / "release.json").write_text(json.dumps(receipt))
    (h / "release-pins.json").write_text(json.dumps({**_rel("0.9.1"), "about": "local edit"}))
    assert "differ from the published" in reason()["reason"]
    (h / "release-pins.json").write_bytes((rel / "0.9.1.json").read_bytes())
    (rel / "0.9.2.json").write_text(json.dumps(_rel("0.9.2", ux="9", agent="7")))
    assert "more than the Wizard UX shell" in reason()["reason"]
    updates._latest.clear(); quick_update._docs.clear()
    assert updates.status(h, rel.as_uri())["quick"] is False


def test_apply_starts_the_quick_runner_instead_of_the_installer(box):
    h, rel = box
    calls = []
    res = updates.apply(h, rel.as_uri(), lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert res["started"] is True and res["quick"] is True and len(calls) == 1
    script = calls[0][-1]
    assert "updates --quick-apply 0.9.2" in script and "os.sh" not in script and "systemd-run --user" in script


def test_apply_uses_the_installer_when_the_release_moves_more(box):
    h, rel = box
    (rel / "0.9.2.json").write_text(json.dumps(_rel("0.9.2", ux="9", agent="7")))
    calls = []
    res = updates.apply(h, rel.as_uri(), lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert res["started"] is True and not res.get("quick")
    assert "aiwizards.com/os.sh | WIZARD_RELEASE=0.9.2 bash -s start" in calls[0][-1]


class FakeOps:
    """Records the steps; `fail` names the step that raises."""

    def __init__(self, h, fail=None):
        self.h, self.fail, self.steps, self.installer, self.failed = h, fail, [], [], False
        self.files_list = [h / "release.json", h / "release-pins.json", h / "alongside.json", h / "override.yml"]
        (h / "override.yml").write_text("image: 0.9.1\n")
        (h / "recipe").mkdir(exist_ok=True)
        (h / "recipe" / ".wizard-commit").write_text("old\n")

    def files(self):
        return self.files_list

    def dirs(self):
        return [self.h / "recipe"]

    def _step(self, name, *a):
        self.steps.append((name, *a))
        if self.fail == name and not self.failed:
            self.failed = True
            self.steps.append(("rollback-mark",))
            raise quick_update.QuickError(f"{name} broke")

    def refresh_index(self, pins, image):
        self._step("refresh_index", json.loads(pins.read_text())["version"], image)

    def update_apps(self, apps):
        v = json.loads((self.h / "release-pins.json").read_text())["version"]
        (self.h / "override.yml").write_text(f"image: {v}\n")       # app_update rewrites it from the pins
        if v == "0.9.2":
            (self.h / "recipe" / ".wizard-commit").write_text("new\n")   # resolve_any stages the target recipe
        self._step("update_apps", v)

    def ensure_image(self, app, digest):
        self._step("ensure_image", app, digest)

    def store_data(self):
        self._step("store_data", json.loads((self.h / "release-pins.json").read_text())["version"])

    def readback(self, pins):
        d = json.loads(pins.read_text())
        (self.h / "release.json").write_text(json.dumps({"release": d["version"], "mode": "alongside",
                                                         "status": "pinned", "install_rc": 0, "checks": []}))
        self._step("readback", d["version"])
        return {"status": "pinned"}

    def verify_shell(self, app, digest):
        self._step("verify_shell", app, digest)

    def start_installer(self, version, auto):
        self.installer.append((version, auto))
        return True


def test_runner_swaps_the_shell_and_records_the_release_like_the_installer(box):
    h, rel = box
    ops = FakeOps(h)
    res = quick_update.run("0.9.2", h, rel.as_uri(), ops=ops)
    assert res["done"] is True and ops.installer == []
    names = [s[0] for s in ops.steps]
    assert names == ["refresh_index", "update_apps", "ensure_image", "store_data", "readback", "verify_shell"]
    assert ("store_data", "0.9.2") in ops.steps          # Codex: the Store adapter follows the shell commit
    assert ops.steps[0][1] == "0.9.2" and ops.steps[0][2] == "ghcr.io/humanitylabs-org/wizard-app-manager@" + D("e")
    assert ops.steps[2] == ("ensure_image", "wizard-ux", D("9"))
    assert (h / "release-pins.json").read_bytes() == (rel / "0.9.2.json").read_bytes()   # the published bytes
    assert updates.installed(h) == "0.9.2"
    st = json.loads((h / "install-status.json").read_text())
    assert st["state"] == "done" and st["quick"] is True and st["exit_code"] == 0
    updates._latest.clear()
    after = updates.status(h, rel.as_uri())
    assert after["update_available"] is False and after["installing"] is False
    assert "[quick] quick update to 0.9.2 done" in (h / "install.log").read_text()


def test_a_failed_check_rolls_back_then_hands_over_to_the_installer(box):
    h, rel = box
    before = {f: f.read_bytes() for f in (h / "release.json", h / "release-pins.json", h / "alongside.json")}
    ops = FakeOps(h, fail="verify_shell")
    res = quick_update.run("0.9.2", h, rel.as_uri(), ops=ops, auto=True)
    assert res["done"] is False and res["rolled_back"] is True and res["fallback"] is True
    assert ops.installer == [("0.9.2", True)]                 # the full update still brings the box to 0.9.2
    for f, data in before.items():
        assert f.read_bytes() == data, f                      # every file back as it was
    assert (h / "override.yml").read_text() == "image: 0.9.1\n"
    assert (h / "recipe" / ".wizard-commit").read_text() == "old\n"   # Codex P2: the staged recipe is put back
    assert not (h / ".quick-rollback").exists()
    # the rollback re-pointed the index and the shell at the installed release's pins
    back = ops.steps[ops.steps.index(("rollback-mark",)) + 1:]
    assert ("refresh_index", "0.9.1", "ghcr.io/humanitylabs-org/wizard-app-manager@" + D("e")) in back
    assert ("update_apps", "0.9.1") in back and ("verify_shell", "wizard-ux", D("1")) in back
    assert back[-1] == ("store_data", "0.9.1")           # and back to the old shell's adapter
    assert updates.installed(h) == "0.9.1"
    assert "rolled back to 0.9.1" in (h / "install.log").read_text()


def test_without_fallback_a_failure_is_reported_and_the_old_release_stays(box, monkeypatch):
    h, rel = box
    monkeypatch.setenv(quick_update.FALLBACK_ENV, "0")
    ops = FakeOps(h, fail="readback")
    res = quick_update.run("0.9.2", h, rel.as_uri(), ops=ops)
    assert res["done"] is False and res["fallback"] is False and ops.installer == []
    st = json.loads((h / "install-status.json").read_text())
    assert st["state"] == "failed" and "readback broke" in st["failure_reason"]
    assert updates.installed(h) == "0.9.1"
    assert (h / "release-pins.json").read_bytes() == (rel / "0.9.1.json").read_bytes()


def test_runner_refuses_while_an_install_holds_the_lock(box):
    import fcntl, os
    h, rel = box
    fd = os.open(h / "install.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        ops = FakeOps(h)
        res = quick_update.run("0.9.2", h, rel.as_uri(), ops=ops)
        assert res["started"] is False and ops.steps == []
    finally:
        os.close(fd)


def test_runner_rechecks_and_never_swaps_a_release_that_moves_more(box):
    h, rel = box
    (rel / "0.9.2.json").write_text(json.dumps(_rel("0.9.2", ux="9", wam_commit="0" * 40)))
    ops = FakeOps(h)
    res = quick_update.run("0.9.2", h, rel.as_uri(), ops=ops)
    assert ops.steps == [] and ops.installer == [("0.9.2", False)] and res["fallback"] is True
    assert (h / "release-pins.json").read_bytes() == (rel / "0.9.1.json").read_bytes()


def test_readback_is_get_sh_s_own_program():
    prog = quick_update.readback_program(Path(quick_update.wam_dir()) / "get.sh")
    compile(prog, "readback", "exec")
    assert "pins_path, wam_dir, mode, out_path, strict, install_rc = sys.argv[1:7]" in prog
    assert prog.rstrip().endswith("sys.exit(1 if core_bad else (3 if drift and strict == \"1\" else 0))")


def test_runner_leaves_the_process_environment_as_it_found_it(box, monkeypatch):
    import os
    h, rel = box
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    seen = []
    ops = FakeOps(h)
    real = ops.update_apps
    ops.update_apps = lambda apps: seen.append(os.environ.get("WIZARD_RELEASE_PINS")) or real(apps)
    assert quick_update.run("0.9.2", h, rel.as_uri(), ops=ops)["done"] is True
    assert seen == [str(h / "release-pins.json")]          # release_updates reads the target pins
    assert "WIZARD_RELEASE_PINS" not in os.environ


def test_a_rolled_back_app_update_fails_fast_instead_of_waiting_for_the_read_back(tmp_path, monkeypatch):
    """Runtipi answers a failed image pull by keeping the old container (status running, no error); the quick path
    must notice the digest did not move right away (live rig: the read-back otherwise waited 5 minutes)."""
    from wam import cli
    ops = quick_update.Ops(tmp_path, lambda m: None)
    ops._c = object()
    monkeypatch.setattr(cli, "app_update", lambda c, app: {"id": app, "status": "running"})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: {})
    monkeypatch.setattr(quick_update.time, "sleep", lambda s: None)
    monkeypatch.setattr(quick_update.Ops, "digests", lambda self, app: [D("1")])
    with pytest.raises(quick_update.QuickError, match="did not switch"):
        ops.ensure_image("wizard-ux", D("9"))
    seq = iter([[D("1")], [D("1")], [D("9")]])
    monkeypatch.setattr(quick_update.Ops, "digests", lambda self, app: next(seq))
    ops.ensure_image("wizard-ux", D("9"))      # switched after the update: fine


def test_a_cli_exit_mid_update_still_rolls_back_and_hands_over(box):
    """Codex P1: app_update() raises SystemExit (not Exception) when the shell is missing or networks refuse."""
    h, rel = box
    before = (h / "release-pins.json").read_bytes()
    ops = FakeOps(h)
    calls = []

    def boom(app, digest):
        calls.append(app)
        if len(calls) == 1:
            raise SystemExit("wizard-ux is not installed")
    ops.ensure_image = boom
    res = quick_update.run("0.9.2", h, rel.as_uri(), ops=ops)
    assert res["done"] is False and res["rolled_back"] is True and ops.installer == [("0.9.2", False)]
    assert (h / "release-pins.json").read_bytes() == before
    st = json.loads((h / "install-status.json").read_text())
    assert st["state"] == "running" and "full update" in st["step"]     # the installer's runner takes over


def test_release_updates_only_touches_the_planned_apps(tmp_path, monkeypatch):
    """Codex P1: an owner-moved Files must not be put back by a shell-only quick update."""
    from wam import cli
    root = tmp_path / "runtipi"
    for app in ("wizard-ux", "files"):
        live = root / "apps" / "wizardapps" / app
        live.mkdir(parents=True)
        (live / ".wizard-commit").write_text("0" * 40)
        (live / ".wizard-source").write_text(json.dumps({"repo": f"o/{app}"}))
    doc = {"default_apps": {"shell": {"app": "wizard-ux", "repo": "o/wizard-ux", "commit": "1" * 40},
                            "files": {"app": "files", "repo": "o/files", "commit": "2" * 40}}}
    monkeypatch.setattr(cli.release_pins, "load", lambda *a: doc)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"wizard-ux", "files"})
    monkeypatch.setattr(cli.registry, "app_meta", lambda *a: {})
    tried = []

    def resolve(c, app):
        tried.append(app)
        raise SystemExit("stop here")
    monkeypatch.setattr(cli, "resolve_any", resolve)
    c = type("C", (), {"runtipi_root": root})()
    failed: dict = {}
    cli.release_updates(c, None, failed, only={"wizard-ux"})
    assert tried == ["wizard-ux"] and set(failed) == {"wizard-ux"}
    tried.clear(); failed.clear()
    cli.release_updates(c, None, failed)                       # the installer still moves every default
    assert tried == ["wizard-ux", "files"]


def test_a_box_whose_other_defaults_moved_since_the_install_is_left_to_the_installer(box):
    """Codex P1: never start a quick run whose read-back would fail on an owner-moved app (then fall back)."""
    h, rel = box
    _apps(h, json.loads((rel / "0.9.1.json").read_text()), files="7" * 40)
    p = quick_update.plan(h, "0.9.2", rel.as_uri())
    assert p["quick"] is False and "files moved" in p["reason"]
    (h / "apps.json").unlink()
    assert quick_update.plan(h, "0.9.2", rel.as_uri())["quick"] is False


def test_a_box_running_another_core_image_is_left_to_the_installer(box):
    """Codex P1: an Agent Service image override must not be reset by the quick read-back."""
    h, rel = box
    old = json.loads((rel / "0.9.1.json").read_text())
    ref = lambda im: im["ref"].split(":")[0] + "@" + im["digest"]   # noqa: E731
    pins = {a: ref(old["images"][a]) for a in ("wizard-ux", "wizard-agent-service")}
    (h / "alongside.json").write_text(json.dumps({"mode": "alongside", "shell": {"port": 8796}, "images": pins}))
    assert quick_update.plan(h, "0.9.2", rel.as_uri())["quick"] is True     # pins as released: fine
    pins["wizard-agent-service"] = "ghcr.io/someone/agent@" + D("7")
    (h / "alongside.json").write_text(json.dumps({"mode": "alongside", "shell": {"port": 8796}, "images": pins}))
    p = quick_update.plan(h, "0.9.2", rel.as_uri())
    assert p["quick"] is False and "wizard-agent-service runs another image" in p["reason"]


def test_a_failing_rollback_phase_does_not_skip_the_others(box, monkeypatch):
    """Codex: the index refresh failing during recovery must still put the apps, shell and Store adapter back."""
    monkeypatch.setenv("WIZARD_QUICK_FALLBACK", "0")
    h, rel = box
    ops = FakeOps(h, fail="verify_shell")
    real = ops.refresh_index

    def flaky(pins, image):
        if ops.failed:                         # the rollback's refresh fails (Docker briefly away)
            ops.steps.append(("refresh_index-failed",))
            raise RuntimeError("docker unavailable")
        return real(pins, image)
    ops.refresh_index = flaky
    res = quick_update.run("0.9.2", h, rel.as_uri(), ops=ops, auto=False)
    assert res["done"] is False and res["rolled_back"] is False
    back = ops.steps[ops.steps.index(("rollback-mark",)) + 1:]
    assert ("refresh_index-failed",) in back
    assert ("update_apps", "0.9.1") in back and ("verify_shell", "wizard-ux", D("1")) in back
    assert back[-1] == ("store_data", "0.9.1")
    assert "rollback incomplete (release index)" in (h / "install.log").read_text()


def test_the_installer_handover_fails_when_the_download_fails(monkeypatch, tmp_path):
    """Codex: `curl … | bash -s start` without pipefail exits 0 when curl fails."""
    monkeypatch.setattr(updates, "INSTALLER", "http://127.0.0.1:9/nothing-here.sh")
    ops = quick_update.Ops(tmp_path, log=lambda *_: None)
    assert ops.start_installer("0.9.2", auto=False) is False
