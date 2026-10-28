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
    assert k(type="view", category="interface", view={"path": "/"}) == "system"         # a shell
    assert kinds.kind_of(None) == "app" and kinds.kind_of(None, core=True) == "system"   # an app with no wizard.md


def test_kind_in_wizard_md_overrides_the_rule_and_bad_values_are_ignored():
    assert k(type="external", kind="app", view={"path": "/"}) == "app"
    assert k(type="service", view={"path": "/"}, kind="Connector") == "connector"
    assert k(core=True, kind="agent") == "agent"
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
    for aid, extra in (("hermes", {}), ("private", {"label": "Private", "privacy": "private",
                                                    "permissions": {"wizard_apps": []}}), ("Bad", {})):
        (home / "agents" / aid).mkdir(parents=True)
        (home / "agents" / aid / "agent.json").write_text(json.dumps({"schema": 1, "id": aid, **extra}))
    (home / "agents" / "junk").mkdir()
    (home / "agents" / "junk" / "agent.json").write_text("{not json")
    assert kinds.agents(home) == [
        {"id": "host-hermes", "name": "host Hermes", "main": True, "private": False, "limited": False},
        {"id": "private", "name": "Private", "main": False, "private": True, "limited": True},
    ]
