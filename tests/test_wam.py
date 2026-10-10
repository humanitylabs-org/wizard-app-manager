import json, shutil, sys, threading, time, urllib.error, urllib.request
from pathlib import Path

import pytest

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
    port = 18799
    proc = subprocess.Popen([sys.executable, "-c", src], env={"PORT": str(port), "PATH": "/usr/bin:/bin"})
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1); break
            except Exception:
                time.sleep(0.1)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode(),
                                     {"Content-Type": "application/json"})
        names = [t["name"] for t in json.load(urllib.request.urlopen(req, timeout=5))["result"]["tools"]]
        assert names == ["wizard_time", "wizard_echo"]
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
