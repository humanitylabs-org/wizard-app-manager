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
    pinned = os.environ.get("WIZARD_HERMES_BIN")   # get.sh: the found Hermes when it is not on PATH
    if pinned and os.access(pinned, os.X_OK):
        return pinned
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


def reconcile_images(st: dict, apps=None) -> None:
    """Selected release -> alongside pins; explicit test overrides win, unpinned runs keep their pins.

    Keep the previous reference on transitions (including an explicit downgrade), not on no-op reruns.
    A receipt is deliberately not a policy input: only WIZARD_RELEASE_PINS selects a release.
    """
    from . import release_pins

    doc = release_pins.load()
    pins = dict(st.get("images") or {})
    if not pins and (old := (st.get("shell") or {}).get("image")):
        pins[LEGACY_BUNDLE] = old
    previous = dict(st.get("previous_images") or {})
    for app in (PIN_VARS if apps is None else apps):
        selected = (doc.get("images") or {}).get(app)
        image = os.environ.get(PIN_VARS[app]) or (release_pins.image_ref(selected) if selected else "")
        if not image:
            continue
        if not re.fullmatch(r"[A-Za-z0-9._/:@-]+", image):
            raise ValueError(f"{PIN_VARS[app]}: invalid image reference")
        if pins.get(app) and pins[app] != image:
            previous[app] = pins[app]
        pins[app] = image
    if pins:
        st["images"] = pins
    if previous:
        st["previous_images"] = previous


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


def enable(wizard_home: Path, home: Path, shell_port: int | None = None, share_history: bool | None = None,
           share_sessions: bool | None = None) -> dict:
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
                                        if share_history is None else share_history),
                  # narrower opt-in: only the session database (helper status, other-surface activity, archive)
                  "share_sessions": bool((prev.get("shell") or {}).get("share_sessions")
                                         if share_sessions is None else share_sessions)},
    }
    # The Agent Service gets its own fixed loopback port; the shell reaches it there (both on the host network).
    # `active` says which app answers the agent's `wizard` gateway: the Agent Service after the split, else the shell.
    st["service"] = {"port": service_port, "active": bool((prev.get("service") or {}).get("active"))}
    # Stored pins are a fallback, never stronger than the selected release. Preserve rollback references.
    st["images"] = dict(prev.get("images") or {})
    if not st["images"] and (old := (prev.get("shell") or {}).get("image")):
        st["images"][LEGACY_BUNDLE] = old
    if prev.get("previous_images"):
        st["previous_images"] = dict(prev["previous_images"])
    reconcile_images(st)
    for app, image in st["images"].items():
        if not re.fullmatch(r"[A-Za-z0-9._/:@-]+", str(image)):
            raise SystemExit(f"{PIN_VARS.get(app, app)} {image!r} is not an image reference")
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
    # Files initializes /media as 1000:1000, mode 775, including on later starts.
    # Keep the host identity for Hermes; add only the shared-media group, never DAC_OVERRIDE.
    group_add:
      - "1000"
    network_mode: host
    networks: !reset []
    ports: !reset []
    environment:
      WIZARD_HOST: 127.0.0.1
      WIZARD_PORT: "{port}"
      WIZARD_SETUP_MODE: alongside
      HERMES_ORIGIN: {origin}{state_env}{media_env}{artifact_env}{stt_env}
      WIZARD_APPS_MCP_ADDRESS: url
      WIZARD_MCP_TOKEN: ${{WIZARD_MCP_TOKEN}}{volumes}
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:{port}/wizard/health >/dev/null || exit 1"]
"""

_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/@+-]+$")


def host_media_root(runtipi_root: Path) -> Path:
    """Where this box's Hermes sees the user-files folder the shell mounts at /media.

    The recipe mounts ${ROOT_FOLDER_HOST}/media; Runtipi records ROOT_FOLDER_HOST in its .env (the CLI's install
    folder when it is absent). The host Hermes is a plain host process, so it reads attachments by that host path,
    never by the container's /media."""
    from .runtipi import read_env
    envf = Path(runtipi_root) / ".env"
    root = ((read_env(envf).get("ROOT_FOLDER_HOST") if envf.is_file() else "") or "").strip() or str(runtipi_root)
    return Path(root).expanduser().absolute() / "media"


# The Hermes session database and its WAL sidecars. A WAL-mode SQLite reader needs all three, and they must be the
# live files: SQLite keeps -wal/-shm while any Hermes process has the database open.
SESSION_STATE_FILES = ("state.db", "state.db-wal", "state.db-shm")
INODE_MARK = "wam-session-inodes:"


def session_state_mounts(hermes_home: str, view: Path, *, prepare: bool = True) -> list[tuple[str, str]]:
    """File bind mounts for --share-sessions: [(host file, /hermes/<name>)]. Only files that exist are mounted
    (Docker would otherwise create a root-owned directory at the missing host path, inside the owner's Hermes home).
    Each needs an empty mount-point file in the read-only view. Without state.db nothing is mounted."""
    if not Path(hermes_home, "state.db").is_file():
        return []
    out = []
    for name in SESSION_STATE_FILES:
        src = Path(hermes_home, name)
        if not src.is_file() or src.is_symlink() or not _SAFE_PATH.match(str(src)):
            continue
        point = view / name
        try:
            if prepare and not point.exists():
                point.touch(mode=0o600)
        except OSError:
            continue
        if point.is_file():
            out.append((str(src), f"/hermes/{name}"))
    return out


def session_inodes(hermes_home: str) -> dict[str, int]:
    out = {}
    for name in SESSION_STATE_FILES:
        try:
            out[name] = os.stat(Path(hermes_home, name)).st_ino
        except OSError:
            pass
    return out


def inode_stamp(hermes_home: str) -> str:
    """Recorded in the override so `wam doctor` can tell when Hermes replaced a mounted file (a file bind mount
    keeps the old inode: after Hermes deletes and recreates its -wal/-shm on a full restart, the service would read
    a stale snapshot until `wam app update` remounts them)."""
    return INODE_MARK + json.dumps(session_inodes(hermes_home), sort_keys=True, separators=(",", ":"))


def stale_session_mounts(override_text: str, hermes_home: str) -> list[str]:
    """Names whose host inode no longer matches the one the override was written with ([] = fresh or not shared)."""
    for line in override_text.splitlines():
        if INODE_MARK in line:
            try:
                then = json.loads(line.split(INODE_MARK, 1)[1].strip())
            except ValueError:
                return list(SESSION_STATE_FILES)
            now = session_inodes(hermes_home)
            return sorted(n for n in set(then) | set(now) if then.get(n) != now.get(n))
    return []


# Folders where the host Hermes writes files it sends back with MEDIA:<host path> (pictures, voice replies,
# screenshots, videos). The Agent Service gets each one read-only at the SAME path, so the path the agent reports
# is the path the service opens; under /hermes it would not match and every picture/voice reply was dropped.
AGENT_OUTPUT_DIRS = ("cache/images", "cache/audio", "cache/screenshots", "cache/videos")


def docker_rootless() -> bool:
    """Rootless Docker maps the owner's uid to uid 0 in the container (and the owner's 1000 to a subuid)."""
    import subprocess
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix:///run/user/"):
        return True
    try:
        out = subprocess.run(["docker", "info", "--format", "{{json .SecurityOptions}}"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        # no docker on PATH (e.g. a minimal PATH): a per-user socket and no system one means rootless
        return Path(f"/run/user/{os.getuid()}/docker.sock").exists() and not Path("/var/run/docker.sock").exists()
    return "rootless" in out


def adopt_state(runtipi_root: Path, store: str, app_id: str, image: str, rootless: bool | None = None) -> bool:
    """Switching an existing Agent Service to the owner's identity on rootless Docker (container uid 0): its saved
    state was written by the recipe's uid 1000, and it can't chmod files it doesn't own, so it crash-looped on
    start. Hand the folder to 0:1000 (group-writable) once, with a throwaway container of the app's own image.
    Rootful Docker and fresh installs need nothing. Returns True when ownership was changed."""
    import subprocess
    if not (docker_rootless() if rootless is None else rootless):
        return False
    data = Path(runtipi_root) / "app-data" / store / app_id / "data"
    if not data.is_dir() or not image or not re.fullmatch(r"[A-Za-z0-9._/:@-]+", image):
        return False
    if not _SAFE_PATH.match(str(data)):
        raise SystemExit(f"app data path {str(data)!r} has characters a command can't carry safely")
    r = subprocess.run(["docker", "run", "--rm", "--pull", "missing", "--network", "none", "--user", "0:0",
                        "--entrypoint", "sh", "-v", f"{data}:/state", image, "-c",
                        "chown -R 0:1000 /state && chmod -R g+rwX /state"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise SystemExit(f"could not hand {app_id}'s saved state to the owner: {r.stderr.strip()[:200]}")
    return True


def adopt_media(runtipi_root: Path, image: str, user: str) -> bool:
    """Attachments land in the shared media folder (Inbox/<date>/, staged in .wizard-uploads/), but the Agent Service
    runs as the owner (`user`), and Runtipi made that folder for its own uid. An owner who is not that uid (any user
    but the first on rootful Docker) got EACCES on every upload, which the phone shows as "Wizard Files is
    unavailable (502)". Hand just those two Wizard folders to the service's identity (group-writable, so the uid-1000
    apps still read them), idempotently, with a throwaway container of the app's own image. Create them before a
    fresh service install too. Only the media root gets gid 1000 + group access (not recursive); Files may repair
    that root's ownership later, so the service override also retains supplementary gid 1000. Returns True when it ran."""
    # The image is the release pin, which an upgrade has not pulled yet (`wam app update` comes after this), so
    # pull the pinned image when it is missing; `--pull never` failed every upgrade with "No such image".
    import subprocess
    media = host_media_root(runtipi_root)
    if not image or not re.fullmatch(r"[A-Za-z0-9._/:@-]+", image) or not re.fullmatch(r"\d+:\d+", user):
        return False
    if not _SAFE_PATH.match(str(media)):
        raise SystemExit(f"media path {str(media)!r} has characters a command can't carry safely")
    r = subprocess.run(["docker", "run", "--rm", "--pull", "missing", "--network", "none", "--user", "0:0",
                        "--entrypoint", "sh", "-v", f"{media}:/media", image, "-c",
                        "mkdir -p /media/Inbox /media/.wizard-uploads"
                        " && chgrp 1000 /media && chmod g+rwx /media"
                        f" && chown -R {user} /media/Inbox /media/.wizard-uploads"
                        " && chmod -R u+rwX,g+rwX /media/Inbox /media/.wizard-uploads"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise SystemExit(f"could not give the Agent Service its attachment folders: {r.stderr.strip()[:200]}")
    return True


def container_user(a: dict, rootless: bool | None = None) -> str:
    """The uid that reads the owner's Hermes files. On rootful Docker that's the owner's own uid; on rootless Docker
    the owner IS container uid 0 (no capabilities), with gid 1000 so it can still write the shared /media folder
    that the other apps (uid 1000) own."""
    if docker_rootless() if rootless is None else rootless:
        return "0:1000"
    return f'{int(a["uid"])}:{int(a["gid"])}'


def transcriber_origin(runtipi_root: Path, store: str = "wizardapps") -> str | None:
    """The Transcriber on the host's loopback. On the host network the recipe's http://transcriber:8000 doesn't
    resolve, so voice showed "Voice unavailable"."""
    envp = Path(runtipi_root) / "app-data" / store / "transcriber" / "app.env"
    port = (read_env(envp).get("APP_PORT") if envp.is_file() else "") or ""
    return f"http://127.0.0.1:{int(port)}" if port.isdigit() else None


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


def shell_override(st: dict, app_id: str, wizard_home: Path | None = None, role: str = "bundle",
                   media_root: Path | None = None, stt_origin: str | None = None,
                   rootless: bool | None = None, *, prepare: bool = True) -> str:
    """The override for the app that talks to the host Hermes: the Agent Service, or the pre-split bundle (which
    also serves the UX, so it keeps the shell's port). `media_root`: the host path of the /media both recipes mount,
    handed to the host Hermes (wizard-agent-service#12); the UX-only shell mounts no media and gets none."""
    if role == "shell":
        return ux_override(st, app_id)
    port = service_port(st) if role == "service" else int(st["shell"]["port"])
    a = st["agent"]
    if not _SAFE_PATH.match(a["hermes_home"]):
        raise SystemExit(f"Hermes home path {a['hermes_home']!r} has characters a compose file can't carry safely")
    if not re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}", a["origin"]):
        raise SystemExit(f"host Hermes API {a['origin']} is not a loopback origin; alongside mode needs 127.0.0.1")
    share = bool(st["shell"].get("share_history"))
    sessions = bool(st["shell"].get("share_sessions")) and not share
    hh = a["hermes_home"]
    # Attachments: the service writes them under its /media, the host Hermes must be handed the host path of the
    # same file (trusted_path), so the agent's file tools can open what the person attached.
    media_env = ""
    if media_root is not None:
        media = os.path.normpath(str(media_root))
        if not _SAFE_PATH.match(media) or media == "/":
            raise SystemExit(f"Runtipi media path {media!r} has characters a compose file can't carry safely")
        media_env = f'\n      WIZARD_MEDIA_AGENT_ROOT: "{media}"'
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
            if prepare:
                (view / "cache" / "images").mkdir(parents=True, exist_ok=True)
                Path(hh, "cache", "images").mkdir(parents=True, exist_ok=True)
            volumes = (f"\n    volumes:\n      - {view}:/hermes:ro"
                       f"\n      - {hh}/cache/images:/hermes/cache/images:ro")
        except OSError:
            volumes = f"\n    volumes:\n      - {view}:/hermes:ro" if view.is_dir() else ""
        # --share-sessions: only the session database (and its WAL sidecars), read-only, never the rest of the home.
        mounted = session_state_mounts(hh, view, prepare=prepare) if sessions and volumes else []
        for src, dst in mounted:
            volumes += f"\n      - {src}:{dst}:ro"
        sessions = bool(mounted)
    # The agent's output folders at their real host paths (see AGENT_OUTPUT_DIRS), plus /media.
    roots = []
    for rel in AGENT_OUTPUT_DIRS:
        d = Path(hh, rel)
        try:
            if prepare:
                d.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if not volumes:
            volumes = "\n    volumes:"
        volumes += f"\n      - {d}:{d}:ro"
        roots.append(str(d))
    artifact_env = f'\n      WIZARD_AGENT_ARTIFACT_ROOTS: "{":".join(roots + ["/media"])}"' if roots else ""
    stt_env = ""
    if stt_origin:
        if not re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}", stt_origin):
            raise SystemExit(f"Transcriber address {stt_origin!r} is not a loopback origin")
        stt_env = f"\n      WIZARD_STT_ORIGIN: {stt_origin}"
    return SHELL_OVERRIDE.format(
        app=app_id, origin=a["origin"], port=port,
        access=("The owner opted in to sharing session history (--share-history): the Hermes home is mounted\n"
                "# read-only, so the shell can read every file there, credentials included."
                if share else "The owner opted in to --share-sessions: only the Hermes session database (state.db and\n"
                f"# its -wal/-shm) is mounted read-only, no credentials or other Hermes files. {inode_stamp(hh)}"
                if sessions else "The shell sees only the agent's images folder, not the Hermes home\n"
                "# (opt in to session history: wam alongside --share-sessions, or --share-history for the whole home)."),
        image=image_lines(st, app_id),
        user=f'\n    user: "{container_user(a, rootless)}"',
        state_env=f'\n      WIZARD_HERMES_STATE_PATH: "{"/hermes/state.db" if share or sessions else ""}"',
        media_env=media_env, artifact_env=artifact_env, stt_env=stt_env, volumes=volumes)


def write_shell_override(runtipi_root: Path, store: str, app_id: str, wizard_home: Path,
                         role: str = "bundle") -> Path | None:
    """Write the user-config override for a shell, Agent Service or pre-split bundle app (`role`, see role_of).
    Returns its path, or None outside alongside mode."""
    st = load(wizard_home)
    if not st:
        return None
    if app_id in PIN_VARS:
        reconcile_images(st, [app_id])
        save(wizard_home, st)
    d = runtipi_root / "user-config" / store / app_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "docker-compose.yml").write_text(shell_override(st, app_id, wizard_home, role,
                                                         None if role == "shell" else host_media_root(runtipi_root),
                                                         None if role == "shell" else transcriber_origin(runtipi_root, store)))
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
