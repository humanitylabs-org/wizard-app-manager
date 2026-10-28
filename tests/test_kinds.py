"""The Connections map's groups come from wizard.md, by one rule (wam/kinds.py), never from a list of app ids."""
import json
from types import SimpleNamespace

from wam import kinds
from wam import network as net


def k(core=False, **w):
    return kinds.kind_of(kinds.fields(w), core)


def test_the_rule_on_the_kinds_of_apps_a_box_has():
    assert k(type="external", category="system", view={"path": "/"}) == "connector"     # a bank / AI / Cloudflare link
    assert k(type="service", category="system") == "connector"                          # transcriber, notifications
    assert k(type="service", category="app") == "connector"                             # no screen: a background service
    assert k(type="service", category="system", view={"path": "/"}) == "connector"      # a screen only to set it up
    assert k(type="service", category="app", view={"path": "/"}) == "app"               # Movies, Search
    assert k(type="view", category="system", view={"path": "/"}) == "app"               # Files
    assert k(type="agent") == "agent"
    assert k(core=True, type="service", category="system") == "system"                  # Agent Service
    assert k(core=True, type="view", category="interface", view={"path": "/"}) == "system"   # the active shell
    assert k(type="view", category="interface", view={"path": "/"}) == "app"            # an inactive spare shell
    assert kinds.kind_of(None) == "app" and kinds.kind_of(None, core=True) == "system"   # an app with no wizard.md


def test_kind_in_wizard_md_overrides_the_rule_and_bad_values_are_ignored():
    assert k(type="external", kind="app", view={"path": "/"}) == "app"
    assert k(type="service", view={"path": "/"}, kind="Connector") == "connector"
    assert k(core=True, kind="agent") == "agent"
    # System is the core only: it can't be cut off, so no other app can put itself there, and the core stays System
    assert k(type="service", view={"path": "/"}, kind="system") == "app"
    assert k(core=True, type="view", kind="app") == "system"
    assert "kind" not in kinds.fields({"type": "service", "kind": "gadget"})
    assert k(type="service", view={"path": "/"}, kind="gadget") == "app"


def test_read_app_keeps_the_fields_and_the_report_says_each_kind(tmp_path):
    root = tmp_path / "runtipi"
    for app_id, front in (("wizard-monarch", "type: external\ncategory: system\nview: {path: /, port: 80}"),
                          ("cinequeue", "type: service\nview: {path: /, port: 80}")):
        d = root / "apps" / "wizardapps" / app_id
        d.mkdir(parents=True)
        (d / "wizard.md").write_text(f"---\nid: {app_id}\nname: {app_id}\n{front}\n---\n")
        (d / "docker-compose.yml").write_text(f"services:\n  {app_id}:\n    image: x\n")
    apps = net.read_apps(root, "wizardapps", ["wizard-monarch", "cinequeue"])
    assert {a["id"]: a["wizard"]["type"] for a in apps} == {"wizard-monarch": "external", "cinequeue": "service"}
    p = net.plan(apps, [], set())
    rep = net.report(SimpleNamespace(), p=p, live={"networks": {}, "containers": []})
    assert {a["id"]: a["kind"] for a in rep["apps"]} == {"wizard-monarch": "connector", "cinequeue": "app"}
    assert rep["agents"] == []


def test_agents_the_box_knows_about(tmp_path):
    home = tmp_path / "wizard-os"
    home.mkdir()
    assert kinds.agents(home) == [] and kinds.agents(None) == []
    (home / "alongside.json").write_text(json.dumps({"mode": "alongside", "agent": {
        "external": True, "id": "host-hermes", "name": "host Hermes", "origin": "http://127.0.0.1:8642"}}))
    # the Agent Service keeps its agents in its own app data
    root = tmp_path / "runtipi" / "app-data" / "wizardapps" / "wizard-agent-service" / "data" / "wizard-os" / "agents"
    for aid, extra in (("hermes", {}), ("private", {"label": "Private", "privacy": "private",
                                                    "permissions": {"wizard_apps": ["files"], "agent_apps": []},
                                                    "runtime": {"kind": "container", "app_id": "agent-private"}}),
                       ("Bad", {})):
        (root / aid).mkdir(parents=True)
        (root / aid / "agent.json").write_text(json.dumps({"schema": 1, "id": aid, **extra}))
    (root / "junk").mkdir()
    (root / "junk" / "agent.json").write_text("{not json")
    (home / "agents").mkdir()   # an older layout loses to the Agent Service's own folder
    assert kinds.agents_dir(home, tmp_path / "runtipi") == root
    assert kinds.agents(home, tmp_path / "runtipi") == [
        {"id": "host-hermes", "name": "host Hermes", "main": True, "private": False, "apps": None, "app": None},
        {"id": "private", "name": "Private", "main": False, "private": True, "apps": ["files"], "app": "agent-private"},
    ]
    # the main agent's own app lists (.main-agent.json): the gateway enforces them, so the map says them
    (root / ".main-agent.json").write_text(json.dumps({"schema": 1, "wizard_apps": ["money", "files"], "agent_apps": ["transcriber"]}))
    assert kinds.main_agent_apps(root) == ["files", "money", "transcriber"]
    assert kinds.agents(home, tmp_path / "runtipi")[0]["apps"] == ["files", "money", "transcriber"]
    assert kinds.main_agent_apps(None) is None


def test_a_private_app_is_reported_private():
    assert kinds.fields({"type": "service", "privacy": "Private"})["private"] is True
    assert "private" not in kinds.fields({"type": "service"})
