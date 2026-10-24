"""`wam release` (wam/release.py): the owner's commits reapplied on top of the channel's shell (wizard-os#186).

Every test runs on real git repos in tmp dirs: a fake upstream (bare, `main`), the owner's checkout on its release
branch, and an injected runner for docker / compose so nothing touches a real box."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from wam import release, updates

ID = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *ID, *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd, path, text, msg):
    p = Path(cwd) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", msg)
    return git(cwd, "rev-parse", "HEAD")


class Docker:
    """Records docker / compose calls; `up` decides whether a recreated shell comes up."""

    def __init__(self, build_ok=True):
        self.calls, self.build_ok, self.manifests = [], build_ok, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "build"]:     # what the shell will serve, read from the build context
            self.manifests.append(json.loads((Path(argv[-1]) / "web/my-release.json").read_text()))
        code = 0 if (argv[:2] != ["docker", "build"] or self.build_ok) else 1
        return subprocess.CompletedProcess(argv, code, "", "")

    def builds(self):
        return [c for c in self.calls if c[:2] == ["docker", "build"]]

    def ups(self):
        return [c for c in self.calls if c[:2] == ["docker", "compose"]]


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A Wizard OS home with a shell in apps.json, Runtipi dirs, a fake upstream and the owner's checkout."""
    monkeypatch.delenv("WAM_RELEASE_DIR", raising=False)
    up_work = tmp_path / "upstream-work"
    up_work.mkdir()
    git(up_work, "init", "-q", "-b", "main")
    commit(up_work, "web/app.js", "line 1\nline 2\nline 3\n", "upstream: first")
    commit(up_work, "tests/ok", "", "upstream: tests pass")
    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(up_work), str(bare)], check=True)
    git(up_work, "remote", "add", "origin", str(bare))

    home = tmp_path / "wos"
    home.mkdir()
    rt = tmp_path / "runtipi"
    (rt / "apps/wizardapps/wizard-ux").mkdir(parents=True)
    (rt / "apps/wizardapps/wizard-ux/docker-compose.generated.yml").write_text("services: {}\n")
    ov = rt / "user-config/wizardapps/wizard-ux"
    ov.mkdir(parents=True)
    (ov / "docker-compose.yml").write_text(
        "# alongside\nservices:\n  wizard-ux:\n    image: ghcr.io/example/wizard-ux:sha-1\n    pull_policy: missing\n"
        "    network_mode: host\n")
    (home / "app-manager.json").write_text(json.dumps({"runtipi_root": str(rt)}))
    (home / "apps.json").write_text(json.dumps({"apps": [
        {"id": "wizard-ux", "provides": ["shell"], "status": "running",
         "view": {"local": "http://127.0.0.1:18796/wizard-os-home/"}, "health": "http://127.0.0.1:18796/shell-health"},
        {"id": "notifications", "provides": ["notifications"], "status": "running",
         "health": "http://127.0.0.1:18790/v1/health"}]}))
    (home / "updates.json").write_text(json.dumps({"mode": "ask"}))

    class B:
        pass
    b = B()
    b.home, b.up, b.bare, b.override, b.tmp = home, up_work, bare, ov / "docker-compose.yml", tmp_path
    b.posts = []
    b.served = {"build": None}
    b.docker = Docker()

    def init(**kw):
        args = dict(home=home, name="Owner's release", source=str(bare), test_cmd="test -f tests/ok",
                    timer=False, systemctl=False, resolver="none")
        args.update(kw)
        res = release.init(**args)
        b.src = Path(res["checkout"])
        git(b.src, "config", "user.name", "Owner"); git(b.src, "config", "user.email", "owner@example.invalid")
        return res

    def run(action, **kw):
        def probe(url, timeout=3):
            if url.endswith("/my-release.json"):
                return {"build": b.served["build"]} if b.served["build"] else None
            return {}

        def docker(argv, **k):
            r = b.docker(argv, **k)
            if argv[:2] == ["docker", "compose"] and b.served.get("follow", True):
                img = [ln.split("image:")[1].strip() for ln in b.override.read_text().splitlines() if "image:" in ln]
                b.served["build"] = img[0].rsplit("release-", 1)[-1] if img and "release-" in img[0] else None
            return r
        args = dict(run=docker, probe=probe, post=lambda url, doc: b.posts.append((url, doc)) or True,
                    wait=0, sleep=lambda s: None)
        args.update(kw)
        return release.run(action, home, **args)
    b.init, b.run = init, run
    return b


def _owner_change(b, path="web/mine.js", text="mine\n", msg="Owner: my tweak"):
    return commit(b.src, path, text, msg)


def _upstream_change(b, path="web/new.js", text="new\n", msg="upstream: new thing"):
    sha = commit(b.up, path, text, msg)
    git(b.up, "push", "-q", "origin", "main")
    return sha


# ---------------------------------------------------------------- init / check

def test_init_creates_the_branch_from_the_channel_and_check_records_whats_new(box):
    res = box.init()
    assert res["branch"] == "release/mine" and res["created_branch"] is True and res["push"] is None
    assert git(box.src, "branch", "--show-current") == "release/mine"
    assert Path(res["dir"]) == box.home / "release"
    cfg = json.loads((box.home / "my-release.json").read_text())
    assert cfg["name"] == "Owner's release" and cfg["channel"] == "latest" and cfg["enabled"] is True
    assert release.enabled(box.home) is False            # nothing built yet: the box still updates the stock way
    _upstream_change(box)
    r = box.run("check")
    assert r["state"] == "checked" and r["behind"] == 1
    pending = json.loads((box.home / "release/pending.json").read_text())
    assert pending["new"][0]["title"] == "upstream: new thing"
    assert box.docker.calls == []                        # check builds nothing
    again = box.init()                                   # init again adopts the branch, never resets it
    assert again["created_branch"] is False


def test_units_are_installed_and_removed(box, tmp_path):
    ud = tmp_path / "units"
    files = release.install_units(ud, systemctl=False, home=box.home)
    assert files == ["wam-release-update.service", "wam-release.service", "wam-release.timer"]
    assert "release auto" in (ud / "wam-release.service").read_text()
    assert "release update" in (ud / "wam-release-update.service").read_text()
    assert "OnCalendar=hourly" in (ud / "wam-release.timer").read_text()
    assert sorted(release.remove_units(ud, systemctl=False)) == files
    assert not any(ud.iterdir())


# ---------------------------------------------------------------- update: clean merge

def test_rebuild_then_clean_merge_update_builds_tests_and_switches(box):
    box.init()
    mine = _owner_change(box)
    r = box.run("rebuild")
    assert r["state"] == "ok" and r["changes"] == 1, r
    assert release.enabled(box.home) is True
    built = json.loads((box.home / "release/built.json").read_text())
    assert built["build"] == mine[:8] and built["previous_image"] == "ghcr.io/example/wizard-ux:sha-1"
    ov = box.override.read_text()
    assert f"image: wizard-ux:release-{mine[:8]}" in ov and "pull_policy: never" in ov and "network_mode: host" in ov
    assert ov.count("image:") == 1
    assert (box.home / "release/backups/override.before-release").read_text().count("sha-1") == 1

    _upstream_change(box)
    assert box.run("check")["behind"] == 1
    st = updates.status(box.home)
    assert st["update_available"] is True and st["release"]["behind"] == 1
    r = box.run("update")
    assert r["state"] == "ok", r
    head = git(box.src, "rev-parse", "HEAD")
    assert git(box.src, "log", "-1", "--format=%P", head).count(" ") == 1            # a merge, never a rebase
    assert subprocess.run(["git", "-C", str(box.src), "merge-base", "--is-ancestor", mine, head]).returncode == 0
    assert (box.home / "release/status").read_text().split(" ", 1)[1].startswith("OK Owner's release")
    assert updates.status(box.home)["release"]["behind"] == 0
    # the manifest the shell reads is in the image build context, listing only the owner's change
    assert len(box.docker.builds()) == 2
    m = box.docker.manifests[-1]
    assert m["name"] == "Owner's release" and m["build"] == head[:8]
    assert m["base"]["label"].startswith("Latest ") and m["base"]["ref"] == git(box.src, "rev-parse", "--short=8",
                                                                                  "refs/wam-release/upstream")
    assert [c["title"] for c in m["changes"]] == ["Owner: my tweak"] and m["changes"][0]["status"] == "Mine"


def test_auto_follows_the_owners_mode(box):
    box.init()
    _owner_change(box)
    box.run("rebuild")
    _upstream_change(box)
    r = box.run("auto")                                  # Ask me: check only
    assert r["state"] == "checked" and r["behind"] == 1 and len(box.docker.builds()) == 1
    (box.home / "updates.json").write_text(json.dumps({"mode": "auto"}))
    r = box.run("auto")                                  # Automatic: update
    assert r["state"] == "ok" and len(box.docker.builds()) == 2


# ---------------------------------------------------------------- clashes

def _clash(box):
    box.init()
    _owner_change(box, "web/app.js", "line 1\nOWNER\nline 3\n", "Owner: change line 2")
    box.run("rebuild")
    up = _upstream_change(box, "web/app.js", "line 1\nUPSTREAM\nline 3\n", "upstream: change line 2")
    return up


def test_clash_resolved_by_a_resolver_is_accepted_after_the_checks(box):
    up = _clash(box)
    seen = {}

    def resolver(w, env, prompt):
        seen.update(env=env, prompt=prompt)
        (w / "web/app.js").write_text("line 1\nOWNER\nUPSTREAM\nline 3\n")
        git(w, "add", "-A")
        git(w, "commit", "-q", "--no-edit", "-m", env["WAM_RELEASE_MESSAGE"])
    r = box.run("update", resolver=resolver)
    assert r["state"] == "ok", r
    assert "web/app.js" in seen["env"]["WAM_RELEASE_FILES"] and "Owner's release" in seen["prompt"]
    head = git(box.src, "rev-parse", "HEAD")
    assert subprocess.run(["git", "-C", str(box.src), "merge-base", "--is-ancestor", up, head]).returncode == 0
    assert (box.src / "web/app.js").read_text() == "line 1\nOWNER\nUPSTREAM\nline 3\n"
    assert box.posts == []


@pytest.mark.parametrize("bad", ["markers", "drops_upstream", "uncommitted", "breaks_tests"])
def test_a_resolver_result_that_fails_a_check_is_rejected(box, bad):
    _clash(box)
    before = git(box.src, "rev-parse", "HEAD")

    def resolver(w, env, prompt):
        if bad == "markers":
            (w / "web/app.js").write_text("<<<<<<< HEAD\nOWNER\n=======\nUPSTREAM\n>>>>>>> x\n")
        elif bad == "drops_upstream":
            git(w, "merge", "--abort")
            commit(w, "web/app.js", "line 1\nOWNER only\nline 3\n", "fake resolve")
            return
        elif bad == "breaks_tests":
            (w / "web/app.js").write_text("line 1\nOWNER\nUPSTREAM\nline 3\n")
            (w / "tests/ok").unlink()
        else:
            (w / "web/app.js").write_text("ok\n")
            git(w, "add", "-A")
            git(w, "commit", "-q", "--no-edit", "-m", "m")
            (w / "web/app.js").write_text("edited after\n")
            return
        git(w, "add", "-A")
        git(w, "commit", "-q", "--no-edit", "-m", "m")
    r = box.run("update", resolver=resolver)
    assert r["state"] == "conflict"
    assert git(box.src, "rev-parse", "HEAD") == before                     # the owner's branch is untouched
    assert len(box.docker.builds()) == 1                                   # nothing new was built


def test_unresolved_clash_keeps_the_build_reports_conflict_and_notifies_once(box):
    up = _clash(box)
    built = json.loads((box.home / "release/built.json").read_text())
    r = box.run("update")                                                  # resolver "none"
    assert r["state"] == "conflict" and r["notified"] is True
    rel = updates.status(box.home)["release"]
    assert rel["failed"].startswith("CONFLICT a change of yours clashes with Latest " + up[:8])
    assert rel["build"] == built["build"] and rel["behind"] == 1
    assert json.loads((box.home / "release/built.json").read_text()) == built
    assert len(box.posts) == 1
    url, doc = box.posts[0]
    assert url == "http://127.0.0.1:18790/" and doc["priority"] == 4 and "web/app.js" in doc["message"]
    assert box.run("update")["notified"] is False and len(box.posts) == 1  # once per upstream commit
    _upstream_change(box, "web/other.js", "x\n", "upstream: another")
    assert box.run("update")["notified"] is True and len(box.posts) == 2   # a new upstream commit: tell again


def test_no_notifications_app_means_no_post(box):
    _clash(box)
    apps = json.loads((box.home / "apps.json").read_text())
    apps["apps"] = [a for a in apps["apps"] if a["id"] != "notifications"]
    (box.home / "apps.json").write_text(json.dumps(apps))
    r = box.run("update")
    assert r["state"] == "conflict" and r["notified"] is False and box.posts == []


def test_resolver_command_default_is_hermes_when_installed(box, monkeypatch):
    box.init()
    c = release.ctx(box.home)
    monkeypatch.setattr(release.shutil, "which", lambda n: "/usr/bin/hermes" if n == "hermes" else None)
    c.cfg["resolver"] = "auto"
    assert release.resolver_argv(c) == ["/usr/bin/hermes", "-z"]
    monkeypatch.setattr(release.shutil, "which", lambda n: None)
    assert release.resolver_argv(c) is None
    c.cfg["resolver"] = "none"
    assert release.resolver_argv(c) is None
    c.cfg["resolver"] = "my-agent --fix"
    assert release.resolver_argv(c) == ["bash", "-c", "my-agent --fix"]


def test_resolver_command_runs_in_the_worktree(box):
    _clash(box)
    script = box.tmp / "resolve.sh"
    script.write_text('#!/bin/sh\ncd "$WAM_RELEASE_WORKTREE"\nprintf "line 1\\nOWNER\\nUPSTREAM\\nline 3\\n" > web/app.js\n'
                      'git add -A && git -c user.name=R -c user.email=r@example.invalid commit -q --no-edit '
                      '-m "$WAM_RELEASE_MESSAGE"\n')
    script.chmod(0o755)
    cfg = json.loads((box.home / "my-release.json").read_text())
    (box.home / "my-release.json").write_text(json.dumps({**cfg, "resolver": str(script)}))
    assert box.run("update")["state"] == "ok"


# ---------------------------------------------------------------- tests, builds, rollback

def test_failing_tests_keep_the_running_build(box):
    box.init()
    _owner_change(box)
    box.run("rebuild")
    built = json.loads((box.home / "release/built.json").read_text())
    ov = box.override.read_text()
    git(box.src, "rm", "-q", "tests/ok"); git(box.src, "commit", "-q", "-m", "Owner: oops")
    r = box.run("rebuild")
    assert r["state"] == "failed" and r["status"].startswith("FAIL tests on")
    assert box.override.read_text() == ov and json.loads((box.home / "release/built.json").read_text()) == built
    assert len(box.docker.builds()) == 1
    assert updates.status(box.home)["release"]["failed"].startswith("FAIL tests")


def test_only_committed_work_is_built_and_a_dirty_checkout_refuses(box):
    box.init()
    (box.src / "web/app.js").write_text("uncommitted\n")
    r = box.run("rebuild")
    assert r["state"] == "failed" and "uncommitted edits" in r["status"] and box.docker.calls == []


def test_image_build_failure_keeps_the_running_build(box):
    box.init()
    _owner_change(box)
    ov = box.override.read_text()
    box.docker.build_ok = False
    r = box.run("rebuild")
    assert r["state"] == "failed" and "image build" in r["status"] and box.override.read_text() == ov
    assert box.docker.ups() == []


def test_rollback_when_the_new_shell_does_not_come_up(box):
    box.init()
    _owner_change(box)
    ov = box.override.read_text()
    box.served["follow"] = False                     # the recreated container never serves the new build
    r = box.run("rebuild")
    assert r["state"] == "rolled_back" and "rolled back to ghcr.io/example/wizard-ux:sha-1" in r["status"]
    assert box.override.read_text() == ov            # byte for byte
    assert len(box.docker.ups()) == 2                # up with the new image, then up again with the old one
    assert not (box.home / "release/built.json").exists()
    assert release.enabled(box.home) is False


def test_one_run_at_a_time(box):
    box.init()
    p = release.paths(box.home)
    with release._locked(p) as got:
        assert got
        assert box.run("update") == {"state": "busy", "note": "a release run is already going"}


def test_push_is_off_by_default_and_opt_in(box):
    box.init()
    _owner_change(box)
    assert "pushed" not in box.run("rebuild")
    mirror = box.tmp / "mine.git"
    subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True)
    git(box.src, "remote", "add", "mine", str(mirror))
    box.init(push="mine")
    _owner_change(box, "web/two.js", "2\n", "Owner: second")
    assert box.run("rebuild")["pushed"] is True
    assert git(mirror, "rev-parse", "release/mine") == git(box.src, "rev-parse", "HEAD")


def test_off_restores_the_shell_and_keeps_the_branch(box, tmp_path):
    box.init()
    _owner_change(box)
    box.run("rebuild")
    calls = []
    res = release.off(box.home, unit_dir=tmp_path / "u", systemctl=False, run=lambda a, **k: calls.append(a) or
                      subprocess.CompletedProcess(a, 0, "", ""))
    assert res["state"] == "off" and res["shell_restored"] is True
    assert "ghcr.io/example/wizard-ux:sha-1" in box.override.read_text()
    assert release.enabled(box.home) is False and git(box.src, "branch", "--show-current") == "release/mine"


def test_with_image_rewrites_only_the_shell_service():
    text = "services:\n  other:\n    image: a\n  wizard-ux:\n    image: b\n    pull_policy: missing\n    ports: []\n"
    out = release.with_image(text, "wizard-ux", "wizard-ux:release-abc")
    assert "  other:\n    image: a\n" in out
    assert "    image: wizard-ux:release-abc\n    pull_policy: never\n    ports: []" in out
    out = release.with_image("services:\n  wizard-ux:\n    ports: []\n", "wizard-ux", "x:y")
    assert out.endswith("    ports: []\n    image: x:y\n    pull_policy: never\n")
    assert "image: x:y" in release.with_image(None, "wizard-ux", "x:y")


def test_stable_channel_reads_the_stable_release_pin(tmp_path):
    rel = tmp_path / "rel"
    rel.mkdir()
    (rel / "stable").write_text("0.3.9\n")
    (rel / "0.3.9.json").write_text(json.dumps({"components": {"wizard-ux": {
        "repo": "example/wizard-ux", "commit": "a" * 40}}}))
    assert release.stable_component("wizard-ux", rel.as_uri()) == {"version": "0.3.9", "repo": "example/wizard-ux",
                                                                    "commit": "a" * 40}
    assert release.source_url("wizard-ux", rel.as_uri()) == "https://github.com/example/wizard-ux"
    assert release.stable_component("wizard-ux", (tmp_path / "none").as_uri()) is None


# ---------------------------------------------------------------- the updates contract (Settings > Updates)

def _release_box(h, line="2026-10-24T04:03:45Z OK Owner's release 26b1422a on Latest 2ebbb689 · 9 changes",
                 behind=3):
    d = h / "release"
    d.mkdir(parents=True, exist_ok=True)
    (h / "my-release.json").write_text(json.dumps({"name": "Owner's release", "channel": "latest"}))
    (d / "status").write_text(line + "\n")
    (d / "pending.json").write_text(json.dumps({
        "behind": behind, "latest": "abc12345", "checked_at": "2026-10-24T05:00:00Z",
        "new": [{"sha": "abc12345", "title": "Newest on main"}, {"sha": "def67890", "title": "Older on main"}][:behind]}))
    (d / "built.json").write_text(json.dumps({"base": "2ebbb689", "build": "26b1422a"}))
    return d


def _dead_pid():
    p = subprocess.Popen(["true"]); p.wait()
    return p.pid


def _ok_runner(calls):
    return lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")


def _no_network(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("a release box never reads the release pointer")
    monkeypatch.setattr(updates.urllib.request, "urlopen", boom)


def test_release_contract_fields(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    h = tmp_path / "wos"
    _release_box(h)
    (h / "updates.json").write_text(json.dumps({"mode": "ask", "channel": "latest"}))
    st = updates.status(h)
    assert st["release"] == {"name": "Owner's release", "base": "2ebbb689", "build": "26b1422a", "behind": 3,
                             "new": [{"sha": "abc12345", "title": "Newest on main"},
                                     {"sha": "def67890", "title": "Older on main"}],
                             "building": False, "failed": None, "checked_at": "2026-10-24T05:00:00Z"}
    assert st["update_available"] is True and st["unpinned"] is False and st["latest"] == "abc12345"
    assert st["mode"] == "ask" and st["installed"] == "26b1422a" and st["checked"] is True
    cfg = json.loads((h / "my-release.json").read_text())
    (h / "my-release.json").write_text(json.dumps({**cfg, "name": " "}))
    assert updates.status(h)["release"]["name"] == "My release"


def test_release_contract_building_and_failures(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    h = tmp_path / "wos"
    d = _release_box(h, behind=0)
    st = updates.status(h)
    assert st["update_available"] is False and st["release"]["new"] == []
    d = _release_box(h, behind=2)
    (d / "building").write_text(json.dumps({"pid": os.getpid()}))
    st = updates.status(h)
    assert st["release"]["building"] is True and st["update_available"] is False and st["installing"] is True
    (d / "building").write_text(json.dumps({"pid": _dead_pid()}))       # a killed run left its marker
    assert updates.status(h)["release"]["building"] is False
    (d / "building").unlink()
    (d / "status").write_text("2026-10-24T06:00:00Z CONFLICT a change of yours clashes with Latest abc12345\n")
    rel = updates.status(h)["release"]
    assert rel["failed"].startswith("CONFLICT a change of yours") and rel["build"] == "26b1422a"


def test_release_apply_starts_the_unit_or_a_detached_run(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    h = tmp_path / "wos"
    d = _release_box(h, behind=2)
    calls, spawned = [], []
    res = updates.apply(h, runner=_ok_runner(calls), spawn=spawned.append)
    assert res["started"] is True and res["via"] == "systemd" and spawned == []
    assert calls == [["systemctl", "--user", "start", "--no-block", "wam-release-update.service"]]
    fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 5, "", "Unit not found")
    res = updates.apply(h, runner=fail, spawn=spawned.append)
    assert res["started"] is True and res["via"] == "detached" and spawned == [h]
    (d / "building").write_text(json.dumps({"pid": os.getpid()}))
    calls.clear()
    assert updates.apply(h, runner=_ok_runner(calls))["started"] is False and calls == []
    (d / "building").unlink()
    _release_box(h, behind=0)
    assert updates.apply(h, runner=_ok_runner(calls))["started"] is False and calls == []
    (h / "updates.json").write_text(json.dumps({"mode": "auto"}))
    _release_box(h, behind=2)
    res = updates.apply_if_auto(h, runner=_ok_runner(calls), is_busy=lambda: False)
    assert res["started"] is False and calls == []                       # the release timer does it


def test_stock_box_is_unchanged(tmp_path):
    h = tmp_path / "wos"
    h.mkdir()
    (h / "release.json").write_text(json.dumps({"release": "0.3.7", "status": "pinned"}))
    rel = tmp_path / "rel"; rel.mkdir(); (rel / "stable").write_text("0.3.8\n")
    (h / "my-release.json").write_text(json.dumps({"name": "x"}))    # set up but never built: still stock
    updates._latest.clear()
    st = updates.status(h, rel.as_uri())
    assert "release" not in st and st["update_available"] is True and st["installed"] == "0.3.7"
    calls = []
    assert updates.apply(h, rel.as_uri(), _ok_runner(calls))["started"] is True
    assert calls[0][0] == "bash" and "WIZARD_RELEASE=0.3.8" in calls[0][2]
    updates._latest.clear()


def test_updates_http_route_carries_the_release(tmp_path, monkeypatch):
    """The shell's Settings > Updates (StoreHandler /_updates) gets `release` and Update starts the release unit."""
    import io
    from wam import cli, store_install as si
    _no_network(monkeypatch)
    h = tmp_path / "wos"
    _release_box(h, behind=2)
    (h / "links.json").write_text(json.dumps({"host": "box.ts.net"}))
    monkeypatch.setenv("WIZARD_OS_HOME", str(h))
    monkeypatch.setattr(si, "peer_uid", lambda *a, **k: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "owner@example.com")
    monkeypatch.setattr(si, "served_origins", lambda host: {"https://box.ts.net:8444"})
    real_apply, units = updates.apply, []        # never the box's real systemctl
    monkeypatch.setattr(updates, "apply", lambda *a, **k: real_apply(*a, runner=_ok_runner(units),
                                                                     spawn=lambda h: None, **k))

    class C:
        home = h
    sent = []

    def call(method, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        o = cli.StoreHandler.__new__(cli.StoreHandler)
        o.cfg, o.path, o.client_address = C(), "/_updates", ("127.0.0.1", 5555)
        o.headers = {"Tailscale-User-Login": "owner@example.com", "Origin": "https://box.ts.net:8444",
                     "X-Wizard-Store": "1", "Content-Type": "application/json", "Content-Length": str(len(raw))}
        o.rfile = io.BytesIO(raw)
        o._json = lambda code, b: sent.append((code, b))
        o._go(method)
        return sent[-1]
    code, b = call("GET")
    assert code == 200 and b["release"]["name"] == "Owner's release" and b["update_available"] is True
    code, b = call("POST", {"action": "apply"})
    assert code == 200 and b["started"] is True and b["via"] == "systemd"
    assert units == [["systemctl", "--user", "start", "--no-block", "wam-release-update.service"]]
