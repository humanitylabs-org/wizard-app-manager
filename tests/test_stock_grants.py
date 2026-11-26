"""App grants and connections served by wam on stock Runtipi (wizard-os#822, wam/stock_grants.py).

Same rules as the fork's backend (packages/backend/src/modules/docker/wizard-app-grants.ts, wizard-storage.ts):
declared connections are approved at install, a Disconnect is sticky, a sensitive app's new connections wait for the
owner, providers get the key hash, consumers get the key. With the switch off nothing here runs.
"""
import hashlib
import json
from pathlib import Path

import pytest

from wam import runtipi, stock_grants as sg, stock_runtipi


class FakeRt:
    def __init__(self, ids):
        self.ids = list(ids)

    def installed(self):
        return [{"info": {"urn": f"{i}:wizardapps"}} for i in self.ids]


def app(root: Path, app_id: str, uses=(), provides=(), port=8000, name=None, store="wizardapps"):
    d = root / "apps" / store / app_id
    d.mkdir(parents=True)
    fm = {"id": app_id, "name": name or app_id.title(), "uses": list(uses), "provides": list(provides)}
    (d / "wizard.md").write_text("---\n" + json.dumps(fm) + "\n---\n# x\n")
    (d / "docker-compose.yml").write_text(json.dumps({"services": {app_id: {
        "image": "x", "x-runtipi": {"is_main": True, "internal_port": port}}}}))
    (d / "config.json").write_text("{}")
    (root / "app-data" / store / app_id).mkdir(parents=True)


def files_of(root, app_id):
    return json.loads((root / "app-data" / "wizardapps" / app_id / "wizard" / "grants.json").read_text())


@pytest.fixture
def box(tmp_path, monkeypatch):
    root = tmp_path / "runtipi"
    home = tmp_path / "home"
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.delenv("WIZARD_STOCK_RUNTIPI", raising=False)
    app(root, "wizard-podcasts", uses=["music-source"], provides=["podcasts"], port=9205, name="Podcasts")
    app(root, "wizard-youtube-music", provides=["music-source"], port=9206, name="YouTube Music")
    app(root, "wizard-notes", port=8799)
    rt = FakeRt(["wizard-podcasts", "wizard-youtube-music", "wizard-notes"])
    sg._stamps.clear()
    sg._last_full["at"] = 0.0
    return root, home, rt


def test_switch_is_off_by_default_and_follows_env_and_file(tmp_path, monkeypatch):
    monkeypatch.delenv("WIZARD_STOCK_RUNTIPI", raising=False)
    assert stock_runtipi.on(tmp_path) is False
    (tmp_path / "stock-runtipi.json").write_text('{"on": true}')
    assert stock_runtipi.on(tmp_path) is True
    monkeypatch.setenv("WIZARD_STOCK_RUNTIPI", "0")
    assert stock_runtipi.on(tmp_path) is False
    monkeypatch.setenv("WIZARD_STOCK_RUNTIPI", "1")
    (tmp_path / "stock-runtipi.json").unlink()
    assert stock_runtipi.on(tmp_path) is True


def test_reconcile_approves_declared_connection_and_publishes_keys_and_hashes(box):
    root, home, rt = box
    res = sg.Grants(root, rt, home).reconcile()
    assert res["apps"] == 3
    state = json.loads((root / "state" / "wizard-grants.json").read_text())
    assert [(g["from"], g["to"], g["approved"]) for g in state["grants"]] == [
        ("wizard-podcasts", "slot:music-source", "install")]
    assert state["seeded"] == ["wizard-podcasts", "wizard-youtube-music", "wizard-notes"]
    consumer = files_of(root, "wizard-podcasts")
    provider = files_of(root, "wizard-youtube-music")
    key = consumer["outgoing"]["slot:music-source"]
    assert len(key) == 64
    assert provider["incoming"] == [{"from": "wizard-podcasts", "sha256": hashlib.sha256(key.encode()).hexdigest()}]
    assert consumer["provider_endpoints"] == {"slot:music-source": "http://wizard-youtube-music:9206"}
    assert consumer["provider_endpoints_all"] == {"slot:music-source": {"wizard-youtube-music": "http://wizard-youtube-music:9206"}}
    assert files_of(root, "wizard-notes") == {"outgoing": {}, "incoming": [], "provider_endpoints": {},
                                              "enforce": False, "provider_endpoints_all": {}}
    assert (root / "app-data" / "wizardapps" / "wizard-notes" / "wizard-requests").is_dir()
    # A second reconcile keeps the same key (no churn) and the first install was quiet (no notice).
    sg.Grants(root, rt, home).reconcile()
    assert files_of(root, "wizard-podcasts")["outgoing"]["slot:music-source"] == key
    assert not json.loads((root / "state" / "wizard-grants.json").read_text()).get("notices")


def test_disconnect_is_sticky_and_revokes_the_key(box):
    root, home, rt = box
    g = sg.Grants(root, rt, home)
    g.reconcile()
    out = sg.handle(g, "POST", "/apps/wizard-podcasts:wizardapps/app-grants", {"to": "slot:music-source", "approve": False})
    assert out["grants"] == []
    assert [(d["from"], d["to"]) for d in out["connections"]["denied"]] == [("wizard-podcasts", "slot:music-source")]
    assert files_of(root, "wizard-podcasts")["outgoing"] == {}
    assert files_of(root, "wizard-youtube-music")["incoming"] == []
    g.reconcile()   # an update or a later reconcile never adds it back
    assert sg.handle(g, "GET", "/apps/wizard/grants", None) == []
    sg.handle(g, "POST", "/apps/wizard-podcasts:wizardapps/app-grants", {"to": "slot:music-source", "approve": True})
    assert files_of(root, "wizard-podcasts")["outgoing"].keys() == {"slot:music-source"}
    assert sg.handle(g, "GET", "/apps/wizard/connections", None)["grants"][0]["approved"] == "owner"


def test_sensitive_provider_makes_a_new_connection_wait_for_the_owner(box):
    root, home, rt = box
    g = sg.Grants(root, rt, home)
    g.reconcile()
    st = sg.handle(g, "POST", "/apps/wizard-notes:wizardapps/sensitive", {"sensitive": True})
    assert st["sensitive"] == ["wizard-notes"]
    req = root / "app-data" / "wizardapps" / "wizard-podcasts" / "wizard-requests" / "connections.json"
    req.write_text(json.dumps({"connections": ["wizard-notes"]}))
    g.reconcile()
    st = g.connections()
    assert [(p["from"], p["to"], p["reason"]) for p in st["pending"]] == [("wizard-podcasts", "wizard-notes", "request")]
    assert st["notices"][-1]["kind"] == "pending"
    assert "which you marked sensitive" in st["notices"][-1]["text"]
    assert "wizard-notes" not in files_of(root, "wizard-podcasts")["outgoing"]
    # The owner approves it: an owner grant, the key goes out.
    sg.handle(g, "POST", "/apps/wizard-podcasts:wizardapps/app-grants", {"to": "wizard-notes", "approve": True})
    assert "wizard-notes" in files_of(root, "wizard-podcasts")["outgoing"]
    assert g.connections()["pending"] == []
    ids = [n["id"] for n in g.connections()["notices"]]
    assert all(n["delivered"] for n in sg.handle(g, "POST", "/apps/wizard/connection-notices", {"delivered": ids})["notices"])


def test_a_gateway_app_cant_be_marked_sensitive_and_bad_input_is_a_400(box):
    root, home, rt = box
    app(root, "wizard-ux", provides=["shell"])
    rt.ids.append("wizard-ux")
    g = sg.Grants(root, rt, home)
    with pytest.raises(sg.GrantError, match="gateway"):
        sg.handle(g, "POST", "/apps/wizard-ux:wizardapps/sensitive", {"sensitive": True})
    with pytest.raises(sg.GrantError, match="mode must be"):
        sg.handle(g, "POST", "/apps/wizard/connection-mode", {"mode": "x"})
    with pytest.raises(sg.GrantError, match="Consumer not installed"):
        sg.handle(g, "POST", "/apps/nope:wizardapps/app-grants", {"to": "wizard-notes", "approve": True})


def test_reset_drops_owner_decisions_and_reapproves_declared(box):
    root, home, rt = box
    g = sg.Grants(root, rt, home)
    g.reconcile()
    sg.handle(g, "POST", "/apps/wizard-podcasts:wizardapps/app-grants", {"to": "slot:music-source", "approve": False})
    sg.handle(g, "POST", "/apps/wizard-notes:wizardapps/app-grants", {"to": "wizard-podcasts", "approve": True})
    st = sg.handle(g, "POST", "/apps/wizard/connections-reset", {})
    assert [(x["from"], x["to"], x["approved"]) for x in st["grants"]] == [("wizard-podcasts", "slot:music-source", "install")]
    assert st["denied"] == []
    assert files_of(root, "wizard-notes")["outgoing"] == {}


def test_mode_and_proposals(box):
    root, home, rt = box
    g = sg.Grants(root, rt, home)
    assert sg.handle(g, "GET", "/apps/wizard-podcasts:wizardapps/proposed-grants", None) == [
        {"kind": "app", "from": "wizard-podcasts", "to": "slot:music-source", "reason": "uses"}]
    assert sg.handle(g, "POST", "/apps/wizard/connection-mode", {"mode": "open"})["mode"] == "open"
    assert json.loads((root / "state" / "wizard-grants.json").read_text())["mode"] == "open"


def test_an_uninstalled_app_loses_its_grants_but_denials_stay(box):
    root, home, rt = box
    g = sg.Grants(root, rt, home)
    g.reconcile()
    sg.handle(g, "POST", "/apps/wizard-notes:wizardapps/app-grants", {"to": "wizard-youtube-music", "approve": False})
    rt.ids.remove("wizard-podcasts")
    res = g.reconcile()
    assert res["removed"] == ["wizard-podcasts"]
    st = g.connections()
    assert st["grants"] == []
    assert [(d["from"], d["to"]) for d in st["denied"]] == [("wizard-notes", "wizard-youtube-music")]
    assert files_of(root, "wizard-youtube-music")["incoming"] == []


def test_tick_reconciles_only_on_a_change(box):
    root, home, rt = box
    assert sg.tick(root, rt, home, now=1000.0)["apps"] == 3
    assert sg.tick(root, rt, home, now=1001.0) is None
    req = root / "app-data" / "wizardapps" / "wizard-notes" / "wizard-requests" / "connections.json"
    req.write_text('["wizard-youtube-music"]')
    assert sg.tick(root, rt, home, now=1002.0)["changed"] is True
    assert "wizard-youtube-music" in files_of(root, "wizard-notes")["outgoing"]
    assert sg.tick(root, rt, home, now=1003.0 + sg.FULL_EVERY)["changed"] is False


def test_runtipi_client_answers_fork_routes_only_with_the_switch_on(box, monkeypatch):
    root, home, rt = box
    client = runtipi.Runtipi("http://127.0.0.1:9/api", "s", root=root)
    monkeypatch.setattr(sg, "installed_urns", lambda _rt: [(i, "wizardapps") for i in rt.ids])
    with pytest.raises(OSError):     # off: the request goes to Runtipi (here: nothing listens)
        client.req("GET", "/apps/wizard/connections", timeout=2)
    monkeypatch.setenv("WIZARD_STOCK_RUNTIPI", "1")
    assert client.req("GET", "/apps/wizard/connections")["mode"] == "declared"
    with pytest.raises(runtipi.RuntipiError, match="-> 400"):
        client.req("POST", "/apps/wizard/connection-mode", {"mode": "nope"})
    # Routes stock Runtipi serves itself are never intercepted.
    assert not sg.handles("/apps/installed") and not sg.handles("/apps/wizard-notes:wizardapps")


def test_a_malformed_app_file_is_never_replaced(box):
    root, home, rt = box
    p = root / "app-data" / "wizardapps" / "wizard-youtube-music" / "wizard"
    p.mkdir()
    (p / "grants.json").write_text('{"outgoing": []}')
    with pytest.raises(ValueError, match="refusing to replace"):
        sg.Grants(root, rt, home).reconcile()
    assert (p / "grants.json").read_text() == '{"outgoing": []}'


def test_files_never_leave_the_runtipi_root(tmp_path):
    f = sg.Files(tmp_path / "runtipi")
    with pytest.raises(ValueError, match="outside the Runtipi root"):
        f.read(tmp_path / "runtipi" / "repos" / ".." / ".." / "etc")
    with pytest.raises(ValueError, match="outside the Runtipi root"):
        f.write(tmp_path / "elsewhere.json", "{}")


# ---------------------------------------------------------------- file shares (the Claude sign-in pair)

class RunningRt(FakeRt):
    def __init__(self, ids):
        super().__init__(ids)
        self.restarted = []

    def app(self, urn):
        return {"app": {"status": "running"}}

    def restart(self, urn):
        self.restarted.append(urn)
        return {}


def claude_pair(root: Path):
    """The two apps as the generated stock store ships them: the Claude app's legacy volume is its own data/claude, the
    Hermes Agent's is a placeholder folder in its engine service; neither wizard.md declares the share."""
    for app_id, services in (
        ("wizard-claude", {"wizard-claude": {"image": "x", "x-runtipi": {"is_main": True, "internal_port": 8799}},
                           "wizard-claude-engine": {"image": "y", "user": "1000:1000",
                                                    "volumes": ["${APP_DATA_DIR}/data/claude:/claude"]}}),
        ("hermes-agent", {"hermes-agent": {"image": "x", "x-runtipi": {"is_main": True, "internal_port": 9119}},
                          "hermes-agent-engine": {"image": "y", "volumes": [
                              "${APP_DATA_DIR}/data:/opt/data", "${APP_DATA_DIR}/volumes/claude-login:/claude"]}}),
    ):
        d = root / "apps" / "wizardapps" / app_id
        d.mkdir(parents=True)
        (d / "wizard.md").write_text("---\n" + json.dumps({"id": app_id, "name": app_id}) + "\n---\n# x\n")
        (d / "docker-compose.yml").write_text(json.dumps({"services": services}))
        (d / "config.json").write_text("{}")
        (root / "app-data" / "wizardapps" / app_id).mkdir(parents=True)


def test_claude_share_is_proposed_then_mounted_in_the_engine_and_revoked(tmp_path, monkeypatch):
    root, home = tmp_path / "runtipi", tmp_path / "home"
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    claude_pair(root)
    override = root / "user-config" / "wizardapps" / "hermes-agent" / "docker-compose.yml"
    override.parent.mkdir(parents=True)
    override.write_text("# App networks: the block marked x-wizard-networks is written by wam (`wam network reconcile`).\n"
                        "services:\n  hermes-agent-engine:\n    networks: !override\n      wizard-core: null\n"
                        "x-wizard-networks:\n  services: [hermes-agent-engine]\n  networks: []\n")
    rt = RunningRt(["wizard-claude", "hermes-agent"])
    g = sg.Grants(root, rt, home)
    g.reconcile()
    # A new pair needs consent: proposed, not mounted.
    assert g.proposed("hermes-agent")[:1] == [
        {"kind": "share", "from": "hermes-agent", "to": "wizard-claude", "share": "claude-login", "reason": "mounts"}]
    assert "x-wizard-shares" not in override.read_text()
    sg.handle(g, "POST", "/apps/hermes-agent:wizardapps/share-grants",
              {"to": "wizard-claude", "share": "claude-login", "approve": True})
    from wam.network import load_yaml
    doc = load_yaml(override.read_text())
    engine = doc["services"]["hermes-agent-engine"]
    assert engine["volumes"] == [{"type": "bind", "source": f"{root}/app-data/wizardapps/wizard-claude/data/claude",
                                  "target": "/claude", "bind": {"create_host_path": False}}]
    assert engine["networks"].tag == "!override"            # wam's network block is kept
    assert doc["x-wizard-networks"]["services"] == ["hermes-agent-engine"]
    assert doc["x-wizard-shares"] == {"services": {"hermes-agent-engine": ["/claude"]}}
    assert "hermes-agent" not in doc["services"]             # only the service that mounts /claude gets it
    assert (root / "app-data" / "wizardapps" / "wizard-claude" / "data" / "claude").is_dir()
    assert rt.restarted == ["hermes-agent:wizardapps"]
    g.reconcile()                                           # idempotent: no second restart
    assert rt.restarted == ["hermes-agent:wizardapps"]
    sg.handle(g, "POST", "/apps/hermes-agent:wizardapps/share-grants",
              {"to": "wizard-claude", "share": "claude-login", "approve": False})
    doc = load_yaml(override.read_text())
    assert "volumes" not in doc["services"]["hermes-agent-engine"] and "x-wizard-shares" not in doc
    assert doc["services"]["hermes-agent-engine"]["networks"].tag == "!override"
    assert rt.restarted == ["hermes-agent:wizardapps"] * 2
    g.reconcile()                                           # a revoke is sticky
    assert "x-wizard-shares" not in override.read_text()


def test_share_override_alone_is_removed_cleanly_and_provider_gone_unmounts(tmp_path, monkeypatch):
    root, home = tmp_path / "runtipi", tmp_path / "home"
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    claude_pair(root)
    rt = RunningRt(["wizard-claude", "hermes-agent"])
    g = sg.Grants(root, rt, home)
    g.change({"kind": "share", "from": "hermes-agent", "to": "wizard-claude", "share": "claude-login"}, True)
    override = root / "user-config" / "wizardapps" / "hermes-agent" / "docker-compose.yml"
    assert "/claude" in override.read_text()
    rt.ids = ["hermes-agent"]                              # the Claude app was removed
    g.reconcile()
    assert override.read_text() == "services: {}\n"
    assert not (root / "user-config" / "wizardapps" / "wizard-claude").exists()


def test_switch_off_never_writes_share_mounts(tmp_path, monkeypatch):
    monkeypatch.delenv("WIZARD_STOCK_RUNTIPI", raising=False)
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "home"))
    claude_pair(tmp_path / "runtipi")
    client = runtipi.Runtipi("http://127.0.0.1:9/api", "s", root=tmp_path / "runtipi")
    with pytest.raises(OSError):
        client.req("POST", "/apps/hermes-agent:wizardapps/share-grants",
                   {"to": "wizard-claude", "share": "claude-login", "approve": True}, timeout=2)
    assert not (tmp_path / "runtipi" / "user-config").exists()
