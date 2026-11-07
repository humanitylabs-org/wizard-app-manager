"""Feature switches for the Wizard OS Tracker app (wam/feature_switches.py; Miguel, 7 Nov 2026): list and turn one on or
off for this box, only on For testers, only for the owner or an app holding a minted key. No real box is touched: the
shell catalog and the Agent Service Pages API are fakes."""
import hashlib
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


def test_only_the_tracker_can_get_a_key_and_only_its_hash_stays_on_the_box(box, tmp_path):
    kdir = tmp_path / "app-data" / "wizard-os-tracker" / "wizard"
    res = fs.grant("wizard-os-tracker", kdir, box)
    key = (kdir / fs.KEYFILE).read_text().strip()
    rec = json.loads((box / "grants" / "feature-switches.json").read_text())
    assert res["granted"] and len(key) == 64 and key not in json.dumps(rec)
    assert rec == {"incoming": [{"from": "wizard-os-tracker", "sha256": hashlib.sha256(key.encode()).hexdigest()}]}
    assert ((box / "grants" / "feature-switches.json").stat().st_mode & 0o077) == 0
    assert fs.caller(key, box) == "wizard-os-tracker"
    assert fs.caller("0" * 64, box) is None and fs.caller(None, box) is None and fs.caller("short", box) is None
    with pytest.raises(fs.SwitchError) as e:
        fs.grant("money", tmp_path / "m", box)
    assert e.value.code == 403
    fs.grant("wizard-os-tracker", kdir, box)                    # re-minting replaces the old key
    assert fs.caller(key, box) is None
    fs.revoke("wizard-os-tracker", kdir, box)
    assert not (kdir / fs.KEYFILE).exists() and fs.caller(key, box) is None


def test_http_gate_owner_token_or_tracker_key_and_nothing_else(box, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(fs, "listing", lambda *a, **k: {"switches": [], "locked": False})
    monkeypatch.setattr(fs, "set_switch", lambda tid, on, *a, **k: calls.append((tid, on, k.get("by"))) or {"ok": True})
    key = None
    kdir = tmp_path / "kd"
    fs.grant("wizard-os-tracker", kdir, box)
    key = (kdir / fs.KEYFILE).read_text().strip()
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
        assert call("GET", {"X-Wizard-Grant": "f" * 64})[0] == 403
        assert call("POST", {"Authorization": "Bearer nope"}, {"id": "miguel/send-and-go", "on": True})[0] == 401
        assert call("GET", {"X-Wizard-Grant": key}) == (200, {"switches": [], "locked": False})
        assert call("POST", {"X-Wizard-Grant": key}, {"id": "miguel/send-and-go", "on": True})[0] == 200
        assert call("POST", {"Authorization": f"Bearer {c.api_token()}"}, {"id": "miguel/send-and-go", "on": False})[0] == 200
        assert calls == [("miguel/send-and-go", True, "wizard-os-tracker"), ("miguel/send-and-go", False, "owner")]
        # The key opens only this route, never the owner's other changes.
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/install", b"{}", method="POST", headers={"X-Wizard-Grant": key})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 401
    finally:
        srv.shutdown()


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
