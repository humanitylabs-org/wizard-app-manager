"""App networks: declared connections are the only app-to-app paths (wam/network.py)."""
import ipaddress
import json
from pathlib import Path

import pytest
import yaml

from wam import network as net


def app(app_id, uses=(), provides=(), services=None, **kw):
    services = services or {app_id: {"main": True, "internal_port": 80, "on_shared": True, "network_mode": "", "exposed": False,
                                     "networks": {"tipi_main_network": {"gw_priority": 1}}}}
    return {"id": app_id, "name": app_id, "uses": list(uses), "provides": list(provides), "router": False,
            "services": services, "custom": None, "host_access": [], **kw}


def carlos():
    twenty = app("twenty-crm", provides=["crm"], services={
        "twenty-crm": {"main": True, "on_shared": True, "network_mode": "", "exposed": False,
                       "networks": {"tipi_main_network": {"gw_priority": 1}, "twenty-crm_wizardapps_network": {"gw_priority": 0}}},
        "twenty-db": {"main": False, "on_shared": False, "network_mode": "", "exposed": False,
                      "networks": {"twenty-crm_wizardapps_network": {"gw_priority": 0}}}})
    return [twenty,
            app("gohighlevel-connector", uses=["twenty-crm"], provides=["sms"]),
            app("resend-connector", uses=["twenty-crm"], provides=["email"]),
            app("calendly-connector", uses=["twenty-crm"]),
            app("stripe-connector", uses=["twenty-crm"]),
            app("carlos-crm", uses=["twenty-crm", "gohighlevel-connector", "resend-connector"]),
            app("carlos-campaigns", uses=["twenty-crm", "slot:email", "sms"])]


def reach(p):
    """(consumer, provider) pairs that share a network, from the plan's network membership."""
    pairs = set()
    for n in p["networks"].values():
        apps = {m["app"] for m in n["members"]}
        for a in apps:
            for b in apps:
                if a != b:
                    pairs.add((a, b))
    return pairs


def test_each_declared_connection_gets_its_own_link_and_nothing_else_connects():
    p = net.plan(carlos(), [], set())
    links = {n for n, v in p["networks"].items() if v["kind"] == "link"}
    assert net.link_name("gohighlevel-connector", "twenty-crm") in links
    assert net.link_name("carlos-campaigns", "resend-connector") in links      # through the slot `email`
    assert net.link_name("carlos-campaigns", "gohighlevel-connector") in links  # through the slot `sms`
    pairs = reach(p)
    assert ("gohighlevel-connector", "twenty-crm") in pairs
    # undeclared pairs share no network: two consumers of one provider stay apart
    assert ("calendly-connector", "resend-connector") not in pairs
    assert ("gohighlevel-connector", "resend-connector") not in pairs
    assert ("carlos-crm", "stripe-connector") not in pairs
    for n, v in p["networks"].items():
        if v["kind"] == "link":
            assert v["internal"] and len({m["app"] for m in v["members"]}) == 2
        if v["kind"] == "app":
            assert not v["internal"] and {m["app"] for m in v["members"]} == {n.removeprefix(net.APP_PREFIX)}


def test_provider_main_service_answers_by_app_id_and_only_it_joins_the_link():
    p = net.plan(carlos(), [], set())
    link = p["networks"][net.link_name("gohighlevel-connector", "twenty-crm")]
    prov = [m for m in link["members"] if m["app"] == "twenty-crm"]
    assert prov == [{"app": "twenty-crm", "service": "twenty-crm", "aliases": ["twenty-crm"]}]
    cons = [m for m in link["members"] if m["app"] == "gohighlevel-connector"]
    assert cons and all(m["aliases"] == [] for m in cons)
    twenty = next(a for a in p["apps"] if a["id"] == "twenty-crm")
    assert "twenty-db" not in twenty["want"]   # the database never joins a link


def test_overlay_replaces_the_shared_network_and_keeps_the_app_network():
    p = net.plan(carlos(), [], set())
    twenty = next(a for a in p["apps"] if a["id"] == "twenty-crm")
    ov = net.overlay_for(twenty)
    nets = ov["services"]["twenty-crm"]
    assert "tipi_main_network" not in nets
    assert "twenty-crm_wizardapps_network" in nets
    assert nets["wizard-app-twenty-crm"] == {"gw_priority": 1}
    assert nets[net.link_name("gohighlevel-connector", "twenty-crm")] == {"aliases": ["twenty-crm"]}
    assert all(v == {"name": k, "external": True} for k, v in ov["networks"].items())
    assert set(ov["services"]) == {"twenty-crm"}


def test_a_slot_links_only_the_owners_active_provider_unless_the_slot_is_multi():
    apps = [app("mailer", uses=["email"]), app("resend", provides=["email"]), app("smtp", provides=["email"]),
            app("chat", uses=["ai-connection"]), app("claude", provides=["ai-connection"]), app("gpt", provides=["ai-connection"])]
    p = net.plan(apps, [], set(), slot_choice={"email": "smtp"}, multi_slots={"ai-connection"})
    pairs = reach(p)
    assert ("mailer", "smtp") in pairs and ("mailer", "resend") not in pairs
    assert ("chat", "claude") in pairs and ("chat", "gpt") in pairs


def test_health_data_is_a_multi_capability_so_health_reaches_every_wearable():
    from wam import slots
    multi = slots.multi_slots()
    assert {"health-data", "ai-connection"} <= multi
    assert "health-data" not in slots.load_defaults()      # a capability, not a system spot (no parking placeholder)
    apps = [app("wizard-health", uses=["health-data"]), app("wizard-whoop", provides=["health-data"]),
            app("wizard-oura", provides=["health-data"]), app("wizard-fitbit", provides=["health-data"]),
            app("mailer", uses=["email"]), app("resend", provides=["email"]), app("smtp", provides=["email"])]
    p = net.plan(apps, [], set(), slot_choice={"health-data": "wizard-whoop", "email": "smtp"}, multi_slots=multi)
    pairs = reach(p)
    assert {("wizard-health", x) for x in ("wizard-whoop", "wizard-oura", "wizard-fitbit")} <= pairs
    assert ("mailer", "smtp") in pairs and ("mailer", "resend") not in pairs   # other spots keep one active provider


def test_grants_and_router_requirements_are_declarations_too():
    apps = [app("wisdom", router=True), app("wizard-connection-router", provides=["connection-router"]),
            app("recorder"), app("transcriber", provides=["transcription"])]
    apps[0]["router"] = True
    p = net.plan(apps, [{"kind": "app", "from": "recorder", "to": "slot:transcription", "approved": "owner"},
                        {"kind": "share", "from": "recorder", "to": "wisdom"}], set())
    pairs = reach(p)
    assert ("wisdom", "wizard-connection-router") in pairs
    assert ("recorder", "transcriber") in pairs
    assert ("recorder", "wisdom") not in pairs     # a share grant carries no network


def test_missing_and_external_providers_are_reported_not_linked():
    p = net.plan([app("wizard-chatgpt", uses=["hermes-agent"]), app("x", uses=["nope"])], [], {"agent", "hermes-agent"})
    states = {(e["from"], e["to"]): e["state"] for e in p["edges"]}
    assert states[("wizard-chatgpt", "hermes-agent")] == "external"
    assert states[("x", "nope")] == "not-installed"
    assert not [n for n in p["networks"].values() if n["kind"] == "link"]


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("collision", ["foreign-label", "wrong-internal", "wrong-owner", "missing-owner"])
def test_connections_never_reports_colliding_network_identity_as_healthy(running, collision):
    p = net.plan([app("x")], [], set())
    live = live_from(p)
    live["containers"][0]["running"] = running
    name = net.app_net("x")
    if collision == "foreign-label":
        live["networks"][name]["labels"] = {}
    elif collision == "wrong-internal":
        live["networks"][name]["internal"] = not p["networks"][name]["internal"]
    elif collision == "wrong-owner":
        live["networks"][name]["labels"]["org.wizard-os.owner"] = "another-app"
    else:
        live["networks"][name]["labels"].pop("org.wizard-os.owner")
    rep = net.report(type("C", (), {})(), p=p, live=live)
    assert rep["live"] and not rep["ok"]
    assert any("different network" in problem for problem in rep["apps"][0]["drift"])
    assert not next(row for row in rep["networks"] if row["name"] == name)["matches"]
    assert "matches the declarations" not in net.describe(rep)
    assert bool(net.drift(p, live)) is running
    if running:
        assert name in net.drift(p, live)[0]["missing"]


@pytest.mark.parametrize("field", ["owner", "consumer", "provider"])
@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("populated", [False, True])
def test_network_ownership_collision_never_attaches_or_deletes(tmp_path, monkeypatch, field, missing, populated):
    grant = {"kind": "app", "from": "a", "to": "b", "reason": "uses", "approved": "install"}
    p = net.plan([app("a", uses=["b"]), app("b")], [grant], set(), approved=[grant])
    label = f"org.wizard-os.{field}"
    name = next(n for n, spec in p["networks"].items() if label in spec["labels"])
    live = live_from(p)
    if missing:
        live["networks"][name]["labels"].pop(label)
    else:
        live["networks"][name]["labels"][label] = "another-app"
    live["networks"][name]["containers"] = ["foreign-container"] if populated else []
    fake = FakeDocker(live)
    monkeypatch.setattr(net, "build", lambda *args, **kwargs: (p, "wizardapps"))
    monkeypatch.setattr(net, "docker_cmd", lambda: ["docker"])
    monkeypatch.setattr(net, "_run", fake.run)
    monkeypatch.setattr(net, "observe", lambda *args, **kwargs: json.loads(json.dumps(fake.live)))
    result = net.reconcile(Cfg(tmp_path))
    assert result["errors"] and any(name in e for e in result["errors"])
    assert not [call for call in fake.calls if call[:2] == ("network", "connect") and name in call]
    assert not [call for call in fake.calls if call[:2] == ("network", "rm") and name in call]
    assert name in fake.live["networks"]


@pytest.mark.parametrize("kind", ["app", "link", "gateway"])
@pytest.mark.parametrize("missing", [False, True])
def test_cleanup_preserves_foreign_orphans_with_incomplete_or_wrong_identity(tmp_path, monkeypatch, kind, missing):
    name = net.link_name("gone", "other") if kind == "link" else net.app_net("gone") if kind == "app" else net.gw_net("gone")
    labels = {net.LABEL: kind, "org.wizard-os.consumer": "gone", "org.wizard-os.provider": "other"} if kind == "link" else {
        net.LABEL: kind, "org.wizard-os.owner": "gone"}
    field = "org.wizard-os.consumer" if kind == "link" else "org.wizard-os.owner"
    if missing:
        labels.pop(field)
    else:
        labels[field] = "another-stack"
    live = {"networks": {name: {"labels": labels, "internal": kind != "app", "subnets": [], "containers": ["foreign"]}}, "containers": []}
    fake = FakeDocker(live)
    monkeypatch.setattr(net, "build", lambda *args, **kwargs: (net.plan([], [], set()), "wizardapps"))
    monkeypatch.setattr(net, "docker_cmd", lambda: ["docker"])
    monkeypatch.setattr(net, "_run", fake.run)
    monkeypatch.setattr(net, "observe", lambda *args, **kwargs: json.loads(json.dumps(fake.live)))
    result = net.reconcile(Cfg(tmp_path))
    assert result["removed"] == [] and name in fake.live["networks"]
    assert not [call for call in fake.calls if call[:2] in (("network", "disconnect"), ("network", "rm"))]


@pytest.mark.parametrize("rows", [None, object(), {"info": {}}, [None, {"info": None}, {"info": "x", "app": {"exposed": True}}]])
def test_unreadable_lifecycle_state_never_authorizes_publication(rows):
    rt = type("RT", (), {"installed": lambda self: rows})()
    assert net.published_urns(rt) == set()


def test_lifecycle_lookup_failure_never_authorizes_publication():
    def unavailable(self):
        raise RuntimeError("temporarily offline")
    assert net.published_urns(type("RT", (), {"installed": unavailable})()) == set()


@pytest.mark.parametrize("main_service", ["base", "arch-main"])
def test_watcher_preserves_the_owner_published_architecture_specific_main(tmp_path, monkeypatch, main_service):
    from wam import test_catalog
    monkeypatch.setattr(test_catalog, "host_architecture", lambda: "arm64")
    recipe = {"services": {"base": {"image": "a", "command": ["base"], "x-runtipi": {"is_main": False}}},
              "x-runtipi": {"overrides": [
                  {"architecture": "amd64", "services": {"other-arch": {"image": "a", "x-runtipi": {"is_main": True}}}},
                  {"architecture": "arm64", "services": {main_service: {"image": "b", "command": ["override"],
                    "x-runtipi": {"is_main": True, "internal_port": 80}}}}]}}
    write_app(tmp_path, "wizardapps", "x", {"id": "x"}, recipe)
    rt = type("RT", (), {"installed": lambda self: [{"info": {"urn": "x:wizardapps"}, "app": {"exposed": True}}]})()
    monkeypatch.setattr(net, "_context", lambda *args: (rt, {"x"}, [], set(), "wizardapps", []))
    c = type("C", (), {"home": tmp_path / "home", "runtipi_root": tmp_path})()
    p, _ = net.build(c, on=True)
    assert p["apps"][0]["state"] == "exposed"
    planned = p["apps"][0]
    assert main_service in planned["services"] and "other-arch" not in planned["services"]
    assert net.keeps_shared(planned, main_service)
    if main_service != "base":
        assert not net.keeps_shared(planned, "base")
    assert net.source_services(recipe)[main_service]["command"] == ["override"]
    assert recipe["services"]["base"]["command"] == ["base"]


@pytest.mark.parametrize("internal_port", [None, 0, 80])
@pytest.mark.parametrize("source_mode", [{}, {"network_mode": None}, {"network_mode": ""}, {"network_mode": False}])
@pytest.mark.parametrize("exposure", ["exposed", "exposedLocal"])
def test_watcher_never_republishes_an_unroutable_main_with_stale_router_labels(tmp_path, monkeypatch, internal_port, source_mode, exposure):
    meta = {"is_main": True}
    if internal_port is not None:
        meta["internal_port"] = internal_port
    recipe = {"services": {"main": {"image": "a", "x-runtipi": meta, **source_mode}}}
    generated = {"services": {"main": {"image": "a", "labels": {"traefik.enable": "true"}, "networks": {net.SHARED: {}}}}}
    write_app(tmp_path, "wizardapps", "x", {"id": "x"}, recipe, generated=generated)
    rt = type("RT", (), {"installed": lambda self: [{"info": {"urn": "x:wizardapps"}, "app": {exposure: True}}]})()
    monkeypatch.setattr(net, "_context", lambda *args: (rt, {"x"}, [], set(), "wizardapps", []))
    c = type("C", (), {"home": tmp_path / "home", "runtipi_root": tmp_path})()
    p, _ = net.build(c, on=True)
    planned = p["apps"][0]
    routed = bool(internal_port) and not source_mode
    assert planned["state"] == ("exposed" if routed else "isolated")
    assert net.keeps_shared(planned, "main") is routed


@pytest.mark.parametrize("published_urn", [None, "x:other-store", "x:wizardapps"])
def test_watcher_never_trusts_recipe_exposure_labels_or_another_stores_same_id(monkeypatch, tmp_path, published_urn):
    class RT:
        def installed(self):
            return [{"info": {"id": "x", "urn": published_urn or "x:wizardapps"}, "app": {"exposed": bool(published_urn)}}]
    rt = RT()
    monkeypatch.setattr(net, "_context", lambda *args: (rt, {"x"}, [], set(), "wizardapps", []))
    forged = app("x")
    forged["services"]["x"]["exposed"] = True
    monkeypatch.setattr(net, "read_apps", lambda *args, **kwargs: [forged])
    c = type("C", (), {"home": tmp_path, "runtipi_root": tmp_path})()
    p, _ = net.build(c, on=True)
    assert p["apps"][0]["state"] == ("exposed" if published_urn == "x:wizardapps" else "isolated")


def test_fresh_report_shows_the_core_an_app_can_reach_over_its_gateway_network(monkeypatch):
    apps = [app("wizard-ux", uses=["agent-service"], provides=["shell"]),
            app("wizard-agent-service", provides=["agent-service"]), app("hermes-agent", provides=["agent"]),
            app("files"), app("defleur-video", uses=["files"])]
    p = net.plan(apps, [], set())
    rep = net.report(type("C", (), {})(), p=p, live={"networks": {}, "containers": []})
    ux = next(a for a in rep["apps"] if a["id"] == "wizard-ux")
    assert {e["to"] for e in ux["can_reach"] if e["reason"] == "gateway network"} == {"files", "defleur-video"}
    assert {e["from"] for e in ux["reached_by"] if e["state"] == "gateway"} == {"files", "defleur-video"}
    files = next(a for a in rep["apps"] if a["id"] == "files")
    assert {e["to"] for e in files["can_reach"] if e["state"] == "core"} == {"wizard-ux", "wizard-agent-service", "hermes-agent"}
    assert any(e["to"] == "defleur-video" and e["state"] == "peer" for e in files["can_reach"])


def test_failed_install_cleans_up_its_planned_networks(tmp_path, monkeypatch):
    import pytest
    cli, installs, steps = gate_env(tmp_path, monkeypatch, "services:\n  x:\n    image: a\n")
    monkeypatch.setattr(cli, "install_failure_reason", lambda urn: "pull access denied")

    class RT:
        def app(self, urn): return {"info": {"id": "x"}}
        def install(self, urn, form): installs.append(urn)
        def wait_status(self, urn, want, timeout=900): return "missing"
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: RT())
    with pytest.raises(SystemExit, match="install failed"):
        cli.install(cli.Config(), "x")
    assert steps == [({"x"}, False), (set(), True)]


def test_live_migration_keeps_the_app_bridge_as_default_route(tmp_path, monkeypatch):
    c = Cfg(tmp_path)
    p0 = net.plan([app("solo")], [], set())
    live = live_from(p0, on_shared={"solo"})
    live["networks"] = {}
    fake = FakeDocker(live)
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(), **k: (net.plan([app("solo")], [], set()), "wizardapps"))
    monkeypatch.setattr(net, "docker_cmd", lambda: ["docker"])
    monkeypatch.setattr(net, "_run", fake.run)
    monkeypatch.setattr(net, "observe", lambda d, store="wizardapps": json.loads(json.dumps(fake.live)))
    net.reconcile(c)
    conn = [x for x in fake.calls if x[:2] == ("network", "connect")]
    assert conn and all("--gw-priority" in x for x in conn if x[-2] == "wizard-app-solo")


def test_alongside_gateway_is_loopback_and_host_network_apps_are_left_alone():
    ux = app("wizard-ux", uses=["agent-service"], provides=["shell"])
    ux["services"]["wizard-ux"]["network_mode"] = "host"
    svc = app("wizard-agent-service", uses=["hermes-agent", "transcriber"], provides=["agent-service"])
    svc["services"]["wizard-agent-service"]["network_mode"] = "host"
    p = net.plan([ux, svc, app("transcriber", provides=["transcription"])], [], {"agent", "hermes-agent"})
    assert p["gateway"]["path"] == "loopback"
    states = {a["id"]: a["state"] for a in p["apps"]}
    assert states == {"wizard-ux": "host-network", "wizard-agent-service": "host-network", "transcriber": "isolated"}
    assert not [n for n in p["networks"] if n.startswith(net.GW_PREFIX)]
    assert ("wizard-agent-service", "transcriber") not in reach(p)   # host side: it uses the published port


def test_fresh_box_gateway_reaches_every_app_but_apps_still_not_each_other():
    apps = [app("wizard-ux", uses=["agent-service"], provides=["shell"]),
            app("wizard-agent-service", uses=["hermes-agent"], provides=["agent-service"]),
            app("hermes-agent", provides=["agent"]), *carlos()]
    p = net.plan(apps, [], set())
    assert p["gateway"]["path"] == "network"
    pairs = reach(p)
    for a in ("twenty-crm", "calendly-connector", "stripe-connector"):
        for core in ("wizard-ux", "wizard-agent-service", "hermes-agent"):
            assert (core, a) in pairs
    assert ("calendly-connector", "resend-connector") not in pairs
    assert ("wizard-ux", "wizard-agent-service") in pairs   # core among themselves (wizard-core)
    gw = p["networks"][net.gw_net("twenty-crm")]
    assert {m["app"] for m in gw["members"]} == {"twenty-crm", "wizard-ux", "wizard-agent-service", "hermes-agent"}
    assert any(m["aliases"] == ["twenty-crm"] for m in gw["members"] if m["app"] == "twenty-crm")


def test_exposed_custom_and_off_keep_the_shared_network():
    ex = app("exposed")
    ex["services"]["exposed"]["exposed"] = True
    cu = app("custom", custom="its user-config override sets the networks of custom by hand")
    p = net.plan([ex, cu, app("plain", uses=["exposed", "custom"]), app("lonely-exposed")], [], set())
    st = {a["id"]: a["state"] for a in p["apps"]}
    assert st == {"exposed": "exposed", "custom": "custom", "plain": "isolated", "lonely-exposed": "isolated"}
    states = {e["provider"]: e["state"] for e in p["edges"] if e["from"] == "plain"}
    # an exposed provider stays on the shared network for the reverse proxy and still gets the declared link
    assert states == {"exposed": "link", "custom": "unmanaged"}
    ov = net.overlay_for(next(a for a in p["apps"] if a["id"] == "exposed"))
    assert set(ov["services"]["exposed"]) == {"tipi_main_network", net.link_name("plain", "exposed")}
    assert net.app_net("exposed") not in p["networks"]
    ex2 = app("ex2")
    ex2["services"]["ex2"]["exposed"] = True
    assert net.overlay_for(net.plan([ex2], [], set())["apps"][0]) == {"services": {}, "networks": {}}
    off = net.plan(carlos(), [], set(), on=False)
    assert not off["networks"] and all(a["state"] == "shared" and a["want"] == {} for a in off["apps"])


def test_host_access_gate():
    risky = {"services": {"a": {"network_mode": "host"}, "b": {"privileged": True},
                          "c": {"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]},
                          "d": {"network_mode": "none", "volumes": ["${APP_DATA_DIR}/x:/x"]}}}
    found = net.host_access(risky)
    assert len(found) == 3 and all(f.split(":")[0] in "abc" for f in found)
    assert net.host_access("services:\n  ok: {image: x}\n") == []


ALONGSIDE = """# Claude login: written by wam (alongside). The Claude app holds the host Hermes' own Claude Code sign-in.
# Remove with `wam alongside --off`, then `wam app update <app>`.
services:
  wizard-claude-engine:
    volumes:
    - /root/.claude:/claude
    user: 0:0
    ports: !reset []
x-wizard-claude-login:
  folder: /root/.claude
"""


def test_override_block_is_added_and_removed_without_touching_other_writers(tmp_path):
    f = tmp_path / "docker-compose.yml"
    f.write_text(ALONGSIDE)
    ov = {"services": {"wizard-claude": {"wizard-app-wizard-claude": {"gw_priority": 1}}},
          "networks": {"wizard-app-wizard-claude": {"name": "wizard-app-wizard-claude", "external": True}}}
    assert net.write_overlay(f, ov) is True
    text = f.read_text()
    assert text.startswith("# Claude login") and net.HEADER in text
    assert "networks: !override" in text and "ports: !reset []" in text
    doc = net.load_yaml(text)
    assert doc["x-wizard-claude-login"]["folder"] == "/root/.claude"
    assert doc[net.MARK] == {"services": ["wizard-claude"], "networks": ["wizard-app-wizard-claude"]}
    assert net.write_overlay(f, ov) is False                 # idempotent
    assert net.write_overlay(f, None) is True
    back = net.load_yaml(f.read_text())
    assert net.MARK not in back and "wizard-claude" not in back["services"] and "networks" not in back
    assert back["services"]["wizard-claude-engine"]["user"] == "0:0"
    assert net.HEADER not in f.read_text()
    other = tmp_path / "alongside.yml"
    other.write_text('services:\n  x:\n    user: "0:0"\n    network_mode: host\n')
    assert net.write_overlay(other, None) is False and other.read_text() == 'services:\n  x:\n    user: "0:0"\n    network_mode: host\n'
    g = tmp_path / "only.yml"
    assert net.write_overlay(g, ov) and net.write_overlay(g, None) and not g.exists()   # nothing else: file removed


def write_app(root, store, app_id, wizard, compose, generated=None, override=None):
    d = root / "apps" / store / app_id
    d.mkdir(parents=True)
    (d / "wizard.md").write_text("---\n" + yaml.safe_dump(wizard) + "---\nbody\n")
    (d / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    if generated is not None:
        (d / "docker-compose.generated.yml").write_text(yaml.safe_dump(generated))
    if override is not None:
        o = root / "user-config" / store / app_id
        o.mkdir(parents=True)
        (o / "docker-compose.yml").write_text(override)


def test_read_app_prefers_generated_compose_and_predicts_runtipi_before_install(tmp_path):
    write_app(tmp_path, "wizardapps", "msg", {"id": "msg", "uses": ["twenty-crm"]},
              {"services": {"msg": {"image": "x", "networks": ["lane"], "x-runtipi": {"is_main": True}},
                            "init": {"image": "x", "network_mode": "none"}}, "networks": {"lane": {"internal": True}}},
              generated={"services": {"msg": {"networks": {"lane": None, "tipi_main_network": {"gw_priority": 1},
                                                          "msg_wizardapps_network": {"gw_priority": 0}}},
                                      "init": {"network_mode": "none"}}})
    a = net.read_app(tmp_path, "wizardapps", "msg")
    assert a["services"]["msg"]["on_shared"] and not a["services"]["init"]["on_shared"]
    assert set(a["services"]["msg"]["networks"]) == {"lane", "tipi_main_network", "msg_wizardapps_network"}
    p = net.plan([a], [], set())
    ov = net.overlay_for(p["apps"][0])
    assert set(ov["services"]["msg"]) == {"lane", "msg_wizardapps_network", "wizard-app-msg"}   # recipe lanes kept
    # store copy only (about to be installed): Runtipi's rules are predicted
    d = tmp_path / "repos" / "wizardapps" / "apps" / "new"
    d.mkdir(parents=True)
    (d / "wizard.md").write_text("---\nid: new\nuses: [msg]\n---\n")
    (d / "docker-compose.yml").write_text(yaml.safe_dump({"services": {"new": {"image": "x", "x-runtipi": {"is_main": True}}}}))
    b = net.read_app(tmp_path, "wizardapps", "new")
    assert b["uses"] == ["msg"] and b["services"]["new"]["networks"] == {"tipi_main_network": {"gw_priority": 1}}


def test_hand_written_networks_and_host_overrides_are_respected(tmp_path):
    comp = {"services": {"a": {"image": "x", "x-runtipi": {"is_main": True}}}}
    write_app(tmp_path, "wizardapps", "a", {"id": "a"}, comp,
              override="services:\n  a:\n    networks: !override [my-net]\n")
    assert "by hand" in net.read_app(tmp_path, "wizardapps", "a")["custom"]
    write_app(tmp_path, "wizardapps", "b", {"id": "b"}, {"services": {"b": comp["services"]["a"]}},
              override="services:\n  b:\n    network_mode: host\n    networks: !reset []\n")
    b = net.read_app(tmp_path, "wizardapps", "b")
    assert b["custom"] is None and net._isolatable(b)[0] == "host-network"
    # our own block is not "by hand"
    write_app(tmp_path, "wizardapps", "c", {"id": "c"}, {"services": {"c": comp["services"]["a"]}})
    p = net.plan([net.read_app(tmp_path, "wizardapps", "c")], [], set())
    f = net.override_path(tmp_path, "wizardapps", "c")
    net.write_overlay(f, net.overlay_for(p["apps"][0]))
    assert net.read_app(tmp_path, "wizardapps", "c")["custom"] is None


def test_subnets_never_overlap_existing_networks():
    pool = ipaddress.IPv4Network("10.255.0.0/16")
    a = net._free_subnet(["10.255.0.0/24", "172.18.0.0/16"], pool, set())
    assert a == "10.255.1.0/27"
    b = net._free_subnet(["10.255.0.0/24"], pool, {a})
    assert b == "10.255.1.32/27"
    with pytest.raises(net.NetworkError):
        net._free_subnet(["10.255.0.0/16"], pool, set())


def live_from(p, on_shared=(), extra=None):
    """Containers exactly as the plan wants them, except apps in on_shared (still on the shared network)."""
    containers = []
    for a in p["apps"]:
        for s in a["services"]:
            nets = {n: list(al) for n, al in (a.get("want") or {}).get(s, {}).items()}
            if a["id"] in on_shared:
                nets = {net.SHARED_DOCKER: [s]}
            nets.update((extra or {}).get((a["id"], s), {}))
            containers.append({"id": "x", "name": f"{a['id']}-{s}", "urn": f"{a['id']}:wizardapps", "service": s,
                               "running": True, "network_mode": "", "networks": nets})
    return {"networks": {n: {"labels": dict(v["labels"]), "internal": v["internal"], "subnets": [],
                             "containers": []} for n, v in p["networks"].items()}, "containers": containers}


def test_project_networks_of_wizard_named_apps_are_never_taken_for_ours():
    """Regression (VM rehearsal): wizard-chatgpt's compose network is wizard-chatgpt_wizardapps_wizard-chatgpt_wizardapps_network;
    treating every `wizard-*` name as ours disconnected it and the app lost its engine."""
    assert net.ours("wizard-link-a--b") and net.ours("wizard-app-wizard-chatgpt") and net.ours("wizard-core")
    assert not net.ours("wizard-chatgpt_wizardapps_wizard-chatgpt_wizardapps_network")
    a = app("wizard-chatgpt", services={
        "wizard-chatgpt": {"main": True, "on_shared": True, "network_mode": "", "exposed": False,
                           "networks": {"tipi_main_network": {}, "wizard-chatgpt_wizardapps_network": {}},
                           "docker_networks": ["wizard-chatgpt_wizardapps_wizard-chatgpt_wizardapps_network"]}})
    p = net.plan([a], [], set())
    proj = "wizard-chatgpt_wizardapps_wizard-chatgpt_wizardapps_network"
    live = {"networks": {}, "containers": [{"name": "c", "urn": "wizard-chatgpt:wizardapps", "service": "wizard-chatgpt",
                                            "running": True, "network_mode": proj,
                                            "networks": {proj: [], "wizard-app-wizard-chatgpt": []}}]}
    assert net.drift(p, live) == []
    live["containers"][0]["networks"].pop(proj)
    rows = net.drift(p, live)
    assert rows[0]["lost"] == [proj] and rows[0]["recreate"] and rows[0]["extra"] == []


def test_drift_finds_shared_network_missing_and_extra_links():
    p = net.plan(carlos(), [], set())
    assert net.drift(p, live_from(p)) == []
    rows = net.drift(p, live_from(p, on_shared={"stripe-connector"},
                                  extra={("calendly-connector", "calendly-connector"): {"wizard-link-x--y": []}}))
    by = {r["app"]: r for r in rows}
    assert by["stripe-connector"]["recreate"] and by["stripe-connector"]["on_shared"]
    assert by["calendly-connector"]["extra"] == ["wizard-link-x--y"] and not by["calendly-connector"]["recreate"]


class FakeDocker:
    """Just enough of `docker network ...` for reconcile."""

    def __init__(self, live):
        self.live, self.calls = live, []

    def run(self, docker, *args, check=True, timeout=60):
        self.calls.append(args)
        import subprocess
        a = list(args)
        if a[:2] == ["network", "create"]:
            name = a[-1]
            labels = dict(x.split("=", 1) for i, x in enumerate(a) if i and a[i - 1] == "--label")
            self.live["networks"][name] = {"labels": labels, "internal": "--internal" in a,
                                           "subnets": [a[a.index("--subnet") + 1]], "containers": []}
        elif a[:2] == ["network", "rm"]:
            self.live["networks"].pop(a[2], None)
        elif a[:2] == ["network", "connect"]:
            c = next(x for x in self.live["containers"] if x["name"] == a[-1])
            c["networks"][a[-2]] = [a[i + 1] for i, x in enumerate(a) if x == "--alias"]
        elif a[:2] == ["network", "disconnect"]:
            c = next((x for x in self.live["containers"] if x["name"] == a[-1]), None)
            if c:
                c["networks"].pop(a[-2], None)
        return subprocess.CompletedProcess(args, 0, "", "")


class Cfg:
    def __init__(self, tmp):
        self.home = tmp / "home"
        self.home.mkdir()
        self.runtipi_root = tmp / "runtipi"


def test_reconcile_migrates_live_is_idempotent_and_rolls_back(tmp_path, monkeypatch):
    c = Cfg(tmp_path)
    p0 = net.plan(carlos(), [], set())
    live = live_from(p0, on_shared={a["id"] for a in p0["apps"]})
    for cont in live["containers"]:
        cont["ports"] = [("127.0.0.1", 18000)] if cont["service"] == "stripe-connector" else []
    live["networks"] = {"runtipi_tipi_main_network": {"labels": {}, "internal": False, "subnets": ["172.18.0.0/16"], "containers": []},
                        "wizard-link-old--gone": {"labels": {net.LABEL: "link", "org.wizard-os.consumer": "old", "org.wizard-os.provider": "gone"}, "internal": True, "subnets": ["10.255.0.0/27"], "containers": []}}
    fake = FakeDocker(live)
    restarted = []
    # stripe's published port stops answering after it leaves the shared network: only it is restarted
    monkeypatch.setattr(net, "ports_answer", lambda ports, **k: not ports)

    def restart(app_id):
        restarted.append(app_id)
        return {"status": "running"}

    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(), **k: (net.plan(carlos(), [], set()), "wizardapps"))
    monkeypatch.setattr(net, "docker_cmd", lambda: ["docker"])
    monkeypatch.setattr(net, "_run", fake.run)
    monkeypatch.setattr(net, "observe", lambda d, store="wizardapps": json.loads(json.dumps(fake.live)))
    res = net.reconcile(c, restart_fn=restart)
    assert not res["errors"], res["errors"]
    assert restarted == ["stripe-connector"]
    assert "wizard-link-old--gone" in res["removed"]
    assert net.link_name("gohighlevel-connector", "twenty-crm") in res["created"]
    assert res["drift"] == []
    for cont in fake.live["containers"]:
        if cont["service"] != "twenty-db":
            assert net.SHARED_DOCKER not in cont["networks"] and f"wizard-app-{cont['urn'].split(':')[0]}" in cont["networks"]
    prov = next(x for x in fake.live["containers"] if x["service"] == "twenty-crm")
    assert prov["networks"][net.link_name("stripe-connector", "twenty-crm")] == ["twenty-crm"]
    # connections are made before the shared network is left (no window without a route)
    order = [x for x in fake.calls if x[:2] in (("network", "connect"), ("network", "disconnect"))]
    first_leave = next(i for i, x in enumerate(order) if x[0:2] == ("network", "disconnect") and net.SHARED_DOCKER in x)
    assert any(x[1] == "connect" and x[-1] == order[first_leave][-1] for x in order[:first_leave])
    subnets = [ipaddress.ip_network(n["subnets"][0]) for k, n in fake.live["networks"].items() if k.startswith("wizard-")]
    assert all(not a.overlaps(b) for i, a in enumerate(subnets) for b in subnets[i + 1:])
    ov = net.load_yaml(net.override_path(c.runtipi_root, "wizardapps", "twenty-crm").read_text())
    assert net.MARK in ov
    # second run: nothing to do
    fake.calls.clear()
    restarted.clear()
    again = net.reconcile(c, restart_fn=restart)
    assert not again["created"] and not again["removed"] and not again["overrides"] and not restarted
    assert not again["connected"] and not again["disconnected"]
    # a new declared connection on a running app is connected live, without a restart
    apps2 = carlos()
    next(a for a in apps2 if a["id"] == "calendly-connector")["uses"].append("resend-connector")
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(), **k: (net.plan(apps2, [], set()), "wizardapps"))
    res3 = net.reconcile(c, restart_fn=restart)
    assert not restarted and any("wizard-link-calendly-connector--resend-connector" in x for x in res3["connected"])
    # and dropped again: disconnected and removed
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(), **k: (net.plan(carlos(), [], set()), "wizardapps"))
    res3b = net.reconcile(c, restart_fn=restart)
    assert "wizard-link-calendly-connector--resend-connector" in res3b["removed"]
    # rollback: isolation off -> overrides stripped, apps back on the shared network, wam networks removed
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(), **k: (net.plan(carlos(), [], set(), on=False), "wizardapps"))
    res4 = net.reconcile(c, restart_fn=restart)
    assert not res4["errors"], res4["errors"]
    assert not [n for n in fake.live["networks"] if n.startswith("wizard-")]
    assert not net.override_path(c.runtipi_root, "wizardapps", "twenty-crm").exists()
    for cont in fake.live["containers"]:
        if cont["service"] != "twenty-db":
            assert list(cont["networks"]) == [net.SHARED_DOCKER]


def test_enabled_switch(tmp_path, monkeypatch):
    monkeypatch.delenv("WIZARD_APP_ISOLATION", raising=False)
    assert net.enabled(tmp_path)
    net.set_enabled(tmp_path, False)
    assert not net.enabled(tmp_path) and json.loads((tmp_path / "app-manager.json").read_text())["app_isolation"] is False
    net.set_enabled(tmp_path, True)
    assert net.enabled(tmp_path)
    monkeypatch.setenv("WIZARD_APP_ISOLATION", "0")
    assert not net.enabled(tmp_path)


def gate_env(tmp_path, monkeypatch, compose):
    from wam import cli
    root = tmp_path / "runtipi"
    d = root / "repos" / "wizardapps" / "apps" / "x"
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text(compose)
    installs, steps = [], []

    class RT:
        def app(self, urn): return {"info": {"id": "x"}}
        def install(self, urn, form): installs.append(urn)
        def wait_status(self, urn, want, timeout=900): return "running"
    monkeypatch.setenv("RUNTIPI_ROOT", str(root))
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: RT())
    monkeypatch.setattr(cli, "ensure_store", lambda c, rt: None)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set())
    monkeypatch.setattr(cli.alongside, "external_slots", lambda h: {})
    monkeypatch.setattr(cli.alongside, "load", lambda h: None)
    monkeypatch.setattr(cli, "resolve_any", lambda c, ref: {"id": "x", "repo": "o/x", "uses": []})
    monkeypatch.setattr(cli, "build_form", lambda *a, **k: ({"port": 1}, []))
    monkeypatch.setattr(cli, "alongside_role", lambda rec, i: None)
    monkeypatch.setattr(cli, "trusted_origin_env", lambda *a, **k: {})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: {"apps": [{"id": "x"}]})
    monkeypatch.setattr(cli, "host_dashboard_consumers", lambda c, ids: {})
    def step(c, rt=None, extra=(), restart=True, fresh=()):
        steps.append((set(extra), restart))
        return step_results.pop(0) if step_results else {}
    step_results = []
    monkeypatch.setattr(cli, "network_step", step)
    gate_env.results = step_results
    return cli, installs, steps


def test_installer_refuses_host_access_unless_the_owner_flags_it(tmp_path, monkeypatch):
    import pytest
    cli, installs, steps = gate_env(tmp_path, monkeypatch,
                                    "services:\n  x:\n    image: a\n    network_mode: host\n    volumes: ['/var/run/docker.sock:/s']\n")
    with pytest.raises(SystemExit, match="REFUSED: x asks for access that escapes app isolation"):
        cli.install(cli.Config(), "x")
    assert installs == [] and steps == []
    res = cli.install(cli.Config(), "x", allow_host_access=True)
    assert installs == ["x:wizardapps"] and res["status"] == "running"


def test_install_plans_the_networks_before_the_first_start_and_reconciles_after(tmp_path, monkeypatch):
    cli, installs, steps = gate_env(tmp_path, monkeypatch, "services:\n  x:\n    image: a\n")
    cli.install(cli.Config(), "x")
    assert steps == [({"x"}, False), (set(), True)] and installs == ["x:wizardapps"]


def test_wam_serve_shows_the_views_and_the_live_report(monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from wam import cli
    monkeypatch.setattr(net, "report", lambda c, rt=None: {"schema": 1, "apps": [], "ok": True})
    cli.Handler.cfg, cli.Handler.token = object(), "t"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    monkeypatch.setattr(urllib.request, "urlopen", opener.open)
    try:
        with urllib.request.urlopen(base + "/views/") as r:
            assert r.headers["Content-Type"].startswith("text/html") and b"Connections" in r.read()
        with urllib.request.urlopen(base + "/views/wizard-apps.js") as r:
            assert b"export async function renderConnections" in r.read()
        with urllib.request.urlopen(base + "/api/connections") as r:
            assert json.loads(r.read())["ok"] is True
        for bad in ("/views/..%2fwam%2fcli.py", "/views/README.md", "/views/.x.js"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad)
            assert e.value.code == 404
        req = urllib.request.Request(base + "/api/connections", headers={"Sec-Fetch-Site": "cross-site"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_network_off_dry_run_saves_nothing(tmp_path, monkeypatch):
    from wam import cli
    seen = {}
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path))
    monkeypatch.setattr(cli.release_pins, "load", lambda *a, **k: None)
    monkeypatch.setattr(net, "reconcile", lambda c, **k: seen.update(k) or {"errors": []})
    assert cli.main(["network", "off", "--dry-run"]) == 0
    assert seen["on"] is False and seen["apply"] is False
    assert net.enabled(tmp_path) and not (tmp_path / "app-manager.json").exists()


def test_install_fails_closed_when_its_networks_cannot_be_prepared(tmp_path, monkeypatch):
    import pytest
    cli, installs, steps = gate_env(tmp_path, monkeypatch, "services:\n  x:\n    image: a\n")
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "home"))
    gate_env.results.append({"errors": ["no free /27 left in 10.255.0.0/16"]})
    with pytest.raises(SystemExit, match="would start on the shared network"):
        cli.install(cli.Config(), "x")
    assert installs == [] and steps == [({"x"}, False), (set(), True)]


def test_recipes_attached_to_reserved_wizard_networks_are_refused_and_never_isolated(tmp_path):
    doc = {"services": {"x": {"image": "a", "networks": ["sneak"]}},
           "networks": {"sneak": {"external": True, "name": "wizard-link-carlos-crm--twenty-crm"}}}
    assert any("reserved Wizard network" in r for r in net.host_access(doc))
    root = tmp_path / "runtipi"
    d = root / "apps" / "wizardapps" / "x"
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text(yaml.safe_dump(doc))
    a = net.read_app(root, "wizardapps", "x")
    assert a["custom"] and "reserved" in a["custom"]
    assert net.plan([a], [], set())["apps"][0]["state"] == "custom"


def test_update_plans_the_override_from_the_incoming_recipe(tmp_path):
    root = tmp_path / "runtipi"
    inst = root / "apps" / "wizardapps" / "x"
    new = root / "repos" / "wizardapps" / "apps" / "x"
    inst.mkdir(parents=True)
    new.mkdir(parents=True)
    (inst / "docker-compose.yml").write_text("services:\n  old:\n    image: a\n    x-runtipi: {is_main: true}\n")
    (new / "docker-compose.yml").write_text("services:\n  renamed:\n    image: a\n    x-runtipi: {is_main: true}\n")
    assert set(net.read_app(root, "wizardapps", "x")["services"]) == {"old"}
    a = net.read_app(root, "wizardapps", "x", fresh=True)
    assert set(a["services"]) == {"renamed"}
    p = net.plan([a], [], set())
    assert set(net.overlay_for(p["apps"][0])["services"]) == {"renamed"}


def test_container_network_mode_is_host_access_but_same_compose_service_mode_is_not():
    bad = {"services": {"x": {"image": "a", "network_mode": "container:runtipi"}}}
    ok = {"services": {"x": {"image": "a"}, "vpn": {"image": "b", "network_mode": "service:x"}}}
    assert any("container:runtipi" in r for r in net.host_access(bad))
    assert net.host_access(ok) == []
    a = app("x")
    a["services"]["x"]["network_mode"] = "container:runtipi"
    assert net.plan([a], [], set())["apps"][0]["state"] == "custom"


# ---------------------------------------------------------------- review round 4: UI paths, foreign networks, updates

def test_external_and_foreign_named_networks_are_escapes_and_never_isolated(tmp_path):
    """A logical network with external: true / a fixed name pointing at the shared network or another app's network
    is an isolation escape: refused at install and never reported as isolated (Codex PRRT_kwDOVCUOXM6wnh8N)."""
    cases = [{"external": True, "name": "runtipi_tipi_main_network"},
             {"external": True, "name": "twenty-crm_wizardapps_twenty-crm_wizardapps_network"},
             {"external": {"name": "runtipi_tipi_main_network"}},
             {"name": "shared-by-two-apps"},
             {"driver": "macvlan"}]
    for i, n in enumerate(cases):
        doc = {"services": {"x": {"image": "a", "networks": ["sneak"], "x-runtipi": {"is_main": True}}},
               "networks": {"sneak": n}}
        assert net.network_escapes(doc, "x"), n
        assert net.host_access(doc, "x"), n
        root = tmp_path / f"r{i}"
        d = root / "apps" / "wizardapps" / "x"
        d.mkdir(parents=True)
        (d / "docker-compose.yml").write_text(yaml.safe_dump(doc))
        a = net.read_app(root, "wizardapps", "x")
        assert a["custom"] and net.plan([a], [], set())["apps"][0]["state"] == "custom", n
    own = {"services": {"x": {"image": "a", "networks": ["lane"]}, "y": {"image": "b", "network_mode": "service:x"}},
           "networks": {"lane": {"internal": True}, "named": {"name": "x_wizardapps_named"},
                        "tipi_main_network": {"external": True, "name": "runtipi_tipi_main_network"}}}
    assert net.network_escapes(own, "x") == []
    assert net.network_escapes({"services": {"x": {"network_mode": "bridge"}}}, "x")


def test_installer_refuses_an_external_network_recipe(tmp_path, monkeypatch):
    import pytest
    cli, installs, steps = gate_env(tmp_path, monkeypatch, "services:\n  x:\n    image: a\n    networks: [sneak]\n"
                                    "networks:\n  sneak:\n    external: true\n    name: runtipi_tipi_main_network\n")
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit, match="REFUSED: x asks for access that escapes app isolation"):
        cli.install(cli.Config(), "x")
    assert installs == [] and steps == []


def test_allow_host_access_is_recorded_for_the_app_managers_start_gate(tmp_path, monkeypatch):
    cli, installs, steps = gate_env(tmp_path, monkeypatch, "services:\n  x:\n    image: a\n    networks: [sneak]\n"
                                    "networks:\n  sneak:\n    external: true\n    name: somewhere\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    cli.install(cli.Config(), "x", allow_host_access=True)
    assert installs == ["x:wizardapps"] and net.host_access_allowed(home) == {"x"}
    c = type("C", (), {"home": home, "runtipi_root": tmp_path / "runtipi"})()
    assert net.write_policy(c, True) is True and net.write_policy(c, True) is False
    pol = json.loads(net.policy_path(c.runtipi_root).read_text())
    assert pol == {"schema": 1, "enabled": True, "allow_host_access": ["x"]}
    net.set_host_access_allowed(home, "x", False)
    net.write_policy(c, False)
    assert json.loads(net.policy_path(c.runtipi_root).read_text()) == {"schema": 1, "enabled": False, "allow_host_access": []}


def update_env(tmp_path, monkeypatch, installed, incoming, step_results=()):
    from wam import cli
    root = tmp_path / "runtipi"
    for where, text in (("apps/wizardapps/x", installed), ("repos/wizardapps/apps/x", incoming)):
        d = root / where
        d.mkdir(parents=True)
        (d / "docker-compose.yml").write_text(text)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RUNTIPI_ROOT", str(root))
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    updates, steps, results = [], [], list(step_results)

    class RT:
        def app(self, urn): return {"app": {"status": "running", "version": "1"}}
        def update(self, urn, perform_backup=True): updates.append(urn)
        def wait_status(self, urn, want, timeout=900): return "running"
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: RT())
    monkeypatch.setattr(cli.alongside, "load", lambda h: None)

    def step(c, rt=None, extra=(), restart=True, fresh=()):
        steps.append((set(fresh), restart))
        return results.pop(0) if results else {}
    monkeypatch.setattr(cli, "network_step", step)
    return cli, updates, steps


def test_update_stops_when_its_network_preflight_fails(tmp_path, monkeypatch):
    """Codex PRRT_kwDOVCUOXM6wnh8J: with isolation on, an update whose networks can't be prepared never runs."""
    import pytest
    plain = "services:\n  x:\n    image: a\n"
    cli, updates, steps = update_env(tmp_path, monkeypatch, plain, plain,
                                     [{"errors": ["wizard-app-x: a different network with this name exists; not touched"]}])
    with pytest.raises(SystemExit, match="x not updated: its isolated networks could not be prepared"):
        cli.app_update(cli.Config(), "x")
    assert updates == [] and steps == [({"x"}, False), (set(), False)]


def test_update_stops_when_network_preflight_is_unavailable(tmp_path, monkeypatch):
    """No Docker probe means no prepared networks: never apply the incoming recipe."""
    import pytest
    plain = "services:\n  x:\n    image: a\n"
    cli, updates, steps = update_env(tmp_path, monkeypatch, plain, plain,
                                     [{"errors": ["Docker unavailable"], "unavailable": True}])
    with pytest.raises(SystemExit, match="x not updated: its isolated networks could not be prepared"):
        cli.app_update(cli.Config(), "x")
    assert updates == [] and steps == [({"x"}, False), (set(), False)]


def test_grant_and_revoke_reconcile_live_networks_before_registry(tmp_path, monkeypatch):
    """Both CLI grant paths apply their link change now, not at next bootstrap."""
    from wam import cli
    c = Cfg(tmp_path)
    events = []

    class RT:
        def req(self, method, path, body):
            events.append((method, path, body))
            return {"ok": True}

    monkeypatch.setattr(cli, "Config", lambda: c)
    monkeypatch.setattr(c, "runtipi", lambda: RT(), raising=False)
    monkeypatch.setattr(cli, "network_step", lambda config, rt: events.append("reconcile") or {"changed": ["link"]})
    monkeypatch.setattr(cli, "write_registry", lambda config: events.append("registry"))
    for action, approved in (("grant", True), ("revoke", False)):
        events.clear()
        assert cli.main(["grants", action, "consumer", "provider"]) == 0
        assert events == [
            ("POST", "/apps/consumer:wizardapps/app-grants", {"to": "provider", "approve": approved}),
            "reconcile", "registry",
        ]


def test_update_that_adds_host_access_needs_the_owner(tmp_path, monkeypatch):
    """Codex PRRT_kwDOVCUOXM6wnYag: an app approved at install must not gain host access or foreign networks by
    being updated; the owner can allow it explicitly."""
    import pytest
    plain = "services:\n  x:\n    image: a\n"
    sneaky = plain + "    networks: [sneak]\nnetworks:\n  sneak:\n    external: true\n    name: wizard-link-a--b\n"
    cli, updates, steps = update_env(tmp_path, monkeypatch, plain, sneaky)
    with pytest.raises(SystemExit, match="REFUSED: the update of x asks for access"):
        cli.app_update(cli.Config(), "x")
    assert updates == [] and steps == []
    assert cli.app_update(cli.Config(), "x", allow_host_access=True)["status"] == "running"
    assert updates == ["x:wizardapps"] and net.host_access_allowed(tmp_path / "home") == {"x"}
    # an unchanged risk the installed recipe already had is not asked again
    cli, updates, steps = update_env(tmp_path / "c", monkeypatch, sneaky, sneaky)
    assert cli.app_update(cli.Config(), "x")["status"] == "running"


def test_exposed_core_components_stay_on_the_gateway_networks():
    """Codex PRRT_kwDOVCUOXM6wnYak: a shell / Agent Service published through Runtipi keeps the shared network AND
    joins wizard-core and every app's wizard-gw-<id>; the plan never falls back to loopback because of it."""
    ux = app("wizard-ux", uses=["agent-service"], provides=["shell"])
    ux["services"]["wizard-ux"]["exposed"] = True
    svc = app("wizard-agent-service", uses=["hermes-agent"], provides=["agent-service"])
    svc["services"]["wizard-agent-service"]["exposed"] = True
    p = net.plan([ux, svc, app("hermes-agent", provides=["agent"]), app("transcriber")], [], set())
    assert p["gateway"]["path"] == "network"
    gw = p["networks"][net.gw_net("transcriber")]
    assert {m["app"] for m in gw["members"]} == {"transcriber", "wizard-ux", "wizard-agent-service", "hermes-agent"}
    assert {m["app"] for m in p["networks"][net.CORE_NET]["members"]} == {"wizard-ux", "wizard-agent-service", "hermes-agent"}
    ov = net.overlay_for(next(a for a in p["apps"] if a["id"] == "wizard-ux"))
    assert {"tipi_main_network", net.CORE_NET, net.gw_net("transcriber")} <= set(ov["services"]["wizard-ux"])
    # every core exposed: still the network path
    allx = [app(i, provides=[s]) for i, s in (("wizard-ux", "shell"), ("wizard-agent-service", "agent-service"))]
    for a in allx:
        a["services"][a["id"]]["exposed"] = True
    p2 = net.plan(allx + [app("transcriber")], [], set())
    assert p2["gateway"]["path"] == "network" and net.gw_net("transcriber") in p2["networks"]


def test_serve_watcher_reconciles_after_every_wizard_app_start():
    """Codex PRRT_kwDOVCUOXM6wnh7z / wnh8A: installs and updates from the App Store views or Runtipi's UI start the
    app through the App Manager API; wam serve reconciles after each start (a burst of starts is one reconcile), and
    reconciles without restarts when idle."""
    import threading
    calls = []
    done = threading.Event()

    def step(restart):
        calls.append(restart)
        if len(calls) >= 2:
            done.set()
    net.watch(step, events=iter(["twenty-crm", "twenty-crm", None]), debounce=0.2, idle=5)
    assert calls == [True]
    stop = threading.Event()
    t = threading.Thread(target=net.watch, args=(step,), kwargs={"events": iter([]), "idle": 0.1, "stop": stop}, daemon=True)
    t.start()
    assert done.wait(5)
    stop.set()
    assert calls[1] is False


def test_reconcile_writes_the_app_managers_policy_first(tmp_path, monkeypatch):
    c = Cfg(tmp_path)
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(), **k: (net.plan(carlos(), [], set(), on=False), "wizardapps"))
    monkeypatch.setattr(net, "docker_cmd", lambda: (_ for _ in ()).throw(net.NetworkError("no docker")))
    res = net.reconcile(c)
    assert res["unavailable"] and json.loads(net.policy_path(c.runtipi_root).read_text())["enabled"] is False
    assert net.reconcile(c, apply=False)["unavailable"]


def test_wam_serve_page_offers_only_the_views_its_api_backs(monkeypatch):
    """Codex PRRT_kwDOVCUOXM6wnh8R: 7790 has /api/connections only, so its page is the Connections view alone."""
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from wam import cli
    monkeypatch.setattr(net, "report", lambda c, rt=None: {"schema": 1, "apps": [], "ok": True})
    cli.Handler.cfg, cli.Handler.token = object(), "t"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{srv.server_address[1]}/views/") as r:
            assert b'data-only="connections"' in r.read()
        for gone in ("/api/apps/installed", "/wizard/v1/index/apps"):
            try:
                opener.open(f"http://127.0.0.1:{srv.server_address[1]}{gone}")
                raise AssertionError(gone)
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
    js = (Path(__file__).resolve().parent.parent / "wizard-views" / "standalone.js").read_text()
    assert "document.body.dataset.only" in js and "const view = only ||" in js


def test_a_revoked_grant_takes_the_link_away_and_automatic_grants_follow_their_declaration():
    """`wam grants revoke` must remove the network path even though `uses:` still names the provider (Codex
    PRRT_kwDOVCUOXM6wnNPB), and an update that drops a declaration drops its link although the App Manager's automatic
    grant for it stays in wizard-grants.json (seen live: UI update of stripe-connector)."""
    auto = lambda frm, to: {"kind": "app", "from": frm, "to": to, "reason": "uses", "approved": "install"}
    apps = [app("twenty-crm"), app("resend-connector"), app("stripe-connector", uses=["twenty-crm"])]
    ok = [auto("stripe-connector", "twenty-crm")]
    assert ("stripe-connector", "twenty-crm") in reach(net.plan(apps, ok, set(), approved=ok))
    p = net.plan(apps, [], set(), approved=[])            # revoked (moved to denied): declared, but no network
    assert ("stripe-connector", "twenty-crm") not in reach(p)
    assert [e["state"] for e in p["edges"] if e["from"] == "stripe-connector"] == ["not-approved"]
    stale = ok + [auto("stripe-connector", "resend-connector")]   # uses: resend-connector was dropped by an update
    assert ("stripe-connector", "resend-connector") not in reach(net.plan(apps, stale, set(), approved=stale))
    owner = stale + [{"kind": "app", "from": "stripe-connector", "to": "resend-connector", "approved": "owner"}]
    assert ("stripe-connector", "resend-connector") in reach(net.plan(apps, owner, set(), approved=owner))
    slot = [app("twenty-crm", provides=["crm"]), app("c", uses=["crm"])]
    g = [auto("c", "slot:crm")]
    assert ("c", "twenty-crm") in reach(net.plan(slot, g, set(), approved=g))
    # an App Manager without grants (approved unknown): declarations alone, as before
    assert ("stripe-connector", "twenty-crm") in reach(net.plan(apps, [], set()))


def test_only_the_active_core_providers_join_the_gateway_networks():
    """Codex PRRT_kwDOVCUOXM6wqhWD: an installed but inactive replacement shell / Agent Service / agent is an ordinary
    app; it never joins every app's wizard-gw-<id>."""
    apps = [app("wizard-ux", provides=["shell"]), app("other-shell", provides=["shell"]),
            app("wizard-agent-service", provides=["agent-service"]), app("hermes-agent", provides=["agent"]),
            app("transcriber")]
    p = net.plan(apps, [], set(), slot_choice={"shell": "wizard-ux"})
    assert p["gateway"]["core"] == ["hermes-agent", "wizard-agent-service", "wizard-ux"]
    gw = {m["app"] for m in p["networks"][net.gw_net("transcriber")]["members"]}
    assert "other-shell" not in gw and "wizard-ux" in gw
    assert ("other-shell", "transcriber") not in reach(p)
    p2 = net.plan(apps, [], set(), slot_choice={"shell": "other-shell"})
    assert "other-shell" in p2["gateway"]["core"] and "wizard-ux" not in p2["gateway"]["core"]
    # another agent provider is the active one: hermes-agent is then an ordinary app
    p3 = net.plan(apps + [app("my-agent", provides=["agent"])], [], set(), slot_choice={"agent": "my-agent"})
    assert "my-agent" in p3["gateway"]["core"] and "hermes-agent" not in p3["gateway"]["core"]


def test_link_names_are_unique_per_pair_even_with_double_hyphen_ids():
    """Codex PRRT_kwDOVCUOXM6wqhWN: `alpha--beta`->`charlie` and `alpha`->`beta--charlie` must not share a link."""
    a, b = net.link_name("alpha--beta", "charlie"), net.link_name("alpha", "beta--charlie")
    assert a != b and net.ours(a) and net.ours(b) and "--" not in a.removeprefix(net.LINK_PREFIX)
    assert net.link_name("carlos-crm", "twenty-crm") == "wizard-link-carlos-crm--twenty-crm"   # unchanged
    assert net.link_name("a-", "b") != net.link_name("a", "-b")
    p = net.plan([app("alpha--beta", uses=["charlie"]), app("charlie"), app("alpha", uses=["beta--charlie"]),
                  app("beta--charlie")], [], set())
    assert ("alpha--beta", "beta--charlie") not in reach(p) and ("alpha", "charlie") not in reach(p)
    assert ("alpha--beta", "charlie") in reach(p) and ("alpha", "beta--charlie") in reach(p)


def test_a_planned_name_taken_by_a_foreign_network_is_never_joined(tmp_path, monkeypatch):
    """Codex PRRT_kwDOVCUOXM6wqhWV: the collision is reported and nothing is connected to the foreign network, no
    override references it, and a container already on it is taken off."""
    c = Cfg(tmp_path)
    apps = lambda: [app("twenty-crm"), app("stripe-connector", uses=["twenty-crm"])]
    p0 = net.plan(apps(), [], set())
    live = live_from(p0, on_shared={"twenty-crm", "stripe-connector"})
    link = net.link_name("stripe-connector", "twenty-crm")
    live["networks"] = {"runtipi_tipi_main_network": {"labels": {}, "internal": False, "subnets": ["172.18.0.0/16"], "containers": []},
                        link: {"labels": {}, "internal": False, "subnets": ["10.9.0.0/24"], "containers": ["intruder"]}}
    prov = next(x for x in live["containers"] if x["service"] == "twenty-crm")
    prov["networks"][link] = []
    fake = FakeDocker(live)
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(), **k: (net.plan(apps(), [], set()), "wizardapps"))
    monkeypatch.setattr(net, "docker_cmd", lambda: ["docker"])
    monkeypatch.setattr(net, "_run", fake.run)
    monkeypatch.setattr(net, "observe", lambda d, store="wizardapps": json.loads(json.dumps(fake.live)))
    monkeypatch.setattr(net, "ports_answer", lambda ports, **k: True)
    res = net.reconcile(c, restart_fn=lambda a: {"status": "running"})
    assert any(link in e and "different network" in e for e in res["errors"])
    assert not any(x[:2] == ("network", "connect") and link in x for x in fake.calls)
    assert link in fake.live["networks"]                                   # the foreign network is left alone
    assert link not in next(x for x in fake.live["containers"] if x["service"] == "twenty-crm")["networks"]
    for app_id in ("twenty-crm", "stripe-connector"):
        f = net.override_path(c.runtipi_root, "wizardapps", app_id)
        assert not f.exists() or link not in f.read_text()


def test_fresh_report_shows_the_core_reaching_each_app_over_its_gateway_network(monkeypatch):
    """Codex PRRT_kwDOVCUOXM6wqhWg: an isolated app on a fresh box is reached by the shell, Agent Service and agent."""
    apps = [app("wizard-ux", provides=["shell"]), app("wizard-agent-service", provides=["agent-service"]),
            app("hermes-agent", provides=["agent"]), app("transcriber")]
    p = net.plan(apps, [], set())
    rep = net.report(type("C", (), {"home": Path("/nonexistent")})(), p=p, live={"networks": {}, "containers": []})
    row = next(a for a in rep["apps"] if a["id"] == "transcriber")
    assert {b["from"] for b in row["reached_by"] if b["state"] == "core"} == {"wizard-ux", "wizard-agent-service", "hermes-agent"}
    assert "reached by: hermes-agent, wizard-agent-service, wizard-ux" in net.describe(rep)


def test_an_exposed_app_keeps_the_shared_network_only_on_its_published_service(tmp_path, monkeypatch):
    """Codex PRRT_kwDOVCUOXM6wq1Yx: a multi-service app publishes `web` through Traefik and also has an unexposed
    `worker` with add_to_main_network: only `web` stays on the shared network; `worker` leaves it (override + live),
    matching the App Manager's per-service start gate."""
    a = app("multi", services={
        "web": {"main": True, "on_shared": True, "network_mode": "", "exposed": True,
                "networks": {"tipi_main_network": {"gw_priority": 1}, "multi_wizardapps_network": {"gw_priority": 0}}},
        "worker": {"main": False, "on_shared": True, "network_mode": "", "exposed": False,
                   "networks": {"tipi_main_network": {}, "multi_wizardapps_network": {}}}})
    p = net.plan([a], [], set())
    assert p["apps"][0]["state"] == "exposed"
    ov = net.overlay_for(p["apps"][0])
    assert "tipi_main_network" in ov["services"]["web"] and "tipi_main_network" not in ov["services"]["worker"]
    assert "multi_wizardapps_network" in ov["services"]["worker"]
    live = live_from(p, on_shared={"multi"})
    rows = {r["service"]: r for r in net.drift(p, live)}
    assert "web" not in rows and rows["worker"]["on_shared"] and not rows["worker"]["should_share"]
    c = Cfg(tmp_path)
    live["networks"] = {"runtipi_tipi_main_network": {"labels": {}, "internal": False, "subnets": ["172.18.0.0/16"], "containers": []}}
    fake = FakeDocker(live)
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(), **k: (net.plan([json.loads(json.dumps(a))], [], set()), "wizardapps"))
    monkeypatch.setattr(net, "docker_cmd", lambda: ["docker"])
    monkeypatch.setattr(net, "_run", fake.run)
    monkeypatch.setattr(net, "observe", lambda d, store="wizardapps": json.loads(json.dumps(fake.live)))
    monkeypatch.setattr(net, "ports_answer", lambda ports, **k: True)
    res = net.reconcile(c, restart_fn=lambda x: {"status": "running"})
    assert not res["errors"] and res["drift"] == []
    nets = {x["service"]: set(x["networks"]) for x in fake.live["containers"]}
    assert net.SHARED_DOCKER in nets["web"] and net.SHARED_DOCKER not in nets["worker"]


def test_an_app_that_becomes_custom_loses_only_wams_stale_override_block(tmp_path, monkeypatch):
    """Codex PRRT_kwDOVCUOXM6wq1Y2: an isolated app updated (with the owner's allowance) to host or foreign networking
    is custom from then on; its old x-wizard-networks !override is removed, the rest of the file is kept, and a
    custom app's override without wam's block is never touched."""
    c = Cfg(tmp_path)
    iso = net.plan([app("x")], [], set())["apps"][0]
    f = net.override_path(c.runtipi_root, "wizardapps", "x")
    f.parent.mkdir(parents=True)
    f.write_text("services:\n  x-engine:\n    user: '0:0'\n")
    net.write_overlay(f, net.overlay_for(iso))
    assert net.MARK in f.read_text()
    custom = app("x", custom="its compose attaches to networks outside the app: network sneak")
    hand = net.override_path(c.runtipi_root, "wizardapps", "y")
    hand.parent.mkdir(parents=True)
    hand.write_text("services:\n  y:\n    network_mode: host\n")
    custom_y = app("y", custom="its user-config override sets the networks of y by hand")
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(), **k: (net.plan([custom, custom_y], [], set()), "wizardapps"))
    monkeypatch.setattr(net, "docker_cmd", lambda: ["docker"])
    fake = FakeDocker({"networks": {}, "containers": []})
    monkeypatch.setattr(net, "_run", fake.run)
    monkeypatch.setattr(net, "observe", lambda d, store="wizardapps": json.loads(json.dumps(fake.live)))
    res = net.reconcile(c)
    assert "x" in res["overrides"] and not res["errors"]
    text = f.read_text()
    assert net.MARK not in text and "networks" not in text and "x-engine" in text
    assert hand.read_text() == "services:\n  y:\n    network_mode: host\n"


def test_a_failed_update_submission_restores_the_installed_topology(tmp_path, monkeypatch):
    """Codex PRRT_kwDOVCUOXM6wrNyC: the incoming-recipe preflight ran, then the update PATCH failed: the installed
    recipe's networks/override are reconciled back before the error is raised."""
    import pytest
    plain = "services:\n  x:\n    image: a\n"
    cli, updates, steps = update_env(tmp_path, monkeypatch, plain, plain)

    def boom(self, urn, perform_backup=True):
        raise cli.RuntipiError("HTTP 502 from the App Manager")
    monkeypatch.setattr(type(cli.Config().runtipi()), "update", boom)
    with pytest.raises(cli.RuntipiError):
        cli.app_update(cli.Config(), "x")
    assert steps == [({"x"}, False), (set(), False)]


def test_network_deny_stops_a_running_app_that_still_has_the_access(tmp_path, monkeypatch):
    """Codex PRRT_kwDOVCUOXM6wrNyI: revoking an allowance must take the live access away too."""
    from wam import cli
    root, home = tmp_path / "runtipi", tmp_path / "home"
    d = root / "apps" / "wizardapps" / "x"
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text("services:\n  x:\n    image: a\n    network_mode: host\n")
    home.mkdir()
    monkeypatch.setenv("RUNTIPI_ROOT", str(root))
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.setattr(cli.release_pins, "load", lambda *a, **k: None)
    net.set_host_access_allowed(home, "x", True)
    calls = []
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: object())
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"x"})
    monkeypatch.setattr(cli, "app_lifecycle", lambda c, verb, a: calls.append((verb, a)) or {"status": "stopped"})
    monkeypatch.setattr(net, "reconcile", lambda c, **k: calls.append("reconcile") or {"errors": []})
    assert cli.main(["network", "deny", "x"]) == 0
    assert calls == [("stop", "x"), "reconcile"] and net.host_access_allowed(home) == set()
    calls.clear()
    assert cli.main(["network", "allow", "x"]) == 0 and calls == ["reconcile"]   # allowing never stops anything


def test_a_service_that_must_be_on_the_shared_network_is_put_back():
    """Codex PRRT_kwDOVCUOXM6wrNyM: isolation off (or an exposed service) but the container is only on its own
    network: drift reports it so reconcile reconnects it."""
    off = net.plan([app("x")], [], set(), on=False)
    live = live_from(off)
    rows = net.drift(off, live)
    assert rows and rows[0]["should_share"] and not rows[0]["on_shared"]
    ex = app("y")
    ex["services"]["y"]["exposed"] = True
    p = net.plan([ex], [], set())
    rows = net.drift(p, live_from(p))
    assert rows and rows[0]["should_share"] and not rows[0]["on_shared"]


def test_app_and_slot_grants_of_the_same_name_stay_distinct():
    """Codex PRRT_kwDOVCUOXM6wrlDf: an approved grant to the app `crm` must not authorize the revoked `slot:crm`
    declaration (and the other way round); targets are named as the App Manager names them."""
    owner = lambda frm, to: {"kind": "app", "from": frm, "to": to, "reason": "uses", "approved": "install"}
    apps = [app("crm"), app("twenty-crm", provides=["crm"]), app("c", uses=["crm", "slot:crm"])]
    only_app = [owner("c", "crm")]
    pairs = reach(net.plan(apps, only_app, set(), approved=only_app))
    assert ("c", "crm") in pairs and ("c", "twenty-crm") not in pairs
    only_slot = [owner("c", "slot:crm")]
    pairs = reach(net.plan(apps, only_slot, set(), approved=only_slot))
    assert ("c", "twenty-crm") in pairs and ("c", "crm") not in pairs
    # `uses: crm` with no app named crm installed is a slot declaration, granted as slot:crm
    apps2 = [app("twenty-crm", provides=["crm"]), app("d", uses=["crm"])]
    g = [owner("d", "slot:crm")]
    assert ("d", "twenty-crm") in reach(net.plan(apps2, g, set(), approved=g))
    assert ("d", "twenty-crm") not in reach(net.plan(apps2, [owner("d", "crm")], set(), approved=[owner("d", "crm")]))


def test_a_stale_generated_compose_never_carries_a_foreign_network_into_the_override(tmp_path):
    """Seen in the VM: the App Manager refused an update whose recipe attached `sneak` (external), the update rolled
    back to the clean recipe, but docker-compose.generated.yml still held the refused version. wam must plan from the
    installed recipe's networks only, or the override names `sneak` and Compose rejects every later start."""
    write_app(tmp_path, "wizardapps", "x", {"id": "x"},
              {"services": {"x": {"image": "a", "x-runtipi": {"is_main": True}}}},
              generated={"services": {"x": {"networks": {"sneak": None, "tipi_main_network": {"gw_priority": 1}}}},
                         "networks": {"sneak": {"external": True, "name": "runtipi_tipi_main_network"},
                                      "tipi_main_network": {"external": True, "name": "runtipi_tipi_main_network"}}})
    a = net.read_app(tmp_path, "wizardapps", "x")
    assert a["custom"] is None and set(a["services"]["x"]["networks"]) == {"tipi_main_network"}
    ov = net.overlay_for(net.plan([a], [], set())["apps"][0])
    assert "sneak" not in ov["services"]["x"] and set(ov["services"]["x"]) == {"wizard-app-x"}


def test_host_network_apps_are_never_asked_to_rejoin_the_shared_network():
    """Seen in the VM after the should_share repair: alongside's shell / Agent Service run with network_mode: host
    (from their user-config override) while the generated compose still lists the shared network. They must produce
    no drift row; otherwise `docker network connect` fails, reconcile restarts them, and the serve watcher reconciles
    again after every restart (a restart loop)."""
    ux = app("wizard-ux", provides=["shell"])
    ux["services"]["wizard-ux"]["network_mode"] = "host"
    p = net.plan([ux, app("transcriber")], [], set())
    assert next(a for a in p["apps"] if a["id"] == "wizard-ux")["state"] == "host-network"
    live = live_from(p, on_shared={"transcriber"})
    for cont in live["containers"]:
        if cont["service"] == "wizard-ux":
            cont["network_mode"], cont["networks"] = "host", {"host": []}
    assert not [r for r in net.drift(p, live) if r["app"] == "wizard-ux"]


def test_the_serve_watcher_never_restarts_the_same_app_twice_in_a_row(monkeypatch):
    """A reconcile that can't converge must not turn the watcher (reconcile after every start) into a restart loop."""
    from wam import cli
    restarts, steps = [], []
    monkeypatch.delenv("WAM_NETWORK_WATCH", raising=False)
    monkeypatch.setattr(cli, "app_lifecycle", lambda c, verb, a: restarts.append(a) or {"status": "running"})
    monkeypatch.setattr(cli, "write_registry", lambda c: None)

    def step(c, rt=None, extra=(), restart=True, fresh=(), restart_fn=None):
        steps.append(restart)
        res = restart_fn("wizard-ux")
        return {"restarted": ["wizard-ux"]} if not res.get("error") else {"errors": [res["error"]]}
    monkeypatch.setattr(cli, "network_step", step)
    captured = {}
    monkeypatch.setattr(net, "watch", lambda fn, **k: captured.setdefault("fn", fn))
    import threading
    monkeypatch.setattr(threading, "Thread", lambda target, args=(), **k: type("T", (), {"start": lambda self: target(*args)})())
    cli.start_network_watch(object())
    for _ in range(3):
        captured["fn"](True)
    assert restarts == ["wizard-ux"] and steps == [True, True, True]


def test_the_report_lists_shared_network_peers_both_ways():
    """Codex PRRT_kwDOVCUOXM6wr7M1: apps still on Runtipi's shared network (isolation off, a published service,
    custom apps) can reach each other undeclared; `wam connections` and the view must say so."""
    C = type("C", (), {"home": Path("/nonexistent")})()
    off = net.plan([app("a"), app("b"), app("c", uses=["a"])], [], set(), on=False)
    rep = net.report(C, p=off, live={"networks": {}, "containers": []})
    row = {r["id"]: r for r in rep["apps"]}
    assert {e["to"] for e in row["b"]["can_reach"] if e["state"] == "shared-network"} == {"a", "c"}
    assert {e["from"] for e in row["b"]["reached_by"] if e["state"] == "shared-network"} == {"a", "c"}
    assert "a (shared network: not declared, but reachable)" in net.describe(rep)
    ex = app("pub")
    ex["services"]["pub"]["exposed"] = True
    on = net.plan([ex, app("cust", custom="hand-set networks"), app("iso")], [], set())
    row = {r["id"]: r for r in net.report(C, p=on, live={"networks": {}, "containers": []})["apps"]}
    assert {e["to"] for e in row["pub"]["can_reach"] if e["state"] == "shared-network"} == {"cust"}
    assert not [e for e in row["iso"]["can_reach"] if e["state"] == "shared-network"]


# ---------------------------------------------------------------- open mode (the default): everything connected
# unless the owner disconnects it (docs/APP_NETWORKS.md "Open mode")

def _st(grants=(), denied=(), pending=(), sensitive=(), mode="open"):
    row = lambda f, t, **k: {"kind": "app", "from": f, "to": t, **k}  # noqa: E731
    return {"grants": [row(*g[:2], approved=(g[2] if len(g) > 2 else "install")) for g in grants],
            "denied": [row(*d, approved="owner") for d in denied], "pending": [row(*p) for p in pending],
            "sensitive": list(sensitive), "notices": [], "mode": mode}


def _many(n):
    return [app(f"app{i:02d}") for i in range(n)]


def test_open_mode_puts_every_app_on_one_mesh_and_every_pair_reaches():
    apps = carlos()
    p = net.plan(apps, [], set(), state=_st(), mode="open")
    ids = {a["id"] for a in apps}
    assert set(p["networks"]) == {net.MESH_NET} | {net.app_net(i) for i in ids}
    mesh = p["networks"][net.MESH_NET]
    assert mesh["internal"] and mesh["kind"] == "mesh" and mesh["prefixlen"] == net.MESH_PREFIXLEN
    assert reach(p) >= {(a, b) for a in ids for b in ids if a != b}
    # every main service answers by its app id on the mesh; a database that never was on the shared network stays off
    twenty = next(m for m in mesh["members"] if m["app"] == "twenty-crm")
    assert twenty == {"app": "twenty-crm", "service": "twenty-crm", "aliases": ["twenty-crm"]}
    assert not any(m["service"] == "twenty-db" for m in mesh["members"])
    assert {e["state"] for e in p["edges"] if e["provider"]} == {"mesh"}
    ov = net.overlay_for(next(a for a in p["apps"] if a["id"] == "carlos-crm"))
    assert ov["services"]["carlos-crm"][net.MESH_NET] == {"aliases": ["carlos-crm"]}
    assert ov["networks"][net.MESH_NET] == {"name": net.MESH_NET, "external": True}


def test_a_disconnect_cuts_exactly_that_pair_both_ways():
    apps = carlos()
    st = _st(denied=[("carlos-crm", "resend-connector")])
    p = net.plan(apps, [], set(), state=st, mode="open")
    pairs = reach(p)
    ids = {a["id"] for a in apps}
    want = {(a, b) for a in ids for b in ids if a != b} - {("carlos-crm", "resend-connector"), ("resend-connector", "carlos-crm")}
    assert pairs == want
    assert p["open"]["out"] == {"resend-connector"}    # the app disconnected from leaves the mesh ...
    links = [n for n, v in p["networks"].items() if v["kind"] == "link"]
    assert len(links) == len(ids) - 2                  # ... and gets one link to each app it still reaches
    for n in links:
        v = p["networks"][n]
        assert v["internal"] and all(m["aliases"] for m in v["members"])   # both ends answer by their id
    e = next(e for e in p["edges"] if e["from"] == "carlos-crm" and e["provider"] == "resend-connector")
    assert e["state"] == "disconnected" and e["network"] is None
    # slot-named Disconnects cut the app behind the slot
    p2 = net.plan(carlos(), [], set(), state=_st(denied=[("carlos-campaigns", "slot:email")]), mode="open")
    assert ("carlos-campaigns", "resend-connector") not in reach(p2) and ("resend-connector", "carlos-campaigns") not in reach(p2)


def test_sensitive_apps_keep_their_connections_and_new_ones_wait():
    apps = carlos()
    st = _st(grants=[("carlos-crm", "resend-connector", "open"), ("twenty-crm", "resend-connector", "open")],
             pending=[("stripe-connector", "resend-connector")], sensitive=["resend-connector"])
    p = net.plan(apps, [], set(), state=st, mode="open")
    pairs = reach(p)
    assert ("carlos-crm", "resend-connector") in pairs and ("resend-connector", "twenty-crm") in pairs
    for other in ("stripe-connector", "calendly-connector", "carlos-campaigns", "gohighlevel-connector"):
        assert (other, "resend-connector") not in pairs and ("resend-connector", other) not in pairs
    assert p["open"]["blocked"][frozenset(("stripe-connector", "resend-connector"))] == "pending"
    assert p["open"]["blocked"][frozenset(("calendly-connector", "resend-connector"))] == "sensitive"
    assert p["open"]["out"] == {"resend-connector"}
    # the rest still reach each other on the mesh
    assert ("stripe-connector", "calendly-connector") in pairs


def test_an_owner_reconnect_from_the_sensitive_apps_side_opens_the_pair():
    base = dict(sensitive=["resend-connector"], grants=[("carlos-crm", "resend-connector", "open")])
    st = _st(**{**base, "grants": base["grants"] + [("resend-connector", "stripe-connector", "owner")]})
    pairs = reach(net.plan(carlos(), [], set(), state=st, mode="open"))
    assert ("stripe-connector", "resend-connector") in pairs and ("resend-connector", "stripe-connector") in pairs
    # an automatic grant of the sensitive app itself (its own declaration) is not the owner's approval
    st = _st(**{**base, "grants": base["grants"] + [("resend-connector", "stripe-connector", "install")]})
    assert ("stripe-connector", "resend-connector") not in reach(net.plan(carlos(), [], set(), state=st, mode="open"))


def test_declared_mode_is_unchanged_and_open_grants_never_link_there():
    st = _st(grants=[("calendly-connector", "resend-connector", "open")], sensitive=["resend-connector"], mode="declared")
    p = net.plan(carlos(), [], set(), state=st, mode="declared")
    assert net.MESH_NET not in p["networks"]
    assert ("calendly-connector", "resend-connector") not in reach(p)
    assert ("calendly-connector", "twenty-crm") in reach(p)
    assert ("calendly-connector", "stripe-connector") not in reach(p)
    # declared is the default (no mode recorded, or an App Manager without connection state); open is the owner's choice
    assert net.connection_mode(None) == "declared" and net.connection_mode({}) == "declared"
    assert net.connection_mode({"mode": "open"}) == "open" and net.connection_mode({"mode": "declared"}) == "declared"
    assert net._state_from({"grants": [], "denied": []})["mode"] == "declared"
    assert net.connection_mode({"mode": "nonsense"}) == "declared"


def test_open_mode_scales_to_sixty_apps_with_a_handful_of_networks():
    apps = _many(60)
    st = _st(denied=[("app01", "app02"), ("app03", "app02"), ("app10", "app11")], sensitive=["app40"],
             grants=[(f"app{i:02d}", "app40", "open") for i in range(40)])
    p = net.plan(apps, [], set(), state=st, mode="open")
    shared = [n for n, v in p["networks"].items() if v["kind"] != "app"]
    # per-pair networks would be 60*59/2 = 1770; here: the mesh + one link per (app out of the mesh, app it reaches)
    assert len(shared) < 1 + 3 * 60
    assert p["open"]["out"] == {"app02", "app40", "app11"} or p["open"]["out"] == {"app02", "app40", "app10"}
    pairs = reach(p)
    ids = [a["id"] for a in apps]
    cut = {frozenset(("app01", "app02")), frozenset(("app03", "app02")), frozenset(("app10", "app11"))}
    cut |= {frozenset((f"app{i:02d}", "app40")) for i in range(41, 60)}
    for a in ids:
        for b in ids:
            if a != b:
                assert ((a, b) in pairs) == (frozenset((a, b)) not in cut), (a, b)


def test_the_mesh_is_reserved_and_owned_like_every_wam_network():
    assert net.ours(net.MESH_NET) and not net.ours("wizard-mesh_wizardapps_network")
    doc = {"services": {"x": {"image": "a", "networks": ["sneak"]}}, "networks": {"sneak": {"external": True, "name": "wizard-mesh"}}}
    assert any("reserved Wizard network" in r for r in net.host_access(doc))
    live = {"labels": {net.LABEL: "mesh"}, "internal": True, "subnets": [], "containers": []}
    assert net.owned_network(net.MESH_NET, live)
    assert not net.owned_network(net.MESH_NET, {**live, "internal": False})
    assert not net.owned_network("wizard-core", live)
    sub = net._free_subnet(["10.255.0.0/27"], ipaddress.IPv4Network("10.255.0.0/16"), set(), net.MESH_PREFIXLEN)
    assert sub == "10.255.4.0/22"


def test_report_and_describe_say_the_mode_plainly(monkeypatch):
    from types import SimpleNamespace
    st = _st(denied=[("carlos-crm", "resend-connector")])
    p = net.plan(carlos(), [], set(), state=st, mode="open")
    p["state"] = st
    live = {"networks": {}, "containers": []}
    rep = net.report(SimpleNamespace(), p=p, live=live)
    assert rep["mode"] == "open"
    crm = next(a for a in rep["apps"] if a["id"] == "carlos-crm")
    assert crm["open"]["unreachable"] == [{"app": "resend-connector", "why": "disconnected"}]
    text = net.describe(rep)
    assert "Open: everything is connected unless you disconnect it" in text
    assert "can reach: every other app except resend-connector (you disconnected it" in text
    assert "reached by: every other app except resend-connector;" in text
    from wam import connections
    assert connections.signature({**st, "mode": "open"}) != connections.signature({**st, "mode": "declared"})


def test_wam_network_mode_goes_through_the_owner_api(monkeypatch, capsys):
    from wam import cli
    calls = []

    class RT:
        def req(self, method, path, body=None):
            calls.append((method, path, body))
            return {"mode": (body or {}).get("mode", "open"), "grants": [], "denied": []}
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: RT())
    monkeypatch.setattr(net, "reconcile", lambda *a, **k: {"errors": []})
    monkeypatch.setattr(cli, "write_registry", lambda c: None)
    monkeypatch.setattr(cli.release_pins, "load", lambda *a, **k: None)
    assert cli.main(["network", "mode", "declared"]) == 0
    assert ("POST", "/apps/wizard/connection-mode", {"mode": "declared"}) in calls
    assert json.loads(capsys.readouterr().out)["mode"] == "declared"
    with pytest.raises(SystemExit):
        cli.main(["network", "mode", "everything"])


def test_a_sensitive_apps_own_waiting_request_is_reported_as_waiting():
    st = _st(sensitive=["resend-connector"], pending=[("resend-connector", "stripe-connector")],
             grants=[("carlos-crm", "resend-connector", "open"), ("resend-connector", "stripe-connector", "install")])
    p = net.plan(carlos(), [], set(), state=st, mode="open")
    assert p["open"]["blocked"][frozenset(("stripe-connector", "resend-connector"))] == "pending"
    assert ("resend-connector", "stripe-connector") not in reach(p)


def test_the_mesh_fits_a_small_configured_pool():
    n = {"name": net.MESH_NET, "prefixlen": net.MESH_PREFIXLEN, "members": [{}] * 20}
    small = ipaddress.IPv4Network("10.99.0.0/25")
    assert net._subnet_for(n, [], small, set()) == "10.99.0.0/25"
    # takes a smaller free block when the big one is used, as long as every member fits with headroom
    assert net._subnet_for(n, ["10.99.0.0/26"], small, set()) == "10.99.0.64/26"
    with pytest.raises(net.NetworkError):
        net._subnet_for(n, ["10.99.0.0/26", "10.99.0.64/27"], small, set())   # 36 addresses don't fit in a /27
    assert net._subnet_for({"name": "wizard-link-a--b"}, [], small, set()) == "10.99.0.0/27"


def test_a_cut_between_two_published_apps_is_reported_not_claimed():
    apps = carlos()
    for a in apps:
        if a["id"] in ("carlos-crm", "resend-connector", "stripe-connector"):
            a["services"][a["id"]]["exposed"] = True       # published through Runtipi's reverse proxy
    st = _st(denied=[("carlos-crm", "resend-connector"), ("carlos-crm", "twenty-crm")])
    p = net.plan(apps, [], set(), state=st, mode="open")
    o = p["open"]
    assert frozenset(("carlos-crm", "resend-connector")) in o["unenforced"]
    assert frozenset(("carlos-crm", "resend-connector")) not in o["blocked"]
    assert o["blocked"][frozenset(("carlos-crm", "twenty-crm"))] == "disconnected"   # twenty-crm isn't published: enforced
    p["state"] = st
    rep = net.report(__import__("types").SimpleNamespace(), p=p, live={"networks": {}, "containers": []})
    crm = next(a for a in rep["apps"] if a["id"] == "carlos-crm")
    assert crm["open"]["unenforced"] == [{"app": "resend-connector", "why": "disconnected"}]
    assert "NOT CUT: resend-connector" in net.describe(rep)
