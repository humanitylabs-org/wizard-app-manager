"""#997: an app the owner's agent built on this box installs with one owner tap (switch wizard/install-agent-builds).

The Store selection `{"source": "build", "build": <id>, "sha256": <hash>, "operation": "install"|"update"}` is resolved
server-side from the agents' own dev folders, snapshotted and checked; tests here use a scratch Runtipi root with a
real `wam new` app in the agent's data folder and stub only the Runtipi lifecycle calls.
"""
import json
import os
import shutil
import threading
from types import SimpleNamespace

import pytest

from wam import agent_builds as ab, app_updates as au, builder, cli, store_install as si, store_selection as ss

DEV = ab.DEVS[0]


def _dev(root, agent="hermes-agent"):
    p = root / "app-data" / "wizardapps" / agent
    for part in DEV:
        p = p / part
    return p


def _agent(root, agent="hermes-agent", wizard_type=None):
    a = root / "apps" / "wizardapps" / agent
    a.mkdir(parents=True, exist_ok=True)
    (a / "config.json").write_text(json.dumps({"id": agent, **({"wizard_type": wizard_type} if wizard_type else {})}))
    d = _dev(root, agent)
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def box(tmp_path, monkeypatch):
    root = tmp_path / "rt"
    home = tmp_path / "home"
    home.mkdir()
    dev = _agent(root)
    src = builder.new_app("galaxy-calc", dev, "plus/minus calculator", name="Galaxy Calc")
    installed = set()
    calls = []
    c = SimpleNamespace(home=home, runtipi_root=root, index="https://index.test", runtipi=lambda: None)
    monkeypatch.setattr(ab, "switch_on", lambda home=None: True)
    monkeypatch.setattr(ab, "_pullable", lambda compose: [])
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set(installed))

    def lifecycle(kind):
        def run(c_, app, **kw):
            calls.append((kind, app, kw))
            shutil.copytree(root / "repos/wizardapps/apps" / app, root / "apps/wizardapps" / app, dirs_exist_ok=True)
            installed.add(app)
            return {"status": "running"}
        return run
    monkeypatch.setattr(cli, "install", lifecycle("install"))
    monkeypatch.setattr(cli, "app_update", lifecycle("update"))
    monkeypatch.setattr(cli, "write_registry", lambda c_: None)
    monkeypatch.setattr(au, "store_update", lambda c_, app, update, **kw: update())
    return SimpleNamespace(c=c, root=root, dev=dev, src=src, installed=installed, calls=calls)


def _sel(box, operation="install", sha=None):
    return {"source": "build", "build": "galaxy-calc", "sha256": sha or builder.source_hash(box.src),
            "operation": operation}


def test_install_writes_the_snapshot_as_an_agent_build_and_runs_the_normal_install(box):
    sel = ss.resolve(box.c, "galaxy-calc", _sel(box))
    assert sel["repo"] == "local/galaxy-calc" and sel["commit"] == sel["sha256"][:40]
    res = ss.execute(box.c, "galaxy-calc", sel)
    assert res["status"] == "running"
    kind, app, kw = box.calls[0]
    assert (kind, app) == ("install", "galaxy-calc") and kw["_record"]["agent"] == "hermes-agent"
    recipe = box.root / "repos/wizardapps/apps/galaxy-calc"
    src = json.loads((recipe / ".wizard-source").read_text())
    assert src["source"] == "agent-build" and src["agent"] == "hermes-agent" and src["sha256"] == sel["sha256"]
    assert json.loads((recipe / ".wizard-selection").read_text()) == {"repo": "local/galaxy-calc",
                                                                      "commit": sel["sha256"][:40]}
    assert (recipe / "docker-compose.yml").is_file() and (recipe / "SKILL.md").is_file()
    # Remove app knows this identity (no GitHub repo): source `local`
    from wam import store_remove
    assert store_remove.identity(box.c, "galaxy-calc") == {"repo": "local/galaxy-calc",
                                                           "commit": sel["sha256"][:40], "source": "local"}


def test_a_file_changed_after_the_link_is_refused_and_nothing_is_installed(box):
    sel = ss.resolve(box.c, "galaxy-calc", _sel(box))
    (box.src / "web" / "styles.css").write_text((box.src / "web" / "styles.css").read_text() + "\n/* later */\n")
    with pytest.raises(ab.BuildError) as e:
        ab.precheck(box.c, sel)
    assert e.value.code == 409 and str(e.value) == ab.CHANGED
    with pytest.raises(ab.BuildError, match="The app changed since"):
        ss.execute(box.c, "galaxy-calc", sel)
    assert not (box.root / "repos/wizardapps/apps/galaxy-calc").exists() and not box.calls


@pytest.mark.parametrize("edit,reason", [
    (lambda svc: svc.update(network_mode="host"), "host"),
    (lambda svc: svc["volumes"].append("/etc:/host-etc"), "/etc"),
])
def test_host_access_is_refused_with_the_checks_reason(box, edit, reason):
    import yaml
    comp = box.src / "runtipi/docker-compose.yml"
    doc = yaml.safe_load(comp.read_text())
    edit(doc["services"]["galaxy-calc"])
    comp.write_text(yaml.safe_dump(doc, sort_keys=False))
    sel = ss.resolve(box.c, "galaxy-calc", _sel(box))
    with pytest.raises(ab.BuildError) as e:
        ab.precheck(box.c, sel)
    assert e.value.code == 422 and reason in str(e.value)
    with pytest.raises(ab.BuildError):
        ss.execute(box.c, "galaxy-calc", sel)
    assert not box.calls


def test_a_build_section_is_refused(box):
    import yaml
    comp = box.src / "runtipi/docker-compose.yml"
    doc = yaml.safe_load(comp.read_text())
    doc["services"]["galaxy-calc"]["build"] = "app"
    comp.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(ab.BuildError, match="build:"):
        ab.precheck(box.c, ss.resolve(box.c, "galaxy-calc", _sel(box)))


def test_the_folder_is_found_by_id_only_never_a_symlink_or_two_agents(box, tmp_path):
    for bad in ("../galaxy-calc", "galaxy-calc/..", "/etc"):
        with pytest.raises(ValueError):
            ss.resolve(box.c, "galaxy-calc", {**_sel(box), "build": bad})
    with pytest.raises(ValueError, match="differ"):
        ss.resolve(box.c, "galaxy-calc", {**_sel(box), "build": "other-app"})
    with pytest.raises(ValueError):
        ss.resolve(box.c, "galaxy-calc", {**_sel(box), "path": "/tmp/x"})
    # a symlinked build folder in an agent's dev folder is refused
    outside = tmp_path / "outside" / "evil-app"
    shutil.copytree(box.src, outside)
    os.symlink(outside, box.dev / "evil-app")
    with pytest.raises(ab.BuildError, match="link"):
        ab.find(box.c, "evil-app")
    # a symlinked dev folder (an agent pointing the App Manager elsewhere) is not searched at all
    other = box.root / "app-data/wizardapps/other-agent/data/home/.wizard-os"
    other.mkdir(parents=True)
    os.symlink(tmp_path / "outside", other / "dev")
    a = box.root / "apps/wizardapps/other-agent"
    a.mkdir(parents=True)
    (a / "config.json").write_text(json.dumps({"wizard_type": "agent"}))
    with pytest.raises(ab.BuildError) as e:
        ab.find(box.c, "evil-app")
    assert "other-agent" not in str(e.value)
    # the same id in two agents: say which
    second = _agent(box.root, "second-agent", wizard_type="agent")
    shutil.copytree(box.src, second / "galaxy-calc")
    with pytest.raises(ab.BuildError) as e:
        ab.find(box.c, "galaxy-calc")
    assert e.value.code == 409 and "hermes-agent" in str(e.value) and "second-agent" in str(e.value)
    shutil.rmtree(second / "galaxy-calc")
    # an app that is not an agent is never searched
    (a / "config.json").write_text(json.dumps({"wizard_type": "service"}))
    assert "other-agent" not in ab.agents(box.c)


def test_a_build_under_the_agents_data_home_is_found_too(box):
    # a shell whose HOME is the engine's /opt/data writes to data/.wizard-os/dev
    other = box.root / "app-data/wizardapps/hermes-agent" / ab.DEVS[1][0]
    for part in ab.DEVS[1][1:]:
        other = other / part
    other.mkdir(parents=True)
    shutil.move(str(box.src), other / "galaxy-calc")
    assert ab.find(box.c, "galaxy-calc") == ("hermes-agent", other / "galaxy-calc")
    shutil.copytree(other / "galaxy-calc", box.dev / "galaxy-calc")
    with pytest.raises(ab.BuildError, match="Two copies"):
        ab.find(box.c, "galaxy-calc")


def test_markers_make_the_dev_folder_of_a_fresh_agent(box):
    fresh = box.root / "app-data/wizardapps/fresh-agent/data/home"
    fresh.mkdir(parents=True)
    a = box.root / "apps/wizardapps/fresh-agent"
    a.mkdir(parents=True)
    (a / "config.json").write_text(json.dumps({"wizard_type": "agent"}))
    box.c.home.joinpath("links.json").write_text("{}")
    ab.sync_markers(box.c, on=True)
    assert json.loads((fresh / ".wizard-os/dev" / ab.MARKER).read_text())["on"] is True
    ab.sync_markers(box.c, on=False)
    assert not (fresh / ".wizard-os/dev" / ab.MARKER).exists()


def test_update_replaces_only_the_recipe_and_the_same_build_is_already_current(box):
    first = ss.resolve(box.c, "galaxy-calc", _sel(box))
    ss.execute(box.c, "galaxy-calc", first)
    data = box.root / "app-data/wizardapps/galaxy-calc/data"
    data.mkdir(parents=True)
    (data / "last-sum.json").write_text('{"sum": 42}')
    assert ab.current(box.c, {**first, "operation": "update"})
    assert ss.execute(box.c, "galaxy-calc", {**first, "operation": "update"})["note"] == ab.CURRENT
    styles = box.src / "web" / "styles.css"
    styles.write_text(styles.read_text() + "\n.wz-app { color: var(--wz-accent); }\n")
    second = ss.resolve(box.c, "galaxy-calc", _sel(box, "update"))
    assert second["sha256"] != first["sha256"] and not ab.current(box.c, second)
    tipi_before = json.loads((box.root / "repos/wizardapps/apps/galaxy-calc/config.json").read_text())["tipi_version"]
    res = ss.execute(box.c, "galaxy-calc", second)
    assert res["status"] == "running" and box.calls[-1][0] == "update"
    recipe = box.root / "repos/wizardapps/apps/galaxy-calc"
    assert json.loads((recipe / ".wizard-source").read_text())["sha256"] == second["sha256"]
    assert json.loads((recipe / "config.json").read_text())["tipi_version"] == tipi_before + 1   # Runtipi sees it
    assert (data / "last-sum.json").read_text() == '{"sum": 42}'   # the app's data is never touched


def test_a_failed_update_check_puts_the_old_recipe_back(box, monkeypatch):
    ss.execute(box.c, "galaxy-calc", ss.resolve(box.c, "galaxy-calc", _sel(box)))
    recipe = box.root / "repos/wizardapps/apps/galaxy-calc"
    old = json.loads((recipe / ".wizard-source").read_text())["sha256"]
    styles = box.src / "web" / "styles.css"
    styles.write_text(styles.read_text() + "\n/* v2 */\n")
    monkeypatch.setattr(au, "store_update", lambda c_, app, update, **kw: {"status": "running", "error": "kept v1"})
    res = ss.execute(box.c, "galaxy-calc", ss.resolve(box.c, "galaxy-calc", _sel(box, "update")))
    assert res["error"] == "kept v1"
    assert json.loads((recipe / ".wizard-source").read_text())["sha256"] == old


def test_an_update_never_replaces_a_store_app(box):
    ss.execute(box.c, "galaxy-calc", ss.resolve(box.c, "galaxy-calc", _sel(box)))
    live = box.root / "apps/wizardapps/galaxy-calc/.wizard-source"
    live.write_text(json.dumps({"repo": "org/galaxy-calc", "source": "standard"}))
    with pytest.raises(ValueError, match="not from your agent"):
        ss.execute(box.c, "galaxy-calc", ss.resolve(box.c, "galaxy-calc", _sel(box, "update")))


def test_the_sheet_read_names_the_app_and_its_installed_state(box):
    got = ab.describe(box.c, "galaxy-calc")
    assert {k: got[k] for k in ("id", "name", "check", "installed", "agent")} == {
        "id": "galaxy-calc", "name": "Galaxy Calc", "check": "ok", "installed": False, "agent": "hermes-agent"}
    assert got["sha256"] == builder.source_hash(box.src) and got["summary"] and got["version"]
    ss.execute(box.c, "galaxy-calc", ss.resolve(box.c, "galaxy-calc", _sel(box)))
    assert ab.describe(box.c, "galaxy-calc")["installed"] == {"sha256": got["sha256"], "version": got["version"]}


def test_app_updates_skip_agent_builds_as_built_on_this_wizard(box):
    ss.execute(box.c, "galaxy-calc", ss.resolve(box.c, "galaxy-calc", _sel(box)))
    rows = [{"app": {"appName": "galaxy-calc", "version": 1, "status": "running", "appStoreSlug": "wizardapps"},
             "metadata": {"latestVersion": 1}}]
    got = au.candidates(rows, store="wizardapps", runtipi_root=box.root, home=box.c.home, release_managed=set())
    assert [(r["id"], r["skip"], r.get("note")) for r in got] == [("galaxy-calc", "built-here", "Built on this Wizard")]


# --- the Store route --------------------------------------------------------------------------------------------

def _serve(box, monkeypatch):
    from http.server import ThreadingHTTPServer
    (box.c.home / "links.json").write_text(json.dumps({"host": "box.example.ts.net"}))
    monkeypatch.setattr(cli.alongside, "load", lambda home: {})
    monkeypatch.setattr(si, "peer_uid", lambda port: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "owner@test")
    monkeypatch.setattr(si, "own_ips", lambda: {"192.0.2.1"})
    monkeypatch.setattr(si, "served_origins", lambda host: {"https://box.example.ts.net:8444"})
    monkeypatch.setattr(cli.StoreHandler, "cfg", box.c, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.StoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


HEAD = {"X-Forwarded-For": "192.0.2.9", "Tailscale-User-Login": "owner@test", "Origin": "https://box.example.ts.net:8444",
        "X-Wizard-Store": "1", "Content-Type": "application/json"}


def _call(srv, method, path, body=None, headers=None):
    import http.client
    h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    data = json.dumps(body).encode() if body is not None else None
    h.request(method, path, data, {**HEAD, **(headers or {}), **({"Content-Length": str(len(data))} if data else {})})
    r = h.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def _wait(srv, app):
    import time
    for _ in range(100):
        code, job = _call(srv, "GET", f"/{app}")
        if job.get("state") != "installing":
            return job
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_route_installs_refuses_changed_links_and_answers_already_current(box, monkeypatch):
    srv = _serve(box, monkeypatch)
    si._jobs.pop("galaxy-calc", None)
    try:
        code, got = _call(srv, "GET", "/_builds/galaxy-calc")
        assert code == 200 and got["name"] == "Galaxy Calc" and got["installed"] is False
        code, job = _call(srv, "POST", "/galaxy-calc", _sel(box))
        assert code == 202 and job["state"] == "installing"
        assert _wait(srv, "galaxy-calc")["state"] == "installed"
        code, got = _call(srv, "POST", "/galaxy-calc", _sel(box, "update"))
        assert (code, got["note"]) == (200, "already current")
        (box.src / "web" / "styles.css").write_text("/* a colour change: a new link */\n")
        stale = _sel(box, "update")
        (box.src / "web" / "styles.css").write_text("/* changed after that link */\n")
        code, got = _call(srv, "POST", "/galaxy-calc", stale)
        assert code == 409 and got["error"] == ab.CHANGED
        assert [k for k, *_ in box.calls] == ["install"]
    finally:
        srv.shutdown()
        si._jobs.pop("galaxy-calc", None)


def test_route_refuses_a_request_from_the_box_itself(box, monkeypatch):
    srv = _serve(box, monkeypatch)
    try:
        for hop in ("192.0.2.1", "127.0.0.1"):   # an app container or the agent reaches Serve from the box
            code, got = _call(srv, "POST", "/galaxy-calc", _sel(box), {"X-Forwarded-For": hop})
            assert code == 403 and got["error"] == si.FROM_BOX
        assert not box.calls
    finally:
        srv.shutdown()


def test_switch_off_changes_nothing(box, monkeypatch):
    srv = _serve(box, monkeypatch)
    monkeypatch.setattr(ab, "switch_on", lambda home=None: False)
    monkeypatch.setattr(ss, "execute", lambda *a: pytest.fail("never runs with the switch off"))
    try:
        code, got = _call(srv, "POST", "/galaxy-calc", _sel(box))
        assert (code, got["error"]) == (400, "exact source, repo, commit and operation required; refresh the Store")
        code, got = _call(srv, "GET", "/_builds/galaxy-calc")
        assert code == 404 and "name" not in got
        with pytest.raises(ValueError, match="exact source, repo, commit and operation"):
            ss.resolve(box.c, "galaxy-calc", _sel(box))
    finally:
        srv.shutdown()
    assert not (box.root / "repos").exists()


# --- the agent's side: wam build-link ----------------------------------------------------------------------------

def test_build_link_prints_the_install_link_only_while_the_box_says_links_work(box, monkeypatch):
    os_home = box.dev.parent
    off = builder.build_link(box.src, os_home, env={})
    assert off["state"] == "install_links_off" and off["do"] == builder.OFF_STEP and "link" not in off and "say" not in off
    # the box writes the marker while the switch is on (wam serve's watch), with the shell's origin
    monkeypatch.setattr(ab, "shell_origin", lambda c: "https://box.example.ts.net:8444")
    assert ab.sync_markers(box.c, on=True) == [str(box.dev)]
    on = builder.build_link(box.src, os_home, env={})
    sha = builder.source_hash(box.src)
    assert on["link"] == f"https://box.example.ts.net:8444/wizard-os-home/?view=install&build=galaxy-calc&sha256={sha}"
    assert on["say"].endswith("Tap Install to add it to your Apps.")
    assert builder.build_link(box.src, os_home, env={"WIZARD_SHELL_ORIGIN": "https://other.example.ts.net"})[
        "link"].startswith("https://other.example.ts.net/wizard-os-home/?view=install&build=galaxy-calc&sha256=")
    # the link's selection installs exactly that build
    ss.execute(box.c, "galaxy-calc", ss.resolve(box.c, "galaxy-calc", _sel(box, sha=sha)))
    assert ab.sync_markers(box.c, on=False) == [str(box.dev)]
    assert builder.build_link(box.src, os_home, env={})["state"] == "install_links_off"


def test_build_link_without_a_known_origin_is_path_only(box, monkeypatch):
    monkeypatch.setattr(ab, "shell_origin", lambda c: "")
    ab.sync_markers(box.c, on=True)
    assert builder.build_link(box.src, box.dev.parent, env={})["link"].startswith(
        "/wizard-os-home/?view=install&build=galaxy-calc&sha256=")


def test_build_link_refuses_a_source_the_box_cannot_find(box, tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "shell_origin", lambda c: "")
    ab.sync_markers(box.c, on=True)
    elsewhere = tmp_path / "elsewhere" / "galaxy-calc"
    shutil.copytree(box.src, elsewhere)
    with pytest.raises(ValueError, match="finds builds only in"):
        builder.build_link(elsewhere, box.dev.parent, env={})


def test_feature_switch_read_says_the_box_supports_install_links(monkeypatch, tmp_path):
    from wam import feature_switches as fs

    class Pages:
        def get(self):
            return {"version": 1, "tweaks": {}}, 1
    monkeypatch.setattr(fs, "_channel", lambda home: "latest")
    monkeypatch.setattr(fs, "catalog", lambda home, fetch: [])
    assert fs.listing(tmp_path, pages=Pages(), app_rows=([], []))["install_agent_builds"] is True


def test_gateway_listener_allows_only_the_read(monkeypatch):
    assert si.gateway_path_ok("_builds/galaxy-calc") and not si.gateway_path_ok("_builds/../x")
    assert not si.gateway_path_ok("_builds/galaxy-calc/x")
