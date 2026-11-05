"""Amendment 1: the owner's device is a device in the Wizard's device record, not any node signed in as the owner."""
import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import owner_devices as od, owner_edge  # noqa: E402

from test_owner_edge import BOX, BOX_IP, OWNER, PHONE, FakeIdentity, call, edge, serve_headers  # noqa: E402,F401

SERVER = "100.64.0.20"      # the owner's other, untagged server: the one where agents run
LAPTOP = "100.64.0.30"


def node(name, os_="linux", login=OWNER, tags=None, sid="nAAA"):
    return {"Node": {"ID": 1, "StableID": sid, "Name": f"{name}.tail1234.ts.net.", "Tags": tags,
                     "Hostinfo": {"OS": os_, "Hostname": name}}, "UserProfile": {"LoginName": login}}


TAILNET = {PHONE: node("pixel", "android", sid="nPHONE"), SERVER: node("agent-box", "linux", sid="nSRV"),
           LAPTOP: node("laptop", "macOS", sid="nLAP"), BOX_IP: node("box", "linux", sid="nBOX")}


def whois(ip):
    if ip not in TAILNET:
        raise od.Unverified("unknown")
    return TAILNET[ip]


def record(tmp, **reg):
    (tmp / "devices.json").write_text(json.dumps(reg))


# ---------------------------------------------------------------- the rule over the record

def test_a_connected_phone_is_an_owner_device_without_any_entry(tmp_path, monkeypatch):
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    assert od.Enrollment(tmp_path, whois).check(PHONE, OWNER) is None


def test_the_owners_other_server_is_not_an_owner_device(tmp_path, monkeypatch):
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    assert od.Enrollment(tmp_path, whois).check(SERVER, OWNER) == od.REQUIRED
    assert od.Enrollment(tmp_path, whois).check(BOX_IP, OWNER) == od.REQUIRED       # and never the box itself


def test_a_machine_marked_human_is_an_owner_device(tmp_path, monkeypatch):
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    record(tmp_path, groups={"agent-box": "human"})                 # a server only counts once the owner says so
    assert od.Enrollment(tmp_path, whois).check(SERVER, OWNER) is None


def test_a_mac_is_not_the_owner_until_marked_human(tmp_path, monkeypatch):
    """Review P1: a Mac that runs agents (common) must not pass because of its OS."""
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    assert od.Enrollment(tmp_path, whois).check(LAPTOP, OWNER) == od.REQUIRED
    record(tmp_path, groups={"laptop": "human"})
    assert od.Enrollment(tmp_path, whois).check(LAPTOP, OWNER) is None
    record(tmp_path, groups={"laptop": "human"}, devices=[{"id": "laptop", "connect": {"type": "ssh", "host": "laptop"}}])
    assert od.Enrollment(tmp_path, whois).check(LAPTOP, OWNER) == od.REQUIRED     # a Mac the Wizard works on is not the owner


def test_a_node_cannot_claim_a_human_mark_by_renaming_itself(tmp_path, monkeypatch):
    """Review P3: Hostinfo.Hostname is self-reported; only the assigned MagicDNS name grants a mark."""
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    record(tmp_path, groups={"laptop": "human"})
    liar = node("agent-box", "linux", sid="nSRV")
    liar["Node"]["Hostinfo"]["Hostname"] = "laptop"
    assert od.Enrollment(tmp_path, lambda ip: liar).check(SERVER, OWNER) == od.REQUIRED
    record(tmp_path, groups={"laptop": "client"})                      # but a mark under any name can still take access away
    assert od.Enrollment(tmp_path, lambda ip: node("pixel", "android")).check(PHONE, OWNER) is None
    pixel = node("pixel", "android"); pixel["Node"]["Hostinfo"]["Hostname"] = "laptop"
    assert od.Enrollment(tmp_path, lambda ip: pixel).check(PHONE, OWNER) == od.REQUIRED


@pytest.mark.parametrize("reg", [
    {"groups": {"agent-box": "agent"}},
    {"groups": {"pixel": "client"}},
    {"groups": {"pixel": "other"}},
    {"devices": [{"id": "pixel", "connect": {"type": "ssh", "host": "x"}}], "groups": {"pixel": "human"}},
    {"devices": [{"id": "build", "connect": {"type": "ssh", "host": "pixel"}}]},
    {"devices": [{"id": "build", "connect": {"type": "ssh", "host": "x"}, "tailnet": ["pixel"]}]},
    {"this": {"tailnet": ["pixel"]}},
])
def test_a_machine_the_wizard_works_on_is_never_the_owner(tmp_path, monkeypatch, reg):
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    record(tmp_path, **reg)
    assert od.Enrollment(tmp_path, whois).check(PHONE if "pixel" in json.dumps(reg) else SERVER, OWNER) == od.REQUIRED


def test_a_server_marked_human_but_registered_as_a_wizard_machine_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    record(tmp_path, groups={"agent-box": "human"},
           devices=[{"id": "agent-box", "connect": {"type": "ssh", "host": "agent-box"}}])
    assert od.Enrollment(tmp_path, whois).check(SERVER, OWNER) == od.REQUIRED


@pytest.mark.parametrize("hop", ["", "not-an-ip", "100.64.0.9, 100.64.0.20", "100.64.0.9:443", "100.64.0.99"])
def test_a_lookup_that_cannot_resolve_one_node_fails_closed(tmp_path, monkeypatch, hop):
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    assert od.Enrollment(tmp_path, whois).check(hop, OWNER) == od.UNVERIFIED


def test_whois_errors_of_any_kind_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    def boom(ip):
        raise RuntimeError("tailscaled is down")
    assert od.Enrollment(tmp_path, boom).check(PHONE, OWNER) == od.UNVERIFIED
    assert od.Enrollment(tmp_path, lambda ip: {}).check(PHONE, OWNER) == od.UNVERIFIED
    assert od.Enrollment(tmp_path, lambda ip: {"Node": {"Name": "x."}, "UserProfile": {"LoginName": OWNER}}
                         ).check(PHONE, OWNER) == od.UNVERIFIED                     # no stable ID


def test_an_unreadable_record_fails_closed_not_open(tmp_path, monkeypatch):
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    (tmp_path / "devices.json").write_text("{not json")
    assert od.Enrollment(tmp_path, whois).check(PHONE, OWNER) == od.UNVERIFIED


def test_a_tagged_node_or_another_login_is_not_the_owners_device(tmp_path, monkeypatch):
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    tagged = node("pixel", "android", tags=["tag:server"])
    assert od.Enrollment(tmp_path, lambda ip: tagged).check(PHONE, OWNER) == od.REQUIRED
    assert od.Enrollment(tmp_path, lambda ip: node("pixel", "android", login="guest@example.com")).check(PHONE, OWNER) == od.REQUIRED


def test_the_real_whois_needs_tailscale_and_a_clean_answer(monkeypatch):
    monkeypatch.setattr(od.shutil, "which", lambda name: None)
    with pytest.raises(od.Unverified):
        od.tailscale_whois("100.64.0.9")


# ---------------------------------------------------------------- the edge asks it

@pytest.fixture
def enrolled(edge, tmp_path, monkeypatch):
    """The conftest edge, but the device check is the real Enrollment over a synthetic tailnet and record."""
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    edge["srv"].devices = od.Enrollment(tmp_path, whois)
    edge["record"] = tmp_path
    return edge


def test_the_owners_phone_gets_a_signed_request(enrolled):
    assert call(enrolled, headers=serve_headers(**{"X-Forwarded-For": PHONE}))[0] == 200


def test_the_owners_other_server_is_refused_before_anything_is_forwarded(enrolled):
    status, _, body = call(enrolled, headers=serve_headers(**{"X-Forwarded-For": SERVER}))
    assert status == 403 and json.loads(body)["error"]["code"] == "owner_device_required"
    assert enrolled["seen"] == []


def test_an_unresolvable_caller_is_refused_with_its_own_code(enrolled):
    status, _, body = call(enrolled, headers=serve_headers(**{"X-Forwarded-For": "100.64.0.99"}))
    assert status == 403 and json.loads(body)["error"]["code"] == "owner_device_unverified"
    assert enrolled["seen"] == []


def test_the_default_edge_has_a_real_enrollment_check_and_no_tailscale_means_no_access(monkeypatch):
    """serve_app without an injected check builds the real one: on a machine with no tailscale, nobody gets in."""
    from wam import owner_proof as op
    monkeypatch.setattr(od.shutil, "which", lambda name: None)
    import shutil, tempfile
    root = Path(tempfile.mkdtemp(prefix="oe"))      # a short path: Unix socket names are limited
    home = root / "h"
    op.ensure_edge_key(home, "messenger")
    srv = owner_edge.serve_app(home, {"id": "messenger", "name": "M", "port": 9, "activity": [], "strip": []}, FakeIdentity(),
                               peer_check=lambda conn: True)
    try:
        assert isinstance(srv.devices, od.Enrollment)
        assert srv.devices.check(PHONE, OWNER) == od.UNVERIFIED
    finally:
        srv.server_close()
        shutil.rmtree(root, ignore_errors=True)
