"""Provisioning the shell-only token on a box (wizard-os#133, slice 4): `wam browser-boundary on|off`.

Off by default: no token, no override block, nothing changes. On: one random token in the App Manager's home, handed
read-only to the Agent Service and the shell through a marked block of their user-config override, and to `wam serve`.
Synthetic paths only.
"""
import json
import os
import stat

from wam import alongside, browser_boundary as bb
from wam.network import load_yaml

STORE = "wizardapps"
APPS = ["wizard-agent-service", "wizard-ux"]
ALONGSIDE_LIKE = """\
# Written by wam (alongside mode): header kept.
services:
  wizard-agent-service:
    network_mode: host
    networks: !reset []
    environment:
      WIZARD_HOST: 127.0.0.1
"""


def _ovr(rt, app):
    return bb.override_path(rt, STORE, app)


def test_off_by_default_writes_nothing(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    assert not bb.box_on(home)
    assert bb.sync_app(home, rt, STORE, "wizard-ux") is False
    assert not _ovr(rt, "wizard-ux").exists() and not bb.token_path(home).exists()
    env = {}
    assert bb.apply_box_state(home, env) is False and env == {}


def test_on_mints_one_token_and_hands_it_to_both_apps(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    p = _ovr(rt, "wizard-agent-service")
    p.parent.mkdir(parents=True)
    p.write_text(ALONGSIDE_LIKE)
    res = bb.set_box(home, rt, STORE, APPS, True)
    assert res["changed"] == APPS and bb.box_on(home)
    tok = bb.token_path(home)
    value = tok.read_text().strip()
    assert bb._HEX64.fullmatch(value)
    assert stat.S_IMODE(os.stat(tok.parent).st_mode) == 0o700
    for app in APPS:
        doc = load_yaml(_ovr(rt, app).read_text())
        svc = doc["services"][app]
        assert svc["environment"][bb.SWITCH] == "1" and svc["environment"][bb.TOKEN_FILE] == bb.MOUNT
        assert f"{tok}:{bb.MOUNT}:ro" in svc["volumes"]
        assert doc[bb.MARK]["service"] == app
    text = _ovr(rt, "wizard-agent-service").read_text()
    assert text.startswith("# Written by wam (alongside mode)") and "!reset" in text and "WIZARD_HOST" in text
    assert value not in text                                     # the token itself is never in a compose file
    bb.set_box(home, rt, STORE, APPS, True)                       # idempotent: same token, no duplicate mount
    assert tok.read_text().strip() == value
    assert load_yaml(_ovr(rt, "wizard-ux").read_text())["services"]["wizard-ux"]["volumes"].count(
        f"{tok}:{bb.MOUNT}:ro") == 1
    env = {}
    assert bb.apply_box_state(home, env) and env == {bb.SWITCH: "1", bb.TOKEN_FILE: str(tok)}
    assert bb.token(env) == value                                 # wam serve reads the same file the apps get
    assert bb.apply_box_state(home, {bb.SWITCH: "0"}) is False    # an explicit environment always wins
    st = bb.status(home, rt, STORE, APPS)
    assert st == {"on": True, "token": True, "apps": {a: True for a in APPS}}


def test_off_removes_exactly_the_block_and_the_token(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    p = _ovr(rt, "wizard-agent-service")
    p.parent.mkdir(parents=True)
    p.write_text(ALONGSIDE_LIKE)
    bb.set_box(home, rt, STORE, APPS, True)
    res = bb.set_box(home, rt, STORE, APPS, False)
    assert res["changed"] == APPS and not bb.box_on(home) and not bb.token_path(home).exists()
    assert not _ovr(rt, "wizard-ux").exists()                     # created only for the block: removed with it
    doc = load_yaml(p.read_text())
    assert bb.MARK not in doc and doc["services"]["wizard-agent-service"]["environment"] == {"WIZARD_HOST": "127.0.0.1"}
    assert "volumes" not in doc["services"]["wizard-agent-service"]


def test_a_hand_written_switch_is_never_overwritten(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    p = _ovr(rt, "wizard-ux")
    p.parent.mkdir(parents=True)
    p.write_text("services:\n  wizard-ux:\n    environment:\n      WIZARD_BROWSER_BOUNDARY: '0'\n")
    try:
        bb.set_box(home, rt, STORE, ["wizard-ux"], True)
    except ValueError as e:
        assert "hand-written" in str(e)
    else:
        raise AssertionError("expected a refusal")


def test_alongside_rewrite_keeps_the_block_when_on(tmp_path, monkeypatch):
    home, rt = tmp_path / "home", tmp_path / "rt"
    st = {"shell": {"port": 8796}, "agent": {"hermes_home": str(tmp_path / "hermes"), "origin": "http://127.0.0.1:8642"}}
    monkeypatch.setattr(alongside, "load", lambda h: st)
    monkeypatch.setattr(alongside, "save", lambda h, s: None)
    monkeypatch.setattr(alongside, "reconcile_images", lambda s, a: None)
    out = alongside.write_shell_override(rt, STORE, "wizard-ux", home, "shell")
    assert bb.MARK not in out.read_text()                         # off: the override is exactly what it was
    bb.set_box(home, rt, STORE, ["wizard-ux"], True)
    out = alongside.write_shell_override(rt, STORE, "wizard-ux", home, "shell")   # rewrites the whole file
    doc = load_yaml(out.read_text())
    assert doc[bb.MARK]["service"] == "wizard-ux" and doc["services"]["wizard-ux"]["environment"][bb.SWITCH] == "1"


def test_dry_run_changes_nothing(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    res = bb.set_box(home, rt, STORE, APPS, True, dry_run=True)
    assert res["would_change"] == APPS and not bb.state_path(home).exists()
    assert json.loads(json.dumps(res))


def test_a_refused_override_leaves_the_box_untouched(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    p = _ovr(rt, "wizard-ux")
    p.parent.mkdir(parents=True)
    p.write_text("services:\n  wizard-ux:\n    environment:\n      WIZARD_BROWSER_BOUNDARY: '0'\n")
    try:
        bb.set_box(home, rt, STORE, ["wizard-agent-service", "wizard-ux"], True)
    except ValueError:
        pass
    assert not bb.box_on(home) and not bb.token_path(home).exists()
    assert not _ovr(rt, "wizard-agent-service").exists()          # the earlier app was not changed either


def test_a_new_token_restarts_every_app_that_mounts_it(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    bb.set_box(home, rt, STORE, APPS, True)
    assert bb.set_box(home, rt, STORE, APPS, True)["changed"] == []   # same token: nothing to restart
    bb.token_path(home).write_text("broken\n")
    assert bb.set_box(home, rt, STORE, APPS, True)["changed"] == APPS


def test_malformed_overrides_are_refused_before_anything_changes(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    for bad in ("services: []\n", "services:\n  wizard-ux: []\n", "services:\n  wizard-ux:\n    volumes: x\n"):
        p = _ovr(rt, "wizard-ux")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(bad)
        try:
            bb.set_box(home, rt, STORE, APPS, True)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected a refusal for {bad!r}")
        assert not bb.box_on(home) and not _ovr(rt, "wizard-agent-service").exists() and p.read_text() == bad


def test_off_with_a_malformed_block_keeps_the_box_on(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    bb.set_box(home, rt, STORE, APPS, True)
    _ovr(rt, "wizard-ux").write_text(f"{bb.MARK}: broken\nservices: []\n")
    try:
        bb.set_box(home, rt, STORE, APPS, False)
    except ValueError:
        pass
    else:
        raise AssertionError("expected a refusal")
    assert bb.box_on(home) and bb.token_path(home).exists()
    assert bb.status_one(rt, STORE, "wizard-agent-service")      # the other app was not changed
