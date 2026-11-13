"""Automatic app updates (wam/app_updates.py): what is due, how each update is verified, rollback, the timer."""
import json
from pathlib import Path

from wam import app_updates as au


def row(app_id, have, latest, status="running", slug="wizardapps", ignored=None, docker="1.2.3"):
    return {"app": {"appName": app_id, "version": have, "status": status, "appStoreSlug": slug,
                    "ignoredVersion": ignored},
            "metadata": {"latestVersion": latest, "latestDockerVersion": docker}}


def test_candidates_skip_system_pinned_held_failed_ignored_and_busy(tmp_path):
    rt_root = tmp_path / "runtipi"
    pinned = rt_root / "repos/wizardapps/apps/pinned-app"
    pinned.mkdir(parents=True)
    (pinned / ".wizard-selection").write_text("{}")
    home = tmp_path / "home"
    au.hold("held-app", home=home)
    import time
    au._write(home, failed={"broken-app": {"to": 5, "at": int(time.time())}, "stale-fail": {"to": 2, "at": 1}})
    rows = [row("money", 21, 22), row("same", 3, 3), row("wizard-ux", 1, 9), row("hermes-agent", 1, 2),
            row("pinned-app", 1, 2), row("held-app", 1, 2), row("broken-app", 4, 5), row("broken-again", 4, 6),
            row("ignored-app", 1, 2, ignored=2), row("mid-update", 1, 2, status="updating"),
            row("other-store", 1, 2, slug="other"), row("release-default", 1, 2), row("stale-fail", 1, 2)]
    got = {r["id"]: r["skip"] for r in au.candidates(rows, store="wizardapps", runtipi_root=rt_root, home=home,
                                                         release_managed={"release-default"})}
    assert got == {"money": None, "wizard-ux": "system", "hermes-agent": "system", "pinned-app": "pinned",
                   "held-app": "held", "broken-app": "failed", "broken-again": None, "ignored-app": "ignored",
                   "mid-update": "busy:updating", "release-default": "system", "stale-fail": None}


def test_mode_defaults_to_auto_and_timer_follows_it(tmp_path):
    home, ud = tmp_path / "home", tmp_path / "units"
    assert au.mode(home) == "auto"
    assert au.ensure_timer(home, ud, systemctl=False) == {"app_mode": "auto", "app_timer": True}
    service = (ud / "wizard-app-updates.service").read_text()
    assert "app-updates --apply-if-auto" in service and (ud / "wizard-app-updates.timer").is_file()
    assert au.set_mode("ask", home, ud, systemctl=False)["app_timer"] is False
    assert not (ud / "wizard-app-updates.timer").exists()
    assert json.loads((home / "app-updates.json").read_text())["mode"] == "ask"


def test_os_update_mode_and_app_update_mode_are_independent(tmp_path, monkeypatch):
    from wam import updates
    home = tmp_path / "home"
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    out = updates.ensure_timer(home, None, systemctl=False)
    assert out["mode"] == "ask" and out["timer"] is False          # WizardOS releases: Ask me by default
    assert out["app_mode"] == "auto" and out["app_timer"] is True   # app updates: Automatic by default
    assert (tmp_path / ".config/systemd/user/wizard-app-updates.timer").is_file()


BEFORE = {"id": "money", "from": 21, "to": 22, "status": "running", "running": 2}


def _verify(res, states=("running", "running"), probe_ok=True, ep=("http://127.0.0.1:1/mcp", True)):
    calls = []
    return au.verify(None, None, "money:wizardapps", "money", res, BEFORE, sleep=lambda s: None,
                     containers=lambda urn: list(states), probe=lambda u, m, t: calls.append(u) or probe_ok,
                     endpoint=lambda c, rt, a: ep, probe_seconds=0), calls


def test_verify_passes_a_working_update():
    problem, calls = _verify({"status": "running", "version": 22})
    assert problem is None and calls == ["http://127.0.0.1:1/mcp"]


def test_verify_catches_every_way_an_update_fails():
    assert "rolled back" in _verify({"status": "running", "version": 21})[0]
    assert "came back stopped" in _verify({"status": "stopped", "version": 22})[0]
    assert "restarting" in _verify({"status": "running", "version": 22}, states=("running", "restarting"))[0]
    assert "1 of 2" in _verify({"status": "running", "version": 22}, states=("running", "exited"))[0]
    assert "MCP endpoint" in _verify({"status": "running", "version": 22}, probe_ok=False)[0]
    assert "did not come back" in _verify({"status": "stopped", "version": 22,
                                           "error": "the app did not come back"})[0]
    # no declared endpoint: containers decide
    assert _verify({"status": "running", "version": 22}, ep=None)[0] is None


def test_verify_ignores_an_endpoint_that_did_not_answer_before_the_update_either():
    # morning-report on a fresh 0.3.73 box: /healthz answers 403 "Unknown host." from loopback before and after
    # the update; requiring it rolled back every working update of that app.
    before = {**BEFORE, "endpoint_ok": False}
    problem = au.verify(None, None, "money:wizardapps", "money", {"status": "running", "version": 22}, before,
                        sleep=lambda s: None, containers=lambda urn: ["running", "running"],
                        probe=lambda u, m, t: False, endpoint=lambda c, rt, a: ("http://127.0.0.1:1/healthz", False),
                        probe_seconds=0)
    assert problem is None
    # containers still decide
    assert "restarting" in au.verify(None, None, "money:wizardapps", "money", {"status": "running", "version": 22},
                                     before, sleep=lambda s: None, containers=lambda urn: ["running", "restarting"],
                                     probe=lambda u, m, t: False, endpoint=lambda c, rt, a: None, probe_seconds=0)
    # an endpoint that answered before must answer after
    assert "health check fails" in au.verify(
        None, None, "money:wizardapps", "money", {"status": "running", "version": 22}, {**BEFORE, "endpoint_ok": True},
        sleep=lambda s: None, containers=lambda urn: ["running", "running"], probe=lambda u, m, t: False,
        endpoint=lambda c, rt, a: ("http://127.0.0.1:1/healthz", False), probe_seconds=0)


def test_baseline_probes_only_running_apps_with_an_endpoint():
    ep = lambda c, rt, a: ("http://127.0.0.1:1/healthz", False)  # noqa: E731
    assert au.baseline(None, None, "money", "running", endpoint=ep, probe=lambda u, m, t: False) is False
    assert au.baseline(None, None, "money", "running", endpoint=ep, probe=lambda u, m, t: True) is True
    assert au.baseline(None, None, "money", "stopped", endpoint=ep, probe=lambda u, m, t: True) is None
    assert au.baseline(None, None, "money", "running", endpoint=lambda c, rt, a: None, probe=lambda u, m, t: True) is None


class FakeRt:
    def __init__(self, backups):
        self.backups, self.restored, self.started = backups, [], []
        self.version = 22

    def list_backups(self, urn):
        return self.backups

    def restore_backup(self, urn, name):
        self.restored.append(name)
        self.version = 21

    def wait_status(self, urn, want, timeout):
        return "running"

    def start(self, urn):
        self.started.append(urn)

    def app(self, urn):
        return {"app": {"version": self.version}}


def test_rollback_restores_the_backup_this_update_made(monkeypatch):
    from wam import cli
    monkeypatch.setattr(au.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "network_step", lambda c, rt: {})
    rt = FakeRt([{"id": "new.tar.gz", "date": 1_000_000}, {"id": "old.tar.gz", "date": 1}])
    assert au.rollback(None, rt, "money:wizardapps", 1_000_000, BEFORE) == {
        "rolled_back": True, "status": "running", "version": 21}
    assert rt.restored == ["new.tar.gz"]
    stale = FakeRt([{"id": "old.tar.gz", "date": 1}])
    assert au.rollback(None, stale, "money:wizardapps", 1_000_000, BEFORE)["rolled_back"] is False
    assert stale.restored == []


def test_summary_tells_the_owner_plainly():
    title, msg, prio = au._summary([{"id": "money", "to": 22, "to_version": "0.8.1"}], [], [], [])
    assert title == "1 app updated" and msg == "Updated money (0.8.1)." and prio == "default"
    title, msg, prio = au._summary([], [{"id": "wisdom"}], [], [{"id": "files", "reason": "REFUSED: host access"}])
    assert title == "App updates need a look" and "Kept the previous version of wisdom" in msg and "files" in msg
    assert au._summary([], [], [], []) is None


def test_notice_names_apps_as_the_owner_sees_them(tmp_path):
    """James: "Why are we still calling this CineQueue?" The notice said "Updated cinequeue (0.2.2)" while the app
    is called Movies everywhere he looks. The notice uses the store recipe's display name; the id is only a fallback."""
    recipe = tmp_path / "repos" / "wizardapps" / "apps" / "cinequeue"
    recipe.mkdir(parents=True)
    (recipe / "config.json").write_text(json.dumps({"id": "cinequeue", "name": "Movies"}))
    rows = [{"app": {"appName": "cinequeue", "appStoreSlug": "wizardapps", "version": 7, "status": "running"},
             "metadata": {"latestVersion": 8, "latestDockerVersion": "0.2.2"}},
            {"app": {"appName": "norecipe", "appStoreSlug": "wizardapps", "version": 1, "status": "running"},
             "metadata": {"latestVersion": 2, "latestDockerVersion": "1.0"}}]
    got = au.candidates(rows, store="wizardapps", runtipi_root=tmp_path, home=tmp_path / "h", release_managed=set())
    assert {r["id"]: r["name"] for r in got} == {"cinequeue": "Movies", "norecipe": "norecipe"}
    title, msg, _ = au._summary(got, [], [], [])
    assert title == "2 apps updated" and msg == "Updated Movies (0.2.2), norecipe (1.0)."
    assert "cinequeue" not in msg
    _, msg, _ = au._summary([], [got[0]], [{**got[0], "reason": "x"}], [{**got[0], "reason": "y"}])
    assert "cinequeue" not in msg and msg.count("Movies") == 3


def test_apply_if_auto_respects_ask_and_busy(tmp_path):
    class C:
        home = tmp_path
    au._write(tmp_path, mode="ask")
    assert au.apply_if_auto(C())["started"] is False
    au._write(tmp_path, mode="auto")
    assert au.apply_if_auto(C(), is_busy=lambda: True)["skipped"] == "busy"


def test_manual_update_button_runs_in_the_background():
    seen = []

    class P:
        returncode, stdout, stderr = 0, "", ""
    assert au.start_detached(lambda cmd, **k: seen.append(cmd) or P()) == {"started": True}
    assert seen[0][0] == "systemd-run" and seen[0][-2:] == ["app-updates", "--apply"]


def test_new_required_settings_are_filled_before_the_update(tmp_path, monkeypatch):
    """wizard-claude 0.2.x added a required shared password; Runtipi refused the update ("Variable ... is required")."""
    from wam import cli
    recipe = tmp_path / "repos/wizardapps/apps/wizard-claude"
    recipe.mkdir(parents=True)
    (recipe / "config.json").write_text(json.dumps({"form_fields": [
        {"env_variable": "OLD", "required": True, "type": "text"},
        {"env_variable": "DASH_PW", "required": True, "type": "password"}]}))
    (recipe / "wizard.md").write_text("---\nid: wizard-claude\nuses: [hermes-agent]\n---\n")

    class C:
        runtipi_root = tmp_path
        secrets = tmp_path / "secrets"

    class Rt:
        patched = []

        def app(self, urn):
            return {"app": {"config": {"OLD": "kept", "port": 1}}}

        def req(self, method, path, body, timeout=0):
            self.patched.append((path, body))

    monkeypatch.setattr(cli, "shared_value", lambda c, var, uses: "shared-pw" if var == "DASH_PW" else None)
    rt = Rt()
    assert au.reconcile_settings(C(), rt, "wizard-claude") == []
    path, body = rt.patched[0]
    assert path.endswith("wizard-claude:wizardapps/update-config")
    assert body["DASH_PW"] == "shared-pw" and body["OLD"] == "kept" and body["port"] == 1
    # nothing the box can fill: the owner is asked and nothing is patched
    (recipe / "config.json").write_text(json.dumps({"form_fields": [
        {"env_variable": "API_KEY", "required": True, "type": "text", "label": "API key"}]}))
    rt.patched.clear()
    assert au.reconcile_settings(C(), rt, "wizard-claude")[0].startswith("API_KEY")
    assert rt.patched == []


def _store_pin(rt_root, app_id, *, source="standard", pin="a" * 40, installed="a" * 40, repo="org/app"):
    store = rt_root / "repos/wizardapps/apps" / app_id
    live = rt_root / "apps/wizardapps" / app_id
    store.mkdir(parents=True)
    (store / ".wizard-selection").write_text(json.dumps({"repo": repo, "commit": pin}) + "\n")
    if installed:
        live.mkdir(parents=True)
        (live / ".wizard-source").write_text(json.dumps({"repo": repo, "source": source}) + "\n")
        (live / ".wizard-commit").write_text(installed + "\n")
    return store / ".wizard-selection"


def test_standard_store_pin_is_dropped_once_installed_and_real_pins_stay(tmp_path, monkeypatch):
    """wizard-os#614: a Store Get/Update of the Standard release pinned the app forever (the index store never
    looked again, so automatic updates never saw a new version). Only that pin goes; every real pin stays."""
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    rt = tmp_path / "runtipi"
    pins = {
        "notes": _store_pin(rt, "notes"),                                        # Standard Get done: dropped
        "private-app": _store_pin(rt, "private-app", source="owner-private"),   # owner-local exact choice
        "linked": _store_pin(rt, "linked", source="link"),                      # link source
        "channel": _store_pin(rt, "channel", source="alpha-test"),              # subscribed channel
        "mid-update": _store_pin(rt, "mid-update", installed="b" * 40),         # update still running
        "mid-install": _store_pin(rt, "mid-install", installed=None),           # not installed yet
        "files": _store_pin(rt, "files"),                                       # a release default
        "hermes-agent": _store_pin(rt, "hermes-agent"),                         # frozen Hermes lock
    }
    monkeypatch.setattr(au, "_release_managed_strict", lambda: {"files"})
    installed = [row(k, 1, 1) for k in pins if k != "mid-install"]
    assert au.unpin_standard_all(rt, "wizardapps", installed) == ["notes"]
    assert {k for k, p in pins.items() if not p.exists()} == {"notes"}
    assert au.unpin_standard_all(rt, "wizardapps", installed) == []   # idempotent


def test_store_pin_holds_while_its_update_is_still_running(tmp_path, monkeypatch):
    """Runtipi copies the store folder twice during an update; the installed commit can already equal the pin
    while the app is still updating. The pin goes only once Runtipi reports the app at rest."""
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    rt = tmp_path / "runtipi"
    pin = _store_pin(rt, "notes")
    for status in ("updating", "installing", "missing"):
        assert au.unpin_standard_all(rt, "wizardapps", [row("notes", 1, 1, status=status)]) == []
    assert pin.exists()
    assert au.unpin_standard_all(rt, "wizardapps", [row("notes", 1, 1, status="stopped")]) == ["notes"]


def test_store_pins_stay_when_the_release_file_cannot_be_read(tmp_path, monkeypatch):
    rt = tmp_path / "runtipi"
    pin = _store_pin(rt, "notes")
    monkeypatch.setattr(au, "_release_managed_strict", lambda: None)
    assert au.unpin_standard_all(rt, "wizardapps", [row("notes", 1, 1)]) == [] and pin.exists()


def test_pinned_app_is_reported_even_without_a_newer_version(tmp_path):
    """A real pin stops the index store from showing a newer version, so `wam app-updates` lists it as pinned
    instead of leaving it out (wizard-os#614). Unpinned, system and release apps without one stay out."""
    rt = tmp_path / "runtipi"
    for app_id in ("pinned-app", "hermes-agent", "release-default"):
        _store_pin(rt, app_id, source="owner-private")
    rows = [row("pinned-app", 4, 4), row("plain", 3, 3), row("hermes-agent", 2, 2), row("release-default", 1, 1)]
    got = au.candidates(rows, store="wizardapps", runtipi_root=rt, home=tmp_path / "home",
                        release_managed={"release-default"})
    assert [(r["id"], r["from"], r["to"], r["skip"]) for r in got] == [("pinned-app", 4, 4, "pinned")]


def test_status_drops_a_standard_pin_and_asks_runtipi_again(tmp_path, monkeypatch):
    """The check itself frees an app a Store Get pinned earlier, then asks again so the newer version shows."""
    from types import SimpleNamespace
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    rt_root = tmp_path / "runtipi"
    pin = _store_pin(rt_root, "notes")
    seen = []

    class RT:
        def installed(self):
            # Runtipi's index store reads the index again only while no pin is there
            seen.append(pin.exists())
            return [row("notes", 1, 1 if pin.exists() else 2)]

    c = SimpleNamespace(runtipi_root=rt_root, home=tmp_path / "home", runtipi=lambda: RT())
    out = au.status(c)
    assert seen == [True, False] and [r["id"] for r in out["due"]] == ["notes"]
