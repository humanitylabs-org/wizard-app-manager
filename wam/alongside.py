"""Alongside mode: Wizard OS next to an owner's existing Hermes, which is never moved or migrated.

wizard-os spec/AGENT_BRIDGE.md, "Installing over an existing Hermes". In this mode:
- the `agent` parking spot is filled by the host's own Hermes ("external: host Hermes"), so `wam bootstrap`
  never installs the `hermes-agent` app;
- the shell (Agent Service + Wizard UX) is pointed at that Hermes. The host Hermes API listens on the host's
  loopback (127.0.0.1:8642), which a container on a bridge network can't reach (on Linux `host-gateway` is the
  docker0 address, not 127.0.0.1). So the shell runs on the host network, bound to 127.0.0.1 only, and reads chats
  over the Hermes API, as the owner's uid. By default it sees only the agent's images folder (<home>/cache/images),
  not the rest of the Hermes home. `--share-history` opts in to mounting that whole home read-only (agent thread titles, other-surface activity and the archived
  list need state.db); that mount also lets the shell read .env, auth.json and any other credential there. This is a stock Runtipi
  per-app user-config override (<runtipi>/user-config/<store>/<app>/docker-compose.yml + app.env), so the app
  recipe in the index stays the same for every box;
- the Hermes API key is read from the owner's Hermes and handed to the shell through Runtipi's install form and
  the 0600 user-config app.env. It is never printed.

wam itself never edits the owner's Hermes. The Hermes-side edits (the `wizard` MCP entry, the skills dir, the
plugin, the API key when Hermes has none yet) are `wizard link`'s, recorded in ~/.wizard-os/linked.json so
`wizard unlink` reverses exactly them.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import urllib.request
from pathlib import Path

import yaml

from .runtipi import read_env

AGENT_SLOT = "agent"
SHELL_SLOT = "shell"
EXTERNAL_ID = "host-hermes"
DEFAULT_ORIGIN = "http://127.0.0.1:8642"
STATE_FILE = "alongside.json"


# ---------------------------------------------------------------- the host's Hermes

def hermes_home(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def hermes_binary() -> str | None:
    found = shutil.which("hermes")
    if found:
        return found
    cand = Path.home() / ".local" / "bin" / "hermes"
    return str(cand) if cand.is_file() and os.access(cand, os.X_OK) else None


def _api_block(cfg: dict) -> tuple[dict, dict]:
    plats = cfg.get("platforms")
    plat = plats.get("api_server") if isinstance(plats, dict) else None
    plat = plat if isinstance(plat, dict) else {}
    extra = plat.get("extra")
    return plat, (extra if isinstance(extra, dict) else {})


def _config(home: Path) -> dict:
    try:
        return yaml.safe_load((home / "config.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _env(home: Path) -> dict[str, str]:
    p = home / ".env"
    return read_env(p) if p.is_file() else {}


def api_origin(home: Path) -> str:
    """Where the host Hermes API server listens: config.yaml platforms.api_server (host/port, or under extra), then
    API_SERVER_HOST/API_SERVER_PORT in its .env, else stock 127.0.0.1:8642. A wildcard bind is reached on loopback."""
    plat, extra = _api_block(_config(home))
    env = _env(home)
    host = str(plat.get("host") or extra.get("host") or env.get("API_SERVER_HOST") or "127.0.0.1")
    port = str(plat.get("port") or extra.get("port") or env.get("API_SERVER_PORT") or "8642")
    if host in ("0.0.0.0", "::", "", "localhost"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def api_key(home: Path) -> str:
    """The host Hermes API key (API_SERVER_KEY in its .env or process env, or platforms.api_server key). '' if none."""
    plat, extra = _api_block(_config(home))
    for v in (_env(home).get("API_SERVER_KEY"), plat.get("key"), extra.get("key"), os.environ.get("API_SERVER_KEY")):
        if isinstance(v, str) and len(v.strip()) >= 16 and not v.strip().startswith("${"):
            return v.strip()
    return ""


def api_up(origin: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"{origin}/health", timeout=timeout) as r:  # noqa: S310 - loopback
            return r.status < 500
    except Exception:  # noqa: BLE001
        return False


def detect(home: Path | None = None) -> dict:
    """Is there an existing Hermes on this box? (a `hermes` binary plus <home>/config.yaml). Never returns secrets."""
    home = home or hermes_home()
    binary = hermes_binary()
    origin = api_origin(home)
    found = bool(binary) and (home / "config.yaml").is_file()
    return {"found": found, "hermes_home": str(home), "binary": binary, "config": (home / "config.yaml").is_file(),
            "api_origin": origin, "api_up": api_up(origin) if found else False, "api_key_present": bool(api_key(home))}


# ---------------------------------------------------------------- Wizard OS side state

def state_path(wizard_home: Path) -> Path:
    return wizard_home / STATE_FILE


def load(wizard_home: Path) -> dict | None:
    try:
        st = json.loads(state_path(wizard_home).read_text())
        return st if st.get("mode") == "alongside" else None
    except (OSError, ValueError):
        return None


def save(wizard_home: Path, st: dict) -> Path:
    p = state_path(wizard_home)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v for k, v in st.items() if k != "detected"}, indent=2) + "\n")
    tmp.replace(p)
    return p


def gateway_token(wizard_home: Path) -> str:
    """The `wizard` MCP gateway token, shared by the shell (WIZARD_MCP_TOKEN) and `wizard link` (the Hermes entry).
    Created once, 0600."""
    p = wizard_home / "mcp-token"
    try:
        t = p.read_text().strip()
        if t:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t + "\n")
    return t


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket()
    try:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0
    finally:
        s.close()


DEFAULT_SHELL_PORT = 8796     # the shell's loopback port (wizard-ux; before the split, the bundled wizard-shell)
DEFAULT_SERVICE_PORT = 8797   # wizard-agent-service's runtipi/config.json port

# What an app is to alongside mode (DECISIONS #36): the Agent Service talks to the host Hermes; the shell (wizard-ux)
# talks only to the Agent Service; the pre-split bundle (wizard-shell) is both in one container.
LEGACY_BUNDLE = "wizard-shell"
SERVICE_SLOT = "agent-service"
# Image pin per app (a release's digest, or a local build under test): env var -> stored in alongside.json "images".
PIN_VARS = {LEGACY_BUNDLE: "WIZARD_SHELL_IMAGE", "wizard-ux": "WIZARD_UX_IMAGE",
            "wizard-agent-service": "WIZARD_AGENT_SERVICE_IMAGE"}


def pin(st: dict, app_id: str) -> str:
    pins = st.get("images") or {}
    if app_id == LEGACY_BUNDLE and not pins:   # a pre-split alongside.json: its one pin was the bundle's
        return str((st.get("shell") or {}).get("image") or "")
    return str(pins.get(app_id) or "")


def image_lines(st: dict, app_id: str) -> str:
    image = pin(st, app_id)
    return f"\n    image: {image}\n    pull_policy: missing" if image else ""


def set_service_active(wizard_home: Path, active: bool) -> None:
    """After the split the Agent Service answers the agent's gateway; after a rollback the bundle does again."""
    st = load(wizard_home)
    if st:
        st.setdefault("service", {"port": DEFAULT_SERVICE_PORT})["active"] = bool(active)
        save(wizard_home, st)



def role_of(app_id: str, provides: list | None) -> str | None:
    """'bundle', 'service', 'shell' or None (not a Wizard shell/service app)."""
    p = set(provides or [])
    if app_id == LEGACY_BUNDLE or {SHELL_SLOT, SERVICE_SLOT} <= p:
        return "bundle"
    if SERVICE_SLOT in p:
        return "service"
    if SHELL_SLOT in p:
        return "shell"
    return None


def pick_port(start: int = DEFAULT_SHELL_PORT, avoid: set[int] | None = None) -> int:
    for p in range(start, start + 200):
        if p not in (avoid or set()) and port_free(p):
            return p
    raise SystemExit(f"no free loopback port from {start}")


def enable(wizard_home: Path, home: Path, shell_port: int | None = None, share_history: bool | None = None) -> dict:
    """Record alongside mode. Idempotent; keeps an earlier shell port. The shell gets a fixed loopback port so the
    agent's `wizard` MCP entry (written by `wizard link`) can name it before the shell is installed."""
    det = detect(home)
    if not det["found"]:
        raise SystemExit(f"no existing Hermes here (need the `hermes` command and {home / 'config.yaml'})")
    prev = load(wizard_home) or {}
    shell_port = int(shell_port or (prev.get("shell") or {}).get("port") or pick_port())
    service_port = int((prev.get("service") or {}).get("port") or pick_port(DEFAULT_SERVICE_PORT, {shell_port}))
    st = {
        "mode": "alongside",
        "agent": {"external": True, "id": EXTERNAL_ID, "name": "host Hermes", "hermes_home": str(home),
                  "origin": det["api_origin"], "uid": os.getuid(), "gid": os.getgid()},
        "shell": {"port": shell_port,
                  # opt-in only; a re-run keeps the owner's earlier choice
                  "share_history": bool((prev.get("shell") or {}).get("share_history")
                                        if share_history is None else share_history)},
    }
    # The Agent Service gets its own fixed loopback port; the shell reaches it there (both on the host network).
    # `active` says which app answers the agent's `wizard` gateway: the Agent Service after the split, else the shell.
    st["service"] = {"port": service_port, "active": bool((prev.get("service") or {}).get("active"))}
    # Image pins (a release's digest, or a local build under test), one per app, kept across re-runs.
    pins = dict(prev.get("images") or {})
    old = (prev.get("shell") or {}).get("image")
    if old and not pins:   # before the split one pin served the bundle: it stays the bundle's
        pins[LEGACY_BUNDLE] = old
    for app, var in PIN_VARS.items():
        if os.environ.get(var):
            pins[app] = os.environ[var]
    for app, image in pins.items():
        if not re.fullmatch(r"[A-Za-z0-9._/:@-]+", str(image)):
            raise SystemExit(f"{PIN_VARS.get(app, app)} {image!r} is not an image reference")
    if pins:
        st["images"] = pins
    gateway_token(wizard_home)
    save(wizard_home, st)
    return {**st, "detected": det}


def disable(wizard_home: Path) -> bool:
    p = state_path(wizard_home)
    if p.is_file():
        p.unlink()
        return True
    return False


def external_slots(wizard_home: Path) -> dict[str, dict]:
    """Slots filled from outside Runtipi, for apps.json and the parking-spot table."""
    st = load(wizard_home)
    if not st:
        return {}
    a = st["agent"]
    return {AGENT_SLOT: {"external": True, "id": a["id"], "name": a["name"], "origin": a["origin"]}}


def shared_secret(wizard_home: Path, var: str) -> str | None:
    """The shell's WIZARD_HERMES_API_KEY comes from the host Hermes (the hermes-agent app's, in fresh mode)."""
    st = load(wizard_home)
    if not st or var.removeprefix("WIZARD_") not in ("HERMES_API_KEY", "API_SERVER_KEY"):
        return None
    return api_key(Path(st["agent"]["hermes_home"])) or None


# ---------------------------------------------------------------- the shell override (stock Runtipi user-config)

SHELL_OVERRIDE = """\
# Written by wam (alongside mode): the Wizard shell runs against this box's own Hermes, which listens on the host's
# loopback. Host network, bound to 127.0.0.1 only; chats are read over the Hermes API. {access}
# Remove with `wam alongside --off`, then `wam app update {app}`.
services:
  {app}:{image}{user}
    network_mode: host
    networks: !reset []
    ports: !reset []
    environment:
      WIZARD_HOST: 127.0.0.1
      WIZARD_PORT: "{port}"
      HERMES_ORIGIN: {origin}{state_env}
      WIZARD_APPS_MCP_ADDRESS: url
      WIZARD_MCP_TOKEN: ${{WIZARD_MCP_TOKEN}}{volumes}
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:{port}/wizard/health >/dev/null || exit 1"]
"""

_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/@+-]+$")


UX_OVERRIDE = """\
# Written by wam (alongside mode): the Wizard shell (UX only) on the host network, bound to 127.0.0.1, talking to the
# Agent Service on its own loopback port. It holds no Hermes credentials and mounts nothing.
# Remove with `wam alongside --off`, then `wam app update {app}`.
services:
  {app}:{image}
    network_mode: host
    networks: !reset []
    ports: !reset []
    environment:
      WIZARD_SHELL_LISTEN: "127.0.0.1:{port}"
      WIZARD_AGENT_SERVICE_ORIGIN: http://127.0.0.1:{service_port}
"""


def ux_override(st: dict, app_id: str) -> str:
    return UX_OVERRIDE.format(
        app=app_id, port=int(st["shell"]["port"]), service_port=service_port(st), image=image_lines(st, app_id))


def service_port(st: dict) -> int:
    return int((st.get("service") or {}).get("port") or DEFAULT_SERVICE_PORT)


def shell_override(st: dict, app_id: str, wizard_home: Path | None = None, role: str = "bundle") -> str:
    """The override for the app that talks to the host Hermes: the Agent Service, or the pre-split bundle (which
    also serves the UX, so it keeps the shell's port)."""
    if role == "shell":
        return ux_override(st, app_id)
    port = service_port(st) if role == "service" else int(st["shell"]["port"])
    a = st["agent"]
    if not _SAFE_PATH.match(a["hermes_home"]):
        raise SystemExit(f"Hermes home path {a['hermes_home']!r} has characters a compose file can't carry safely")
    if not re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}", a["origin"]):
        raise SystemExit(f"host Hermes API {a['origin']} is not a loopback origin; alongside mode needs 127.0.0.1")
    share = bool(st["shell"].get("share_history"))
    hh = a["hermes_home"]
    # Without the opt-in the shell gets only the folder of images the agent made (no secrets), so pictures in
    # chats still show; the recipe's state.db path is blanked so the Agent Service opens no Hermes file.
    # Default: the shell's /hermes is a small wam-owned folder (replacing the recipe's /hermes mount) that holds
    # only an empty cache/images mount point, with the owner's images folder mounted on it read-only. The mount
    # point must already exist: Docker can't create it inside a read-only /hermes. A new Hermes makes
    # cache/images on its first picture, after the shell started, so it is created here (empty, owner-owned).
    if share:
        volumes = f"\n    volumes:\n      - {hh}:/hermes:ro"
    else:
        view = Path(wizard_home or Path(hh).parent / ".wizard-os") / "hermes-view"
        if not _SAFE_PATH.match(str(view)):
            raise SystemExit(f"Wizard OS home path {str(view)!r} has characters a compose file can't carry safely")
        try:
            (view / "cache" / "images").mkdir(parents=True, exist_ok=True)
            Path(hh, "cache", "images").mkdir(parents=True, exist_ok=True)
            volumes = (f"\n    volumes:\n      - {view}:/hermes:ro"
                       f"\n      - {hh}/cache/images:/hermes/cache/images:ro")
        except OSError:
            volumes = f"\n    volumes:\n      - {view}:/hermes:ro" if view.is_dir() else ""
    return SHELL_OVERRIDE.format(
        app=app_id, origin=a["origin"], port=port,
        access=("The owner opted in to sharing session history (--share-history): the Hermes home is mounted\n"
                "# read-only, so the shell can read every file there, credentials included."
                if share else "The shell sees only the agent's images folder, not the Hermes home\n"
                "# (opt in to session history: wam alongside --share-history)."),
        image=image_lines(st, app_id),
        user=f'\n    user: "{int(a["uid"])}:{int(a["gid"])}"',
        state_env=f'\n      WIZARD_HERMES_STATE_PATH: "{"/hermes/state.db" if share else ""}"',
        volumes=volumes)


def write_shell_override(runtipi_root: Path, store: str, app_id: str, wizard_home: Path,
                         role: str = "bundle") -> Path | None:
    """Write the user-config override for a shell, Agent Service or pre-split bundle app (`role`, see role_of).
    Returns its path, or None outside alongside mode."""
    st = load(wizard_home)
    if not st:
        return None
    d = runtipi_root / "user-config" / store / app_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "docker-compose.yml").write_text(shell_override(st, app_id, wizard_home, role))
    if role == "shell":   # the UX holds no gateway token
        (d / "app.env").unlink(missing_ok=True)
        return d / "docker-compose.yml"
    envp = d / "app.env"
    fd = os.open(envp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"WIZARD_MCP_TOKEN={gateway_token(wizard_home)}\n")
    os.chmod(envp, 0o600)
    return d / "docker-compose.yml"


def remove_shell_override(runtipi_root: Path, store: str, app_id: str) -> bool:
    d = runtipi_root / "user-config" / store / app_id
    if d.is_dir():
        shutil.rmtree(d)
        return True
    return False
