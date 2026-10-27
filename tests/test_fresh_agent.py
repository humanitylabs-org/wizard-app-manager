"""Pointer skills on every registry write, and the fresh-mode connection helper (fake docker/hermes, real editor)."""
import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from test_lifecycle import TOKEN, bridge, op  # noqa: F401  (fixture)
from test_wam import _fake_store_app
from wam import alongside, fresh_agent, registry
from wam.lifecycle import Server
from wam.vendor.wizard_os_plugin import kernel

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "wam" / "vendor"
POINTER = "wizard-job-diagnostics"   # the hello-wizard template provides `diagnostics`


def _reg(tmp_path, installed=True):
    rt = tmp_path / "runtipi"
    if not (rt / "repos").exists():
        _fake_store_app(rt)
    rows = [{"app": {"status": "running", "port": 8799}, "info": {"urn": "hello-wizard:wizardapps"}}] if installed else []
    return registry.build(rows, "wizardapps", "127.0.0.1", rt)


# ---------------------------------------------------------------- vendored plugin pin

def test_vendored_plugin_matches_its_pinned_source():
    src = json.loads((VENDOR / "SOURCE.json").read_text())
    assert len(src["commit"]) == 40 and src["plugin_source"].endswith("#hermes-plugin/wizard-os")
    for name, want in src["sha256"].items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == want, f"wam/vendor/{name} drifted"
    assert fresh_agent.plugin_hashes() == {k.split("/", 1)[1]: v for k, v in src["sha256"].items()}
    try:
        import urllib.request
        for name, path in src["files"].items():
            url = f"https://raw.githubusercontent.com/{src['repo']}/{src['commit']}/{path}"
            with urllib.request.urlopen(url, timeout=10) as r:   # noqa: S310
                assert hashlib.sha256(r.read()).hexdigest() == src["sha256"][name], f"{name} differs from {url}"
    except OSError as e:
        pytest.skip(f"upstream not readable from here ({str(e)[:60]}): checked the local pin only")


# ---------------------------------------------------------------- pointer skills

def test_pointers_written_with_the_plugin_renderer_and_removed_at_once(tmp_path):
    home = tmp_path / "home"
    reg = _reg(tmp_path)
    registry.write(reg, home)
    p = home / "skills" / POINTER / "SKILL.md"
    want = kernel.pointer_skills(kernel.bridge_apps(registry.public_projection(reg), "url"))
    assert p.read_text() == want[POINTER]   # byte-identical to what the plugin/Agent Service render
    assert "mcp__wizard__hello_wizard__" in p.read_text() and "http://127.0.0.1:8799" in p.read_text()
    stamp = home / "skills" / kernel.JOB_STAMP
    assert stamp.stat().st_mtime >= (home / "apps.json").stat().st_mtime   # the plugin skips a redundant refresh
    registry.write(_reg(tmp_path, installed=False), home)
    assert not (home / "skills" / POINTER).exists()
    assert not (home / "skills" / "hello-wizard").exists()


def test_pointer_refresh_preserves_owner_system_and_linked_skills(tmp_path):
    home = tmp_path / "home"
    sk = home / "skills"
    owner = sk / "wizard-job-owner-notes" / "SKILL.md"           # owner's own skill using the prefix, no marker
    registry.atomic_write(owner, b"---\nname: wizard-job-owner-notes\n---\nmine\n")
    edited = sk / POINTER / "SKILL.md"                            # owner took over the name: not generated
    registry.atomic_write(edited, b"my own diagnostics routing\n")
    builder = home / "system-skills" / "wizard-builder" / "SKILL.md"
    registry.atomic_write(builder, b"builder")
    sentinel = sk / "owner-sentinel" / "SKILL.md"
    registry.atomic_write(sentinel, b"sentinel")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (sk / "wizard-job-linked").symlink_to(target, target_is_directory=True)
    extra = sk / "wizard-job-extra-files"                          # generated text, but extra files: not ours
    registry.atomic_write(extra / "SKILL.md", (registry.POINTER_MARK + "\n").encode())
    registry.atomic_write(extra / "notes.txt", b"keep")
    registry.write(_reg(tmp_path), home)
    assert edited.read_text() == "my own diagnostics routing\n"
    registry.write(_reg(tmp_path, installed=False), home)
    assert owner.read_text().endswith("mine\n") and edited.read_text() == "my own diagnostics routing\n"
    assert builder.read_text() == "builder" and sentinel.read_text() == "sentinel"
    assert (sk / "wizard-job-linked").is_symlink() and (extra / "notes.txt").is_file()


def test_bridge_results_refresh_pointers_in_home_and_shared_immediately(bridge):  # noqa: F811
    b = bridge
    shared = b.cfg.runtipi_root / "app-data" / "wizard-os"
    code, _ = op(b, "install")
    assert code == 200
    home_text = (b.cfg.home / "skills" / POINTER / "SKILL.md").read_text()
    shared_text = (shared / "skills" / POINTER / "SKILL.md").read_text()
    assert "http://127.0.0.1:8799" in home_text            # host Hermes (alongside) reaches the published port
    assert "http://hello-wizard:" in shared_text           # a container Hermes reaches the app by its network name
    assert TOKEN not in home_text + shared_text
    code, _ = op(b, "uninstall", "fixture-remove-key-001")
    assert code == 200
    assert not (b.cfg.home / "skills" / POINTER).exists() and not (shared / "skills" / POINTER).exists()


def test_apps_json_rewritten_after_the_stamp_never_lets_the_plugin_delete_owner_pointers(bridge, monkeypatch):  # noqa: F811
    """lifecycle.refresh rewrites apps.json after registry.write stamped the pointers (a collision found only in the
    shared projection). The stamp must still be newest, so the pinned kernel's session-start refresh (which deletes
    every wizard-job-* folder it doesn't render) skips; an owner's own wizard-job-* folder survives."""
    import time as _time
    b = bridge
    owner = b.cfg.home / "skills" / "wizard-job-owner-notes" / "SKILL.md"
    registry.atomic_write(owner, b"---\nname: wizard-job-owner-notes\n---\nmine\n")
    real = registry.public_projection
    calls = {"n": 0}

    def drifting(reg):   # the second projection (lifecycle's canonical rewrite) differs from what write() wrote
        calls["n"] += 1
        out = real(reg)
        return {**out, "note": "canonical"} if calls["n"] > 2 else out
    monkeypatch.setattr(registry, "public_projection", drifting)
    _time.sleep(0.02)
    code, _ = op(b, "install")
    assert code == 200 and "canonical" in (b.cfg.home / "apps.json").read_text()
    stamp = b.cfg.home / "skills" / kernel.JOB_STAMP
    assert stamp.stat().st_mtime_ns >= (b.cfg.home / "apps.json").stat().st_mtime_ns
    monkeypatch.setenv("WIZARD_OS_HOME", str(b.cfg.home))
    assert kernel.refresh_pointer_skills(b.cfg.home) is None   # skipped: nothing deleted
    assert owner.read_text().endswith("mine\n")


def test_internal_service_network_view_upstream(bridge):  # noqa: F811
    b = bridge
    (b.cfg.home / "app-manager.json").write_text(json.dumps({"service_app_network": "internal"}))
    op(b, "install")   # the running bridge follows the declaration without a restart
    up = json.loads((b.cfg.home / "view-upstreams.json").read_text())
    assert up["hello-wizard"] == "http://hello-wizard:8799/ui/"
    reg = json.loads((b.cfg.home / "apps.json").read_text())
    assert reg["apps"][0]["mcp"]["url"] == "http://127.0.0.1:8799/mcp"
    (b.cfg.home / "app-manager.json").write_text(json.dumps({"service_app_network": "bridge"}))
    with pytest.raises(Exception):
        b.refresh()


def test_container_listener_binds_only_the_declared_private_gateway(bridge):  # noqa: F811
    for addr, declared in [("10.9.8.7", None), ("8.8.8.8", "8.8.8.8"), ("0.0.0.0", "0.0.0.0"), ("10.9.8.7", "10.9.8.6")]:
        with pytest.raises(ValueError):
            Server((addr, 0), bridge, container_bind=declared)
    with pytest.raises(OSError):   # the policy accepts it (no ValueError); this host just doesn't own the address
        Server(("10.254.253.252", 0), bridge, container_bind="10.254.253.252")


# ---------------------------------------------------------------- fresh mode: fakes

FAKE_DOCKER = r'''
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args[0] == "info":
    sys.exit(0)
if args[:2] == ["network", "inspect"]:
    print('[{"Subnet":"172.18.0.0/16","Gateway":"172.18.0.1"}]'); sys.exit(0)
if args[0] == "ps":
    print(os.environ.get("FAKE_ENGINE", "engine1")); sys.exit(0)
if args[0] == "inspect":
    print('["PATH=/usr/bin","HERMES_UID=1000","HERMES_GID=1000"]'); sys.exit(0)
if args[0] == "exec":
    i, env = 1, dict(os.environ)
    while args[i].startswith("-"):
        if args[i] == "-e":
            k, _, v = args[i + 1].partition("="); env[k] = v; i += 2
        elif args[i] == "-u":
            i += 2
        else:
            i += 1
    env["PATH"] = os.environ["FAKE_BIN"] + os.pathsep + env.get("PATH", "")
    p = subprocess.run(args[i + 1:], input=sys.stdin.read(), capture_output=True, text=True, env=env)
    sys.stdout.write(p.stdout); sys.stderr.write(p.stderr); sys.exit(p.returncode)
sys.exit(1)
'''

FAKE_HERMES = r'''#!PYTHON
import json, os, shutil, sys
args = sys.argv[1:]
home = os.environ["HERMES_HOME"]
if args[:1] == ["--print-runtime-command"]:
    sys.exit(1)
if args[:2] == ["plugins", "install"]:
    d = os.path.join(home, "plugins", "wizard-os")
    shutil.copytree(os.environ["FAKE_PLUGIN_SRC"], d)
    if os.environ.get("FAKE_TAMPER"):
        open(os.path.join(d, "kernel.py"), "a").write("# tampered\n")
    sys.exit(0)
if args[:2] == ["plugins", "enable"]:
    assert sys.stdin.read() == ""   # never the installer's terminal
    if os.environ.get("FAKE_OLD_HERMES"):
        sys.stderr.write("hermes plugins: error: argument: invalid choice: 'enable'\n"); sys.exit(2)
    p = os.path.join(home, "config.yaml")
    text = open(p).read()
    assert "plugins:" not in text
    open(p, "a").write("plugins:\n  enabled:\n    - " + args[2] + "\n")
    sys.exit(0)
sys.exit(3)
'''

CONFIG = """\
# Hermes config (comments and order are the owner's)
model:
  provider: custom   # keep me
mcp_servers: []
skills:
  # external_dirs:
  disabled: []
"""


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer " + self.server.token:
            self.send_response(401)
            self.end_headers()
            return
        result = {"tools": [{"name": "wizard_apps"}, {"name": "hello_wizard__wizard_time"}]} \
            if body["method"] == "tools/list" else {"protocolVersion": "2025-06-18"}
        raw = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "s1")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeRuntipi:
    def __init__(self, apps):
        self.apps, self.calls = apps, []

    def installed(self):
        return [{"app": {"status": "running"}, "info": {"urn": f"{a}:wizardapps"}} for a in self.apps]

    def req(self, method, path, body=None, timeout=60):
        self.calls.append((method, path))
        return {"requestId": "x"}

    def wait_status(self, urn, want, timeout=900):
        return "running"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    pytest.importorskip("ruamel.yaml")   # the container's Hermes interpreter carries it
    home, rt_root = tmp_path / "wos", tmp_path / "runtipi"
    hermes_home, bindir = tmp_path / "opt-data", tmp_path / "bin"
    for d in (home, rt_root / "app-data" / "wizard-os" / "skills", hermes_home, bindir):
        d.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(CONFIG)
    (hermes_home / ".env").write_text("OPENAI_API_KEY=owner-value\n")
    os.chmod(hermes_home / ".env", 0o600)
    (tmp_path / "docker.py").write_text(FAKE_DOCKER)
    (bindir / "hermes").write_text(FAKE_HERMES.replace("PYTHON", sys.executable))
    (bindir / "hermes").chmod(0o755)
    log = tmp_path / "docker.log"
    monkeypatch.setenv("WAM_DOCKER", f"{sys.executable} {tmp_path / 'docker.py'}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_BIN", str(bindir))
    monkeypatch.setenv("FAKE_PLUGIN_SRC", str(VENDOR / "wizard_os_plugin"))
    monkeypatch.setattr(fresh_agent, "CONTAINER_HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(fresh_agent, "CONTAINER_OS_HOME", str(rt_root / "app-data" / "wizard-os"))
    monkeypatch.setattr(fresh_agent, "VENV_PYTHON", sys.executable)
    monkeypatch.setattr(fresh_agent, "RESTART_SETTLE", 0)
    monkeypatch.setenv("WIZARD_APP_OWNER_LOGINS", "owner@example.test")
    monkeypatch.setenv("WIZARD_ALLOWED_HOSTS", "box.example.ts.net")
    monkeypatch.setattr(alongside, "_tailscale_status", lambda: {})
    units = []
    monkeypatch.setattr(fresh_agent, "_systemctl_user", lambda *a: units.append(a) or "ok")
    gw = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    gw.token = alongside.gateway_token(home)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    monkeypatch.setattr(fresh_agent, "gateway_url", lambda app: f"http://127.0.0.1:{gw.server_port}/{app}/wizard/mcp")
    rt = FakeRuntipi(["hermes-agent", "wizard-agent-service", "files"])
    c = SimpleNamespace(home=home, runtipi_root=rt_root, runtipi=lambda: rt)
    yield SimpleNamespace(c=c, rt=rt, hermes=hermes_home, log=log, token=gw.token, gw=gw, units=units)
    gw.shutdown()
    gw.server_close()


def _config(f):
    return yaml.safe_load((f.hermes / "config.yaml").read_text())


@pytest.mark.parametrize("order", [
    ["hermes-agent", "wizard-agent-service"],
    ["wizard-agent-service", "hermes-agent"],
])
def test_prepared_bootstrap_skips_legacy_linker_and_connects(fresh, monkeypatch, order):
    from wam import agent_link, builder_bundle, cli
    f = fresh
    f.rt.apps = []
    fresh_agent.prepare(f.c)
    monkeypatch.setattr(cli, "ensure_store", lambda *a: None)
    monkeypatch.setattr(cli, "trusted_origin_env", lambda *a: {})
    monkeypatch.setattr(builder_bundle, "fresh", lambda *a: None)
    monkeypatch.setattr(f.rt, "app", lambda urn: {"info": {"port": 0}}, raising=False)
    monkeypatch.setattr(f.rt, "install", lambda urn, form: f.rt.apps.append(urn.split(":")[0]), raising=False)
    monkeypatch.setattr(cli, "write_registry", lambda *a: {"apps": [{"id": a} for a in f.rt.apps]})
    # Reproduce the actual legacy overlay rewrite, without launching real Docker.
    source = Path(agent_link.skills_mount(f.c)["source"])
    source.mkdir(parents=True)
    legacy_calls = []
    def legacy(c, store):
        legacy_calls.append(True)
        agent_link.add_skills_mount(c, store)
    monkeypatch.setattr(agent_link, "link", legacy)
    for app in order:
        result = cli.install(f.c, app, _record={"id": app, "repo": "fixture", "uses": []})
        assert result["status"] == "running"
    assert legacy_calls == []
    assert fresh_agent.main(f.c, "connect")["ok"]
    assert (f.hermes / "plugins/wizard-os/plugin.yaml").is_file()
    assert fresh_agent.CONTAINER_OS_HOME + "/skills" in _config(f)["skills"]["external_dirs"]


# ---------------------------------------------------------------- fresh mode: overlays

def test_prepare_and_install_overlays(fresh):
    f = fresh
    uc = f.c.runtipi_root / "user-config" / "wizardapps"
    pinned = uc / "wizard-agent-service" / "app.env"
    pinned.parent.mkdir(parents=True)
    pinned.write_text("WIZARD_AGENT_SERVICE_IMAGE=127.0.0.1:5000/svc:test\n")
    assert not fresh_agent.write_install_overlay(f.c.runtipi_root, "wizardapps", "wizard-agent-service", f.c.home)
    res = fresh_agent.prepare(f.c)
    assert res["ok"] and json.loads((f.c.home / "app-manager.json").read_text()) == {
        "container_bridge_bind": "172.18.0.1", "service_app_network": "internal"}
    assert fresh_agent.write_install_overlay(f.c.runtipi_root, "wizardapps", "wizard-agent-service", f.c.home)
    compose = (uc / "wizard-agent-service" / "docker-compose.yml").read_text()
    spec = yaml.safe_load(compose)["services"]["wizard-agent-service"]
    assert spec["extra_hosts"] == ["wizard-app-lifecycle:172.18.0.1"]
    assert spec["environment"]["WIZARD_APP_MANAGER_URL"] == "http://wizard-app-lifecycle:7791"
    # The Service admits exactly that bridge name and exactly the host's peer address on the Runtipi network.
    assert spec["environment"]["WIZARD_APP_MANAGER_HOSTS"] == "wizard-app-lifecycle"
    assert spec["environment"]["WIZARD_APP_TRUSTED_PROXY_IPS"] == "172.18.0.1"
    assert spec["environment"]["WIZARD_UX_PREFIX"] == "wizard-os-home"
    # ...and only for a named owner login behind it, with the box's tailnet name in the Host fence.
    assert spec["environment"]["WIZARD_APP_OWNER_LOGINS"] == "owner@example.test"
    assert spec["environment"]["WIZARD_ALLOWED_HOSTS"] == "box.example.ts.net"
    assert "network_mode" not in spec   # stays on the Runtipi network: UX and Hermes reach it by name
    env = pinned.read_text()
    assert env.startswith("WIZARD_AGENT_SERVICE_IMAGE=127.0.0.1:5000/svc:test\n")
    assert f"WIZARD_MCP_TOKEN={f.token}" in env and "WIZARD_APP_MANAGER_TOKEN=" in env
    assert pinned.stat().st_mode & 0o077 == 0 and f.token not in compose
    assert not fresh_agent.write_install_overlay(f.c.runtipi_root, "wizardapps", "wizard-agent-service", f.c.home)
    assert fresh_agent.write_install_overlay(f.c.runtipi_root, "wizardapps", "hermes-agent", f.c.home)
    agent = yaml.safe_load((uc / "hermes-agent" / "docker-compose.yml").read_text())["services"]["hermes-agent-engine"]
    assert agent["environment"]["WIZARD_OS_HOME"] == fresh_agent.CONTAINER_OS_HOME
    assert agent["volumes"] == [f"${{ROOT_FOLDER_HOST}}/app-data/wizard-os:{fresh_agent.CONTAINER_OS_HOME}:ro"]
    assert not (uc / "hermes-agent" / "app.env").exists()
    foreign = uc / "wizard-shell" / "docker-compose.yml"
    foreign.parent.mkdir()
    foreign.write_text("services: {}\n")
    with pytest.raises(fresh_agent.FreshError) as e:
        fresh_agent.write_install_overlay(f.c.runtipi_root, "wizardapps", "wizard-shell", f.c.home)
    assert e.value.code == "overlay_not_ours" and foreign.read_text() == "services: {}\n"


def test_prepare_requires_a_named_owner_from_env_or_this_box_tailnet(fresh, monkeypatch):
    f = fresh
    monkeypatch.delenv("WIZARD_APP_OWNER_LOGINS")
    monkeypatch.delenv("WIZARD_ALLOWED_HOSTS")
    res = fresh_agent.main(f.c, "prepare")
    assert res["error"] == "owner_login_required" and not (f.c.home / "fresh-agent.json").exists()
    # This node's own Tailscale identity (Self -> User LoginName, DNSName) is the inferred owner.
    monkeypatch.setattr(alongside, "_tailscale_status", lambda: {
        "Self": {"UserID": 1111111111111111, "DNSName": "box.tail1234.ts.net."},
        "User": {"1111111111111111": {"LoginName": "owner@example.invalid"}}})
    assert fresh_agent.main(f.c, "prepare")["ok"]
    assert json.loads((f.c.home / "fresh-agent.json").read_text())["owner"] == {
        "logins": ["owner@example.invalid"], "hosts": ["box.tail1234.ts.net"]}
    monkeypatch.setenv("WIZARD_APP_OWNER_LOGINS", "owner@x.test\nWIZARD_REQUIRE_CF_ACCESS=0")
    with pytest.raises(SystemExit):
        alongside.owner_identity()


def test_status_flags_a_wildcard_service_publication(fresh, monkeypatch):
    f = fresh
    fresh_agent.prepare(f.c)
    assert fresh_agent.main(f.c, "connect")["ok"]
    monkeypatch.setattr(fresh_agent, "service_publications", lambda store, app: ["0.0.0.0", "::"])
    res = fresh_agent.main(f.c, "status")
    row = next(r for r in res["checks"] if r["check"] == "Service published on 127.0.0.1 only")
    assert not row["ok"] and not res["ok"]


def test_prepare_refuses_alongside(fresh, tmp_path):
    alongside.save(fresh.c.home, {"mode": "alongside"})
    assert fresh_agent.main(fresh.c, "prepare")["error"] == "alongside_mode"
    assert not (fresh.c.home / "fresh-agent.json").exists()


# ---------------------------------------------------------------- fresh mode: connect / status / disconnect

def test_connect_one_gateway_skills_plugin_idempotent_and_reversible(fresh):
    f = fresh
    original = (f.hermes / "config.yaml").read_bytes()
    assert fresh_agent.main(f.c, "connect")["error"] == "not_prepared"
    fresh_agent.prepare(f.c)
    res = fresh_agent.main(f.c, "connect")
    assert res["ok"], res
    cfg = _config(f)
    gw = fresh_agent.gateway_url("wizard-agent-service")
    assert cfg["mcp_servers"] == {"wizard": {"url": gw, "headers": {"Authorization": "Bearer ${WIZARD_MCP_TOKEN}"}}}
    assert cfg["skills"]["external_dirs"] == [f"{fresh_agent.CONTAINER_OS_HOME}/skills"]
    assert cfg["plugins"]["enabled"] == ["wizard-os"]
    text = (f.hermes / "config.yaml").read_text()
    assert "# Hermes config (comments and order are the owner's)" in text and "provider: custom   # keep me" in text
    env = (f.hermes / ".env").read_text()
    assert env == f"OPENAI_API_KEY=owner-value\nWIZARD_MCP_TOKEN={f.token}\n"
    assert (f.hermes / ".env").stat().st_mode & 0o077 == 0
    backups = list((f.hermes / "backups" / "wizard-os").iterdir())
    assert len(backups) == 1 and (backups[0] / "config.yaml").read_bytes() == original
    assert {r["check"]: r["ok"] for r in res["checks"]}["live tools/list through the gateway"]
    assert res["live"]["app_tools"] == ["hello_wizard__wizard_time"]
    assert res["restarted"] == {"wizard-agent-service": "running", "hermes-agent": "running"}
    calls = [json.loads(line) for line in f.log.read_text().splitlines()]
    install = next(c for c in calls if "install" in c and "plugins" in c)
    pin = json.loads((VENDOR / "SOURCE.json").read_text())
    assert install[-6:] == ["install", pin["plugin_source"], "--ref", pin["commit"], "--no-enable", "--no-deps"]
    assert f.token not in f.log.read_text() and f.token not in json.dumps(res)   # stdin and 0600 files only
    state = json.loads((f.c.home / "fresh-agent.json").read_text())
    assert state["connected"]["service_app"] == "wizard-agent-service"
    assert (f.c.home / "fresh-agent.json").stat().st_mode & 0o077 == 0

    f.rt.calls.clear()
    again = fresh_agent.main(f.c, "connect")
    assert again["ok"] and again["restarted"] == {} and f.rt.calls == []
    assert again["changed"] == {"service_overlays": [], "agent": False}
    assert (f.hermes / ".env").read_text() == env and len(list((f.hermes / "backups" / "wizard-os").iterdir())) == 1

    out = fresh_agent.main(f.c, "disconnect")
    assert out["ok"], out
    assert yaml.safe_load((f.hermes / "config.yaml").read_text()) == yaml.safe_load(original)
    assert (f.hermes / ".env").read_text() == "OPENAI_API_KEY=owner-value\n"
    assert not (f.hermes / "plugins" / "wizard-os").exists()
    assert not (f.c.runtipi_root / "user-config" / "wizardapps" / "wizard-agent-service").exists()
    assert not (f.c.home / "fresh-agent.json").exists()
    assert json.loads((f.c.home / "app-manager.json").read_text()) == {}
    # The container listener has nothing to bind any more: disabled, not left restart-looping.
    assert ("disable", "--now", "wizard-app-lifecycle-containers.service") in f.units and out["container_listener"] == "ok"


def test_connect_prefers_modular_service_and_wires_legacy_bundle_too(fresh):
    f = fresh
    f.rt.apps.append("wizard-shell")
    fresh_agent.prepare(f.c)
    res = fresh_agent.main(f.c, "connect")
    assert res["ok"] and res["gateway"].endswith("/wizard-agent-service/wizard/mcp")
    assert len(_config(f)["mcp_servers"]) == 1
    assert sorted(res["changed"]["service_overlays"]) == ["wizard-agent-service", "wizard-shell"]
    assert f.rt.calls and all(m == "POST" and path.endswith("/restart") for m, path in f.rt.calls)   # nothing installed


class _ResetLoader(yaml.SafeLoader):
    """Compose's `!reset` (drop the base file's value) as a plain value, for reading generated overlays."""


_ResetLoader.add_constructor("!reset", lambda loader, node: ("!reset", loader.construct_sequence(node)))


def test_split_shell_overlay_is_loopback_only_and_reaches_the_service_port(fresh):
    f = fresh
    uc = f.c.runtipi_root / "user-config" / "wizardapps"
    fresh_agent.prepare(f.c)
    # Before install: the recipes' ports; the Service's real APP_PORT once Runtipi wrote it.
    svc_env = f.c.runtipi_root / "app-data" / "wizardapps" / "wizard-agent-service" / "app.env"
    svc_env.parent.mkdir(parents=True)
    svc_env.write_text("APP_PORT=18797\nOTHER=x\n")
    assert fresh_agent.write_install_overlay(f.c.runtipi_root, "wizardapps", "wizard-ux", f.c.home)
    compose = (uc / "wizard-ux" / "docker-compose.yml").read_text()
    spec = yaml.load(compose, Loader=_ResetLoader)["services"]["wizard-ux"]
    assert spec["network_mode"] == "host" and spec["ports"] == ("!reset", []) and spec["networks"] == ("!reset", [])
    assert spec["environment"] == {"WIZARD_SHELL_LISTEN": "127.0.0.1:8796", "WIZARD_AGENT_SERVICE_ORIGIN": "http://127.0.0.1:18797"}
    assert spec["entrypoint"] == ["/bin/sh", "/etc/wizard-shell-adapter/shell-adapter.sh"]
    assert spec["volumes"] == ["${ROOT_FOLDER_HOST}/user-config/wizardapps/wizard-ux/shell-adapter.sh:/etc/wizard-shell-adapter/shell-adapter.sh:ro"]
    # The shell holds no token: no app.env, nothing secret in the overlay or the adapter.
    assert not (uc / "wizard-ux" / "app.env").exists()
    adapter = (uc / "wizard-ux" / "shell-adapter.sh").read_text()
    assert f.token not in compose + adapter and (uc / "wizard-ux" / "shell-adapter.sh").stat().st_mode & 0o777 == 0o644
    assert not fresh_agent.write_install_overlay(f.c.runtipi_root, "wizardapps", "wizard-ux", f.c.home)   # idempotent
    # connect wires an installed shell too and restarts it; status checks it; disconnect removes overlay + adapter.
    f.rt.apps.append("wizard-ux")
    (uc / "wizard-ux" / "shell-adapter.sh").write_text("# Written by wam (stale)\n")
    res = fresh_agent.main(f.c, "connect")
    assert res["ok"] and "wizard-ux" in res["changed"]["service_overlays"]
    assert [r for r in res["checks"] if r["check"].startswith("Shell overlay")][0]["ok"]
    assert (uc / "wizard-ux" / "shell-adapter.sh").read_text() == fresh_agent.SHELL_ADAPTER_SH
    fresh_agent.main(f.c, "disconnect")
    assert not (uc / "wizard-ux").exists()


def test_shell_adapter_script_adapts_the_recipe_config_or_refuses(tmp_path):
    """Runs the generated adapter on copies shaped like the wizard-ux recipe's own files (PR5 795ff1a layout)."""
    etc, bin_ = tmp_path / "etc", tmp_path / "bin"
    etc.mkdir(), bin_.mkdir()
    (etc / "nginx.conf.template").write_text(
        "http {\n  server {\n    listen ${WIZARD_SHELL_LISTEN};\n    server_name _;\n    set $wizard_api \"x\";\n"
        "    location ^~ /wizard-os-home/api/ { proxy_pass $wizard_api$request_uri; include /tmp/wizard-proxy.conf; }\n"
        "    location /wizard-os-home/ {\n      alias /usr/share/nginx/wizard-ux/; include /tmp/wizard-static.conf;\n    }\n  }\n}\n")
    (etc / "static.conf").write_text("add_header Content-Security-Policy \"default-src 'self'; frame-ancestors 'none'\" always;\n"
                                     "add_header X-Frame-Options \"DENY\" always;\n")
    (etc / "proxy.conf").write_text("proxy_http_version 1.1;\n")
    (bin_ / "entry").write_text(f"cp {etc}/static.conf OUT/static\ncp {etc}/nginx.conf.template OUT/tpl\n")
    out = tmp_path / "out"
    out.mkdir()
    (bin_ / "entry").write_text((bin_ / "entry").read_text().replace("OUT", str(out)))
    script = (fresh_agent.SHELL_ADAPTER_SH.replace("/etc/wizard-shell", str(etc))
              .replace("/usr/local/bin/wizard-shell-entrypoint", str(bin_ / "entry"))
              .replace("/tmp/wizard-shell-adapted", str(tmp_path / "adapted")))

    def run(listen):
        return subprocess.run(["sh", "-c", script], env={"PATH": os.environ["PATH"], "WIZARD_SHELL_LISTEN": listen},
                              capture_output=True, text=True)
    assert run("8788").returncode == 3   # never a container-network listener
    p = run("127.0.0.1:8796")
    assert p.returncode == 0, p.stderr
    tpl, static = (out / "tpl").read_text(), (out / "static").read_text()
    assert "    server_name _;\n    allow 127.0.0.1;\n    deny all;\n" in tpl
    assert ("    location ^~ /wizard-os-home/store-data/ {\n      limit_except GET { deny all; }\n"
            "      proxy_pass $wizard_api$request_uri; include /tmp/wizard-proxy.conf;\n    }\n    location /wizard-os-home/ {") in tpl
    assert "location ^~ /wizard-os-home/api/" in tpl   # the recipe's routes are untouched
    assert "frame-ancestors 'self'" in static and 'X-Frame-Options "SAMEORIGIN"' in static and "'none'" not in static
    (etc / "static.conf").write_text("add_header X-Frame-Options \"DENY\" always;\n")   # the recipe changed
    assert run("127.0.0.1:8796").returncode == 3


def test_connect_refuses_a_second_gateway_and_missing_apps(fresh):
    f = fresh
    fresh_agent.prepare(f.c)
    f.rt.apps.remove("hermes-agent")
    assert fresh_agent.main(f.c, "connect")["error"] == "agent_not_installed"
    f.rt.apps.append("hermes-agent")
    (f.hermes / "config.yaml").write_text("mcp_servers:\n  wizard-old:\n    url: http://wizard-shell:8787/wizard/mcp\n")
    before = (f.hermes / "config.yaml").read_bytes()
    res = fresh_agent.main(f.c, "connect")
    assert res["error"] == "duplicate_gateway"
    assert (f.hermes / "config.yaml").read_bytes() == before and not (f.hermes / "plugins").exists()


def test_tampered_plugin_is_never_enabled(fresh, monkeypatch):
    f = fresh
    monkeypatch.setenv("FAKE_TAMPER", "1")
    fresh_agent.prepare(f.c)
    res = fresh_agent.main(f.c, "connect")
    assert res["error"] == "plugin_mismatch"
    assert "wizard-os" not in str(_config(f).get("plugins")) and "wizard" not in (_config(f).get("mcp_servers") or {})
    assert not (f.hermes / "plugins" / "wizard-os").exists()


def test_older_hermes_without_plugins_enable_uses_the_allow_list(fresh, monkeypatch):
    f = fresh
    monkeypatch.setenv("FAKE_OLD_HERMES", "1")
    fresh_agent.prepare(f.c)
    res = fresh_agent.main(f.c, "connect")
    assert res["ok"], res
    assert _config(f)["plugins"]["enabled"] == ["wizard-os"]
    assert "provider: custom   # keep me" in (f.hermes / "config.yaml").read_text()


def test_status_reports_without_changing_anything(fresh):
    f = fresh
    fresh_agent.prepare(f.c)
    before = (f.hermes / "config.yaml").read_bytes()
    res = fresh_agent.main(f.c, "status")
    assert not res["ok"] and (f.hermes / "config.yaml").read_bytes() == before
    assert not (f.hermes / "plugins").exists() and f.rt.calls == []


def test_cli_exposes_fresh_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "wos"))
    p = subprocess.run([sys.executable, "-m", "wam", "fresh-agent", "--help"], cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0 and "prepare" in p.stdout and "disconnect" in p.stdout


def test_agent_ssh_key_is_mounted_from_outside_app_data(fresh):
    """Runtipi chmods an app's data folder a+rwx on every start; the box's agent key must live outside it."""
    f = fresh
    uc = f.c.runtipi_root / "user-config" / "wizardapps" / "hermes-agent"
    fresh_agent.prepare(f.c)
    assert fresh_agent.write_agent_overlay(f.c.runtipi_root, "wizardapps", f.c.home)
    plain = yaml.safe_load((uc / "docker-compose.yml").read_text())["services"]["hermes-agent-engine"]["volumes"]
    assert len(plain) == 1   # no key folder on this box: nothing extra mounted
    (f.c.home / "agent-ssh").mkdir(mode=0o700)
    assert fresh_agent.write_agent_overlay(f.c.runtipi_root, "wizardapps", f.c.home)
    vols = yaml.safe_load((uc / "docker-compose.yml").read_text())["services"]["hermes-agent-engine"]["volumes"]
    assert f"{f.c.home / 'agent-ssh'}:/opt/agent-ssh:ro" in vols
    assert not fresh_agent.write_agent_overlay(f.c.runtipi_root, "wizardapps", f.c.home)   # idempotent


def test_agent_overlay_rewrite_keeps_the_builder_bundle_mount(fresh):
    """builder-bundle and fresh-agent both write the agent overlay; fresh-agent's rewrite must not drop the builder."""
    from wam import builder_bundle
    f = fresh
    uc = f.c.runtipi_root / "user-config" / "wizardapps" / "hermes-agent"
    fresh_agent.prepare(f.c)
    (f.c.home / "builder" / "current").mkdir(parents=True)
    assert fresh_agent.write_agent_overlay(f.c.runtipi_root, "wizardapps", f.c.home)
    text = (uc / "docker-compose.yml").read_text()
    assert text.startswith(fresh_agent.MARK + " (fresh mode)")
    engine = yaml.safe_load(text)["services"]["hermes-agent-engine"]
    assert engine["environment"]["WIZARD_BUILDER_ROOT"] == builder_bundle.RUNTIME
    assert {"type": "bind", "source": str((f.c.home / "builder").resolve()), "target": builder_bundle.RUNTIME,
            "read_only": True} in engine["volumes"]
    assert engine["environment"]["WIZARD_OS_HOME"]
    assert not fresh_agent.write_agent_overlay(f.c.runtipi_root, "wizardapps", f.c.home)   # idempotent
    # builder-bundle's own merge over fresh-agent's file is a no-op and keeps the ownership mark
    assert builder_bundle.with_builder_mount(text, f.c.home) == text
