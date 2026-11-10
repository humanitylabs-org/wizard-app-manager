"""App switches in the box's one feature-switch catalog (wam/app_switches.py, wizard.app-switches/1): discovery from the
installed wizard.md `switches:` declaration, server-side calls signed with the app's owner proof, merged rows and
flips through feature_switches. No real box: a fake app on loopback verifies the proof like Messenger does."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wam import app_switches as aps, feature_switches as fs, owner_proof, updates

KEY = bytes(range(32))


class FakeApp(BaseHTTPRequestHandler):
    state = {"drafts-first": False}
    seen = []

    def log_message(self, *a):
        pass

    def _send(self, code, doc):
        b = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _listing(self):
        return {"contract": aps.CONTRACT, "switches": [
            {"id": "drafts-first", "name": "Drafts at top", "description": "d", "on": self.state["drafts-first"],
             "default": False, "issues": ["humanitylabs-org/wizard-messenger#133", "not an issue"]},
            {"id": "../bad", "name": "x"}]}

    def _ok(self):
        FakeApp.seen.append((self.command, self.path, dict(self.headers)))
        try:
            owner_proof.verify(self.headers.get(owner_proof.HEADER, ""), KEY, "messenger", self.command, self.path)
        except ValueError:
            self._send(403, {"error": "Open Messenger through your authenticated Wizard"})
            return False
        if self.headers.get("X-Wizard-Switch") != "1":
            self._send(403, {"error": "Send X-Wizard-Switch: 1"})
            return False
        return True

    def do_GET(self):
        if self._ok():
            self._send(200, self._listing())

    def do_POST(self):
        if not self._ok():
            return
        if self.headers.get("Origin") != "https://box.example:8444":
            return self._send(403, {"error": "wrong origin"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["id"] not in self.state:
            return self._send(404, {"error": "No such switch"})
        self.state[body["id"]] = body["on"]
        self._send(200, self._listing())


@pytest.fixture
def box(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeApp)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeApp.state = {"drafts-first": False}
    FakeApp.seen = []
    h, rt = tmp_path / "wos", tmp_path / "runtipi"
    h.mkdir()
    port = srv.server_address[1]
    (h / "apps.json").write_text(json.dumps({"apps": [
        {"id": "wizard-ux", "health": "http://127.0.0.1:8796/shell-health"},
        {"id": "messenger", "name": "Messenger", "urn": "messenger:wizardapps",
         "view": {"url": "/messenger/", "local": f"http://127.0.0.1:{port}/"}},
        {"id": "news", "name": "News", "urn": "news:wizardapps", "view": {"local": f"http://127.0.0.1:{port}/"}}]}))
    (h / "app-manager.json").write_text(json.dumps({"runtipi_root": str(rt)}))
    (h / "settings.json").write_text(json.dumps({"network": {"base_url": "https://box.example:8444"}}))
    for app, front in (("messenger", "switches:\n  contract: wizard.app-switches/1\n  path: /api/view/switches\n"),
                       ("news", "name: News\n")):
        d = rt / "apps" / "wizardapps" / app
        d.mkdir(parents=True)
        (d / "wizard.md").write_text(f"---\nid: {app}\n{front}---\n\n# {app}\n")
    kd = h / "messenger-owner"                      # a box's adopted hand-built key (the platform edge key also works)
    kd.mkdir(mode=0o700)
    (kd / "proof").write_bytes(KEY)
    os.chmod(kd / "proof", 0o600)
    monkeypatch.setenv("WIZARD_OS_HOME", str(h))
    monkeypatch.delenv("RUNTIPI_ROOT", raising=False)
    yield h
    srv.shutdown()


def test_parse_accepts_the_short_and_long_forms_only():
    assert aps.parse("/api/switches") == "/api/switches"
    assert aps.parse({"contract": aps.CONTRACT, "path": "/api/view/switches"}) == "/api/view/switches"
    for bad in (None, "", "api/switches", "/a/../b", {"contract": "other/2", "path": "/x"}, {"path": 3}, ["/x"]):
        assert aps.parse(bad) is None, bad


def test_only_apps_that_declare_switches_are_discovered(box):
    assert [a["id"] for a in aps.declared(box)] == ["messenger"]
    assert aps.declared(box)[0]["path"] == "/api/view/switches"


def test_listing_calls_the_app_server_side_with_the_owner_proof(box):
    rows, status = aps.listing(box)
    assert status == [{"id": "messenger", "name": "Messenger", "ok": True}]
    assert rows == [{"id": "messenger:drafts-first", "kind": "app", "app": "messenger", "app_name": "Messenger",
                     "switch": "drafts-first", "name": "Drafts at top", "description": "d", "author": "messenger",
                     "on": False, "default": False, "issues": ["humanitylabs-org/wizard-messenger#133"],
                     "locked": False}]
    method, path, headers = FakeApp.seen[-1]
    assert (method, path) == ("GET", "/api/view/switches") and "Cookie" not in headers


def test_flip_goes_to_the_app_and_the_merged_catalog_shows_it(box, monkeypatch):
    updates.set_channel("latest", box)
    monkeypatch.setattr(fs, "catalog", lambda home=None, fetch=None: [
        {"id": "miguel/send-and-go", "name": "Send and go", "description": "", "author": "miguel", "kind": "core",
         "issues": ["humanitylabs-org/wizard-ux#9"]}])

    class Pages:
        def get(self):
            return {"version": 1}, None
    out = fs.set_switch("messenger:drafts-first", True, box, pages=Pages(), by="wizard-os-tracker")
    assert FakeApp.state["drafts-first"] is True
    assert out["changed"] == {"id": "messenger:drafts-first", "on": True, "by": "wizard-os-tracker"}
    assert {r["id"]: r["on"] for r in out["switches"]} == {"miguel/send-and-go": False, "messenger:drafts-first": True}
    assert out["apps"] == [{"id": "messenger", "name": "Messenger", "ok": True}]
    fs.set_switch("messenger:drafts-first", False, box, pages=Pages())
    assert FakeApp.state["drafts-first"] is False


def test_unknown_app_switch_or_bad_ids_are_refused_plainly(box):
    for tid, code in (("news:x", 404), ("messenger:nope", 404), ("messenger:", 400), ("Messenger:x", 400)):
        with pytest.raises(fs.SwitchError) as e:
            fs.set_switch(tid, True, box)
        assert e.value.code == code, tid
    with pytest.raises(fs.SwitchError):
        fs.set_switch("messenger:drafts-first", "yes", box)


def test_an_app_without_a_key_or_a_wrong_key_is_reported_not_fatal(box):
    (box / "messenger-owner" / "proof").write_bytes(b"x" * 32)
    rows, status = aps.listing(box)
    assert rows == [] and status[0]["ok"] is False and status[0]["status"] == 403
    (box / "messenger-owner" / "proof").unlink()
    rows, status = aps.listing(box)
    assert rows == [] and "no owner proof key" in status[0]["error"]


def test_platform_edge_key_wins_over_a_hand_built_one(box):
    p = owner_proof.edge_key_path(box, "messenger")
    p.parent.mkdir(parents=True, mode=0o700)
    p.write_bytes(KEY)
    os.chmod(p, 0o600)
    (box / "messenger-owner" / "proof").write_bytes(b"y" * 32)
    assert aps.proof_key(box, "messenger") == KEY
