"""Owner-controllable app connections: the network side (wam/network.py controls), `wam grants`, and the `wam serve`
connections watcher that applies decisions live and delivers notices (wam/connections.py). The policy itself lives in
the App Manager (packages/backend wizard-connection-policy.test.ts)."""
import json
from pathlib import Path

from wam import connections as conn
from wam import network as net



def app(app_id, uses=(), provides=(), **kw):
    services = {app_id: {"main": True, "internal_port": 80, "on_shared": True, "network_mode": "", "exposed": False,
                         "networks": {"tipi_main_network": {"gw_priority": 1}}}}
    return {"id": app_id, "name": app_id, "uses": list(uses), "provides": list(provides), "router": False,
            "services": services, "custom": None, "host_access": [], **kw}


def reach(p):
    return {(m["app"], o["app"]) for n in p["networks"].values() for m in n["members"] for o in n["members"]
            if m["app"] != o["app"]}


def g(frm, to, approved: "str | None" = "install", reason="uses", **kw):
    return {"kind": "app", "from": frm, "to": to, "reason": reason, "approved": approved, **kw}


def apps():
    return [app("twenty-crm", name="Twenty CRM"), app("resend-connector", name="Resend"),
            app("carlos-campaigns", uses=["twenty-crm", "resend-connector"], name="Carlos Campaigns")]


def test_controls_show_connected_disconnected_pending_and_requested():
    state = {"grants": [g("carlos-campaigns", "twenty-crm"), g("twenty-crm", "resend-connector", "request", "request")],
             "denied": [], "pending": [g("carlos-campaigns", "resend-connector", None)], "sensitive": ["resend-connector"],
             "notices": []}
    p = net.plan(apps(), state["grants"], set(), approved=state["grants"], state=state)
    rows = {(r["from"], r["to"]): r for r in p["controls"]}
    r = rows[("carlos-campaigns", "twenty-crm")]
    assert (r["status"], r["origin"], r["providers"]) == ("connected", "declared", ["twenty-crm"])
    assert rows[("carlos-campaigns", "resend-connector")]["status"] == "pending"
    assert rows[("twenty-crm", "resend-connector")]["origin"] == "requested"
    assert ("carlos-campaigns", "resend-connector") not in reach(p)       # pending: no network
    assert ("twenty-crm", "resend-connector") in reach(p)                 # a requested grant links on its own
    edge = [e for e in p["edges"] if e["from"] == "carlos-campaigns" and e["to"] == "resend-connector"]
    assert edge[0]["state"] == "pending"
    # the owner's Disconnect: no network, still listed so the view can offer Reconnect
    state2 = {**state, "grants": [g("carlos-campaigns", "twenty-crm")], "pending": [],
              "denied": [g("carlos-campaigns", "resend-connector", "owner", "owner")]}
    p2 = net.plan(apps(), state2["grants"], set(), approved=state2["grants"], state=state2)
    rows2 = {(r["from"], r["to"]): r for r in p2["controls"]}
    assert rows2[("carlos-campaigns", "resend-connector")]["status"] == "disconnected"
    assert ("carlos-campaigns", "resend-connector") not in reach(p2)
    # a disconnected runtime request (not declared) stays listed too
    state3 = {**state2, "denied": [g("twenty-crm", "resend-connector", "owner", "request")]}
    rows3 = {(r["from"], r["to"]): r for r in net.plan(apps(), state3["grants"], set(), approved=state3["grants"],
                                                        state=state3)["controls"]}
    assert rows3[("twenty-crm", "resend-connector")]["status"] == "disconnected"
    assert rows3[("twenty-crm", "resend-connector")]["origin"] == "requested"


def test_stale_automatic_grants_are_not_controls_and_old_app_managers_still_work():
    a = [app("twenty-crm"), app("c")]                          # c no longer declares twenty-crm
    st = {"grants": [g("c", "twenty-crm")], "denied": [], "pending": [], "sensitive": [], "notices": []}
    assert net.plan(a, st["grants"], set(), approved=st["grants"], state=st)["controls"] == []
    # no connection state (older App Manager): declarations alone, shown as connected
    rows = net.plan(apps(), [], set())["controls"]
    assert {r["status"] for r in rows} == {"connected"}


def test_report_carries_plain_labels_sensitive_flags_and_recent_notices():
    st = {"grants": [g("carlos-campaigns", "twenty-crm")], "denied": [], "sensitive": ["twenty-crm"],
          "pending": [g("carlos-campaigns", "resend-connector", None)],
          "notices": [{"id": "a" * 12, "kind": "added", "text": "old", "at": 1}, {"id": "b" * 12, "kind": "pending", "text": "new", "at": 2}]}
    p = net.plan(apps(), st["grants"], set(), approved=st["grants"], state=st)
    p["state"] = st
    C = type("C", (), {"home": Path("/nonexistent")})()
    rep = net.report(C, p=p, live={"networks": {}, "containers": []})
    assert rep["owner_state"] is True and rep["sensitive"] == ["twenty-crm"]
    assert {r["id"]: r["sensitive"] for r in rep["apps"]}["twenty-crm"] is True
    labels = {r["label"]: r["status"] for r in rep["controls"]}
    assert labels == {"Carlos Campaigns → Twenty CRM": "connected", "Carlos Campaigns → Resend": "pending"}
    assert [n["text"] for n in rep["notices"]] == ["new", "old"]        # newest first
    text = net.describe(rep)
    assert "Waiting for your approval" in text and "carlos-campaigns -> resend-connector" in text
    assert "sensitive: new connections to it wait for your approval" in text


def test_connection_state_reads_the_file_else_the_owner_api(tmp_path):
    C = type("C", (), {"runtipi_root": tmp_path})()
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "wizard-grants.json").write_text(json.dumps(
        {"schema": 1, "grants": [g("a", "b")], "denied": [], "pending": [], "sensitive": ["b", "../x"], "notices": []}))
    st = net.connection_state(C, None)
    assert st["sensitive"] == ["b"] and st["grants"][0]["to"] == "b"
    (tmp_path / "state" / "wizard-grants.json").unlink()

    class RT:
        def req(self, method, path, body=None):
            assert (method, path) == ("GET", "/apps/wizard/connections")
            return {"grants": [], "denied": [g("a", "b")], "pending": [], "sensitive": [], "notices": []}
    assert net.connection_state(C, RT())["denied"][0]["from"] == "a"

    class Old:
        def req(self, *a, **k):
            raise RuntimeError("404")
    assert net.connection_state(C, Old()) is None


def test_watcher_reconciles_when_connections_change_and_not_for_notices(monkeypatch):
    states = iter([
        {"grants": [g("a", "b")], "denied": [], "pending": [], "sensitive": [], "notices": []},
        {"grants": [g("a", "b")], "denied": [], "pending": [], "sensitive": [], "notices": [{"id": "c" * 12, "delivered": True}]},
        {"grants": [], "denied": [g("a", "b", "owner", "owner")], "pending": [], "sensitive": [], "notices": []},
    ])
    monkeypatch.setattr(net, "connection_state", lambda c, rt: next(states))
    applied, regs = [], []
    C = type("C", (), {"home": Path("/nonexistent")})()
    last = conn.step(C, None, "", lambda: applied.append(1), lambda: regs.append(1))
    assert applied == [1]                    # first poll applies: a decision made while wam serve was down
    last = conn.step(C, None, last, lambda: applied.append(1), lambda: regs.append(1))
    assert applied == [1]                    # a notice change alone moves no network
    conn.step(C, None, last, lambda: applied.append(1), lambda: regs.append(1))
    assert applied == [1, 1] and regs == [1, 1]   # the owner's Disconnect: reconcile now, live


def test_notices_go_to_notifications_and_are_marked_delivered(tmp_path):
    (tmp_path / "apps.json").write_text(json.dumps({"apps": [
        {"id": "notifications", "provides": ["notifications"], "mcp": {"url": "http://127.0.0.1:8790/mcp"}}]}))
    C = type("C", (), {"home": tmp_path})()
    posted = []

    class RT:
        def req(self, method, path, body=None):
            posted.append((method, path, body))
    st = {"notices": [{"id": "a" * 12, "at": 1000, "kind": "added", "text": "Carlos Campaigns now connects to Resend.", "delivered": False},
                      {"id": "b" * 12, "at": 1000, "kind": "pending", "text": "X wants to connect to Y", "delivered": False},
                      {"id": "c" * 12, "at": 1000, "kind": "added", "text": "done", "delivered": True},
                      {"id": "d" * 12, "at": 1, "kind": "added", "text": "too old", "delivered": False}]}
    sent = []
    out = conn.deliver(C, RT(), st, send=lambda n: sent.append(conn.notice_message(n)), now=1000 + conn.NOTICE_MAX_AGE)
    assert out == ["a" * 12, "b" * 12]
    assert sent[0] == {"title": "App connection added", "message": "Carlos Campaigns now connects to Resend.",
                       "priority": "low", "tags": "link"}
    assert sent[1]["title"] == "Connection needs your OK" and sent[1]["priority"] == "high"
    assert posted == [("POST", "/apps/wizard/connection-notices", {"delivered": ["a" * 12, "b" * 12, "d" * 12]})]
    # no Notifications app: notices stay in the Connections view, nothing marked
    (tmp_path / "apps.json").write_text(json.dumps({"apps": []}))
    posted.clear()
    assert conn.deliver(C, RT(), st, now=1000) == [] and posted == []
    assert conn.notifications_mcp(tmp_path) is None


def test_a_failed_send_is_retried_later(tmp_path):
    C = type("C", (), {"home": tmp_path})()
    posted = []

    class RT:
        def req(self, method, path, body=None):
            posted.append(body)

    def boom(n):
        raise OSError("down")
    st = {"notices": [{"id": "a" * 12, "at": 1000, "kind": "added", "text": "t"}]}
    assert conn.deliver(C, RT(), st, send=boom, now=1000) == [] and posted == []


def test_wam_grants_cli_sensitive_approve_deny_and_pending(monkeypatch, capsys):
    from wam import cli
    calls = []

    class RT:
        def req(self, method, path, body=None):
            calls.append((method, path, body))
            if path == "/apps/wizard/connections":
                return {"pending": [g("a", "b", None)], "sensitive": ["b"], "notices": [{"id": "x"}]}
            return {"ok": True}

    class Cfg:
        def runtipi(self):
            return RT()
    monkeypatch.setattr(cli, "Config", Cfg)
    monkeypatch.setattr(cli, "network_step", lambda c, rt=None, **k: {"created": ["wizard-link-a--b"]})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: {})
    assert cli.main(["grants", "sensitive", "twenty-crm", "on"]) == 0
    assert calls[-1] == ("POST", "/apps/twenty-crm:wizardapps/sensitive", {"sensitive": True})
    assert cli.main(["grants", "approve", "carlos-campaigns", "twenty-crm"]) == 0
    assert calls[-1] == ("POST", "/apps/carlos-campaigns:wizardapps/app-grants", {"to": "twenty-crm", "approve": True})
    assert cli.main(["grants", "deny", "carlos-campaigns", "twenty-crm"]) == 0
    assert calls[-1] == ("POST", "/apps/carlos-campaigns:wizardapps/app-grants", {"to": "twenty-crm", "approve": False})
    capsys.readouterr()
    assert cli.main(["grants", "pending"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sensitive"] == ["b"] and out["pending"][0]["to"] == "b"


def test_the_loopback_page_cannot_change_connections_without_the_owner():
    """Owner-only: wam serve's loopback API has no unauthenticated write route (POSTs need the CLI's bearer token);
    the view's buttons call the App Manager's owner-authenticated API."""
    from wam import cli
    import inspect
    src = inspect.getsource(cli.Handler.do_POST)
    assert src.index("if not self._authorized()") < src.index("path ==")
    js = (Path(__file__).resolve().parent.parent / "wizard-views" / "wizard-apps.js").read_text()
    assert "/app-grants" in js and "/sensitive" in js
    assert "connections-change" not in js


def test_a_failed_reconcile_is_retried_with_backoff(monkeypatch):
    st = {"grants": [], "denied": [g("a", "b", "owner", "owner")], "pending": [], "sensitive": [], "notices": []}
    monkeypatch.setattr(net, "connection_state", lambda c, rt: st)
    monkeypatch.setattr(conn, "_retry", {"at": 0.0, "delay": 0.0})
    C = type("C", (), {"home": Path("/nonexistent")})()
    calls = []
    results = iter([False, False, True])
    run = lambda: calls.append(1) or next(results)   # noqa: E731
    last = conn.step(C, None, "", run, lambda: None, now=100)
    assert last == "" and calls == [1]          # a Docker operation failed: not recorded as handled
    last = conn.step(C, None, last, run, lambda: None, now=101)
    assert calls == [1]                         # waits for the retry time (3 s)
    last = conn.step(C, None, last, run, lambda: None, now=103)
    assert last == "" and calls == [1, 1]       # retried, failed again: next retry in 6 s
    last = conn.step(C, None, last, run, lambda: None, now=106)
    assert calls == [1, 1]
    last = conn.step(C, None, last, run, lambda: None, now=109)
    assert last and calls == [1, 1, 1]          # applied
    conn.step(C, None, last, run, lambda: None, now=110)
    assert calls == [1, 1, 1]                   # unchanged afterwards: no more work


def test_a_reconcile_with_docker_errors_counts_as_failed(monkeypatch):
    from wam import cli
    got = {}
    monkeypatch.setenv("WAM_CONNECTIONS_WATCH", "1")
    monkeypatch.setenv("WAM_NETWORK_WATCH", "1")
    monkeypatch.setattr(conn, "start", lambda c, apply, reg: got.setdefault("apply", apply))
    cli.start_connections_watch(object())
    for res, ok in (({"enabled": True, "errors": []}, True), ({"enabled": True, "errors": ["x: disconnect failed"]}, False),
                    ({"errors": ["raised"]}, False), ({"enabled": True, "unavailable": True, "errors": []}, False)):
        monkeypatch.setattr(cli, "network_step", lambda c, **k: res)
        assert got["apply"]() is ok


def test_the_owners_chosen_notifications_app_is_used(tmp_path):
    (tmp_path / "apps.json").write_text(json.dumps({"apps": [
        {"id": "notifications", "provides": ["notifications"], "mcp": {"url": "http://127.0.0.1:8790/mcp"}},
        {"id": "my-ntfy", "provides": ["notifications"], "mcp": {"url": "http://127.0.0.1:9911/mcp"}}],
        "slots": {"notifications": {"default": "my-ntfy", "candidates": ["notifications", "my-ntfy"]}}}))
    assert conn.notifications_mcp(tmp_path) == "http://127.0.0.1:9911/mcp"


def test_a_sent_notice_is_not_sent_again_while_its_acknowledgement_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(conn, "_sent", set())
    C = type("C", (), {"home": tmp_path})()
    up = {"ok": False}
    acks = []

    class RT:
        def req(self, method, path, body=None):
            if not up["ok"]:
                raise OSError("App Manager restarting")
            acks.append(body)
    st = {"notices": [{"id": "a" * 12, "at": 1000, "kind": "added", "text": "t", "delivered": False}]}
    sent = []
    for _ in range(3):
        conn.deliver(C, RT(), st, send=lambda n: sent.append(n["id"]), now=1000)
    assert sent == ["a" * 12]                   # once, although the acknowledgement failed three times
    up["ok"] = True
    conn.deliver(C, RT(), st, send=lambda n: sent.append(n["id"]), now=1000)
    assert sent == ["a" * 12] and acks == [{"delivered": ["a" * 12]}] and not conn._sent
