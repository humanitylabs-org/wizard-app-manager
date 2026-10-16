import json, os, shutil, subprocess, sys, threading, time, urllib.error, urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wam import cli, index, registry, wizardmd  # noqa: E402
from wam.runtipi import cli_token  # noqa: E402

TEMPLATE = ROOT / "templates" / "hello-wizard"


def test_template_passes_checker():
    res = wizardmd.check(TEMPLATE)
    assert wizardmd.passed(res), wizardmd.failures(res)


@pytest.mark.parametrize("snippet", [
    "    privileged: true\n",
    "    network_mode: host\n",
    "    volumes: ['/var/run/docker.sock:/var/run/docker.sock']\n",
    "    cap_add: [SYS_ADMIN]\n",
])
def test_host_level_container_access_fails(tmp_path, snippet):
    bad = tmp_path / "bad"
    shutil.copytree(TEMPLATE, bad)
    c = bad / "runtipi" / "docker-compose.yml"
    c.write_text(c.read_text().replace("    read_only: true\n", "    read_only: true\n" + snippet, 1))
    assert "container asks for no host-level access" in wizardmd.failures(wizardmd.check(bad))


def test_index_resolve_by_id_and_repo(monkeypatch):
    rec = {"repo": "a/hello", "id": "hello-wizard", "install": {"git": "https://github.com/a/hello", "commit": "f" * 40}}
    calls = []

    def fake_get(url):
        calls.append(url)
        return {"apps": [rec]} if "/v1/search" in url else {**rec, "listed": True}
    monkeypatch.setattr(index, "_get", fake_get)
    assert index.resolve("https://i", "hello-wizard")["repo"] == "a/hello"
    assert "id=hello-wizard" in calls[-1]
    assert index.resolve("https://i", "a/hello")["install"]["commit"] == "f" * 40
    monkeypatch.setattr(index, "_get", lambda url: {"apps": []})
    with pytest.raises(index.IndexError_):
        index.resolve("https://i", "nope")


def _fake_store_app(rt_root: Path, app_id: str = "hello-wizard"):
    """What the App Manager's index store writes for one app: repos/wizardapps/apps/<id>/..."""
    d = rt_root / "repos" / "wizardapps" / "apps" / app_id
    (d / "metadata").mkdir(parents=True)
    shutil.copy2(TEMPLATE / "wizard.md", d / "wizard.md")
    shutil.copy2(TEMPLATE / "runtipi" / "config.json", d / "config.json")
    shutil.copy2(TEMPLATE / "skill" / "SKILL.md", d / "SKILL.md")
    (d / ".wizard-commit").write_text("a" * 40 + "\n")
    (d / ".wizard-source").write_text(json.dumps({"repo": "x/hello-wizard"}))
    return d


def test_registry_reads_app_manager_files(tmp_path):
    rt = tmp_path / "runtipi"
    _fake_store_app(rt)
    installed = [{"app": {"status": "running", "port": 8799}, "info": {"urn": "hello-wizard:wizardapps"}},
                 {"app": {"status": "running", "port": 80}, "info": {"urn": "nextcloud:migrated"}}]
    reg = registry.build(installed, "wizardapps", "127.0.0.1", rt)
    assert [a["id"] for a in reg["apps"]] == ["hello-wizard"]
    a = reg["apps"][0]
    assert a["mcp"]["url"] == "http://127.0.0.1:8799/mcp"
    assert a["commit"] == "a" * 40 and a["repo"] == "x/hello-wizard"
    assert reg["slots"]["diagnostics"]["default"] == "hello-wizard"
    p = registry.write(reg, tmp_path / "home")
    data = json.loads(p.read_text())
    assert data["apps"][0]["skill"] == "skills/hello-wizard/SKILL.md"
    assert (tmp_path / "home" / "skills" / "hello-wizard" / "SKILL.md").is_file()
    assert "_skill_src" not in data["apps"][0]


def test_views_are_same_origin_routes_and_staged_installs_never_shadow_owner_tiles(tmp_path):
    rt = tmp_path / "runtipi"
    d = _fake_store_app(rt)
    wm = d / "wizard.md"
    wm.write_text(wm.read_text().replace("\n---\n", "\nview:\n  path: /\n  port: 8799\n---\n", 1))
    installed = [{"app": {"status": "running", "port": 8799}, "info": {"urn": "hello-wizard:wizardapps"}}]
    reg = registry.build(installed, "wizardapps", "127.0.0.1", rt)
    home = tmp_path / "home"
    home.mkdir()
    # This owner opted into same-origin routing; new generic installs without this file keep normal URLs.
    (home / "routes.json").write_text("{}")
    a = json.loads(registry.write(reg, home).read_text())["apps"][0]
    assert a["view"] is None   # no owner route yet: never a raw host URL
    (home / "routes.json").write_text(json.dumps({"hello-wizard": {"view": "/hello-next/", "staged_as": "hello-next"},
                                                   "evil": {"view": "http://1.2.3.4/", "staged_as": "Bad Id"}}))
    a = json.loads(registry.write(reg, home).read_text())["apps"][0]
    assert a["view"] == {"url": "/hello-next/"} and a["id"] == "hello-next" and a["app_id"] == "hello-wizard"
    assert a["staged"] is True and a["skill"] == "skills/hello-wizard/SKILL.md"
    assert (home / "skills" / "hello-wizard" / "SKILL.md").is_file()
    assert registry.routes(home)["evil"] == {"view": None, "staged_as": None}


def test_build_form_generates_required_passwords(tmp_path, monkeypatch):
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "home"))
    c = cli.Config()
    info = {"form_fields": [
        {"env_variable": "PW", "type": "password", "required": True, "label": "pw"},
        {"env_variable": "R", "type": "random", "required": True, "label": "r"},
        {"env_variable": "D", "type": "text", "required": True, "default": "x", "label": "d"},
        {"env_variable": "N", "type": "text", "required": True, "label": "name", "hint": "h"},
    ]}
    form, missing = c and cli.build_form(c, "app1", info, {"N": "given"})
    assert form["openPort"] is False and form["N"] == "given" and len(form["PW"]) >= 24
    assert "R" not in form and "D" not in form and missing == []
    saved = c.secrets / "app1.json"
    assert json.loads(saved.read_text())["PW"] == form["PW"] and (saved.stat().st_mode & 0o077) == 0
    _, missing = cli.build_form(c, "app2", info, {})
    assert missing and missing[0].startswith("N ")


def test_short_ids(tmp_path):
    (tmp_path / "apps" / "wizardapps" / "transcriber").mkdir(parents=True)
    assert cli._short_ids(tmp_path, ["app", "update", "transcriber"]) == ["app", "update", "transcriber:wizardapps"]
    assert cli._short_ids(tmp_path, ["app", "update", "x:migrated"]) == ["app", "update", "x:migrated"]
    assert cli._short_ids(tmp_path, ["installed"]) == ["installed"]


def test_cli_token_is_valid_hs256():
    jwt = pytest.importorskip("jwt")
    assert jwt.decode(cli_token("s3cret"), "s3cret", algorithms=["HS256"])["sub"] == "cli"


def test_api_requires_token_for_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "home"))
    c = cli.Config()
    cli.Handler.cfg, cli.Handler.token = c, c.api_token()
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 18732), cli.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert json.load(urllib.request.urlopen("http://127.0.0.1:18732/healthz"))["ok"]
        for method, path in (("POST", "/api/install"), ("DELETE", "/api/apps/x")):
            req = urllib.request.Request(f"http://127.0.0.1:18732{path}", b"{}" if method == "POST" else None, method=method)
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            assert e.value.code == 401
        assert (c.token_file.stat().st_mode & 0o077) == 0 and c.bind == "127.0.0.1"
    finally:
        srv.shutdown()


def test_template_mcp_server_speaks_mcp():
    import subprocess
    src = (TEMPLATE / "app" / "server.py").read_text()
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-c", src], env={"PORT": str(port), "PATH": "/usr/bin:/bin"})
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1); break
            except Exception:
                time.sleep(0.1)
        assert proc.poll() is None, "The owned fixture server did not start"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode(),
                                     {"Content-Type": "application/json"})
        names = [t["name"] for t in json.load(urllib.request.urlopen(req, timeout=5))["result"]["tools"]]
        assert names == ["wizard_time", "wizard_echo", "habits_list", "habits_add", "habits_set_done"]
    finally:
        proc.terminate()


def test_parking_spots_fill_block_and_bootstrap():
    from wam import slots
    defaults = {"shell": {"app": None, "required": True}, "agent": {"app": "hermes-agent", "required": True},
                "files": {"app": "files", "required": True}, "browser": {"app": "browser", "required": False}}
    reg = {"apps": [{"id": "hermes-agent", "provides": ["agent"]}, {"id": "my-files", "provides": ["files"]}],
           "slots": {"agent": {"default": "hermes-agent"}}}
    t = {r["slot"]: r for r in slots.table(reg, defaults)}
    assert t["agent"]["active"] == "hermes-agent" and t["agent"]["state"] == "filled"
    assert t["files"]["active"] == "my-files"           # an alternative fills the spot; the default isn't forced
    assert t["shell"]["state"] == "no default yet"
    assert slots.to_install(reg, defaults) == ["browser"]
    assert slots.blocks_uninstall(reg, "hermes-agent", defaults) == ["agent"]
    assert slots.blocks_uninstall(reg, "browser", defaults) == []
    assert [p["slot"] for p in slots.merge_into_registry(dict(reg), defaults)["parking"]] == list(defaults)


def test_shipped_defaults_are_valid():
    from wam import slots
    d = slots.load_defaults()
    assert {"shell", "agent", "files"} <= set(d) and all(d[k]["required"] for k in ("shell", "agent", "files"))


def test_registry_is_shared_with_containers(tmp_path):
    c = cli.Config.__new__(cli.Config)
    c.home, c.runtipi_root = tmp_path / "home", tmp_path / "runtipi"
    (c.home / "skills" / "x").mkdir(parents=True)
    (c.home / "apps.json").write_text('{"schema": 2, "apps": []}')
    (c.home / "skills" / "x" / "SKILL.md").write_text("hi")
    (c.home / "api-token").write_text("secret")
    assert cli.share_registry(c)
    shared = c.runtipi_root / "app-data" / "wizard-os"
    assert json.loads((shared / "apps.json").read_text())["schema"] == 2
    assert (shared / "skills" / "x" / "SKILL.md").read_text() == "hi"
    assert not (shared / "api-token").exists()


def test_private_images_and_shared_secret(tmp_path):
    from wam import private, slots
    comp = {"services": {"a": {"image": "${WIZARD_SHELL_IMAGE:-ghcr.io/o/shell:0.1.0}"}, "b": {"image": "redis:7"}}}
    assert private.images(comp) == ["ghcr.io/o/shell:0.1.0", "redis:7"]
    assert slots.repo_for("wizard-shell") == "humanitylabs-org/wizard-agent-service"
    assert slots.repo_for("hermes-agent") is None
    c = cli.Config.__new__(cli.Config)
    c.runtipi_root = tmp_path
    env = tmp_path / "app-data" / "wizardapps" / "hermes-agent"
    env.mkdir(parents=True)
    (env / "app.env").write_text("HERMES_API_KEY=k1\n")
    assert cli.shared_value(c, "WIZARD_HERMES_API_KEY", ["hermes-agent"]) == "k1"
    assert cli.shared_value(c, "OTHER", ["hermes-agent"]) is None
    d = {"shell": {"app": "s", "required": True}, "agent-service": {"app": "s", "required": True}}
    t = {r["slot"]: r for r in slots.table({"apps": [{"id": "s", "provides": ["shell"]}]}, d)}
    assert t["agent-service"]["state"] == "filled" and t["shell"]["state"] == "filled"


def test_skills_survive_second_write(tmp_path):
    """write_registry writes apps.json twice (the second adds parking); app skills must survive both passes."""
    rt = tmp_path / "rt"
    _fake_store_app(rt, "hello-wizard")
    installed = [{"app": {"status": "running", "port": 8799}, "info": {"urn": "hello-wizard:wizardapps"}}]
    reg = registry.build(installed, "wizardapps", "127.0.0.1", rt)
    home = tmp_path / "home"
    registry.write(reg, home)
    registry.write(reg, home)
    assert (home / "skills" / "hello-wizard" / "SKILL.md").is_file()
    assert "_skill_src" not in (home / "apps.json").read_text()


class _FakeStores:
    """Just enough of Runtipi's /marketplace API: the slug comes from the name, a duplicate URL is a 409."""
    def __init__(self, stores):
        self.s = [dict(x) for x in stores]
        self.calls = []

    def stores(self):
        return [dict(x) for x in self.s]

    def req(self, method, path, body=None, **_):
        self.calls.append((method, path))
        if method == "POST" and path == "/marketplace/create":
            if any(x["url"].rstrip("/") == body["url"].rstrip("/") for x in self.s):
                raise RuntimeError("POST /marketplace/create -> 409 SERVER_ERROR_APP_STORE_ALREADY_EXISTS")
            self.s.append({"slug": body["name"].lower().replace(" ", "-"), "name": body["name"], "url": body["url"], "enabled": True})
        elif method == "PATCH":
            slug = path.rsplit("/", 1)[1]
            for x in self.s:
                if x["slug"] == slug:
                    x.update(name=body["name"], enabled=body["enabled"])
        elif method == "DELETE":
            slug = path.rsplit("/", 1)[1]
            self.s = [x for x in self.s if x["slug"] != slug]
        return {}


class _Cfg:
    index = "https://apps.aiwizards.com"


@pytest.mark.parametrize("before", [
    [],                                                                                    # fresh box
    [{"slug": "wizard-apps", "name": "Wizard Apps", "url": "https://apps.aiwizards.com", "enabled": True}],  # earlier wam left a stray copy
])
def test_ensure_store_lands_on_the_wizardapps_slug(before):
    rt = _FakeStores([{"slug": "migrated", "name": "migrated", "url": "https://github.com/runtipi/runtipi-appstore", "enabled": True}] + before)
    cli.ensure_store(_Cfg(), rt)
    by_slug = {x["slug"]: x for x in rt.s}
    assert by_slug[cli.STORE]["enabled"] and by_slug[cli.STORE]["name"] == cli.STORE_NAME
    assert "wizard-apps" not in by_slug and "migrated" in by_slug
    cli.ensure_store(_Cfg(), rt)                                                          # second run: nothing to do
    assert rt.calls[-1][0] != "POST"


def test_alongside_external_agent_slot_and_shell_override(tmp_path, monkeypatch):
    """Alongside mode: the host's Hermes fills the agent spot (bootstrap skips hermes-agent); the shell gets a
    host-network override pointed at it, the gateway token in a 0600 env file, and by default no Hermes home: only
    the agent's images folder, and the recipe's state.db path blanked."""
    from wam import alongside, slots
    hh = tmp_path / "hermes"; hh.mkdir(); (hh / "cache" / "images").mkdir(parents=True)
    (hh / "config.yaml").write_text("platforms:\n  api_server:\n    extra:\n      port: 8650\n")
    (hh / ".env").write_text("API_SERVER_KEY=host-key-not-real\n")
    wh = tmp_path / "wos"; wh.mkdir()
    monkeypatch.setattr(alongside, "hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(alongside, "api_up", lambda origin, timeout=3.0: False)
    st = alongside.enable(wh, hh, 18796)
    assert st["agent"]["origin"] == "http://127.0.0.1:8650" and st["shell"]["port"] == 18796
    assert "host-key-not-real" not in json.dumps(st)
    assert alongside.shared_secret(wh, "WIZARD_HERMES_API_KEY") == "host-key-not-real"
    reg = {"apps": [], "external": alongside.external_slots(wh)}
    d = {"agent": {"app": "hermes-agent", "required": True}, "shell": {"app": "wizard-shell", "required": True}}
    rows = {r["slot"]: r for r in slots.table(reg, d)}
    assert rows["agent"]["state"] == "external" and rows["agent"]["external"] == "external: host Hermes"
    assert slots.to_install(reg, d) == ["wizard-shell"]
    p = alongside.write_shell_override(tmp_path / "rt", "wizardapps", "wizard-shell", wh)
    text = p.read_text()
    assert "network_mode: host" in text and "HERMES_ORIGIN: http://127.0.0.1:8650" in text
    assert f"{hh}:/hermes:ro" not in text and f"{hh}/cache/images:/hermes/cache/images:ro" in text
    # /hermes is wam's own folder with the mount point already in it (Docker can't mkdir inside a read-only /hermes)
    view = wh / "hermes-view"
    assert f"{view}:/hermes:ro" in text and (view / "cache" / "images").is_dir()
    assert sorted(x.name for x in view.rglob("*")) == ["cache", "images"]      # nothing else: no secrets
    vols = yaml.safe_load(text.replace("!reset", ""))["services"]["wizard-shell"]["volumes"]
    assert [v.split(":")[1] for v in vols][:2] == ["/hermes", "/hermes/cache/images"]   # parent before child
    assert 'WIZARD_HERMES_STATE_PATH: ""' in text
    yaml.safe_load(text.replace("!reset", ""))   # valid compose YAML (Runtipi's loader knows !reset)
    assert 'WIZARD_PORT: "18796"' in text and "host-key-not-real" not in text
    # attachments: the host Hermes is handed the host path of the user-files folder, not the container's /media
    env = yaml.safe_load(text.replace("!reset", ""))["services"]["wizard-shell"]["environment"]
    assert env["WIZARD_MEDIA_AGENT_ROOT"] == str(tmp_path / "rt" / "media")
    envp = p.parent / "app.env"
    assert (envp.stat().st_mode & 0o777) == 0o600 and envp.read_text().startswith("WIZARD_MCP_TOKEN=")
    assert alongside.disable(wh) and alongside.external_slots(wh) == {}
    assert alongside.remove_shell_override(tmp_path / "rt", "wizardapps", "wizard-shell")


def test_alongside_service_sees_agent_outputs_and_transcriber(tmp_path, monkeypatch):
    """Pictures and voice replies: the host Hermes reports MEDIA:<host path> under its cache folders, so the Agent
    Service mounts each read-only at that same path and trusts them as artifact roots. Voice: on the host network
    the Transcriber is reached on its loopback port, not by its compose name. Rootless Docker runs it as uid 0
    (the owner) with gid 1000, so it can read the owner's 0700 folders and still write the shared /media."""
    from wam import alongside
    hh = tmp_path / "hermes"; hh.mkdir()
    (hh / "config.yaml").write_text("platforms:\n  api_server:\n    extra:\n      port: 8650\n")
    wh = tmp_path / "wos"; wh.mkdir()
    rt = tmp_path / "rt"; (rt / "app-data" / "wizardapps" / "transcriber").mkdir(parents=True)
    (rt / "app-data" / "wizardapps" / "transcriber" / "app.env").write_text("APP_PORT=8791\n")
    monkeypatch.setattr(alongside, "hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(alongside, "api_up", lambda origin, timeout=3.0: False)
    monkeypatch.setattr(alongside, "docker_rootless", lambda: True)
    alongside.enable(wh, hh, 18796)
    p = alongside.write_shell_override(rt, "wizardapps", "wizard-agent-service", wh, "service")
    svc = yaml.safe_load(p.read_text().replace("!reset", ""))["services"]["wizard-agent-service"]
    env = svc["environment"]
    assert env["WIZARD_STT_ORIGIN"] == "http://127.0.0.1:8791"
    roots = env["WIZARD_AGENT_ARTIFACT_ROOTS"].split(":")
    for rel in alongside.AGENT_OUTPUT_DIRS:
        d = str(hh / rel)
        assert d in roots and f"{d}:{d}:ro" in svc["volumes"] and Path(d).is_dir()
    assert roots[-1] == "/media" and svc["user"] == "0:1000"
    monkeypatch.setattr(alongside, "docker_rootless", lambda: False)
    p = alongside.write_shell_override(rt, "wizardapps", "wizard-agent-service", wh, "service")
    assert yaml.safe_load(p.read_text().replace("!reset", ""))["services"]["wizard-agent-service"]["user"] == f"{os.getuid()}:{os.getgid()}"
    # no Transcriber installed: the recipe's default stays, nothing invented
    (rt / "app-data" / "wizardapps" / "transcriber" / "app.env").unlink()
    p = alongside.write_shell_override(rt, "wizardapps", "wizard-agent-service", wh, "service")
    assert "WIZARD_STT_ORIGIN" not in p.read_text()
    # the UX-only shell still mounts nothing and gets no agent paths
    p = alongside.write_shell_override(rt, "wizardapps", "wizard-ux", wh, "shell")
    assert "volumes" not in p.read_text() and "ARTIFACT" not in p.read_text()


def test_alongside_adopts_existing_service_state_on_rootless(tmp_path, monkeypatch):
    """An Agent Service that already ran as the recipe's uid 1000 crash-loops once it runs as the owner (rootless
    uid 0) because it can't chmod its own old files. adopt_state hands the folder over once, with the app's own
    image; rootful Docker and an app with no saved state are left alone."""
    from wam import alongside
    import subprocess
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    rt = tmp_path / "rt"
    assert alongside.adopt_state(rt, "wizardapps", "wizard-agent-service", "img:1", rootless=True) is False  # no state yet
    (rt / "app-data" / "wizardapps" / "wizard-agent-service" / "data").mkdir(parents=True)
    assert alongside.adopt_state(rt, "wizardapps", "wizard-agent-service", "img:1", rootless=False) is False
    assert calls == []
    assert alongside.adopt_state(rt, "wizardapps", "wizard-agent-service", "img:1", rootless=True) is True
    cmd = calls[-1]
    assert cmd[:2] == ["docker", "run"] and "--network" in cmd and "none" in cmd and "img:1" in cmd
    assert f"{rt}/app-data/wizardapps/wizard-agent-service/data:/state" in cmd
    assert "chown -R 0:1000 /state" in cmd[-1]
    assert alongside.adopt_state(rt, "wizardapps", "wizard-agent-service", "bad image;rm", rootless=True) is False


def test_alongside_split_service_and_shell(tmp_path, monkeypatch):
    """DECISIONS #36 split: the Agent Service gets the host-Hermes override on its own loopback port (with the
    gateway token); the shell (wizard-ux) gets a UX-only override pointed at the service, no token, no mounts.
    The pre-split wizard-shell bundle keeps its old override, so existing boxes keep working."""
    from wam import alongside
    hh = tmp_path / "hermes"; hh.mkdir()
    (hh / "config.yaml").write_text("platforms:\n  api_server:\n    extra:\n      port: 8650\n")
    wh = tmp_path / "wos"; wh.mkdir()
    monkeypatch.setattr(alongside, "hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(alongside, "api_up", lambda origin, timeout=3.0: False)
    monkeypatch.setattr(alongside, "port_free", lambda p: p != 8797)       # something already holds 8797
    monkeypatch.setenv("WIZARD_AGENT_SERVICE_IMAGE", "wizard-agent-service:test")
    monkeypatch.setenv("WIZARD_UX_IMAGE", "wizard-ux:test")
    st = alongside.enable(wh, hh, 18796)
    assert st["service"] == {"port": 8798, "active": False}
    assert st["images"] == {"wizard-agent-service": "wizard-agent-service:test", "wizard-ux": "wizard-ux:test"}
    monkeypatch.delenv("WIZARD_AGENT_SERVICE_IMAGE"); monkeypatch.delenv("WIZARD_UX_IMAGE")
    assert alongside.enable(wh, hh)["service"]["port"] == 8798                 # a re-run keeps the ports and pins
    assert alongside.load(wh)["images"]["wizard-agent-service"] == "wizard-agent-service:test"
    alongside.set_service_active(wh, True)
    assert alongside.enable(wh, hh)["service"]["active"] is True
    assert alongside.role_of("wizard-agent-service", ["agent-service"]) == "service"
    assert alongside.role_of("wizard-ux", ["shell"]) == "shell"
    assert alongside.role_of("wizard-shell", ["shell"]) == "bundle"
    assert alongside.role_of("files", ["files"]) is None
    assert cli.alongside_role({}, "wizard-agent-service") == "service"        # slot defaults count before apps.json
    assert cli.alongside_role({}, "wizard-ux") == "shell"
    rt = tmp_path / "rt"
    sp = alongside.write_shell_override(rt, "wizardapps", "wizard-agent-service", wh, "service")
    s = sp.read_text(); sy = yaml.safe_load(s.replace("!reset", ""))["services"]["wizard-agent-service"]
    assert sy["environment"]["HERMES_ORIGIN"] == "http://127.0.0.1:8650" and sy["environment"]["WIZARD_PORT"] == "8798"
    assert sy["image"] == "wizard-agent-service:test" and "8798/wizard/health" in s
    assert sy["environment"]["WIZARD_MEDIA_AGENT_ROOT"] == str((rt / "media").absolute())   # host path of /media
    assert (sp.parent / "app.env").read_text().startswith("WIZARD_MCP_TOKEN=")
    up = alongside.write_shell_override(rt, "wizardapps", "wizard-ux", wh, "shell")
    uy = yaml.safe_load(up.read_text().replace("!reset", ""))["services"]["wizard-ux"]
    assert uy["environment"] == {"WIZARD_SHELL_LISTEN": "127.0.0.1:18796",
                                 "WIZARD_AGENT_SERVICE_ORIGIN": "http://127.0.0.1:8798"}
    assert uy["image"] == "wizard-ux:test" and uy["network_mode"] == "host"
    assert "volumes" not in uy and "HERMES" not in up.read_text() and not (up.parent / "app.env").exists()
    bp = alongside.write_shell_override(rt, "wizardapps", "wizard-shell", wh)  # legacy bundle: unchanged shape
    by = yaml.safe_load(bp.read_text().replace("!reset", ""))["services"]["wizard-shell"]
    assert by["environment"]["WIZARD_PORT"] == "18796" and by["environment"]["HERMES_ORIGIN"] == "http://127.0.0.1:8650"


def test_split_carries_bundle_state_once_and_keeps_the_bundle(tmp_path):
    """Upgrading a box from the wizard-shell bundle: the Agent Service starts from a copy of the bundle's state."""
    bundle = tmp_path / "app-data" / cli.STORE / "wizard-shell" / "data"
    (bundle / "wizard-os").mkdir(parents=True)
    (bundle / "wizard-os" / "thread-dispositions.json").write_text('{"s1":"done"}')
    assert cli.carry_bundle_state(tmp_path, "wizard-agent-service") == bundle
    dst = tmp_path / "app-data" / cli.STORE / "wizard-agent-service" / "data" / "wizard-os" / "thread-dispositions.json"
    assert dst.read_text() == '{"s1":"done"}' and (bundle / "wizard-os" / "thread-dispositions.json").is_file()
    dst.write_text('{"s1":"later"}')
    assert cli.carry_bundle_state(tmp_path, "wizard-agent-service") is None     # never over existing service state
    assert dst.read_text() == '{"s1":"later"}'
    assert cli.carry_bundle_state(tmp_path / "fresh", "wizard-agent-service") is None


def test_bundle_box_fills_both_spots_and_install_refuses_a_second_service():
    """A box still on the wizard-shell bundle: bootstrap installs no Agent Service beside it, and installing one
    by hand is refused (two Agent Services would both write to the same Hermes)."""
    from wam import slots
    reg = {"apps": [{"id": "wizard-shell", "provides": ["shell"]}]}
    rows = {r["slot"]: r for r in slots.table(reg)}
    assert rows["shell"]["state"] == rows["agent-service"]["state"] == "filled"
    assert "wizard-agent-service" not in slots.to_install(reg) and "wizard-ux" not in slots.to_install(reg)
    assert set(slots.blocks_uninstall(reg, "wizard-shell")) >= {"shell", "agent-service"}


def test_alongside_pins_are_per_app_and_old_pin_stays_with_the_bundle(tmp_path, monkeypatch):
    from wam import alongside
    hh = tmp_path / "hermes"; hh.mkdir(); (hh / "config.yaml").write_text("{}\n")
    wh = tmp_path / "wos"; wh.mkdir()
    monkeypatch.setattr(alongside, "hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(alongside, "api_up", lambda origin, timeout=3.0: False)
    for v in alongside.PIN_VARS.values():
        monkeypatch.delenv(v, raising=False)
    st = alongside.enable(wh, hh, 18796)
    st["shell"]["image"] = "ghcr.io/x/wizard-shell@sha256:" + "a" * 64      # a pre-split alongside.json
    st.pop("images", None); alongside.save(wh, {k: v for k, v in st.items() if k != "detected"})
    monkeypatch.setenv("WIZARD_UX_IMAGE", "wizard-ux:test")
    st = alongside.enable(wh, hh)
    assert st["images"] == {"wizard-shell": "ghcr.io/x/wizard-shell@sha256:" + "a" * 64, "wizard-ux": "wizard-ux:test"}
    monkeypatch.delenv("WIZARD_UX_IMAGE")
    rt = tmp_path / "rt"
    ux = alongside.write_shell_override(rt, "wizardapps", "wizard-ux", wh, "shell").read_text()
    bundle = alongside.write_shell_override(rt, "wizardapps", "wizard-shell", wh, "bundle").read_text()
    svc = alongside.write_shell_override(rt, "wizardapps", "wizard-agent-service", wh, "service").read_text()
    assert "image: wizard-ux:test" in ux and "sha256:" not in ux
    assert "wizard-shell@sha256:" in bundle and "wizard-ux:test" not in bundle
    assert "image:" not in svc                                   # no pin: the recipe's own image


def test_carry_is_atomic_and_refuses_a_uid_it_cannot_set(tmp_path, monkeypatch):
    src = tmp_path / "app-data" / cli.STORE / "wizard-shell" / "data"
    (src / "a").mkdir(parents=True); (src / "a" / "f").write_text("1")
    dst = tmp_path / "app-data" / cli.STORE / "wizard-agent-service" / "data"
    if os.geteuid() != 0:
        with pytest.raises(SystemExit, match="runs as uid"):
            cli.carry_bundle_state(tmp_path, "wizard-agent-service", uid=os.getuid() + 1, gid=os.getgid())
        assert not dst.exists() and not list(dst.parent.glob(".data.carry-*"))
    real = shutil.copytree
    def broken(s, d, *a, **k):
        real(s, d, *a, **k); raise PermissionError("unreadable file")
    monkeypatch.setattr(cli.shutil, "copytree", broken)
    with pytest.raises(SystemExit, match="nothing was changed"):
        cli.carry_bundle_state(tmp_path, "wizard-agent-service", uid=os.getuid(), gid=os.getgid())
    assert not dst.exists() and not list(dst.parent.glob(".data.carry-*"))
    monkeypatch.setattr(cli.shutil, "copytree", real)
    assert cli.carry_bundle_state(tmp_path, "wizard-agent-service", uid=os.getuid(), gid=os.getgid()) == src
    assert (dst / "a" / "f").read_text() == "1"


class _SplitRt:
    """Fake Runtipi: per-app status that follows start/stop/uninstall; `stuck` apps refuse to stop."""
    def __init__(self, have, stuck=()):
        self.have, self.state, self.stuck = set(have), {a: "running" for a in have}, set(stuck)
    def wait_status(self, urn, want, timeout=0):
        return self.state.get(urn.split(":")[0], "missing")
    def uninstall(self, urn, remove_backups=False):
        app = urn.split(":")[0]; self.have.discard(app); self.state.pop(app, None)


def _split_env(tmp_path, monkeypatch, rt, calls):
    c = cli.Config.__new__(cli.Config); c.runtipi_root = tmp_path; c.home = tmp_path / "wos"; c.home.mkdir(exist_ok=True)
    c.runtipi = lambda: rt
    monkeypatch.setattr(cli, "installed_ids", lambda r: set(r.have))
    def app(c_, verb, a):
        calls.append((verb, a))
        if verb == "stop" and a in rt.stuck:
            return                                     # the stop "succeeds" but the app keeps running
        if verb == "stop" and a == "wizard-ux" and "ux-stop-fails" in rt.stuck:
            raise SystemExit("runtipi-cli app stop wizard-ux failed")
        rt.state[a] = "stopped" if verb == "stop" else "running"
    monkeypatch.setattr(cli, "_app", app)
    monkeypatch.setattr(cli, "can_carry", lambda root, uid, gid: "")
    monkeypatch.setattr(cli, "_bundle_queue", lambda port: {"active": 0, "queued": 0})
    monkeypatch.setattr(cli, "carry_bundle_state", lambda root, a, **k: calls.append(("carry", a)) or root)
    def install(c_, a, *x, _split=False, **k):
        calls.append(("install", a, _split))
        if a in rt.stuck:
            rt.have.add(a); rt.state[a] = "installing"
            raise SystemExit(f"{a}: index unreachable")
        rt.have.add(a); rt.state[a] = "running"; return {"status": "running"}
    monkeypatch.setattr(cli, "install", install)
    return c


def test_shell_split_is_ordered_and_refuses_live_runs(tmp_path, monkeypatch):
    calls = []
    rt = _SplitRt({"wizard-shell"})
    c = _split_env(tmp_path, monkeypatch, rt, calls)
    monkeypatch.setattr(cli, "_bundle_queue", lambda port: {"active": 1, "queued": 0})
    with pytest.raises(SystemExit, match="1 running"):
        cli.shell_split(c)
    monkeypatch.setattr(cli, "_bundle_queue", lambda port: None)
    with pytest.raises(SystemExit, match="does not report its run queue"):
        cli.shell_split(c)
    monkeypatch.setattr(cli, "can_carry", lambda root, uid, gid: "the Agent Service runs as uid 1000")
    with pytest.raises(SystemExit, match="Nothing was changed"):
        cli.shell_split(c)
    assert calls == [] and rt.state["wizard-shell"] == "running"
    monkeypatch.setattr(cli, "can_carry", lambda root, uid, gid: "")
    monkeypatch.setattr(cli, "_bundle_queue", lambda port: {"active": 0, "queued": 0})
    rt.stuck = {"wizard-shell"}
    with pytest.raises(SystemExit, match="did not stop"):
        cli.shell_split(c)
    assert calls == [("stop", "wizard-shell"), ("start", "wizard-shell")]
    calls.clear(); rt.stuck = set()
    out = cli.shell_split(c)
    assert calls == [("stop", "wizard-shell"), ("carry", "wizard-agent-service"),
                     ("install", "wizard-agent-service", True), ("install", "wizard-ux", True)]
    assert out["split"] is True and rt.state == {"wizard-shell": "stopped", "wizard-agent-service": "running", "wizard-ux": "running"}
    calls.clear()
    out = cli.shell_split(c, rollback=True)
    assert calls == [("stop", "wizard-ux"), ("stop", "wizard-agent-service"), ("start", "wizard-shell")]
    assert rt.state == {"wizard-shell": "running", "wizard-agent-service": "stopped", "wizard-ux": "stopped"}


def test_shell_split_failure_restores_the_bundle_and_a_rerun_starts_clean(tmp_path, monkeypatch):
    calls = []
    rt = _SplitRt({"wizard-shell"}, stuck={"wizard-ux"})          # the shell's install fails
    c = _split_env(tmp_path, monkeypatch, rt, calls)
    with pytest.raises(SystemExit, match="while installing wizard-ux") as e:
        cli.shell_split(c)
    assert "uninstalled wizard-ux" in str(e.value) and "uninstalled wizard-agent-service" in str(e.value)
    assert rt.have == {"wizard-shell"} and rt.state["wizard-shell"] == "running"   # back as it was
    calls.clear(); rt.stuck = set()
    out = cli.shell_split(c)                                     # the advised re-run works
    assert out["split"] is True and out["resumed"] is False
    assert ("install", "wizard-ux", True) in calls


def test_shell_split_copy_failure_after_the_stop_restarts_the_bundle(tmp_path, monkeypatch):
    calls = []
    rt = _SplitRt({"wizard-shell"})
    c = _split_env(tmp_path, monkeypatch, rt, calls)
    def boom(*a, **k):
        raise SystemExit("could not copy")
    monkeypatch.setattr(cli, "carry_bundle_state", boom)
    with pytest.raises(SystemExit, match="while copying"):
        cli.shell_split(c)
    assert calls == [("stop", "wizard-shell"), ("start", "wizard-shell")] and rt.state["wizard-shell"] == "running"


def test_shell_split_recovery_is_best_effort(tmp_path, monkeypatch):
    """A stop that fails during recovery doesn't keep the bundle down; every error is reported."""
    calls = []
    rt = _SplitRt({"wizard-shell", "wizard-ux", "wizard-agent-service"}, stuck={"ux-stop-fails"})
    c = _split_env(tmp_path, monkeypatch, rt, calls)
    rt.state["wizard-shell"] = "stopped"
    with pytest.raises(SystemExit, match="rollback incomplete: stop wizard-ux"):
        cli.shell_split(c, rollback=True)
    assert ("start", "wizard-shell") in calls and rt.state["wizard-shell"] == "running"


def test_shell_split_resumes_a_run_killed_after_the_service_install(tmp_path, monkeypatch):
    calls = []
    rt = _SplitRt({"wizard-shell", "wizard-agent-service"})
    c = _split_env(tmp_path, monkeypatch, rt, calls)
    rt.state["wizard-shell"] = "stopped"
    out = cli.shell_split(c)
    assert out["resumed"] is True and calls == [("install", "wizard-ux", True)]


def test_split_again_after_a_rollback_copies_the_bundles_current_state(tmp_path, monkeypatch):
    """Rollback, then `wam uninstall wizard-ux`, then split again: the owner used the bundle meanwhile, so the
    service's earlier state is set aside and the bundle's current state copied, never silently reused."""
    calls = []
    rt = _SplitRt({"wizard-shell", "wizard-agent-service"})
    c = _split_env(tmp_path, monkeypatch, rt, calls)
    old = tmp_path / "app-data" / cli.STORE / "wizard-agent-service" / "data"
    old.mkdir(parents=True); (old / "x").write_text("split-era")
    with pytest.raises(SystemExit, match="running next to the bundle"):
        cli.shell_split(c)                                         # service running too: refuse, nothing changed
    assert calls == []
    rt.state["wizard-agent-service"] = "stopped"
    out = cli.shell_split(c)
    assert out["again"] is True and out["resumed"] is False
    # same after the owner also stopped the bundle by hand: still a split again, never a silent resume
    calls.clear(); rt.have.discard("wizard-ux"); rt.state.pop("wizard-ux")
    rt.state.update({"wizard-shell": "stopped", "wizard-agent-service": "stopped"})
    out = cli.shell_split(c)
    assert out["again"] is True and ("carry", "wizard-agent-service") in calls and ("stop", "wizard-shell") not in calls
    calls.clear(); rt.have.discard("wizard-ux"); rt.state.pop("wizard-ux"); rt.state["wizard-agent-service"] = "stopped"
    rt.state["wizard-shell"] = "running"
    out = cli.shell_split(c)
    assert calls == [("stop", "wizard-shell"), ("carry", "wizard-agent-service"), ("start", "wizard-agent-service"),
                     ("install", "wizard-ux", True)]
    kept = list(old.parent.glob("data.before-split-*"))
    assert any((k / "x").is_file() and (k / "x").read_text() == "split-era" for k in kept)


def test_failed_uninstall_stops_the_service_and_never_starts_a_second_writer(tmp_path, monkeypatch):
    calls = []
    rt = _SplitRt({"wizard-shell"}, stuck={"wizard-ux"})
    c = _split_env(tmp_path, monkeypatch, rt, calls)
    def bad_uninstall(urn, remove_backups=False):
        raise RuntimeError("runtipi busy (409)")
    rt.uninstall = bad_uninstall
    with pytest.raises(SystemExit, match="uninstall wizard-agent-service: runtipi busy") as e:
        cli.shell_split(c)
    assert ("stop", "wizard-agent-service") in calls and rt.state["wizard-agent-service"] == "stopped"
    assert rt.state["wizard-shell"] == "running"                   # service confirmed stopped: bundle back
    calls.clear(); rt.state.update({"wizard-shell": "stopped", "wizard-agent-service": "running"}); rt.stuck = {"wizard-agent-service"}
    r = cli._restore_bundle(c, rt, None, stop=["wizard-agent-service"])
    assert "stop wizard-agent-service" in "; ".join(r["errors"]) and r["bundle"].startswith("stopped (not started")
    assert ("start", "wizard-shell") not in calls


def test_alongside_side_follows_what_runs_then_the_saved_choice():
    apps = {"wizard-shell": "bundle", "wizard-agent-service": "service", "wizard-ux": "shell"}
    rt = _SplitRt({"wizard-shell", "wizard-agent-service", "wizard-ux"})
    rt.state.update({"wizard-shell": "running", "wizard-agent-service": "stopped"})      # rolled back
    assert cli.alongside_side(rt, apps, saved=False) is False
    rt.state.update({"wizard-shell": "stopped", "wizard-agent-service": "running"})      # split
    assert cli.alongside_side(rt, apps, saved=False) is True
    rt.state.update({"wizard-agent-service": "stopped"})                                 # split, service restarting
    assert cli.alongside_side(rt, apps, saved=True) is True
    assert cli.alongside_side(rt, apps, saved=False) is True                             # split in fresh mode, no flag
    assert cli.alongside_side(rt, {"wizard-shell": "bundle", "wizard-agent-service": "service"}, saved=False) is False
    assert cli.alongside_side(rt, {"wizard-shell": "bundle"}, saved=True) is False       # never split


def test_alongside_media_agent_root_follows_runtipi_root_folder_host(tmp_path):
    """Runtipi's ROOT_FOLDER_HOST decides where /media lives on the host; unsafe paths are refused, not quoted."""
    from wam import alongside
    rt = tmp_path / "rt"; rt.mkdir()
    assert alongside.host_media_root(rt) == rt / "media"
    (rt / ".env").write_text("ROOT_FOLDER_HOST=/srv/runtipi\n")
    assert alongside.host_media_root(rt) == Path("/srv/runtipi/media")
    st = {"agent": {"hermes_home": str(tmp_path / "h"), "origin": "http://127.0.0.1:8642", "uid": 1000, "gid": 1000},
          "shell": {"port": 18796, "share_history": True}}
    text = alongside.shell_override(st, "wizard-shell", tmp_path / "wos", media_root=Path("/srv/runtipi/media"))
    env = yaml.safe_load(text.replace("!reset", ""))["services"]["wizard-shell"]["environment"]
    assert env["WIZARD_MEDIA_AGENT_ROOT"] == "/srv/runtipi/media"
    assert "WIZARD_MEDIA_AGENT_ROOT" not in alongside.shell_override(st, "wizard-shell", tmp_path / "wos")
    try:
        alongside.shell_override(st, "wizard-shell", tmp_path / "wos", media_root=Path("/srv/run tipi/media"))
    except SystemExit:
        pass
    else:
        raise AssertionError("an unsafe media path must be refused")
    # split roles: the Agent Service (which mounts /media) is handed the host path; the UX-only shell is not
    svc = alongside.shell_override(st, "wizard-agent-service", tmp_path / "wos", "service", Path("/srv/runtipi/media"))
    senv = yaml.safe_load(svc.replace("!reset", ""))["services"]["wizard-agent-service"]["environment"]
    assert senv["WIZARD_MEDIA_AGENT_ROOT"] == "/srv/runtipi/media"
    assert "WIZARD_MEDIA_AGENT_ROOT" not in alongside.shell_override(st, "wizard-ux", tmp_path / "wos", "shell", Path("/srv/runtipi/media"))


def test_alongside_share_history_is_an_opt_in_kept_across_reruns(tmp_path, monkeypatch):
    """--share-history mounts the whole Hermes home (session history) and says it exposes credentials; a re-run
    without the flag keeps the owner's choice; --no-share-history takes it back."""
    from wam import alongside
    hh = tmp_path / "hermes"; hh.mkdir()
    (hh / "config.yaml").write_text("{}\n")
    wh = tmp_path / "wos"; wh.mkdir()
    monkeypatch.setattr(alongside, "hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(alongside, "api_up", lambda origin, timeout=3.0: False)
    assert alongside.enable(wh, hh, 18796)["shell"]["share_history"] is False
    text = alongside.shell_override(alongside.load(wh), "wizard-shell", wh)
    assert (hh / "cache" / "images").is_dir()                                 # made so a later picture shows
    assert f"{hh}/cache/images:/hermes/cache/images:ro" in text and f"{hh}:/hermes:ro" not in text
    assert "credentials included" not in text
    assert alongside.enable(wh, hh, None, True)["shell"]["share_history"] is True
    text = alongside.shell_override(alongside.load(wh), "wizard-shell", wh)
    assert f"{hh}:/hermes:ro" in text and 'WIZARD_HERMES_STATE_PATH: "/hermes/state.db"' in text
    assert "credentials included" in text
    yaml.safe_load(text.replace("!reset", ""))
    assert alongside.enable(wh, hh)["shell"]["share_history"] is True          # re-run keeps the choice
    assert alongside.enable(wh, hh, None, False)["shell"]["share_history"] is False


def test_alongside_share_sessions_mounts_only_the_session_database(tmp_path, monkeypatch):
    """--share-sessions: helper status needs Hermes's state.db. Mount only state.db + -wal/-shm read-only, never the
    Hermes home (no .env/auth.json), and set WIZARD_HERMES_STATE_PATH. --share-history still wins when both are on."""
    from wam import alongside
    hh = tmp_path / "hermes"; hh.mkdir()
    (hh / "config.yaml").write_text("{}\n")
    (hh / ".env").write_text("SECRET=1\n"); (hh / "auth.json").write_text("{}")
    for n in alongside.SESSION_STATE_FILES:
        (hh / n).write_bytes(b"x")
    wh = tmp_path / "wos"; wh.mkdir()
    monkeypatch.setattr(alongside, "hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(alongside, "api_up", lambda origin, timeout=3.0: False)
    st = alongside.enable(wh, hh, 18796, None, True)
    assert st["shell"]["share_sessions"] is True and st["shell"]["share_history"] is False
    text = alongside.shell_override(alongside.load(wh), "wizard-agent-service", wh, "service")
    svc = yaml.safe_load(text.replace("!reset", ""))["services"]["wizard-agent-service"]
    assert svc["environment"]["WIZARD_HERMES_STATE_PATH"] == "/hermes/state.db"
    vols = svc["volumes"]
    for n in alongside.SESSION_STATE_FILES:
        assert f"{hh}/{n}:/hermes/{n}:ro" in vols
        assert (wh / "hermes-view" / n).is_file()                          # mount point inside the read-only view
    assert f"{hh}:/hermes:ro" not in vols and not any(".env" in v or "auth.json" in v for v in vols)
    assert all(v.endswith(":ro") for v in vols if "/hermes" in v.split(":")[1])
    assert "credentials included" not in text and alongside.INODE_MARK in text
    assert alongside.stale_session_mounts(text, str(hh)) == []
    (hh / "new-wal").write_bytes(b"y"); (hh / "new-wal").replace(hh / "state.db-wal")   # Hermes recreated its WAL
    assert alongside.stale_session_mounts(text, str(hh)) == ["state.db-wal"]
    assert alongside.enable(wh, hh)["shell"]["share_sessions"] is True          # a re-run keeps the choice
    # whole-home sharing supersedes it: no duplicate /hermes/state.db mounts
    alongside.enable(wh, hh, None, True)
    both = alongside.shell_override(alongside.load(wh), "wizard-agent-service", wh, "service")
    bvols = yaml.safe_load(both.replace("!reset", ""))["services"]["wizard-agent-service"]["volumes"]
    assert f"{hh}:/hermes:ro" in bvols and not any(v.startswith(f"{hh}/state.db") for v in bvols)
    alongside.enable(wh, hh, None, False, False)
    off = alongside.shell_override(alongside.load(wh), "wizard-agent-service", wh, "service")
    assert 'WIZARD_HERMES_STATE_PATH: ""' in off and "state.db:" not in off


def test_alongside_share_sessions_without_a_database_mounts_nothing(tmp_path, monkeypatch):
    """No state.db yet: Docker would create root-owned directories in the Hermes home, so nothing is mounted and the
    service stays on the HTTP-only path (state path blank)."""
    from wam import alongside
    hh = tmp_path / "hermes"; hh.mkdir(); (hh / "config.yaml").write_text("{}\n")
    wh = tmp_path / "wos"; wh.mkdir()
    monkeypatch.setattr(alongside, "hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(alongside, "api_up", lambda origin, timeout=3.0: False)
    alongside.enable(wh, hh, 18796, None, True)
    text = alongside.shell_override(alongside.load(wh), "wizard-agent-service", wh, "service")
    assert 'WIZARD_HERMES_STATE_PATH: ""' in text and "state.db" not in text.split("volumes:")[1]
    assert not (hh / "state.db").exists()


GET_SH = Path(__file__).resolve().parents[1] / "get.sh"


def _mode(tmp_path, *, mode="auto", hermes=True, wos=None, wizard_user_wos=None, runtipi=False):
    """Run get.sh's mode helpers (has_hermes .. want_alongside) with stub getent/docker; return the picked mode."""
    src = GET_SH.read_text()
    funcs = src[src.index("has_hermes() {"):src.index("# Root with an existing Hermes")]
    homes = {}
    for user, state in (("owner", wos), ("wizard", wizard_user_wos)):
        h = tmp_path / user; h.mkdir(exist_ok=True); homes[user] = h
        if state:
            (h / ".wizard-os").mkdir(exist_ok=True)
            (h / ".wizard-os" / "apps.json").write_text("{}")
            if state == "alongside":
                (h / ".wizard-os" / "alongside.json").write_text("{}")
    if hermes:
        (homes["owner"] / ".hermes").mkdir(exist_ok=True)
        (homes["owner"] / ".hermes" / "config.yaml").write_text("{}")
        (homes["owner"] / ".local" / "bin").mkdir(parents=True, exist_ok=True)
        hb = homes["owner"] / ".local" / "bin" / "hermes"; hb.write_text("#!/bin/sh\n"); hb.chmod(0o755)
    script = f"""
set -u
MODE={mode}; WUSER=wizard
getent() {{ case "$2" in owner) echo "owner:x:1:1::{homes['owner']}:/bin/sh";; wizard) echo "wizard:x:2:2::{homes['wizard']}:/bin/sh";; esac; }}
docker() {{ {"echo runtipi" if runtipi else "true"}; }}
sudo() {{ return 1; }}
id() {{ echo owner; }}
{funcs}
if [ "$(wos_mode owner)" = alongside ]; then echo alongside; elif want_alongside owner; then echo alongside; else echo fresh; fi
"""
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def test_get_sh_first_install_next_to_hermes_is_alongside(tmp_path):
    assert _mode(tmp_path) == "alongside"
    d = tmp_path / "nohermes"; d.mkdir()
    assert _mode(d, hermes=False) == "fresh"


def test_get_sh_rerun_on_a_fresh_box_stays_fresh(tmp_path):
    """Oscar's review of #12: a fresh-mode box whose owner also runs Hermes must not switch on update."""
    assert _mode(tmp_path, wos="fresh") == "fresh"
    d = tmp_path / "b"; d.mkdir()
    assert _mode(d, wizard_user_wos="fresh") == "fresh"          # installed under the wizard user
    d = tmp_path / "c"; d.mkdir()
    assert _mode(d, runtipi=True) == "fresh"                      # a running Runtipi, home not readable
    d = tmp_path / "e"; d.mkdir()
    assert _mode(d, wos="fresh", mode="alongside") == "alongside"  # explicit WIZARD_MODE=alongside still switches


def test_get_sh_rerun_on_an_alongside_box_stays_alongside(tmp_path):
    assert _mode(tmp_path, wos="alongside", runtipi=True) == "alongside"


def test_vendored_wizard_cli_matches_recorded_provenance():
    """Verify local hashes and the unchanged upstream base separately."""
    import hashlib
    d = Path(__file__).resolve().parents[1] / "wizard-cli"
    src = json.loads((d / "SOURCE.json").read_text())
    for name, want in src["sha256"].items():
        assert hashlib.sha256((d / name).read_bytes()).hexdigest() == want, f"wizard-cli/{name} changed: update the local provenance hash"
    try:
        import urllib.request
        for name, path in src["files"].items():
            url = f"https://raw.githubusercontent.com/{src['repo']}/{src['commit']}/{path}"
            with urllib.request.urlopen(url, timeout=10) as r:   # noqa: S310
                assert hashlib.sha256(r.read()).hexdigest() == src['upstream_sha256'][name], f"{name} base differs from {url}"
    except OSError as e:   # wizard-os is private: CI without a token checks the local pin only
        pytest.skip(f"wizard-os not readable from here ({str(e)[:60]}): checked the local pin only")


def test_install_resolves_slot_dependency_to_its_default_app(tmp_path, monkeypatch):
    """wizard-ux `uses: [agent-service]` names a spot, not an app: install that spot's default (the Agent Service)
    first, and skip it when something already provides the spot."""
    from wam import cli
    calls = []

    class RT:
        def app(self, urn): return {"info": {"id": urn.split(":")[0]}}
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: RT())
    monkeypatch.setattr(cli, "ensure_store", lambda c, rt: None)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set())
    monkeypatch.setattr(cli.alongside, "external_slots", lambda h: {})
    recs = {"wizard-ux": {"id": "wizard-ux", "uses": ["agent-service"]}, "wizard-agent-service": {"id": "wizard-agent-service", "uses": []}}
    monkeypatch.setattr(cli, "resolve_any", lambda c, ref: recs[ref])
    provided = {"apps": []}
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: provided)

    def stop(c, app_id, *a, **k):
        calls.append(app_id); raise SystemExit("stop here")
    monkeypatch.setattr(cli, "build_form", stop)
    try:
        cli.install(cli.Config(), "wizard-ux")
    except SystemExit:
        pass
    assert calls[0] == "wizard-agent-service"          # the service is installed before the screen
    calls.clear()
    provided["apps"] = [{"id": "other-service", "provides": ["agent-service"]}]
    try:
        cli.install(cli.Config(), "wizard-ux")
    except SystemExit:
        pass
    assert calls == ["wizard-ux"]                      # spot already filled: no second service
