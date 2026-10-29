"""The Connections map's groups come from wizard.md, by one rule (wam/kinds.py), never from a list of app ids."""
import json
from types import SimpleNamespace

from wam import kinds
from wam import network as net


def k(core=False, **w):
    return kinds.kind_of(kinds.fields(w), core)


def test_the_rule_on_the_kinds_of_apps_a_box_has():
    assert k(type="external", category="system", view={"path": "/"}) == "connector"     # a bank / AI / Cloudflare link
    assert k(type="service", category="system") == "agent-app"                          # transcriber, notifications, browser
    assert k(type="service", category="app") == "agent-app"                             # no screen: a tool for the agent
    assert k(type="service", category="system", view={"path": "/"}) == "connector"      # a screen only to set it up
    assert k(type="service", category="app", view={"path": "/"}) == "app"               # Movies, Search
    assert k(type="view", category="system", view={"path": "/"}) == "app"               # Files
    assert k(type="agent") == "agent"
    # Wizard's own core is grouped by the same rule (it carries the "comes with Wizard" badge instead of a group)
    assert k(core=True, type="service", category="system") == "connector"               # Agent Service: links agent and apps
    assert k(core=True, type="view", category="interface", view={"path": "/"}) == "app"  # the active shell
    assert k(type="view", category="interface", view={"path": "/"}) == "app"            # an inactive spare shell
    assert kinds.kind_of(None) == "app" and kinds.kind_of(None, core=True) == "app"     # an app with no wizard.md
    assert "system" not in kinds.KINDS


def test_kind_in_wizard_md_overrides_the_rule_and_bad_values_are_ignored():
    assert k(type="external", kind="app", view={"path": "/"}) == "app"
    assert k(type="service", view={"path": "/"}, kind="Connector") == "connector"
    assert k(type="service", view={"path": "/"}, kind="agent-app") == "agent-app"
    assert k(type="service", kind="app") == "app"                                       # a tool that says it is an app
    assert k(core=True, kind="agent") == "agent"
    # System is not a group any more: `kind: system` is ignored like any other bad value
    assert k(type="service", view={"path": "/"}, kind="system") == "app"
    assert "kind" not in kinds.fields({"type": "service", "kind": "gadget"})
    assert "kind" not in kinds.fields({"type": "service", "kind": "system"})
    assert k(type="service", view={"path": "/"}, kind="gadget") == "app"


def test_the_owners_box_level_kinds_win_and_bad_entries_are_ignored(tmp_path):
    assert kinds.box_kinds(None) == {} and kinds.box_kinds(tmp_path) == {}
    (tmp_path / "app-kinds.json").write_text("{not json")
    assert kinds.box_kinds(tmp_path) == {}
    (tmp_path / "app-kinds.json").write_text(json.dumps({"kinds": {
        "defleur-video": "agent-app", "money": "connector", "Bad Id": "app", "x": "system", "y": 3}}))
    box = kinds.box_kinds(tmp_path)
    assert box == {"defleur-video": "agent-app", "money": "connector"}
    # it wins over wizard.md and over the rule
    assert kinds.kind_of(kinds.fields({"type": "service", "view": {"path": "/"}, "kind": "app"}), box=box["defleur-video"]) == "agent-app"
    assert kinds.kind_of(kinds.fields({"type": "view"}), box=box.get("cinequeue")) == "app"


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
    assert not any(a.get("builtin") for a in rep["apps"])
    # the owner's box-level choice (app-kinds.json in the Wizard home) wins over wizard.md
    home = tmp_path / "wizard-os"
    home.mkdir()
    (home / "app-kinds.json").write_text(json.dumps({"kinds": {"cinequeue": "agent-app"}}))
    rep = net.report(SimpleNamespace(home=home), p=p, live={"networks": {}, "containers": []})
    assert {a["id"]: a["kind"] for a in rep["apps"]} == {"wizard-monarch": "connector", "cinequeue": "agent-app"}
    # Wizard's own core carries the "comes with Wizard" badge in whatever group the rule gives it
    p["gateway"]["core"] = ["wizard-monarch"]
    rep = net.report(SimpleNamespace(), p=p, live={"networks": {}, "containers": []})
    assert {a["id"]: (a["kind"], a.get("builtin", False)) for a in rep["apps"]} == {
        "wizard-monarch": ("connector", True), "cinequeue": ("app", False)}


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
    # a registry `hermes` (a fresh box's main agent) takes the same lists, never its own permissions
    (home / "alongside.json").unlink()
    (root / "hermes" / "agent.json").write_text(json.dumps({"schema": 1, "id": "hermes", "permissions": {"wizard_apps": ["x"]}}))
    assert kinds.agents(home, tmp_path / "runtipi")[0] == {"id": "hermes", "name": "hermes", "main": True, "private": False,
                                                           "apps": ["files", "money", "transcriber"], "app": None}
    assert kinds.main_agent_apps(None) is None


def test_a_private_app_is_reported_private():
    assert kinds.fields({"type": "service", "privacy": "Private"})["private"] is True
    assert "private" not in kinds.fields({"type": "service"})
