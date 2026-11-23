"""A new box starts with the browser boundary on (wizard-os#133, Miguel 23 Nov, option A).

`wam bootstrap` settles it once, before anything is installed: a box with no shell / Agent Service yet and no
recorded choice is switched on; a box that already runs Wizard, or whose owner chose, is left as it is. The box's
own programs that call the owner API on its loopback send the shell token. Synthetic paths only.
"""
import json
import urllib.request
from types import SimpleNamespace

from wam import browser_boundary as bb, cli

CORE = {"wizard-ux", "wizard-agent-service", "wizard-shell"}
DEFAULTS = {"wizard-ux", "wizard-agent-service", "hermes-agent", "files", "notifications"}


def test_a_new_box_starts_on_and_mints_the_token(tmp_path):
    out = bb.settle_new_box(tmp_path, set(), CORE, DEFAULTS, env={})
    assert out == {"on": True, "why": "new box"}
    assert bb.box_on(tmp_path)
    assert bb._HEX64.fullmatch(bb.token_path(tmp_path).read_text().strip())
    assert json.loads(bb.state_path(tmp_path).read_text())["why"] == "new box"
    # the shell and the Agent Service then get their block when bootstrap installs them
    assert bb.sync_app(tmp_path, tmp_path / "rt", "wizardapps", "wizard-ux") is True
    assert bb.MARK in bb.override_path(tmp_path / "rt", "wizardapps", "wizard-ux").read_text()
    # decided once: a later installer run changes nothing
    assert bb.settle_new_box(tmp_path, {"wizard-ux"}, CORE, DEFAULTS, env={}) is None


def test_a_box_that_already_runs_wizard_is_left_off(tmp_path):
    assert bb.settle_new_box(tmp_path, {"wizard-ux", "files"}, CORE, DEFAULTS, env={}) is None
    assert bb.settle_new_box(tmp_path, {"wizard-shell"}, CORE, DEFAULTS, env={}) is None   # pre-split bundle
    assert not bb.box_on(tmp_path) and not bb.state_path(tmp_path).exists()
    assert not bb.token_path(tmp_path).exists()


def test_a_box_with_owner_apps_but_no_shell_is_left_off(tmp_path):
    assert bb.settle_new_box(tmp_path, {"my-own-app"}, CORE, DEFAULTS, env={}) is None
    assert not bb.state_path(tmp_path).exists()


def test_an_owner_choice_is_never_changed(tmp_path):
    bb.set_box(tmp_path, tmp_path / "rt", "wizardapps", [], False)          # `wam browser-boundary off`
    assert bb.settle_new_box(tmp_path, set(), CORE, DEFAULTS, env={}) is None
    assert not bb.box_on(tmp_path)


def test_the_installer_can_keep_a_new_box_off(tmp_path):
    assert bb.settle_new_box(tmp_path, set(), CORE, DEFAULTS, env={bb.SWITCH: "0"}) is None
    assert not bb.state_path(tmp_path).exists()
    # get.sh forwards an unset value as empty: that is the default (on)
    assert bb.settle_new_box(tmp_path, set(), CORE, DEFAULTS, env={bb.SWITCH: ""})["on"] is True


def test_bootstrap_settles_it_before_installing_anything(monkeypatch, tmp_path):
    from wam import release_pins, slots
    order = []
    monkeypatch.delenv(bb.SWITCH, raising=False)
    monkeypatch.setattr(release_pins, "load", lambda: None)
    monkeypatch.setattr(cli, "settle_connection_mode", lambda c, rt: None)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set())
    monkeypatch.setattr(cli, "write_registry", lambda c, rt: {"apps": []})
    monkeypatch.setattr(slots, "to_install", lambda reg: ["wizard-ux"])
    monkeypatch.setattr(slots, "table", lambda reg: [])
    monkeypatch.setattr(cli, "install", lambda c, a: order.append((a, bb.box_on(tmp_path))) or {"id": a})
    monkeypatch.setattr(cli, "hand_overrides_step", lambda c, rt: None)
    monkeypatch.setattr(cli, "release_updates", lambda *a, **k: [])
    monkeypatch.setattr(cli, "relink_fresh_agent", lambda c, rt: None)
    monkeypatch.setattr(cli, "network_step", lambda c, rt: None)
    out = cli.bootstrap(SimpleNamespace(runtipi=lambda: None, home=tmp_path))
    assert order == [("wizard-ux", True)]
    assert out["browser_boundary"] == {"on": True, "why": "new box"}


def test_bootstrap_never_fails_on_it(monkeypatch, capsys):
    monkeypatch.setattr(cli, "installed_ids", lambda rt: (_ for _ in ()).throw(RuntimeError("runtipi down")))
    assert cli.settle_browser_boundary(SimpleNamespace(home=None), None) is None
    assert "browser boundary left off" in capsys.readouterr().err


def test_box_programs_send_the_token_to_the_owner_api(tmp_path, monkeypatch):
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path))
    assert bb.owner_headers() == {}
    bb.ensure_token(tmp_path)
    tok = bb.token_path(tmp_path).read_text().strip()
    assert bb.owner_headers() == {bb.HEADER: tok}
    seen = []

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return b"{}"

    def fake_urlopen(req, timeout=None):
        seen.append(req.get_header(bb.HEADER.capitalize()))
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    from wam import agent_access, feature_switches, graceful_restart
    agent_access._req("http://127.0.0.1:1", "GET", "/wizard/v1/agents")
    graceful_restart.http("GET", "http://127.0.0.1:1/wizard/v1/restart")
    feature_switches.Pages("http://127.0.0.1:1")._req("GET")
    assert len(seen) == 3 and all(s == tok for s in seen)
