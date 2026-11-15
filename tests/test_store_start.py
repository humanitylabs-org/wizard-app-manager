"""#711: an app an update left stopped can be started by the owner from their own device (Store "Start"), and a Store
update whose app didn't come back is reported failed, not "installed"."""
import threading

from wam import store_install as si, store_start as ss
from test_store_install import GW, _gateway, _get


def _post(srv, path, headers, body):
    import http.client, json as _j
    raw = _j.dumps(body)
    h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    h.request("POST", path, raw, {**headers, "Content-Length": str(len(raw))})
    r = h.getresponse()
    return r.status, _j.loads(r.read() or b"{}")


class RT:
    def __init__(self, status):
        self.status = status

    def app(self, urn):
        return {"app": {"status": self.status}} if self.status else {}


class C:
    def __init__(self, status):
        self.rt = RT(status)

    def runtipi(self):
        return self.rt


def _wait(job, busy):
    for _ in range(200):
        if job["state"] != busy:
            return job
        threading.Event().wait(0.02)
    return job


def test_a_stopped_app_starts_once_and_a_second_tap_joins():
    ss._jobs.clear(); si._jobs.clear()
    gate, calls = threading.Event(), []

    def life(c, verb, app):
        calls.append((verb, app)); gate.wait(5); return {"status": "running"}
    code, job = ss.start(C("stopped"), "messenger", {}, life)
    assert code == 202 and job["state"] == "starting"
    assert ss.start(C("stopped"), "messenger", {}, life)[1] is job   # second tap joins
    gate.set()
    assert _wait(job, "starting")["state"] == "running" and calls == [("start", "messenger")]
    assert ss.status("messenger") == (200, job)


def test_start_refuses_what_it_must_not_touch():
    ss._jobs.clear(); si._jobs.clear()
    never = lambda *a: (_ for _ in ()).throw(AssertionError("must not start"))
    assert ss.start(C("stopped"), "../etc", {}, never)[0] == 400
    assert ss.start(C("stopped"), "messenger", {"x": 1}, never)[0] == 400
    assert ss.start(C(None), "messenger", {}, never)[0] == 404
    assert ss.start(C("running"), "messenger", {}, never) == (200, {"app": "messenger", "state": "running",
                                                                    "status": "running", "note": "already running"})
    assert ss.start(C("updating"), "messenger", {}, never)[0] == 409
    si._jobs["messenger"] = {"app": "messenger", "state": "installing"}   # a Store update of it is running
    assert ss.start(C("stopped"), "messenger", {}, never)[0] == 409
    si._jobs.clear()


def test_a_failed_start_says_why():
    ss._jobs.clear(); si._jobs.clear()
    code, job = ss.start(C("stopped"), "messenger", {}, lambda c, v, a: {"status": "stopped", "error": "start did not finish"})
    assert _wait(job, "starting")["state"] == "failed" and job["error"] == "start did not finish"

    def boom(c, v, a):
        raise SystemExit("messenger is not installed")
    code, job = ss.start(C("start_error"), "wisdom", {}, boom)
    assert _wait(job, "starting")["state"] == "failed" and "not installed" in job["error"]


def test_update_whose_app_did_not_come_back_is_failed_not_installed(monkeypatch):
    """On main the Store job said `installed` (status stopped) while the app sat stopped on its old version."""
    si._jobs.clear()
    monkeypatch.setattr(si, "_links", lambda c: None)
    sel = {"source": "standard", "repo": "org/m", "commit": "a" * 40, "operation": "update"}
    code, job = si.start(None, "messenger", lambda c, a: {"status": "stopped", "error": "the app did not come back"},
                         lambda: {"messenger"}, selection=sel)
    assert _wait(job, "installing")["state"] == "failed" and job["error"] == "the app did not come back"


def test_gateway_routes_start_for_the_owner_only(monkeypatch, tmp_path):
    srv = _gateway(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(ss, "start", lambda c, a, body: (seen.append((a, body)), (202, {"app": a, "state": "starting"}))[1])
    monkeypatch.setattr(ss, "status", lambda a: (200, {"app": a, "state": "running"}))
    try:
        assert _post(srv, "/_start/messenger", GW, {}) == (202, {"app": "messenger", "state": "starting"})
        assert seen == [("messenger", {})]
        assert _get(srv, "/_start/messenger", {"Tailscale-User-Login": "owner@x.com", "X-Wizard-Store-Proof": GW["X-Wizard-Store-Proof"]}) \
            == (200, {"app": "messenger", "state": "running"})
        # every owner check still holds: from the box itself, another origin, a guest, no proof, a bad id
        assert "this Wizard itself" in _post(srv, "/_start/messenger", {**GW, "X-Forwarded-For": "100.64.0.1"}, {})[1]["error"]
        assert _post(srv, "/_start/messenger", {**GW, "Origin": "https://evil.example"}, {})[1]["error"] == "wrong origin"
        assert "owner" in _post(srv, "/_start/messenger", {**GW, "Tailscale-User-Login": "guest@x.com"}, {})[1]["error"]
        assert _post(srv, "/_start/messenger", {k: v for k, v in GW.items() if k != "X-Wizard-Store-Proof"}, {})[0] == 403
        assert _post(srv, "/_start/Bad_ID", GW, {})[0] == 404
        assert _post(srv, "/_start/a/b", GW, {})[0] == 404
        assert seen == [("messenger", {})]
    finally:
        srv.shutdown()


def test_store_update_is_refused_while_runtipi_still_updates(monkeypatch, tmp_path):
    """A retry (Tracker, or a second tap) must never queue a second update on top of one still running."""
    srv = _gateway(monkeypatch, tmp_path)
    from wam import store_selection
    monkeypatch.setattr(store_selection, "resolve", lambda c, a, sel: sel)
    monkeypatch.setattr(si, "start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not update")))
    monkeypatch.setattr(ss, "_status", lambda c, a: "updating")
    sel = {"source": "standard", "repo": "org/app", "commit": "a" * 40, "operation": "update"}
    try:
        code, j = _post(srv, "/messenger", GW, sel)
        assert code == 409 and "messenger is updating" in j["error"]
    finally:
        srv.shutdown()


def test_busy_names_only_transitional_statuses():
    assert ss.busy(C("stopped"), "x") is None and ss.busy(C("running"), "x") is None and ss.busy(C("backing_up"), "x") == "backing_up"

    class Broken:
        def runtipi(self):
            raise OSError("no runtipi")
    assert ss.busy(Broken(), "x") is None
