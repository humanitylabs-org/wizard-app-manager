"""App networks: declared connections are the only app-to-app paths (wam/network.py)."""
import ipaddress
import json
from pathlib import Path

import pytest
import yaml

from wam import network as net


def app(app_id, uses=(), provides=(), services=None, **kw):
    services = services or {app_id: {"main": True, "on_shared": True, "network_mode": "", "exposed": False,
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
    p = net.plan([ex, cu, app("plain", uses=["exposed"])], [], set())
    st = {a["id"]: a["state"] for a in p["apps"]}
    assert st == {"exposed": "exposed", "custom": "custom", "plain": "isolated"}
    assert next(e for e in p["edges"] if e["from"] == "plain")["state"] == "unmanaged"
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
    return {"networks": {n: {"labels": {net.LABEL: v["kind"]}, "internal": v["internal"], "subnets": [],
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
                        "wizard-link-old--gone": {"labels": {net.LABEL: "link"}, "internal": True, "subnets": ["10.255.0.0/27"], "containers": []}}
    fake = FakeDocker(live)
    restarted = []
    # stripe's published port stops answering after it leaves the shared network: only it is restarted
    monkeypatch.setattr(net, "ports_answer", lambda ports, **k: not ports)

    def restart(app_id):
        restarted.append(app_id)
        return {"status": "running"}

    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(): (net.plan(carlos(), [], set()), "wizardapps"))
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
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(): (net.plan(apps2, [], set()), "wizardapps"))
    res3 = net.reconcile(c, restart_fn=restart)
    assert not restarted and any("wizard-link-calendly-connector--resend-connector" in x for x in res3["connected"])
    # and dropped again: disconnected and removed
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(): (net.plan(carlos(), [], set()), "wizardapps"))
    res3b = net.reconcile(c, restart_fn=restart)
    assert "wizard-link-calendly-connector--resend-connector" in res3b["removed"]
    # rollback: isolation off -> overrides stripped, apps back on the shared network, wam networks removed
    monkeypatch.setattr(net, "build", lambda c_, rt=None, extra=(): (net.plan(carlos(), [], set(), on=False), "wizardapps"))
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
    monkeypatch.setattr(cli, "network_step", lambda c, rt=None, extra=(), restart=True: steps.append((set(extra), restart)) or {})
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
