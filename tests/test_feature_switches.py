"""Feature switches for Settings and the Wizard OS Tracker app (wam/feature_switches.py; Miguel, 7 Nov 2026): list and
turn one on or off for this box, only on For testers, only for the owner (Tailscale owner login on the same-origin store
route, or the owner token on wam serve). No app key exists. No real box is touched: the shell catalog and the Agent
Service Pages API are fakes."""
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from wam import cli, feature_switches as fs, updates

CATALOG = {"tweaks": ["miguel/send-and-go", "wizard/unified-inbox", "miguel/orb-shell", "miguel/snooze-someday"],
           "defaults": ["wizard/unified-inbox"]}
MANIFESTS = {"miguel/send-and-go": {"name": "Send and go", "description": "Close a chat when you send"},
             "miguel/orb-shell": {"name": "Orb", "view": "Orb"},
             "miguel/snooze-someday": {"name": "One-tap Snooze"}}


def fetch(url):
    if url.endswith("/tweaks/catalog.json"):
        return CATALOG
    tid = url.split("/tweaks/", 1)[1].removesuffix("/manifest.json")
    return MANIFESTS.get(tid, {})


class FakePages:
    """The Pages file API: versioned text, 409 with the newer text on a stale version."""

    def __init__(self, doc=None):
        self.text = json.dumps(doc) if doc is not None else None
        self.version = "v1" if doc is not None else None
        self.puts = []
        self.race = None        # a device save to land just before our first PUT

    def get(self):
        return (json.loads(self.text) if self.text else {"version": 1}), self.version

    def put(self, doc, version):
        if self.race is not None:
            self.text, self.version, self.race = json.dumps(self.race), "v-race", None
        if version != self.version:
            return False, json.loads(self.text), self.version
        self.text = json.dumps(doc)
        self.version = f"v{len(self.puts) + 2}"
        self.puts.append(doc)
        return True, None, self.version


@pytest.fixture
def box(tmp_path, monkeypatch):
    h = tmp_path / "wos"
    h.mkdir()
    (h / "apps.json").write_text(json.dumps({"apps": [{"id": "wizard-ux", "health": "http://127.0.0.1:8796/shell-health"}]}))
    monkeypatch.setenv("WIZARD_OS_HOME", str(h))
    return h


def test_lists_only_feature_switches_with_their_box_state_on_for_testers(box):
    updates.set_channel("latest", box)
    pages = FakePages({"version": 1, "tweaks": {"miguel/send-and-go": {"on": True, "at": 5}}})
    out = fs.listing(box, fetch, pages)
    assert out["channel"] == "latest" and out["channel_name"] == "For testers" and out["locked"] is False
    assert [(t["id"], t["on"]) for t in out["switches"]] == [("miguel/snooze-someday", False), ("miguel/send-and-go", True)]
    assert all(t["author"] == "miguel" for t in out["switches"])   # built-ins and views are not feature switches


def test_turning_one_on_writes_look_json_with_a_revision_newer_than_every_device_choice(box):
    updates.set_channel("latest", box)
    pages = FakePages({"version": 1, "skin": {"id": "nebula", "at": 1793977338050},
                       "tweaks": {"miguel/send-and-go": {"on": True, "at": 7}}, "views": {"desktop": {"id": "", "at": 3}}})
    out = fs.set_switch("miguel/snooze-someday", True, box, fetch, pages, by="wizard-os-tracker")
    saved = json.loads(pages.text)
    assert saved["tweaks"]["miguel/snooze-someday"] == {"on": True, "at": 1793977338051}
    assert saved["tweaks"]["miguel/send-and-go"] == {"on": True, "at": 7} and saved["skin"]["id"] == "nebula"
    assert saved["views"] == {"desktop": {"id": "", "at": 3}}, "everything else in look.json is kept"
    assert out["changed"] == {"id": "miguel/snooze-someday", "on": True, "by": "wizard-os-tracker"}
    assert {t["id"]: t["on"] for t in out["switches"]}["miguel/snooze-someday"] is True
    fs.set_switch("miguel/snooze-someday", False, box, fetch, pages)
    assert json.loads(pages.text)["tweaks"]["miguel/snooze-someday"]["on"] is False


def test_a_device_saving_meanwhile_is_merged_not_lost(box):
    updates.set_channel("latest", box)
    pages = FakePages({"version": 1, "tweaks": {}})
    pages.race = {"version": 1, "tweaks": {"miguel/send-and-go": {"on": True, "at": 40}}}
    fs.set_switch("miguel/snooze-someday", True, box, fetch, pages)
    saved = json.loads(pages.text)["tweaks"]
    assert saved["miguel/send-and-go"] == {"on": True, "at": 40}
    assert saved["miguel/snooze-someday"] == {"on": True, "at": 41}


def test_first_switch_on_a_box_without_look_json_creates_it(box):
    updates.set_channel("latest", box)
    pages = FakePages()
    fs.set_switch("miguel/send-and-go", True, box, fetch, pages)
    assert json.loads(pages.text) == {"version": 1, "tweaks": {"miguel/send-and-go": {"on": True, "at": 1}}}


def test_for_everyone_keeps_every_switch_off_and_refuses_flips(box):
    pages = FakePages({"version": 1, "tweaks": {"miguel/send-and-go": {"on": True, "at": 5}}})
    out = fs.listing(box, fetch, pages)                        # stable is the default channel
    assert out["locked"] is True and out["channel_name"] == "For everyone" and "For testers" in out["note"]
    assert all(t["on"] is False and t["locked"] is True for t in out["switches"])
    for on in (True, False):
        with pytest.raises(fs.SwitchError) as e:
            fs.set_switch("miguel/send-and-go", on, box, fetch, pages)
        assert e.value.code == 409
    assert pages.puts == [] and json.loads(pages.text)["tweaks"]["miguel/send-and-go"]["on"] is True, \
        "the owner's choice is kept for when the box goes back to For testers"


def test_unknown_builtin_view_or_malformed_ids_are_refused(box):
    updates.set_channel("latest", box)
    pages = FakePages({"version": 1})
    for tid, code in (("miguel/nope", 404), ("wizard/unified-inbox", 404), ("miguel/orb-shell", 404), ("../x", 400), (None, 400)):
        with pytest.raises(fs.SwitchError) as e:
            fs.set_switch(tid, True, box, fetch, pages)
        assert e.value.code == code, tid
    with pytest.raises(fs.SwitchError):
        fs.set_switch("miguel/send-and-go", "yes", box, fetch, pages)
    assert pages.puts == []


def test_no_app_key_path_exists():
    assert not hasattr(fs, "grant") and not hasattr(fs, "caller") and not hasattr(fs, "APPS")
    with pytest.raises(SystemExit):
        cli.main(["feature-switches", "--grant", "wizard-os-tracker"])


def test_http_gate_is_the_owner_token_and_nothing_else(box, monkeypatch):
    calls = []
    monkeypatch.setattr(fs, "listing", lambda *a, **k: {"switches": [], "locked": False})
    monkeypatch.setattr(fs, "set_switch", lambda tid, on, *a, **k: calls.append((tid, on, k.get("by"))) or {"ok": True})
    c = cli.Config()
    cli.Handler.cfg, cli.Handler.token = c, c.api_token()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, headers, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/feature-switches",
                                     json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        assert call("GET", {})[0] == 401
        assert call("GET", {"X-Wizard-Grant": "f" * 64})[0] == 401, "an app grant header opens nothing"
        assert call("POST", {"X-Wizard-Grant": "f" * 64}, {"id": "miguel/send-and-go", "on": True})[0] == 401
        assert call("POST", {"Authorization": "Bearer nope"}, {"id": "miguel/send-and-go", "on": True})[0] == 401
        auth = {"Authorization": f"Bearer {c.api_token()}"}
        assert call("GET", auth) == (200, {"switches": [], "locked": False})
        assert call("POST", auth, {"id": "miguel/send-and-go", "on": False})[0] == 200
        assert calls == [("miguel/send-and-go", False, "owner")]
    finally:
        srv.shutdown()


def test_same_origin_store_route_is_owner_only_like_settings_updates(box, monkeypatch):
    """The Tracker page runs on the shell's origin (https://<box>:8444/wizard-os-tracker/), so it calls
    /wizard-os-home/store-install/_feature-switches, which Serve proxies to the store listener with the owner's login."""
    from wam import store_install as si
    (box / "links.json").write_text(json.dumps({"host": "box.ts.net"}))
    monkeypatch.setattr(si, "peer_uid", lambda *a, **k: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "owner@example.com")
    monkeypatch.setattr(si, "served_origins", lambda host: {"https://box.ts.net:8444"})
    monkeypatch.setattr(si, "own_ips", lambda: {"100.64.0.1"})
    calls = []
    monkeypatch.setattr(fs, "listing", lambda *a, **k: {"switches": [{"id": "miguel/send-and-go", "on": False}], "locked": False})

    def setter(tid, on, *a, **k):
        if tid == "miguel/locked":
            raise fs.SwitchError("Feature switches are for testers", 409)
        calls.append((tid, on, k.get("by")))
        return {"ok": True}
    monkeypatch.setattr(fs, "set_switch", setter)

    class C:
        home = box
    sent = []

    def call(method, body=None, **hdr):
        raw = json.dumps(body).encode() if body is not None else b""
        hd = {"X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": "owner@example.com",
              "Origin": "https://box.ts.net:8444", "X-Wizard-Store": "1", "Content-Type": "application/json",
              "Content-Length": str(len(raw)), **hdr}
        o = cli.StoreHandler.__new__(cli.StoreHandler)
        o.cfg, o.path, o.client_address = C(), "/_feature-switches", ("127.0.0.1", 5555)
        o.headers = {k: v for k, v in hd.items() if v is not None}
        o.rfile = io.BytesIO(raw)
        o._json = lambda code, b: sent.append((code, b))
        o._go(method)
        return sent[-1]

    assert call("GET")[0] == 200 and call("GET")[1]["switches"][0]["id"] == "miguel/send-and-go"
    assert call("GET", **{"Tailscale-User-Login": "intruder@example.com"})[0] == 403
    assert call("POST", {"id": "miguel/send-and-go", "on": True})[0] == 200
    assert calls == [("miguel/send-and-go", True, "owner")]
    for bad in ({"Origin": "https://evil.example"}, {"X-Wizard-Store": None}, {"Tailscale-User-Login": "x@y.z"},
                {"Content-Type": "text/plain"}):
        assert call("POST", {"id": "miguel/send-and-go", "on": False}, **bad)[0] == 403, bad
    assert call("POST", {"id": "miguel/locked", "on": True})[0] == 409
    assert call("POST", ["nope"])[0] == 400
    assert calls == [("miguel/send-and-go", True, "owner")], "refused calls never reach look.json"


def test_http_maps_for_everyone_to_409(box, tmp_path, monkeypatch):
    def locked(*a, **k):
        raise fs.SwitchError("Feature switches are for testers", 409)
    monkeypatch.setattr(fs, "set_switch", locked)
    c = cli.Config()
    cli.Handler.cfg, cli.Handler.token = c, c.api_token()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/feature-switches",
                                     json.dumps({"id": "miguel/send-and-go", "on": True}).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {c.api_token()}", "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 409 and "for testers" in json.loads(e.value.read())["error"]
    finally:
        srv.shutdown()


def test_catalog_reads_the_running_shell_and_fails_plainly_without_one(box):
    assert [t["id"] for t in fs.catalog(box, fetch)] == ["miguel/snooze-someday", "miguel/send-and-go"]
    (box / "apps.json").write_text(json.dumps({"apps": []}))
    with pytest.raises(fs.SwitchError) as e:
        fs.catalog(box, fetch)
    assert e.value.code == 503


def test_an_unreadable_or_newer_look_json_is_never_overwritten(box):
    updates.set_channel("latest", box)
    for bad in ({"version": 2, "skin": {"id": "nebula", "at": 9}}, fs.UNREADABLE):
        pages = FakePages(bad)
        pages.get = lambda d=bad: ((d if fs._readable(d) else fs.UNREADABLE), "v1")
        assert all(t["on"] is False for t in fs.listing(box, fetch, pages)["switches"])
        with pytest.raises(fs.SwitchError) as e:
            fs.set_switch("miguel/snooze-someday", True, box, fetch, pages)
        assert e.value.args and pages.puts == [], "nothing was written over the owner's look.json"


def test_pages_get_marks_malformed_and_other_versions_unreadable(monkeypatch):
    p = fs.Pages("http://127.0.0.1:1")
    for text in ("{not json", json.dumps({"version": 2}), json.dumps([1])):
        monkeypatch.setattr(p, "_req", lambda m, b=None, t=text: (200, {"text": t, "version": "v9"}))
        assert p.get() == (fs.UNREADABLE, "v9")
    monkeypatch.setattr(p, "_req", lambda m, b=None: (200, {"text": json.dumps({"version": 1, "x": 1}), "version": "v9"}))
    assert p.get() == ({"version": 1, "x": 1}, "v9")
