"""Settings > About & updates > Apps (wizard-ux#1191): the owner-only `_app-updates` route on the store listener.

Off by default: while the feature switch wizard/app-updates-list is off the route answers 404 and changes nothing.
"""
import io
import json
import time

from wam import app_updates as au


def _installed():
    def row(app_id, have, latest, status="running", ignored=None):
        return {"app": {"appName": app_id, "appStoreSlug": "wizardapps", "version": have, "status": status,
                        "ignoredVersion": ignored}, "metadata": {"latestVersion": latest}}
    return [row("messenger", 93, 97), row("movies", 4, 5), row("notes", 2, 3), row("wizard-ux", 1, 2)]


class _Rt:
    def installed(self):
        return _installed()


def _cfg(tmp_path):
    class C:
        home = tmp_path / "wos"
        runtipi_root = tmp_path / "tipi"

        def runtipi(self):
            return _Rt()
    C.home.mkdir(parents=True, exist_ok=True)
    (C.home / "links.json").write_text(json.dumps({"host": "box.ts.net"}))
    return C()


def _state(c, **doc):
    (c.home / au.STATE).write_text(json.dumps(doc))


def test_owner_status_adds_failed_reason_and_last_run(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "_release_managed", lambda: set())
    c = _cfg(tmp_path)
    now = int(time.time())
    _state(c, mode="ask", holds=["notes"],
           failed={"messenger": {"to": 97, "at": now, "reason": "a container keeps restarting"},
                   "bad": "not a dict"},
           last={"at": "2026-11-09T04:31:00Z", "updated": ["movies"], "rolled_back": ["messenger"], "failed": []})
    s = au.owner_status(c)
    assert s["app_mode"] == "ask" and s["holds"] == ["notes"]
    assert s["failed"] == {"messenger": {"to": 97, "at": now, "reason": "a container keeps restarting"}}
    assert s["last"]["at"] == "2026-11-09T04:31:00Z" and s["last"]["rolled_back"] == ["messenger"]
    skip = {r["id"]: r["skip"] for r in s["skipped"]}
    assert skip == {"messenger": "failed", "notes": "held", "wizard-ux": "system"}
    assert [r["id"] for r in s["due"]] == ["movies"]


def test_owner_status_with_no_state_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "_release_managed", lambda: set())
    c = _cfg(tmp_path)
    _state(c, last="garbage")
    s = au.owner_status(c)
    assert s["app_mode"] == au.DEFAULT_MODE and s["failed"] == {} and s["last"] is None


def test_start_detached_can_name_one_app():
    seen = []

    class P:
        returncode, stdout, stderr = 0, "", ""
    assert au.start_detached(lambda cmd, **k: seen.append(cmd) or P(), only=["movies"]) == {"started": True}
    assert seen[-1][-3:] == ["app-updates", "--apply", "movies"]
    au.start_detached(lambda cmd, **k: seen.append(cmd) or P())
    assert seen[-1][-2:] == ["app-updates", "--apply"]


def _route(tmp_path, monkeypatch, on):
    from wam import cli, store_install as si
    monkeypatch.setattr(au, "_release_managed", lambda: set())
    monkeypatch.setattr(si, "peer_uid", lambda *a, **k: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "owner@example.com")
    monkeypatch.setattr(si, "served_origins", lambda host: {"https://box.ts.net:8444"})
    monkeypatch.setattr(au, "switch_on", lambda home=None: on[0])
    calls = []
    monkeypatch.setattr(au, "start_detached", lambda runner=None, only=None: calls.append(("apply", only))
                        or {"started": True})
    monkeypatch.setattr(au, "set_mode", lambda m, home=None, *a, **k: calls.append(("mode", m))
                        or {"app_mode": m, "chosen": True})
    c = _cfg(tmp_path)
    sent = []

    def call(method, body=None, **hdr):
        hd = {"X-Forwarded-For": ".".join(["100", "64", "0", "9"]), "Tailscale-User-Login": "owner@example.com", "Origin": "https://box.ts.net:8444",
              "X-Wizard-Store": "1", "Content-Type": "application/json", **hdr}
        raw = json.dumps(body).encode() if body is not None else b""
        hd["Content-Length"] = str(len(raw))
        o = cli.StoreHandler.__new__(cli.StoreHandler)
        o.cfg, o.path, o.client_address = c, "/_app-updates", ("127.0.0.1", 5555)
        o.headers = {k: v for k, v in hd.items() if v is not None}
        o.rfile = io.BytesIO(raw)
        o._json = lambda code, b: sent.append((code, b))
        o._go(method)
        return sent[-1]
    return call, calls, c


def test_route_is_404_and_inert_while_the_switch_is_off(tmp_path, monkeypatch):
    on = [False]
    call, calls, c = _route(tmp_path, monkeypatch, on)
    assert call("GET")[0] == 404
    assert call("POST", {"action": "apply"})[0] == 404
    assert call("POST", {"hold": "movies"})[0] == 404
    assert calls == [] and not (c.home / au.STATE).exists()


def test_route_owner_only_and_actions(tmp_path, monkeypatch):
    on = [True]
    call, calls, c = _route(tmp_path, monkeypatch, on)
    code, b = call("GET")
    assert code == 200 and [r["id"] for r in b["due"]] == ["messenger", "movies", "notes"] and b["failed"] == {}
    assert call("GET", **{"Tailscale-User-Login": "intruder@example.com"})[0] == 403
    assert call("POST", {"hold": "movies"}, Origin="https://evil.example")[0] == 403
    assert call("POST", {"hold": "movies"}, **{"X-Wizard-Store": None})[0] == 403
    assert call("POST", {"hold": "movies"}) == (200, {"holds": ["movies"]})
    assert au.holds(c.home) == ["movies"]
    assert call("POST", {"unhold": "movies"}) == (200, {"holds": []})
    assert call("POST", {"hold": "../etc"})[0] == 400
    assert call("POST", {"hold": 5})[0] == 400
    assert call("POST", {"mode": "ask"})[0] == 200 and calls[-1] == ("mode", "ask")
    assert call("POST", {"mode": "never"})[0] == 400
    assert call("POST", {"action": "apply"})[0] == 200 and calls[-1] == ("apply", None)
    assert call("POST", {"action": "apply", "app": "messenger"})[0] == 200 and calls[-1] == ("apply", ["messenger"])
    assert call("POST", {"action": "apply", "app": "Messenger!"})[0] == 400
    assert call("POST", {"action": "apply", "force": True})[0] == 400
    assert call("POST", {})[0] == 400


def test_route_says_plainly_when_runtipi_cant_be_read(tmp_path, monkeypatch):
    call, calls, c = _route(tmp_path, monkeypatch, [True])

    def boom(*a, **k):
        raise ConnectionRefusedError("/".join(["", "home", "someone", "secret"]))
    monkeypatch.setattr(au, "owner_status", boom)
    code, b = call("GET")
    assert code == 503 and "someone" not in b["error"]


def test_switch_reads_the_shared_feature_switch(monkeypatch):
    from wam import feature_switches as fs
    seen = []
    monkeypatch.setattr(fs, "is_on", lambda tid, home=None: seen.append(tid) or False)
    assert au.switch_on() is False and seen == ["wizard/app-updates-list"]


def test_gateway_admits_the_app_updates_route():
    from wam import store_install as si
    assert si.gateway_path_ok("/_app-updates") and "_app-updates" in si.GATEWAY_CONTROL
    assert not si.gateway_path_ok("/_app-updates/x")


def _store_copy(c, app_id, tipi, front):
    d = c.runtipi_root / "repos" / "wizardapps" / "apps" / app_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"id": app_id, "tipi_version": tipi}))
    (d / "wizard.md").write_text(f"---\nid: {app_id}\n{front}---\n\n# {app_id}\n")


def test_owner_status_carries_one_line_whats_new_from_the_store_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "_release_managed", lambda: set())
    c = _cfg(tmp_path)
    _store_copy(c, "messenger", 97, "whats_new: |\n  Replies stay in their thread.\n  Faster start.\n")
    _store_copy(c, "movies", 5, "summary: Films\n")                       # no whats_new: no line
    _store_copy(c, "notes", 3, "whats_new: [not, a, string]\n")           # not text: no line
    _store_copy(c, "wizard-ux", 2, "whats_new: '" + "x" * 300 + "'\n")     # capped at one short line
    s = au.owner_status(c)
    assert s["notes"]["messenger"] == {"version": 97, "text": "Replies stay in their thread. Faster start."}
    assert "movies" not in s["notes"] and "notes" not in s["notes"]
    assert s["notes"]["wizard-ux"] == {"version": 2, "text": "x" * au.WHATS_NEW_MAX}


def test_whats_new_never_fails_on_broken_or_foreign_copies(tmp_path):
    root = tmp_path / "tipi"
    d = root / "repos" / "wizardapps" / "apps" / "broken"
    d.mkdir(parents=True)
    (d / "wizard.md").write_text("no front matter")
    (d / "config.json").write_text("{")
    rows = [{"app": {"appName": "broken", "appStoreSlug": "wizardapps"}},
            {"app": {"appName": "../etc", "appStoreSlug": "wizardapps"}},
            {"app": {"appName": "other", "appStoreSlug": "another-store"}},
            {"app": {"appName": "missing", "appStoreSlug": "wizardapps"}}, {}]
    assert au.whats_new(rows, store="wizardapps", runtipi_root=root) == {}


def test_owner_status_without_store_copies_has_empty_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "_release_managed", lambda: set())
    assert au.owner_status(_cfg(tmp_path))["notes"] == {}


def test_checker_rule_matches_the_spec(tmp_path):
    from wam import wizardmd
    assert wizardmd.whats_new_ok("Replies stay in their thread.") and wizardmd.whats_new_ok("x" * 140)
    for bad in ["", "   ", "x" * 141, "two\nlines", "tab\there", None, 3, ["a"], {"a": 1}]:
        assert not wizardmd.whats_new_ok(bad), bad
    (tmp_path / "wizard.md").write_text("---\nwizard: 0.2\nid: whats-new-test\ntype: service\nwhats_new: \"\"\n---\n")
    assert ("whats_new is one line of 1-140 chars", False) in [(n, ok) for n, ok, _ in wizardmd.check(tmp_path)]


def test_owner_status_links_the_failed_version_to_its_bug(tmp_path, monkeypatch):
    """wizard-ux#1191: a failed row links to the bug #524 filed or found; none while that switch is off."""
    monkeypatch.setattr(au, "_release_managed", lambda: set())
    c = _cfg(tmp_path)
    url = "https://github.com/humanitylabs-org/wizard-messenger/issues/12"
    _state(c, failed={"messenger": {"to": 97, "at": 1, "reason": "r", "bug": url},
                      "movies": {"to": 5, "at": 1, "reason": "r", "bug": "https://evil.test/issues/1"},
                      "notes": {"to": 3, "at": 1, "reason": "r"}})
    monkeypatch.setattr(au, "_reports_on", lambda: True)
    f = au.owner_status(c)["failed"]
    assert f["messenger"]["bug"] == url
    assert "bug" not in f["movies"] and "bug" not in f["notes"]
    monkeypatch.setattr(au, "_reports_on", lambda: False)
    assert all("bug" not in v for v in au.owner_status(c)["failed"].values())


def test_reports_switch_is_the_update_bug_reports_switch(monkeypatch):
    from wam import update_reports
    monkeypatch.setattr(update_reports, "switch_on", lambda: True)
    assert au._reports_on() is True
    monkeypatch.setattr(update_reports, "switch_on", lambda: False)
    assert au._reports_on() is False
