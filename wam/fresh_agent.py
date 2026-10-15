"""Fresh mode: connect the hermes-agent container to the one Wizard gateway. An installation helper only.

docs/lifecycle-bridge.md, "Fresh mode". The box has no Hermes of its own; `wam bootstrap` installed the hermes-agent
app (the stock Hermes image, data at /opt/data) and the Agent Service app (wizard-agent-service, or the legacy
wizard-shell bundle). This module joins them with generated, stock Runtipi user-config overlays and Hermes' own
config surface. No recipe, image or Hermes core change; the host (and any host Hermes) is never touched.

  prepare     record fresh mode: the gateway token, the Runtipi network gateway address the lifecycle bridge's
              container listener binds (wizard-app-lifecycle-containers.service), Service-side view addressing.
              Overlays for apps installed later are written at their install.
  connect     overlays for the installed apps; inside the hermes-agent container: exactly one `wizard` MCP server
              (http://<service app>:8787/wizard/mcp, token from the Hermes .env), skills.external_dirs gets the
              shared /wizard-os/skills, the pinned wizard-os plugin is installed and enabled. Backed up first,
              recorded in $WIZARD_OS_HOME/fresh-agent.json, then one Runtipi restart of what changed. Idempotent.
  status      the same checks, read-only, plus one live tools/list through the gateway from inside the container.
  disconnect  reverse exactly the recorded edits and overlays.

App installs/uninstalls after this need no agent restart: the bridge rewrites the shared apps.json, app skills and
wizard-job-* pointers the container Hermes reads, and the gateway announces tools/list_changed.
Secrets travel only through 0600 files and process stdin, never argv, output or logs.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from . import alongside, registry
from .runtipi import RuntipiError

STATE_FILE = "fresh-agent.json"
AGENT_APP = "hermes-agent"
ENGINE = "hermes-agent-engine"              # the hermes-agent recipe's stock Hermes container (data at /opt/data)
SERVICE_APPS = ("wizard-agent-service", "wizard-shell")   # the modular Agent Service first, then the legacy bundle
SHELL_APP = "wizard-ux"                      # the modular shell (UX only), beside wizard-agent-service
SERVICE_PORT = 8787                          # both recipes' internal port
DEFAULT_PORTS = {"wizard-agent-service": 8797, SHELL_APP: 8796}   # the recipes' runtipi/config.json ports
SHELL_ADAPTER = "shell-adapter.sh"
BRIDGE_HOST = "wizard-app-lifecycle"         # the name the Service resolves to the bridge's container listener
BRIDGE_PORT = 7791
RUNTIPI_NETWORK = "runtipi_tipi_main_network"
CONTAINER_HERMES_HOME = "/opt/data"
CONTAINER_OS_HOME = "/wizard-os"             # the shared, read-only app-data/wizard-os mount
VENV_PYTHON = "/opt/hermes/.venv/bin/python"
MCP_NAME, PLUGIN_NAME, TOKEN_ENV = "wizard", "wizard-os", "WIZARD_MCP_TOKEN"
MARK = "# Written by wam"
AGENT_SSH_DIR = "agent-ssh"                  # $WIZARD_OS_HOME/agent-ssh: the agent's key for a terminal on this box
CONTAINER_AGENT_SSH = "/opt/agent-ssh"
_SAFE_PATH = __import__("re").compile(r"/[A-Za-z0-9._/@+-]+")
RESTART_SETTLE = 3                           # seconds between Runtipi's restart answer and the status poll
VENDOR = Path(__file__).with_name("vendor")


class FreshError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


# ---------------------------------------------------------------- state

def load(home: Path) -> dict | None:
    st = registry._json_file(home / STATE_FILE)
    return st if st.get("mode") == "fresh" else None


def save(home: Path, st: dict) -> None:
    registry.atomic_write(home / STATE_FILE, (json.dumps(st, indent=2) + "\n").encode(), 0o600)


def plugin_pin() -> dict:
    return json.loads((VENDOR / "SOURCE.json").read_text())


def plugin_hashes() -> dict[str, str]:
    """The pinned plugin files' sha256, keyed by their name in the installed plugin folder."""
    return {k.split("/", 1)[1]: v for k, v in plugin_pin()["sha256"].items()}


def gateway_url(service_app: str) -> str:
    return f"http://{service_app}:{SERVICE_PORT}/wizard/mcp"


def _merge_json(path: Path, update: dict, drop: tuple = ()) -> None:
    cur = registry._json_file(path)
    new = {k: v for k, v in {**cur, **update}.items() if k not in drop}
    if new != cur or not path.exists():
        registry.atomic_write(path, (json.dumps(new, indent=2) + "\n").encode())


# ---------------------------------------------------------------- docker

def docker_cmd() -> list[str]:
    """`docker`, or `sudo -n docker` for an owner not yet in the docker group (as install.sh does). $WAM_DOCKER wins."""
    if os.environ.get("WAM_DOCKER"):
        return shlex.split(os.environ["WAM_DOCKER"])
    for cand in (["docker"], ["sudo", "-n", "docker"]):
        try:
            if subprocess.run(cand + ["info"], capture_output=True, timeout=60).returncode == 0:
                return cand
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise FreshError("docker_unavailable", "neither `docker` nor `sudo -n docker` answers")


def _docker(*args: str, stdin: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(docker_cmd() + list(args), input=stdin, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FreshError("docker_timeout", " ".join(args[:2])) from None


def network_gateway(network: str = RUNTIPI_NETWORK) -> str:
    """The host's address on the Runtipi Docker network: private IPv4, reachable from containers on it only."""
    p = _docker("network", "inspect", network, "--format", "{{json .IPAM.Config}}")
    if p.returncode:
        raise FreshError("runtipi_network_missing", network)
    try:
        configs = json.loads(p.stdout.strip() or "[]") or []
    except ValueError:
        raise FreshError("runtipi_network_unreadable", network) from None
    for c in configs:
        gw = (c or {}).get("Gateway") or ""
        try:
            ip = ipaddress.ip_address(gw)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not ip.is_loopback:
            return gw
    raise FreshError("runtipi_network_no_gateway", network)


def engine_container(store: str) -> str:
    p = _docker("ps", "-q", "--filter", f"label=com.docker.compose.project={AGENT_APP}_{store}",
                "--filter", f"label=com.docker.compose.service={ENGINE}")
    ids = p.stdout.split()
    if p.returncode or len(ids) != 1:
        raise FreshError("agent_container_not_running", f"{len(ids)} {ENGINE} containers running")
    return ids[0]


def _engine_user(container: str) -> str:
    p = _docker("inspect", "--format", "{{json .Config.Env}}", container)
    env = {}
    try:
        for kv in json.loads(p.stdout or "[]") or []:
            k, _, v = kv.partition("=")
            env[k] = v
    except ValueError:
        pass
    uid, gid = env.get("HERMES_UID", ""), env.get("HERMES_GID", "")
    return f"{uid}:{gid}" if uid.isdigit() and gid.isdigit() else "hermes"


def _exec(container: str, user: str, argv: list[str], stdin: str = "",
          timeout: int = 300) -> subprocess.CompletedProcess:
    # stdin is always given (empty by default): a stock Hermes prompt answers "no" instead of waiting on the
    # installer's terminal.
    env = ["-e", f"HERMES_HOME={CONTAINER_HERMES_HOME}", "-e", f"HOME={CONTAINER_HERMES_HOME}", "-e", "NO_COLOR=1"]
    return _docker("exec", "-i", "-u", user, *env, container, *argv, stdin=stdin, timeout=timeout)


def _python(container: str, user: str) -> list[str]:
    """Hermes' own interpreter inside the container (it carries ruamel.yaml): the published runtime command when
    the install has one, else the image's sealed venv. Same discovery as `wizard link`."""
    probe = "json.tool"
    p = _exec(container, user, ["hermes", "--print-runtime-command", "--module", probe], timeout=60)
    try:
        cmd = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else None
    except (ValueError, IndexError):
        cmd = None
    run = f"runpy.run_module('{probe}', run_name='__main__', alter_sys=True)"
    if isinstance(cmd, list) and cmd and isinstance(cmd[-1], str) and cmd[-1].count(run) == 1:
        return cmd[:-1] + [cmd[-1].replace(run, f"exec({CONTAINER_SCRIPT!r})")]
    return [VENV_PYTHON, "-c", CONTAINER_SCRIPT]


def _script(container: str, user: str, python: list[str], req: dict) -> dict:
    req = {"hermes_home": CONTAINER_HERMES_HOME, **req}
    p = _exec(container, user, python, stdin=json.dumps(req))
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = None
    if p.returncode or not isinstance(out, dict):
        # The last stderr line is the container interpreter's error; it never holds the token (stdin only).
        tail = (p.stderr or "").strip().splitlines()
        raise FreshError("agent_config_script_failed", tail[-1][:200] if tail else f"exit {p.returncode}")
    if out.get("ok") is False:
        raise FreshError(out.get("error", "agent_config_refused"), out.get("detail", ""))
    return out


def _hermes(container: str, user: str, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return _exec(container, user, ["hermes", *args], timeout=timeout)


# ---------------------------------------------------------------- generated user-config overlays

SERVICE_OVERLAY = """\
# Written by wam (fresh mode): the Agent Service's gateway token and its route to the host lifecycle bridge, whose
# container listener binds only this box's address on the Runtipi network (docs/lifecycle-bridge.md, "Fresh mode").
# Tokens are in the 0600 app.env next to this file. Remove with `wam fresh-agent disconnect`.
services:
  {app}:
    image: wizard-shell:mac-mvp-20261013
    pull_policy: never
    extra_hosts:
      - "{bridge_host}:{bridge_ip}"
    environment:
      WIZARD_UX_PREFIX: wizard-os-home{mac_apps_env}
      WIZARD_MCP_TOKEN: ${{WIZARD_MCP_TOKEN}}
      WIZARD_APP_MANAGER_URL: http://{bridge_host}:{bridge_port}
      WIZARD_APP_MANAGER_TOKEN: ${{WIZARD_APP_MANAGER_TOKEN}}
      WIZARD_APP_MANAGER_HOSTS: {bridge_host}
      WIZARD_APP_TRUSTED_PROXY_IPS: {bridge_ip}{owner}{mac_apps_ports}
"""

AGENT_OVERLAY = """\
# Written by wam (fresh mode): the container Hermes reads the shared apps.json, app skills and wizard-job-* pointers
# (read-only) that the App Manager rewrites on every app change; the wizard-os plugin finds them via WIZARD_OS_HOME.
# Remove with `wam fresh-agent disconnect`.
services:
  {engine}:
    image: ghcr.io/humanitylabs-org/hermes-agent:v2026.9.24-wizard-slim1
    pull_policy: missing
    extra_hosts:
      - "wizard-vm:host-gateway"
    environment:
      WIZARD_OS_HOME: {os_home}
      WIZARD_APPS_MCP_ADDRESS: internal
    volumes:
      - ${{ROOT_FOLDER_HOST}}/app-data/wizard-os:{os_home}:ro{agent_ssh}
"""


SHELL_OVERLAY = """\
# Written by wam (fresh mode): the Wizard shell (UX only) on the host network, listening on 127.0.0.1 only, reaching
# the Agent Service on its 127.0.0.1-published port. Tailscale Serve (the owner's private route) points at this
# loopback port; no container can reach it. The Service sees its Runtipi gateway peer, which it trusts only with the
# named owner login (the Service overlay), so an app container forging that header is refused at both doors.
# The shell holds no token. {adapter} runs the recipe's own entrypoint on adapted copies of its own config.
# Remove with `wam fresh-agent disconnect`.
services:
  {app}:
    network_mode: host
    networks: !reset []
    ports: !reset []
    entrypoint: ["/bin/sh", "/etc/wizard-shell-adapter/{adapter}"]
    environment:
      WIZARD_SHELL_LISTEN: "127.0.0.1:{port}"
      WIZARD_AGENT_SERVICE_ORIGIN: http://127.0.0.1:{service_port}
    volumes:
      - ${{ROOT_FOLDER_HOST}}/user-config/{store}/{app}/{adapter}:/etc/wizard-shell-adapter/{adapter}:ro
"""

# Derived at every start from the image's own files, so recipe routing (API, voice, streaming, health, the box-only
# gateway rule) stays the recipe's. Three changes, each checked; a template without the anchors refuses to start.
SHELL_ADAPTER_SH = r"""#!/bin/sh
# Written by wam (fresh mode). The wizard-ux recipe's entrypoint, run on adapted copies of its own config:
#  1. loopback ingress only: the listen address must be 127.0.0.1:<port>, and nginx allows 127.0.0.1 alone;
#  2. /wizard-os-home/store-data/ (the Store's read-only data, GET/HEAD) goes to the Agent Service, not static files;
#  3. the shell may frame its own pages (frame-ancestors 'self', X-Frame-Options SAMEORIGIN): the Store window.
set -eu
case "${WIZARD_SHELL_LISTEN:-}" in 127.0.0.1:*) ;; *) echo "wam adapter: WIZARD_SHELL_LISTEN must be 127.0.0.1:<port>" >&2; exit 3;; esac
src=/etc/wizard-shell; dst=/tmp/wizard-shell-adapted
rm -rf "$dst"; mkdir -p "$dst"; cp "$src"/* "$dst"/
t="$dst/nginx.conf.template"; s="$dst/static.conf"
grep -q '^    server_name _;$' "$t" && grep -q '^    location /wizard-os-home/ {$' "$t" \
  && grep -q "frame-ancestors 'none'" "$s" && grep -q 'X-Frame-Options "DENY"' "$s" \
  || { echo "wam adapter: the shell recipe's nginx files changed; regenerate the adapter" >&2; exit 3; }
sed -e 's|^    server_name _;$|&\
    allow 127.0.0.1;\
    deny all;|' \
  -e 's|^    location /wizard-os-home/ {$|    location ^~ /wizard-os-home/store-data/ {\
      limit_except GET { deny all; }\
      proxy_pass $wizard_api$request_uri; include /tmp/wizard-proxy.conf;\
    }\
&|' "$t" > "$t.new" && mv "$t.new" "$t"
sed -e "s|frame-ancestors 'none'|frame-ancestors 'self'|" -e 's|X-Frame-Options "DENY"|X-Frame-Options "SAMEORIGIN"|' "$s" > "$s.new" && mv "$s.new" "$s"
[ "$(grep -c '^    allow 127.0.0.1;$' "$t")" = 1 ] && [ "$(grep -c 'store-data' "$t")" = 1 ] && grep -q "frame-ancestors 'self'" "$s" \
  || { echo "wam adapter: adaptation did not apply" >&2; exit 3; }
sed "s|/etc/wizard-shell/|$dst/|g" /usr/local/bin/wizard-shell-entrypoint > "$dst/entrypoint.sh"
exec /bin/sh "$dst/entrypoint.sh"
"""


def app_port(runtipi_root: Path, store: str, app_id: str) -> int:
    """The app's host port: Runtipi's APP_PORT for an installed app, else its recipe's config.json port."""
    envp = runtipi_root / "app-data" / store / app_id / "app.env"
    if envp.is_file():
        for ln in envp.read_text().splitlines():
            k, _, v = ln.partition("=")
            if k.strip() == "APP_PORT" and v.strip().isdigit():
                return int(v.strip())
    cfg = registry._json_file(runtipi_root / "repos" / store / "apps" / app_id / "config.json")
    port = cfg.get("port")
    return int(port) if isinstance(port, int) and 0 < port < 65536 else DEFAULT_PORTS[app_id]


def _env_text(path: Path, values: dict[str, str]) -> str:
    lines = path.read_text().splitlines() if path.is_file() else []
    keep = [ln for ln in lines if ln.split("=", 1)[0].strip() not in values]
    return "\n".join(keep + [f"{k}={v}" for k, v in values.items()]) + "\n"


def _write_overlay(runtipi_root: Path, store: str, app_id: str, compose: str, env: dict[str, str]) -> bool:
    """One app's stock Runtipi user-config: our compose file (refusing to replace one wam didn't write) and our keys
    in its app.env (0600), keeping any others there (e.g. an image pin). Returns whether anything changed."""
    d = runtipi_root / "user-config" / store / app_id
    d.mkdir(parents=True, exist_ok=True)
    cp, envp = d / "docker-compose.yml", d / "app.env"
    if cp.is_symlink() or envp.is_symlink():
        raise FreshError("overlay_symlink", str(d))
    if cp.is_file() and not cp.read_text().startswith(MARK):
        raise FreshError("overlay_not_ours", f"{cp} was not written by wam; merge the fresh-mode settings by hand")
    changed = False
    if not cp.is_file() or cp.read_text() != compose:
        registry.atomic_write(cp, compose.encode())
        changed = True
    if env:
        text = _env_text(envp, env)
        if not envp.is_file() or envp.read_text() != text or envp.stat().st_mode & 0o077:
            registry.atomic_write(envp, text.encode(), 0o600)
            changed = True
    return changed


def _remove_overlay(runtipi_root: Path, store: str, app_id: str, keys: tuple[str, ...]) -> bool:
    d = runtipi_root / "user-config" / store / app_id
    cp, envp = d / "docker-compose.yml", d / "app.env"
    removed = False
    if cp.is_file() and not cp.is_symlink() and cp.read_text().startswith(MARK + " (fresh mode)"):
        cp.unlink()
        removed = True
        adapter = d / SHELL_ADAPTER
        if adapter.is_file() and not adapter.is_symlink() and MARK in adapter.read_text():
            adapter.unlink()
    if keys and envp.is_file() and not envp.is_symlink():
        lines = [ln for ln in envp.read_text().splitlines() if ln.split("=", 1)[0].strip() not in keys]
        if lines:
            registry.atomic_write(envp, ("\n".join(lines) + "\n").encode(), 0o600)
        else:
            envp.unlink()
        removed = True
    if d.is_dir() and not any(d.iterdir()):
        d.rmdir()
    return removed


def write_service_overlay(runtipi_root: Path, store: str, app_id: str, home: Path) -> bool:
    st = load(home)
    if not st or app_id not in SERVICE_APPS:
        return False
    from .lifecycle import token_file   # late: lifecycle imports this module
    b = st["bridge"]
    owner = st.get("owner") or {}
    if not owner.get("logins"):   # a trusted gateway peer without a named owner would be refused by the Service
        raise FreshError("owner_login_required", "run `wam fresh-agent prepare` with WIZARD_APP_OWNER_LOGINS set")
    # Mac MVP opts into a separate browser origin via its guest ingress. Other boxes keep their existing route.
    mac_apps = registry._json_file(home / 'app-manager.json').get('mac_apps_origin')
    if mac_apps and mac_apps != 'http://localhost:48023':
        raise FreshError('invalid_mac_apps_origin', 'Mac apps origin must be http://localhost:48023')
    compose = SERVICE_OVERLAY.format(app=app_id, bridge_host=BRIDGE_HOST, bridge_ip=b["ip"], bridge_port=b["port"],
                                     owner=alongside.owner_env(owner["logins"], owner.get("hosts")),
                                     mac_apps_env='\n      WIZARD_APPS_PORT: "8788"\n      WIZARD_APPS_ORIGIN: http://localhost:48023' if mac_apps else '',
                                     mac_apps_ports='\n    ports:\n      - "127.0.0.1:8798:8788"' if mac_apps else '')
    return _write_overlay(runtipi_root, store, app_id, compose, {
        "WIZARD_MCP_TOKEN": alongside.gateway_token(home),
        "WIZARD_APP_MANAGER_TOKEN": token_file(home / "lifecycle-token")})


def write_shell_overlay(runtipi_root: Path, store: str, home: Path) -> bool:
    """The modular shell beside wizard-agent-service: loopback-only on the host network, the Service on its loopback
    port (the Service overlay trusts that gateway peer for the named owner only), plus the generated nginx adapter."""
    st = load(home)
    if not st:
        return False
    if not (st.get("owner") or {}).get("logins"):
        raise FreshError("owner_login_required", "run `wam fresh-agent prepare` with WIZARD_APP_OWNER_LOGINS set")
    d = runtipi_root / "user-config" / store / SHELL_APP
    d.mkdir(parents=True, exist_ok=True)
    adapter = d / SHELL_ADAPTER
    if adapter.is_symlink():
        raise FreshError("overlay_symlink", str(adapter))
    changed = False
    if not adapter.is_file() or adapter.read_text() != SHELL_ADAPTER_SH:
        registry.atomic_write(adapter, SHELL_ADAPTER_SH.encode(), 0o644)
        changed = True
    compose = SHELL_OVERLAY.format(app=SHELL_APP, store=store, adapter=SHELL_ADAPTER,
                                   port=app_port(runtipi_root, store, SHELL_APP),
                                   service_port=app_port(runtipi_root, store, "wizard-agent-service"))
    return _write_overlay(runtipi_root, store, SHELL_APP, compose, {}) or changed


def write_agent_overlay(runtipi_root: Path, store: str, home: Path) -> bool:
    if not load(home):
        return False
    (runtipi_root / "app-data" / "wizard-os" / "skills").mkdir(parents=True, exist_ok=True)
    # A box that gives the agent a terminal on the box itself (Wizard for Mac: ssh into the VM) keeps that key in
    # $WIZARD_OS_HOME/agent-ssh, mounted read-only. Never under the app's data folder: Runtipi runs
    # `chmod -R a+rwx` there on every start/stop, and ssh then refuses the key.
    ssh_dir = home / AGENT_SSH_DIR
    agent_ssh = f"\n      - {ssh_dir}:{CONTAINER_AGENT_SSH}:ro" if ssh_dir.is_dir() and _SAFE_PATH.fullmatch(str(ssh_dir)) else ""
    return _write_overlay(runtipi_root, store, AGENT_APP,
                          AGENT_OVERLAY.format(engine=ENGINE, os_home=CONTAINER_OS_HOME, agent_ssh=agent_ssh), {})


def write_install_overlay(runtipi_root: Path, store: str, app_id: str, home: Path) -> bool:
    """Called just before an install (cli.install, the lifecycle bridge) so the app starts already wired."""
    if not load(home) or alongside.load(home):
        return False
    if app_id == AGENT_APP:
        return write_agent_overlay(runtipi_root, store, home)
    if app_id == SHELL_APP:
        return write_shell_overlay(runtipi_root, store, home)
    return write_service_overlay(runtipi_root, store, app_id, home)


# ---------------------------------------------------------------- commands

def prepare(c) -> dict:
    if alongside.load(c.home):
        raise FreshError("alongside_mode", "this box runs alongside its own Hermes; fresh-agent is for fresh boxes")
    # The Service trusts the Runtipi gateway peer (Tailscale Serve -> 127.0.0.1-published port) only for a named
    # owner login: from $WIZARD_APP_OWNER_LOGINS or this box's own tailnet identity. No owner, no fresh mode.
    owner = alongside.owner_identity()
    if not owner["logins"]:
        raise FreshError("owner_login_required", "set WIZARD_APP_OWNER_LOGINS=<your tailnet login> (or sign this "
                         "box into Tailscale) so the Agent Service knows its owner behind Tailscale Serve")
    alongside.gateway_token(c.home)
    from .lifecycle import token_file
    token_file(c.home / "lifecycle-token")
    ip = network_gateway(os.environ.get("WAM_RUNTIPI_NETWORK", RUNTIPI_NETWORK))
    _merge_json(c.home / "app-manager.json", {"container_bridge_bind": ip, "service_app_network": "internal"})
    prev = load(c.home) or {}
    st = {**prev, "mode": "fresh", "bridge": {"host": BRIDGE_HOST, "ip": ip, "port": BRIDGE_PORT}, "owner": owner}
    save(c.home, st)
    (c.runtipi_root / "app-data" / "wizard-os" / "skills").mkdir(parents=True, exist_ok=True)
    return {"ok": True, "mode": "fresh", "bridge": f"{BRIDGE_HOST} -> {ip}:{BRIDGE_PORT}",
            "next": "systemctl --user enable --now wizard-app-lifecycle-containers.service; wam fresh-agent connect"}


def _installed(c) -> tuple[object, set[str]]:
    from .cli import installed_ids
    rt = c.runtipi()
    return rt, installed_ids(rt)


CONTAINER_UNIT = "wizard-app-lifecycle-containers.service"


def _systemctl_user(*args: str) -> str:
    try:
        p = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
        return "ok" if p.returncode == 0 else f"exit {p.returncode}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return type(e).__name__


def service_publications(store: str, app_id: str) -> list[str]:
    """Host addresses the Service app's containers publish on (`docker ps` Ports). The Service trusts its gateway
    peer for a named owner only, but a wildcard publication would let an app container hairpin to it."""
    p = _docker("ps", "--filter", f"label=com.docker.compose.project={app_id}_{store}", "--format", "{{.Ports}}")
    out = []
    for part in ",".join(p.stdout.split("\n")).split(","):
        part = part.strip()
        if "->" in part:
            out.append(part.split("->")[0].rsplit(":", 1)[0].strip("[]"))
    return out


def _restart(rt, store: str, app_id: str) -> str:
    urn = f"{app_id}:{store}"
    rt.req("POST", f"/app-lifecycle/{urn}/restart", {}, timeout=120)
    time.sleep(RESTART_SETTLE)
    return rt.wait_status(urn, {"running"}, timeout=600)


def _inspect(container: str, user: str, python: list[str], gateway: str | None = None) -> dict:
    return _script(container, user, python, {"mode": "inspect", "skills_dir": f"{CONTAINER_OS_HOME}/skills",
                                             "gateway": gateway or ""})


def connect(c, restart: bool = True) -> dict:
    from .cli import STORE
    st = load(c.home)
    if not st:
        raise FreshError("not_prepared", "run `wam fresh-agent prepare` first")
    if alongside.load(c.home):
        raise FreshError("alongside_mode")
    rt, have = _installed(c)
    if AGENT_APP not in have:
        raise FreshError("agent_not_installed", "install hermes-agent (wam bootstrap) first")
    services = [a for a in SERVICE_APPS if a in have]
    if not services:
        raise FreshError("service_not_installed", "install wizard-agent-service (or wizard-shell) first")
    service = services[0]   # one gateway: the modular Service when present, else the legacy bundle
    gateway = gateway_url(service)
    restarts = [a for a in services if write_service_overlay(c.runtipi_root, STORE, a, c.home)]
    if SHELL_APP in have and write_shell_overlay(c.runtipi_root, STORE, c.home):
        restarts.append(SHELL_APP)
    agent_changed = write_agent_overlay(c.runtipi_root, STORE, c.home)

    container = engine_container(STORE)
    user = _engine_user(container)
    python = _python(container, user)
    seen = _inspect(container, user, python)
    rec = dict(st.get("connected") or {})
    if seen["gateways"] and set(seen["gateways"]) != {MCP_NAME}:
        raise FreshError("duplicate_gateway", "another MCP server already points at a Wizard gateway: "
                         + ", ".join(sorted(seen["gateways"])))
    want = plugin_hashes()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    plugin_missing = not seen["plugin_files"].get("plugin.yaml")
    plugin_off = PLUGIN_NAME not in (seen.get("plugins_enabled") or [])
    backup = None
    if plugin_missing or plugin_off:   # stock plugin commands may rewrite config.yaml: back it up first
        backup = _script(container, user, python, {"mode": "backup", "stamp": stamp})["backup"]
    if plugin_missing:
        pin = plugin_pin()
        p = _hermes(container, user, "plugins", "install", os.environ.get("WIZARD_PLUGIN_SOURCE") or pin["plugin_source"],
                    "--ref", os.environ.get("WIZARD_PLUGIN_REF") or pin["commit"], "--no-enable", "--no-deps")
        if p.returncode:
            raise FreshError("plugin_install_failed", "\n".join((p.stderr or p.stdout).strip().splitlines()[-2:])[:300])
        rec["plugin_installed"] = True
        st["connected"] = rec
        save(c.home, st)   # recorded at once, so a later failure still lets `disconnect` remove it
    got = _inspect(container, user, python)["plugin_files"]
    if any(got.get(n) != h for n, h in want.items()):
        if plugin_missing:   # ours, from this run: take it out again
            _script(container, user, python, {"mode": "remove_plugin"})
            rec.pop("plugin_installed", None)
            st["connected"] = rec
            save(c.home, st)
        raise FreshError("plugin_mismatch", "the installed wizard-os plugin differs from the pinned files "
                         "(wam/vendor/SOURCE.json); not enabled")
    if plugin_off:
        rec.setdefault("plugins_enabled_before", seen.get("plugins_enabled_raw"))
        st["connected"] = rec
        save(c.home, st)
        p = _hermes(container, user, "plugins", "enable", PLUGIN_NAME)
        if p.returncode and "invalid choice" in (p.stderr or ""):
            # An older Hermes without `plugins enable`: the allow-list edit `wizard link` makes.
            _script(container, user, python, {"mode": "enable_plugin", "plugin": PLUGIN_NAME})
        elif p.returncode:
            raise FreshError("plugin_enable_failed", "\n".join((p.stderr or p.stdout).strip().splitlines()[-2:])[:300])
    # The token goes to the container over stdin only and lands in the Hermes .env (0600), like `wizard link`.
    out = _script(container, user, python, {"mode": "connect", "gateway": gateway, "mcp_name": MCP_NAME,
                                            "skills_dir": f"{CONTAINER_OS_HOME}/skills", "stamp": stamp,
                                            "token": alongside.gateway_token(c.home)})
    backup = backup or out.get("backup")
    rec["edits"] = (rec.get("edits") or []) + [e for e in out["edits"]
                                                if e["key"] not in {x["key"] for x in rec.get("edits") or []}]
    if out["env"] in ("added", "replaced"):
        rec["env_token_added"] = True
    agent_changed = agent_changed or plugin_missing or plugin_off or bool(out["edits"]) or out["env"] != "same"
    rec.update(service_app=service, gateway=gateway, backup=backup or rec.get("backup"), at=stamp)
    st["connected"] = rec
    save(c.home, st)
    restarted = {}
    if restart:
        for a in restarts:
            restarted[a] = _restart(rt, STORE, a)
        if agent_changed:
            restarted[AGENT_APP] = _restart(rt, STORE, AGENT_APP)
    res = status(c)
    return {**res, "changed": {"service_overlays": restarts, "agent": agent_changed}, "restarted": restarted,
            "next": "" if restart or not (restarts or agent_changed) else
            "restart the changed apps (wam app restart <id>) so they load the new settings"}


def status(c) -> dict:
    """Read-only: exactly one Wizard gateway in the container Hermes, the shared skills linked, the pinned plugin
    enabled, the token in its .env, the Service overlay present, and one live tools/list through the gateway."""
    from .cli import STORE
    st = load(c.home) or {}
    rows: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        rows.append({"check": name, "ok": bool(ok), "detail": detail})

    check("fresh mode prepared", bool(st), str(c.home / STATE_FILE))
    service = (st.get("connected") or {}).get("service_app")
    check("connected to a Service app", bool(service), service or "run `wam fresh-agent connect`")
    gateway = gateway_url(service) if service else ""
    live = None
    try:
        container = engine_container(STORE)
        user = _engine_user(container)
        python = _python(container, user)
        seen = _inspect(container, user, python, gateway)
        check("exactly one Wizard gateway", seen["gateways"] == [MCP_NAME] and seen.get("wizard_url") == gateway,
              f"{seen['gateways']} -> {seen.get('wizard_url')}")
        check("shared skills linked", bool(seen.get("skills_linked")), f"{CONTAINER_OS_HOME}/skills in skills.external_dirs")
        check("shared skills mounted", seen.get("skills_visible") is not None,
              f"{len(seen.get('skills_visible') or [])} wizard-job-* pointers visible")
        pinned = all(seen["plugin_files"].get(n) == h for n, h in plugin_hashes().items())
        check("wizard-os plugin pinned and enabled", pinned and PLUGIN_NAME in (seen.get("plugins_enabled") or []),
              plugin_pin()["commit"][:12])
        check("gateway token in the Hermes .env", bool(seen.get("env_token")), f"{TOKEN_ENV} (value not shown)")
        if gateway:
            probe = _script(container, user, python, {"mode": "probe", "gateway": gateway})
            live = {k: probe.get(k) for k in ("status", "tools", "app_tools")}
            check("live tools/list through the gateway", probe.get("status") == 200 and bool(probe.get("tools")),
                  f"HTTP {probe.get('status')}, {probe.get('tools')} tools")
    except FreshError as e:
        check("hermes-agent container reachable", False, f"{e.code}: {e.detail}"[:200])
    if service:
        ovr = c.runtipi_root / "user-config" / STORE / service / "docker-compose.yml"
        check("Service overlay (token + bridge route)", ovr.is_file() and BRIDGE_HOST in ovr.read_text(), str(ovr))
        check("Service overlay names the owner", ovr.is_file() and "WIZARD_APP_OWNER_LOGINS" in ovr.read_text(),
              ",".join((st.get("owner") or {}).get("logins") or []) or "missing")
        try:
            pubs = service_publications(STORE, service)
            check("Service published on 127.0.0.1 only", all(h in ("127.0.0.1", "::1") for h in pubs),
                  ", ".join(pubs) or "not published on the host")
        except FreshError as e:
            check("Service published on 127.0.0.1 only", False, e.code)
    shell = c.runtipi_root / "user-config" / STORE / SHELL_APP / "docker-compose.yml"
    try:
        shell_installed = SHELL_APP in _installed(c)[1]
    except (RuntipiError, OSError):
        shell_installed = False
    if shell_installed or shell.is_file():
        text = shell.read_text() if shell.is_file() else ""
        check("Shell overlay (loopback only, Service loopback port)", "network_mode: host" in text
              and '"127.0.0.1:' in text and "WIZARD_AGENT_SERVICE_ORIGIN: http://127.0.0.1:" in text, str(shell))
    bind = registry._json_file(c.home / "app-manager.json").get("container_bridge_bind")
    check("bridge container listener declared", bool(bind), f"{bind}:{BRIDGE_PORT}" if bind else "missing")
    return {"ok": all(r["ok"] for r in rows), "checks": rows, "gateway": gateway, "live": live}


def disconnect(c, restart: bool = True) -> dict:
    from .cli import STORE
    st = load(c.home)
    if not st:
        return {"ok": True, "mode": None, "message": "fresh mode not prepared; nothing to undo"}
    rec = st.get("connected") or {}
    reversed_keys: list[str] = []
    if rec:
        container = engine_container(STORE)
        user = _engine_user(container)
        python = _python(container, user)
        edits = list(rec.get("edits") or [])
        if "plugins_enabled_before" in rec:
            before = rec["plugins_enabled_before"]
            edits.insert(0, {"key": "plugins.enabled", "before": {"present": before is not None, "value": before}})
        out = _script(container, user, python, {"mode": "disconnect", "edits": edits,
                                                "env_remove": bool(rec.get("env_token_added")),
                                                "remove_plugin": bool(rec.get("plugin_installed")),
                                                "stamp": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())})
        reversed_keys = out.get("reversed") or []
    removed = [a for a in (*SERVICE_APPS, SHELL_APP, AGENT_APP) if _remove_overlay(
        c.runtipi_root, STORE, a, ("WIZARD_MCP_TOKEN", "WIZARD_APP_MANAGER_TOKEN") if a in SERVICE_APPS else ())]
    _merge_json(c.home / "app-manager.json", {}, drop=("container_bridge_bind", "service_app_network"))
    (c.home / STATE_FILE).unlink()
    # Without a declared container_bridge_bind the second bridge exits at start; don't leave it restart-looping.
    unit = _systemctl_user("disable", "--now", CONTAINER_UNIT)
    restarted = {}
    if restart:
        rt, have = _installed(c)
        for a in removed:
            if a in have:
                restarted[a] = _restart(rt, STORE, a)
    return {"ok": True, "reversed": reversed_keys, "overlays_removed": removed, "restarted": restarted,
            "container_listener": unit}


def main(c, action: str, restart: bool = True) -> dict:
    try:
        if action == "prepare":
            return prepare(c)
        if action == "connect":
            return connect(c, restart)
        if action == "status":
            return status(c)
        return disconnect(c, restart)
    except FreshError as e:
        return {"ok": False, "error": e.code, "detail": e.detail}
    except RuntipiError as e:
        return {"ok": False, "error": "runtipi_unavailable", "detail": str(e)[:200]}


# ---------------------------------------------------------------- inside the hermes-agent container
# Runs with Hermes' interpreter (ruamel.yaml), request JSON on stdin, one JSON line out. Config edits are minimal-diff
# (the `wizard link` editor: only changed lines are spliced into the original bytes) and atomic; a backup of
# config.yaml and .env goes to $HERMES_HOME/backups/wizard-os/<stamp>/ (0700) before the first change.

CONTAINER_SCRIPT = r'''
import difflib, hashlib, io, json, os, shutil, sys, urllib.error, urllib.request
req = json.load(sys.stdin)
home = req["hermes_home"]
cfgp, envp = os.path.join(home, "config.yaml"), os.path.join(home, ".env")
TOKEN_ENV, GATEWAY_PATH = "WIZARD_MCP_TOKEN", "/wizard/mcp"


def done(obj):
    print(json.dumps(obj))
    sys.exit(0)


def env_lines():
    try:
        with open(envp, encoding="utf-8") as f:
            return f.read().splitlines(True)
    except FileNotFoundError:
        return []


def env_name(line):
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    return s.split("=", 1)[0].strip().removeprefix("export ").strip()


def env_value(name):
    for ln in env_lines():
        if env_name(ln) == name:
            return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def write_atomic(path, text, mode):
    tmp = path + ".wizard-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def backup(stamp):
    d = os.path.join(home, "backups", "wizard-os", stamp)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    for p in (cfgp, envp):
        if os.path.isfile(p) and not os.path.exists(os.path.join(d, os.path.basename(p))):
            shutil.copy2(p, d)
    return d


if req["mode"] == "probe":
    token = env_value(TOKEN_ENV) or ""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": "Bearer " + token, "MCP-Protocol-Version": "2025-06-18"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(msg, sid=None):
        h = dict(headers, **({"Mcp-Session-Id": sid} if sid else {}))
        r = opener.open(urllib.request.Request(req["gateway"], data=json.dumps(msg).encode(), headers=h), timeout=20)
        body = r.read().decode("utf-8", "replace")
        if "event-stream" in r.headers.get("Content-Type", ""):
            body = "".join(l[5:].strip() for l in body.splitlines() if l.startswith("data:")) or "{}"
        return r.status, r.headers.get("Mcp-Session-Id"), json.loads(body or "{}")
    try:
        _, sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "wam-fresh-agent", "version": "1"}}})
        status, _, res = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
        names = [t.get("name") for t in (res.get("result") or {}).get("tools") or []]
        done({"ok": True, "status": status, "tools": len(names), "app_tools": [n for n in names if "__" in str(n)][:20]})
    except urllib.error.HTTPError as e:
        done({"ok": True, "status": e.code, "tools": 0, "app_tools": []})
    except Exception as e:
        done({"ok": True, "status": None, "tools": 0, "app_tools": [], "error": type(e).__name__})

if req["mode"] == "backup":
    done({"ok": True, "backup": backup(req["stamp"])})

if req["mode"] == "remove_plugin":
    shutil.rmtree(os.path.join(home, "plugins", "wizard-os"), ignore_errors=True)
    done({"ok": True})

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
y = YAML()
y.preserve_quotes = True
y.width = 4096
y.indent(mapping=2, sequence=4, offset=2)
src = open(cfgp, encoding="utf-8").read() if os.path.isfile(cfgp) else ""
doc = y.load(src) if src.strip() else None
doc = doc if doc is not None else CommentedMap()


def dump(d):
    o = io.StringIO()
    y.dump(d, o)
    return o.getvalue()


base = dump(doc)


def plain(v):
    return json.loads(json.dumps(v, default=str))


def get(key):
    cur = doc
    for k in key.split("."):
        if not isinstance(cur, dict) or k not in cur:
            raise KeyError(key)
        cur = cur[k]
    return cur


def setv(key, value):
    parts, cur = key.split("."), doc
    for k in parts[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = CommentedMap()
        cur = cur[k]
    cur[parts[-1]] = value


def unset(key):
    parts, cur, chain = key.split("."), doc, [doc]
    for k in parts[:-1]:
        if not isinstance(cur, dict) or k not in cur:
            return
        cur = cur[k]
        chain.append(cur)
    if isinstance(cur, dict) and parts[-1] in cur:
        del cur[parts[-1]]
        for i in range(len(parts) - 1, 0, -1):
            if isinstance(chain[i], dict) and not chain[i]:
                del chain[i - 1][parts[i - 1]]
            else:
                break


def save():
    new = dump(doc)
    if new == base:
        return False
    O, B, N = src.splitlines(True), base.splitlines(True), new.splitlines(True)
    if len(O) == len(B):
        out = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, B, N, autojunk=False).get_opcodes():
            out += O[i1:i2] if tag == "equal" else N[j1:j2]
        text = "".join(out)
    else:
        text = new
    mode = os.stat(cfgp).st_mode & 0o777 if os.path.exists(cfgp) else 0o600
    write_atomic(cfgp, text, mode)
    return True


def gateways():
    m = doc.get("mcp_servers")
    if not isinstance(m, dict):
        return {}
    return {n: str(v.get("url", "")) for n, v in m.items() if isinstance(v, dict) and GATEWAY_PATH in str(v.get("url", ""))}


def str_list(key):
    try:
        v = get(key)
    except KeyError:
        return None
    return plain(v) if isinstance(v, list) else v


if req["mode"] == "inspect":
    g = gateways()
    dirs = str_list("skills.external_dirs")
    enabled = str_list("plugins.enabled")
    pdir = os.path.join(home, "plugins", "wizard-os")
    files = {}
    for n in ("__init__.py", "kernel.py", "plugin.yaml"):
        p = os.path.join(pdir, n)
        files[n] = hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.isfile(p) and not os.path.islink(p) else None
    sk = req.get("skills_dir") or ""
    visible = sorted(n for n in os.listdir(sk) if n.startswith("wizard-job-")) if sk and os.path.isdir(sk) else None
    done({"ok": True, "gateways": sorted(g), "wizard_url": g.get("wizard"),
          "skills_linked": isinstance(dirs, list) and sk in dirs, "skills_visible": visible,
          "plugins_enabled": enabled if isinstance(enabled, list) else [], "plugins_enabled_raw": enabled,
          "plugin_files": files, "env_token": bool(env_value(TOKEN_ENV))})

edits = []


def change(key, value):
    try:
        before = {"present": True, "value": plain(get(key))}
    except KeyError:
        before = {"present": False}
    if before.get("value") == value:
        return
    edits.append({"key": key, "before": before})
    setv(key, value)


if req["mode"] == "enable_plugin":
    enabled = str_list("plugins.enabled")
    enabled = enabled if isinstance(enabled, list) else []
    if req["plugin"] not in enabled:
        change("plugins.enabled", enabled + [req["plugin"]])
    save()
    done({"ok": True, "edits": edits})

if req["mode"] == "connect":
    raw = doc.get("mcp_servers")
    if raw is not None and not isinstance(raw, dict) and raw != []:
        done({"ok": False, "error": "mcp_servers_unsupported", "detail": "mcp_servers is not a mapping"})
    g = gateways()
    name = req["mcp_name"]
    if set(g) - {name}:
        done({"ok": False, "error": "duplicate_gateway", "detail": ", ".join(sorted(g))})
    if isinstance(raw, dict) and name in raw and name not in g:
        done({"ok": False, "error": "wizard_name_taken", "detail": "an MCP server called wizard is not a Wizard gateway"})
    if raw == []:
        edits.append({"key": "mcp_servers", "before": {"present": True, "value": []}})
        doc["mcp_servers"] = CommentedMap()
    change("mcp_servers." + name, {"url": req["gateway"], "headers": {"Authorization": "Bearer ${" + TOKEN_ENV + "}"}})
    dirs = str_list("skills.external_dirs")
    if dirs is not None and not isinstance(dirs, list):
        done({"ok": False, "error": "external_dirs_unsupported", "detail": "skills.external_dirs is not a list"})
    if req["skills_dir"] not in (dirs or []):
        change("skills.external_dirs", (dirs or []) + [req["skills_dir"]])
    current = env_value(TOKEN_ENV)
    saved = None
    if edits or current != req["token"]:
        saved = backup(req["stamp"])
    save()
    state = "same"
    if current != req["token"]:
        keep = [ln for ln in env_lines() if env_name(ln) != TOKEN_ENV]
        if keep and not keep[-1].endswith("\n"):
            keep[-1] += "\n"
        write_atomic(envp, "".join(keep) + TOKEN_ENV + "=" + req["token"] + "\n", 0o600)
        state = "replaced" if current is not None else "added"
    done({"ok": True, "edits": edits, "env": state, "backup": saved})

if req["mode"] == "disconnect":
    backup(req["stamp"])
    done_keys = []
    for e in reversed(req["edits"]):
        if e["before"]["present"]:
            setv(e["key"], e["before"]["value"])
        else:
            unset(e["key"])
        done_keys.append(e["key"])
    save()
    if req.get("env_remove"):
        keep = [ln for ln in env_lines() if env_name(ln) != TOKEN_ENV]
        if os.path.isfile(envp):
            write_atomic(envp, "".join(keep), 0o600)
    if req.get("remove_plugin"):
        shutil.rmtree(os.path.join(home, "plugins", "wizard-os"), ignore_errors=True)
    done({"ok": True, "reversed": done_keys})

done({"ok": False, "error": "unknown_mode"})
'''
