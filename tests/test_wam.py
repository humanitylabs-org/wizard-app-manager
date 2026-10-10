import json, shutil, subprocess, sys, threading, time, urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wam import index, registry, store, wizardmd  # noqa: E402
from wam.runtipi import cli_token  # noqa: E402

TEMPLATE = ROOT / "templates" / "hello-wizard"


def test_template_passes_checker():
    res = wizardmd.check(TEMPLATE)
    assert wizardmd.passed(res), wizardmd.failures(res)


def test_missing_skill_fails(tmp_path):
    bad = tmp_path / "bad"
    shutil.copytree(TEMPLATE, bad)
    (bad / "skill" / "SKILL.md").unlink()
    assert "skill ships with name+description" in wizardmd.failures(wizardmd.check(bad))


def test_bad_slot_fails(tmp_path):
    bad = tmp_path / "bad"
    shutil.copytree(TEMPLATE, bad)
    p = bad / "wizard.md"
    p.write_text(p.read_text().replace("provides: [diagnostics]", "provides: [Not A Slot]"))
    assert "provides are slot ids" in wizardmd.failures(wizardmd.check(bad))


@pytest.mark.parametrize("snippet", [
    "    privileged: true\n",
    "    network_mode: host\n",
    "    volumes: ['/var/run/docker.sock:/var/run/docker.sock']\n",
    "    volumes: ['/etc:/x:ro']\n",
    "    cap_add: [SYS_ADMIN]\n",
])
def test_host_level_container_access_fails(tmp_path, snippet):
    bad = tmp_path / "bad"
    shutil.copytree(TEMPLATE, bad)
    c = bad / "runtipi" / "docker-compose.yml"
    c.write_text(c.read_text().replace("    read_only: true\n", "    read_only: true\n" + snippet, 1))
    assert "container asks for no host-level access" in wizardmd.failures(wizardmd.check(bad))


def test_future_timestamp_fails(tmp_path):
    bad = tmp_path / "bad"
    shutil.copytree(TEMPLATE, bad)
    p = bad / "runtipi" / "config.json"
    cfg = json.loads(p.read_text()); cfg["created_at"] = int(time.time() * 1000) + 86_400_000
    p.write_text(json.dumps(cfg))
    assert "runtipi timestamps not in future" in wizardmd.failures(wizardmd.check(bad))


def test_loopback_compose_publishes_main_port_on_loopback_only():
    out = yaml.safe_load(store.loopback_compose((TEMPLATE / "runtipi" / "docker-compose.yml").read_text()))
    assert out["services"]["hello-wizard"]["ports"] == ["127.0.0.1:${APP_PORT}:8799"]
    # an app that declares its own ports keeps them
    own = "services:\n  a:\n    image: x\n    ports: ['127.0.0.1:${APP_PORT}:9222']\n    x-runtipi: {is_main: true}\n"
    assert yaml.safe_load(store.loopback_compose(own))["services"]["a"]["ports"] == ["127.0.0.1:${APP_PORT}:9222"]


def test_local_store_is_stock_runtipi_layout_and_only_installed_apps(tmp_path):
    out = tmp_path / "store"
    m = store.meta(TEMPLATE, str(TEMPLATE), "local")
    store.add_app(out, TEMPLATE, m)
    cat = store.write_catalog(out, [m])
    app = out / "apps" / "hello-wizard"
    for f in ("config.json", "docker-compose.yml", "metadata/description.md", "metadata/logo.jpg"):
        assert (app / f).is_file(), f
    assert json.loads((app / "config.json").read_text())["id"] == "hello-wizard"
    assert (out / "skills" / "hello-wizard" / "SKILL.md").is_file()
    assert [a["id"] for a in cat["apps"]] == ["hello-wizard"]
    assert len(store.commit_store(out)) == 40
    store.remove_app(out, "hello-wizard")
    assert not app.exists() and store.write_catalog(out, [])["apps"] == []


def test_fetch_commit_pins_exact_commit(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(TEMPLATE, src)
    run = lambda *c: subprocess.run(c, cwd=src, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    run("git", "init", "-q", "-b", "main"); run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    first = run("git", "rev-parse", "HEAD")
    (src / "README.md").write_text("two\n"); run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two")
    got = store.fetch_commit(f"file://{src}", first, tmp_path / "cache")
    assert store.local_commit(got) == first and not (got / "README.md").exists()
    with pytest.raises(RuntimeError):
        store.fetch_commit(f"file://{src}", "main", tmp_path / "cache")   # branch names are refused


def test_index_resolve_by_id_and_repo(monkeypatch):
    rec = {"repo": "a/hello", "id": "hello-wizard", "install": {"git": "https://github.com/a/hello", "commit": "f" * 40}}
    calls = []

    def fake_get(url):
        calls.append(url)
        if "/v1/search" in url:
            return {"apps": [rec]}
        return {**rec, "listed": True}
    monkeypatch.setattr(index, "_get", fake_get)
    assert index.resolve("https://i", "hello-wizard")["repo"] == "a/hello"
    assert "id=hello-wizard" in calls[-1]
    assert index.resolve("https://i", "a/hello")["install"]["commit"] == "f" * 40
    assert calls[-1] == "https://i/v1/apps/a/hello"
    monkeypatch.setattr(index, "_get", lambda url: {"apps": []})
    with pytest.raises(index.IndexError_):
        index.resolve("https://i", "nope")


def test_registry_join_and_slots(tmp_path):
    out = tmp_path / "store"
    m = store.meta(TEMPLATE, str(TEMPLATE), "local")
    store.add_app(out, TEMPLATE, m)
    cat = store.write_catalog(out, [m])
    installed = [{"app": {"status": "running", "port": 8799}, "info": {"urn": "hello-wizard:wizardapps"}},
                 {"app": {"status": "running", "port": 80}, "info": {"urn": "nextcloud:migrated"}}]
    reg = registry.build(cat, installed, "wizardapps", "127.0.0.1", out / "skills")
    assert [a["id"] for a in reg["apps"]] == ["hello-wizard"]
    a = reg["apps"][0]
    assert a["mcp"]["url"] == "http://127.0.0.1:8799/mcp"
    assert a["mcp"]["internal"] == "http://hello-wizard:8799/mcp"
    assert reg["slots"]["diagnostics"]["default"] == "hello-wizard"
    p = registry.write(reg, out / "skills", tmp_path / "home")
    assert json.loads(p.read_text())["apps"][0]["skill"] == "skills/hello-wizard/SKILL.md"


def test_cli_token_is_valid_hs256():
    jwt = pytest.importorskip("jwt")
    claims = jwt.decode(cli_token("s3cret"), "s3cret", algorithms=["HS256"])
    assert claims["sub"] == "cli"


def test_store_server_serves_smart_http_and_refuses_push(tmp_path):
    out = tmp_path / "store"
    m = store.meta(TEMPLATE, str(TEMPLATE), "local")
    store.add_app(out, TEMPLATE, m)
    store.write_catalog(out, [m])
    head = store.commit_store(out)
    backend = Path("/usr/lib/git-core/git-http-backend")
    if not backend.exists():
        pytest.skip("git-http-backend not installed")
    srv = (ROOT / "wam" / "store_server.py").read_text().replace('ROOT = "/srv"', f'ROOT = "{tmp_path}"')
    port = 18731
    proc = subprocess.Popen([sys.executable, "-c", srv], env={"PORT": str(port), "PATH": "/usr/bin:/bin"})
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1); break
            except Exception:
                time.sleep(0.1)
        clone = tmp_path / "clone"
        r = subprocess.run(["git", "clone", "-q", f"http://127.0.0.1:{port}/store.git", str(clone)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True).stdout.strip() == head
        assert (clone / "apps" / "hello-wizard" / "config.json").is_file()
        (clone / "x").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], cwd=clone, check=True)
        assert subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, capture_output=True).returncode != 0
    finally:
        proc.terminate()


def test_api_requires_token_for_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WAM_PORT", "18732")
    from wam import cli
    c = cli.Config()
    cli.Handler.cfg, cli.Handler.token = c, c.api_token()
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 18732), cli.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert json.load(urllib.request.urlopen("http://127.0.0.1:18732/healthz"))["ok"]
        req = urllib.request.Request("http://127.0.0.1:18732/api/install", b'{"app":"x"}', method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 401
        req = urllib.request.Request("http://127.0.0.1:18732/api/apps/x", method="DELETE")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 401
        assert (c.token_file.stat().st_mode & 0o077) == 0
        assert c.bind == "127.0.0.1"
    finally:
        srv.shutdown()


def test_template_mcp_server_speaks_mcp():
    src = (TEMPLATE / "app" / "server.py").read_text()
    port = 18799
    proc = subprocess.Popen([sys.executable, "-c", src], env={"PORT": str(port), "PATH": "/usr/bin:/bin"})
    try:
        def rpc(body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", json.dumps(body).encode(), {"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=5))
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1); break
            except Exception:
                time.sleep(0.1)
        names = [t["name"] for t in rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        assert names == ["wizard_time", "wizard_echo"]
    finally:
        proc.terminate()


def test_amd64_only_app_allowed_on_arm64_with_emulation(tmp_path):
    app = {"id": "x", "architectures": ["amd64"]}
    assert not store.matches_arch(app, "aarch64", binfmt=tmp_path)
    (tmp_path / "rosetta").write_text("enabled\n")
    assert store.matches_arch(app, "aarch64", binfmt=tmp_path)
    assert store.matches_arch(app, "x86_64", binfmt=tmp_path)
    assert not store.matches_arch({"id": "y", "architectures": ["arm64"]}, "x86_64", binfmt=tmp_path)


def test_amd64_only_app_pinned_for_emulation(tmp_path):
    (tmp_path / "rosetta").write_text("enabled\n")
    cfg = {"supported_architectures": ["amd64"]}
    assert store.needs_amd64_emulation(cfg, "aarch64", tmp_path)
    assert not store.needs_amd64_emulation(cfg, "x86_64", tmp_path)
    assert not store.needs_amd64_emulation({"supported_architectures": ["amd64", "arm64"]}, "aarch64", tmp_path)
    out = yaml.safe_load(store.pin_amd64("services:\n  a:\n    image: x\n  b:\n    image: y\n"))
    assert {s["platform"] for s in out["services"].values()} == {"linux/amd64"}
