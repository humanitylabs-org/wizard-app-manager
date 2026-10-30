"""Store "Remove app": owner-only, exact installed identity, acknowledged consequences, readback-verified."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from wam import cli, store_install as si

C1, C2 = "a" * 40, "b" * 40


def _box(tmp_path, apps, *, installed=None, grants=(), external=None, identity=None):
    """A fake Wizard: apps.json, installed recipe identities and a Runtipi that tracks removals."""
    home, root = tmp_path / "wos", tmp_path / "runtipi"
    home.mkdir()
    reg = {"apps": apps, "grants": list(grants), **({"external": external} if external else {})}
    (home / "apps.json").write_text(json.dumps(reg))
    for a in apps:
        d = root / "apps" / cli.STORE / a["id"]
        d.mkdir(parents=True)
        ident = (identity or {}).get(a["id"], {"repo": a.get("repo"), "commit": a.get("commit")})
        if ident is not None:
            if ident.get("repo") is not None:
                (d / ".wizard-source").write_text(json.dumps({"repo": ident["repo"], "source": "standard"}))
            if ident.get("commit"):
                (d / ".wizard-commit").write_text(ident["commit"] + "\n")
    rt = SimpleNamespace(have=set(installed if installed is not None else [a["id"] for a in apps]), status={})
    return SimpleNamespace(home=home, runtipi_root=root, runtipi=lambda: rt), rt


def _app(id_, repo=None, **kw):
    return {"id": id_, "name": id_.title(), "repo": repo or f"org/{id_}", "commit": C1, "provides": [], "uses": [], **kw}


@pytest.fixture
def sr(monkeypatch):
    from wam import store_remove
    store_remove._jobs.clear()
    si._jobs.clear()
    monkeypatch.setattr(store_remove, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set(rt.have))
    return store_remove


def _wait(job):
    for _ in range(200):
        if job["state"] != "removing":
            return job
        threading.Event().wait(0.01)
    raise AssertionError("job never finished")


def _body(plan):
    return {"repo": plan["identity"]["repo"], "commit": plan["identity"]["commit"],
            "dependents": [d["id"] for d in plan["dependents"]], "empties": [e["slot"] for e in plan["empties"]]}


def test_plan_names_exact_identity_dependents_and_slots_it_empties(tmp_path, sr):
    apps = [_app("money"), _app("wizard-simplefin", provides=["bank-connection"]),
            _app("budget", uses=["wizard-simplefin"]), _app("reader", uses=["transcription"]),
            _app("transcriber-lite", provides=["transcription"])]
    c, _ = _box(tmp_path, apps, grants=[{"kind": "share", "from": "notes", "to": "wizard-simplefin"}])
    p = sr.plan(c, "money")
    assert p["removable"] is True and p["installed"] is True and p["reason"] == ""
    assert p["identity"] == {"repo": "org/money", "commit": C1, "source": "standard"}
    assert p["dependents"] == [] and p["empties"] == []
    p = sr.plan(c, "wizard-simplefin")
    assert [d["id"] for d in p["dependents"]] == ["budget"]          # a grant from an app that isn't installed is ignored
    assert p["dependents"][0]["name"] == "Budget"
    # The only transcription provider empties that (non-required) spot; whoever uses the spot is named.
    p = sr.plan(c, "transcriber-lite")
    assert [e["slot"] for e in p["empties"]] == ["transcription"] and [d["id"] for d in p["dependents"]] == ["reader"]


def test_plan_refuses_protected_required_unknown_identity_and_busy(tmp_path, sr):
    apps = [_app("browser"), _app("wizard-ux", provides=["shell"]), _app("money"),
            _app("only-files", provides=["files"]), _app("mystery"), _app("shellish", category="interface")]
    c, rt = _box(tmp_path, apps, installed={a["id"] for a in apps} - {"ghost"},
                 identity={"mystery": {"repo": None}})
    for app_id in ("browser", "wizard-ux", "shellish"):           # system defaults, the shell and interface apps
        p = sr.plan(c, app_id)
        assert p["removable"] is False and p["protected"] is True and "part of Wizard" in p["reason"], app_id
    p = sr.plan(c, "only-files")
    assert p["removable"] is False and p["protected"] is False and "Files" in p["reason"]   # a required spot never goes empty
    assert sr.plan(c, "money")["protected"] is False
    p = sr.plan(c, "mystery")
    assert p["removable"] is False and "source" in p["reason"]
    p = sr.plan(c, "ghost")
    assert p["installed"] is False and p["removable"] is False
    si._jobs["money"] = {"app": "money", "state": "installing"}
    assert "installed or updated" in sr.plan(c, "money")["reason"]
    with pytest.raises(ValueError):
        sr.plan(c, "../etc")


def test_remove_runs_the_lifecycle_once_and_verifies_by_readback(tmp_path, sr):
    c, rt = _box(tmp_path, [_app("money")])
    gate, calls = threading.Event(), []

    def uninstall(c_, a):
        calls.append(a); gate.wait(5); rt.have.discard(a); return {"removed": True}
    body = _body(sr.plan(c, "money"))
    code, job = sr.start(c, "money", body, uninstall_fn=uninstall)
    assert code == 202 and job["state"] == "removing" and job["identity"] == {"repo": "org/money", "commit": C1}
    assert sr.start(c, "money", body, uninstall_fn=uninstall)[1] is job                 # a second tap joins
    assert sr.start(c, "money", {**body, "commit": C2}, uninstall_fn=uninstall)[0] == 409
    # A Store Get/Update for the same app waits for the removal
    assert si.start(c, "money", lambda *a: {}, set, selection={"source": "standard", "repo": "org/money",
                                                               "commit": C1, "operation": "update"})[0] == 409
    gate.set()
    assert _wait(job)["state"] == "removed" and calls == ["money"]
    assert sr.status("money") == (200, job)
    # Repeating the request after it finished returns the same receipt; nothing runs again.
    assert sr.start(c, "money", body, uninstall_fn=uninstall) == (200, job) and calls == ["money"]
    # Reinstalled at the same release later: an old receipt never answers a new removal.
    rt.have.add("money")
    code, again = sr.start(c, "money", body, uninstall_fn=uninstall)
    assert code == 202 and again is not job and again["state"] in ("removing", "removed")
    assert _wait(again)["state"] == "removed" and calls == ["money", "money"]


def test_remove_refuses_stale_identity_or_consequences_and_never_calls_the_lifecycle(tmp_path, sr):
    c, _ = _box(tmp_path, [_app("money"), _app("bank", provides=["bank-connection"]), _app("budget", uses=["bank"])])
    never = lambda *a: (_ for _ in ()).throw(AssertionError("must not uninstall"))
    good = _body(sr.plan(c, "bank"))
    for bad in ({**good, "commit": C2}, {**good, "repo": "fork/bank"}, {**good, "dependents": []},
                {**good, "empties": ["bank-connection"]}):
        code, out = sr.start(c, "bank", bad, uninstall_fn=never)
        assert code == 409 and out["error"], bad
    for bad in ({}, {**good, "extra": 1}, {**good, "dependents": "budget"}, {**good, "repo": 7}, [good]):
        with pytest.raises(ValueError):
            sr.start(c, "bank", bad, uninstall_fn=never)
    assert sr.start(c, "browser", {"repo": "org/browser", "commit": C1, "dependents": [], "empties": []},
                    uninstall_fn=never)[0] == 404                                   # not installed on this box
    assert sr.status("bank")[0] == 404


def test_failed_and_unknown_outcomes_never_claim_removal(tmp_path, sr):
    c, rt = _box(tmp_path, [_app("money"), _app("notes")])

    def boom(c_, a):
        raise SystemExit("runtipi busy (409)")
    code, job = sr.start(c, "money", _body(sr.plan(c, "money")), uninstall_fn=boom, status_fn=lambda a: "running")
    job = _wait(job)
    assert job["state"] == "failed" and "busy" in job["error"] and "still installed" in job["error"]
    # The lifecycle returned, but the app is still listed and still uninstalling: unknown, not removed.
    code, job = sr.start(c, "notes", _body(sr.plan(c, "notes")), uninstall_fn=lambda *a: {"removed": True},
                         status_fn=lambda a: "uninstalling")
    job = _wait(job)
    assert job["state"] == "unknown" and "Refresh" in job["error"]
    # A readback that itself fails is unknown as well.
    sr._jobs.clear()
    def unreadable(rt_):
        raise RuntimeError("runtipi down")
    code, job = sr.start(c, "notes", _body(sr.plan(c, "notes")), uninstall_fn=lambda *a: {},
                         installed_fn=lambda: unreadable(None))
    assert _wait(job)["state"] == "unknown"
    # A failed job can be retried; a fresh plan is required again.
    rt.have.discard("money")
    assert sr.plan(c, "money")["installed"] is False


def test_plan_fails_closed_without_a_registry_record_full_commit_or_exact_id(tmp_path, sr):
    apps = [_app("money"), _app("staged", app_id="money"), _app("oldpin")]
    c, rt = _box(tmp_path, apps, installed={"money", "staged", "oldpin", "runtipi-only"},
                 identity={"oldpin": {"repo": "org/oldpin", "commit": ""}})
    assert sr.plan(c, "money")["removable"] is True
    p = sr.plan(c, "runtipi-only")                         # installed, but the App Manager never registered it
    assert p["installed"] is True and p["removable"] is False and "source" in p["reason"]
    p = sr.plan(c, "staged")                               # a staged copy under another app's id
    assert p["removable"] is False and "source" in p["reason"]
    p = sr.plan(c, "oldpin")                               # no recorded commit: identity is not exact
    assert p["removable"] is False and "source" in p["reason"]
    reg = json.loads((c.home / "apps.json").read_text())
    reg["apps"][0]["commit"] = C2                          # registry and installed recipe disagree
    (c.home / "apps.json").write_text(json.dumps(reg))
    assert "out of date" in sr.plan(c, "money")["reason"]
    (c.home / "apps.json").write_text("{not json")
    p = sr.plan(c, "money")
    assert p["removable"] is False and "app list" in p["reason"]
    with pytest.raises(ValueError):
        sr.start(c, "money", {"repo": "org/money", "commit": "", "dependents": [], "empties": []})


def test_unknown_outcome_keeps_the_barrier_until_a_readback_settles_it(tmp_path, sr, monkeypatch):
    monkeypatch.setattr(sr, "SETTLE_SECONDS", 0)
    c, rt = _box(tmp_path, [_app("money")])
    rt.status = {"money": "uninstalling"}
    rt.app = lambda urn: {"app": {"status": rt.status.get(urn.split(":")[0], "missing")}}
    calls = []
    body = _body(sr.plan(c, "money"))
    code, job = sr.start(c, "money", body, uninstall_fn=lambda c_, a: calls.append(a))
    assert _wait(job)["state"] == "unknown"
    # Still uninstalling: no Store Get/Update, and another tap joins instead of uninstalling twice.
    assert sr.removing("money", c) is True
    sel = {"source": "standard", "repo": "org/money", "commit": C1, "operation": "update"}
    assert si.start(c, "money", lambda *a: {}, set, selection=sel)[0] == 409
    assert sr.start(c, "money", body, uninstall_fn=lambda c_, a: calls.append(a)) == (202, job)
    assert calls == ["money"]
    assert sr.status("money", c)[1]["state"] == "unknown"
    # Runtipi finishes later: the next read settles it as removed and the barrier lifts.
    rt.have.discard("money")
    assert sr.status("money", c)[1]["state"] == "removed" and sr.removing("money", c) is False
    # The same unknown case, but Runtipi reports the uninstall failed: settled as failed, retry allowed.
    sr._jobs.clear(); rt.have.add("money"); rt.status = {"money": "uninstalling"}
    code, job = sr.start(c, "money", body, uninstall_fn=lambda c_, a: None)
    assert _wait(job)["state"] == "unknown"
    rt.status = {"money": "stopped"}
    assert sr.removing("money", c) is False and job["state"] == "failed" and "still installed" in job["error"]


def test_two_removals_never_empty_a_required_spot(tmp_path, sr):
    """Two providers of a required spot, removed at once: each plan alone sees the other, so both pass. The
    removals run one at a time and re-plan right before the lifecycle call, so only the first goes ahead."""
    apps = [_app("files-a", provides=["files"]), _app("files-b", provides=["files"])]
    c, rt = _box(tmp_path, apps)
    gate, calls = threading.Event(), []

    def uninstall(c_, a):   # like Runtipi: the app goes; the registry isn't rewritten until later
        calls.append(a)
        if a == "files-a":
            gate.wait(5)
        rt.have.discard(a)
    body_a, body_b = _body(sr.plan(c, "files-a")), _body(sr.plan(c, "files-b"))   # both reviewed up front
    ja = sr.start(c, "files-a", body_a, uninstall_fn=uninstall)[1]
    for _ in range(200):                                    # files-a is mid-uninstall when files-b is confirmed
        if calls:
            break
        threading.Event().wait(0.01)
    jb = sr.start(c, "files-b", body_b, uninstall_fn=uninstall)[1]
    assert ja["state"] == jb["state"] == "removing"
    gate.set()
    assert _wait(ja)["state"] == "removed" and _wait(jb)["state"] == "failed"
    assert calls == ["files-a"] and "files-b" in rt.have and "Files" in jb["error"]


def _serve(monkeypatch, cfg, method, path, body=None):
    monkeypatch.setattr(si, "peer_uid", lambda p: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "o@x")
    monkeypatch.setattr(si, "served_origins", lambda h: {"https://b"})
    monkeypatch.setattr(si, "check_request", lambda *a: None)
    monkeypatch.setattr(cli.StoreHandler, "cfg", cfg, raising=False)
    (cfg.home / "links.json").write_text(json.dumps({"host": "b"}))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.StoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        raw = json.dumps(body) if body is not None else None
        h.request(method, path, raw, {"Content-Type": "application/json", "Tailscale-User-Login": "o@x",
                                       **({"Content-Length": str(len(raw))} if raw else {})})
        r = h.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        srv.shutdown()


def test_listener_serves_plan_and_removal_behind_the_store_gate(tmp_path, monkeypatch, sr):
    c, rt = _box(tmp_path, [_app("money")])
    code, plan = _serve(monkeypatch, c, "GET", "/_remove/money/plan")
    assert code == 200 and plan["removable"] is True and plan["identity"]["repo"] == "org/money"
    seen = []
    monkeypatch.setattr(sr, "start", lambda c_, a, b, **k: (seen.append((a, b)) or (202, {"app": a, "state": "removing"})))
    code, job = _serve(monkeypatch, c, "POST", "/_remove/money", _body(plan))
    assert code == 202 and seen == [("money", _body(plan))]
    for bad in ("/_remove/../money", "/_remove/money/extra", "/_remove/Money/plan"):
        assert _serve(monkeypatch, c, "GET", bad)[0] == 404, bad
    assert _serve(monkeypatch, c, "POST", "/_remove/money/plan", _body(plan))[0] == 404


def test_listener_refuses_a_removal_from_the_box_itself(tmp_path, monkeypatch, sr):
    c, _ = _box(tmp_path, [_app("money")])
    monkeypatch.setattr(si, "peer_uid", lambda p: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "owner@x.com")
    monkeypatch.setattr(si, "served_origins", lambda h: {"https://box.tail1.ts.net:8796"})
    monkeypatch.setattr(si, "own_ips", lambda: {"192.0.2.1"})
    monkeypatch.setattr(sr, "start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not remove")))
    monkeypatch.setattr(cli.StoreHandler, "cfg", c, raising=False)
    (c.home / "links.json").write_text(json.dumps({"host": "box.tail1.ts.net"}))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.StoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"repo": "org/money", "commit": C1, "dependents": [], "empties": []})
        good = {"Tailscale-User-Login": "owner@x.com", "Origin": "https://box.tail1.ts.net:8796",
                "X-Wizard-Store": "1", "Content-Type": "application/json", "Content-Length": str(len(body))}
        for headers, why in (({**good, "X-Forwarded-For": "192.0.2.1"}, "this Wizard itself"),
                             ({**good, "X-Forwarded-For": "192.0.2.9", "Origin": "https://evil.example"}, "wrong origin"),
                             ({**good, "X-Forwarded-For": "192.0.2.9", "X-Wizard-Store": ""}, "bad request"),
                             ({**good, "X-Forwarded-For": "192.0.2.9", "Tailscale-User-Login": "guest@x.com"}, "owner")):
            h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            h.request("POST", "/_remove/money", body, headers)
            r = h.getresponse()
            assert r.status == 403 and why in json.loads(r.read())["error"], why
    finally:
        srv.shutdown()
