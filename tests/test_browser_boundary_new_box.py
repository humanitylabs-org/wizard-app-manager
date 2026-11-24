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


def test_restart_serve_only_restarts_a_running_unit():
    calls = []
    ok = lambda *a: calls.append(a) or SimpleNamespace(returncode=0, stderr="")
    assert bb.restart_serve(ok) == "restarted" and calls == [("try-restart", bb.API_UNIT)]
    bad = lambda *a: SimpleNamespace(returncode=1, stderr="Failed to connect to bus")
    assert bb.restart_serve(bad) == "error: Failed to connect to bus"


def test_the_bootstrap_command_restarts_serve_when_it_switched_on(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Config", lambda: SimpleNamespace(home=None))
    monkeypatch.setattr(cli, "bootstrap", lambda c: {"installed": [], "browser_boundary": {"on": True, "why": "new box"}})
    monkeypatch.setattr(bb, "restart_serve", lambda run=None: "restarted")
    cli.main(["bootstrap"])
    assert json.loads(capsys.readouterr().out)["browser_boundary"]["serve"] == "restarted"


# ---------------------------------------------------------------- a box that already ran Wizard: one release later

def _existing(tmp_path, **kw):
    args = dict(installed_before={"wizard-ux", "wizard-agent-service", "files"}, core=CORE - {"wizard-shell"},
                legacy="wizard-shell", current=True, mac=False, env={})
    args.update(kw)
    return bb.existing_box_reason(tmp_path, **args)


def test_an_existing_box_on_the_release_may_switch_on(tmp_path):
    assert _existing(tmp_path) is None


def test_an_existing_box_is_left_alone_when_it_should_be(tmp_path):
    assert _existing(tmp_path, installed_before=set()) == "not a box that already ran Wizard"
    assert _existing(tmp_path, installed_before={"wizard-shell", "files"}).startswith("the pre-split")
    # a split box keeps the stopped bundle as rollback material: it follows like any other
    assert _existing(tmp_path, installed_before={"wizard-shell", "wizard-ux", "wizard-agent-service"}) is None
    assert _existing(tmp_path, current=False).startswith("the shell or Agent Service is not on this release")
    assert _existing(tmp_path, mac=True).startswith("a Mac guest")
    assert _existing(tmp_path, env={bb.SWITCH: "0"}).startswith(bb.SWITCH)
    bb.set_box(tmp_path, tmp_path / "rt", "wizardapps", [], False)          # the owner chose off
    assert _existing(tmp_path) == "the owner chose"


def _bootstrap_existing(monkeypatch, tmp_path, live_commit="c0ffee", mac=False, extra_installed=frozenset(), mac_caps=None):
    from wam import release_pins, slots
    rt_root = tmp_path / "rt"
    for app in ("wizard-ux", "wizard-agent-service"):
        d = rt_root / "apps" / cli.STORE / app
        d.mkdir(parents=True)
        (d / ".wizard-commit").write_text(live_commit + "\n")
    pins = {"default_apps": {"shell": {"app": "wizard-ux", "commit": "c0ffee"},
                             "agent-service": {"app": "wizard-agent-service", "commit": "c0ffee"}}}
    recreated = []
    monkeypatch.delenv(bb.SWITCH, raising=False)
    monkeypatch.setattr(release_pins, "load", lambda: pins)
    monkeypatch.setattr(bb, "mac_guest", lambda: mac)
    caps = tmp_path / "host-capabilities.json"
    if mac_caps is not None:
        caps.write_text(json.dumps(mac_caps))
    monkeypatch.setattr(bb, "MAC_HOST_CAPS", str(caps))
    monkeypatch.setattr(cli, "settle_connection_mode", lambda c, rt: None)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"wizard-ux", "wizard-agent-service", "files", *extra_installed})
    if not extra_installed:
        monkeypatch.setattr(cli, "_alongside_apps", lambda c, rt=None: {"wizard-agent-service": "service", "wizard-ux": "shell"})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt: {"apps": []})
    monkeypatch.setattr(slots, "to_install", lambda reg: [])
    monkeypatch.setattr(slots, "table", lambda reg: [])
    monkeypatch.setattr(cli, "hand_overrides_step", lambda c, rt: None)
    monkeypatch.setattr(cli, "release_updates", lambda *a, **k: [])
    monkeypatch.setattr(cli, "relink_fresh_agent", lambda c, rt: None)
    monkeypatch.setattr(cli, "network_step", lambda c, rt: None)
    monkeypatch.setattr(cli, "recreate_apps", lambda c, rt, ids: recreated.extend(ids) or {a: "recreated" for a in ids})
    c = SimpleNamespace(runtipi=lambda: None, home=tmp_path / "home", runtipi_root=rt_root)
    return cli.bootstrap(c), c, recreated


def test_bootstrap_switches_an_existing_box_on_and_recreates_its_apps(monkeypatch, tmp_path):
    out, c, recreated = _bootstrap_existing(monkeypatch, tmp_path)
    assert out["browser_boundary"]["on"] is True and out["browser_boundary"]["why"] == "existing box"
    assert json.loads(bb.state_path(c.home).read_text())["why"] == "existing box"
    assert sorted(recreated) == ["wizard-agent-service", "wizard-ux"]
    for app in recreated:
        assert bb.status_one(c.runtipi_root, cli.STORE, app)
    # decided once: the next installer run changes nothing
    out2 = cli.bootstrap(c)
    assert "browser_boundary" not in out2


def test_bootstrap_waits_while_the_shell_is_not_on_the_release(monkeypatch, tmp_path):
    out, c, recreated = _bootstrap_existing(monkeypatch, tmp_path, live_commit="0ld")
    assert "browser_boundary" not in out and not bb.state_path(c.home).exists() and not recreated


def test_bootstrap_leaves_a_mac_guest_off(monkeypatch, tmp_path):
    out, c, recreated = _bootstrap_existing(monkeypatch, tmp_path, mac=True)
    assert "browser_boundary" not in out and not bb.state_path(c.home).exists() and not recreated


def test_bootstrap_switches_a_mac_guest_on_once_its_mac_app_sends_the_token(monkeypatch, tmp_path):
    # wizard-os#133: the guest update of a Mac app whose own owner calls carry the token says so
    out, c, recreated = _bootstrap_existing(monkeypatch, tmp_path, mac=True, mac_caps={"shell_token": True})
    assert out["browser_boundary"]["why"] == "existing box" and sorted(recreated) == ["wizard-agent-service", "wizard-ux"]


def test_a_mac_guest_stays_off_without_a_clear_yes_from_its_mac_app(monkeypatch, tmp_path):
    for i, caps in enumerate(({"shell_token": False}, {"shell_token": "yes"}, {}, [])):
        (tmp_path / str(i)).mkdir()
        out, c, recreated = _bootstrap_existing(monkeypatch, tmp_path / str(i), mac=True, mac_caps=caps)
        assert "browser_boundary" not in out and not recreated, caps
    assert bb.mac_host_sends_token(str(tmp_path / "missing.json")) is False
    (tmp_path / "bad.json").write_text("{not json")
    assert bb.mac_host_sends_token(str(tmp_path / "bad.json")) is False


def test_bootstrap_switches_a_split_box_that_keeps_the_stopped_bundle_on(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_alongside_apps", lambda c, rt=None: {"wizard-agent-service": "service", "wizard-ux": "shell",
                                                                      "wizard-shell": "bundle"})
    out, c, recreated = _bootstrap_existing(monkeypatch, tmp_path, extra_installed={"wizard-shell"})
    assert out["browser_boundary"]["why"] == "existing box"
