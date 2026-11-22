"""The agent runtime helper on every fresh box (wizard-agent-service#582, DECISIONS #115).

The Agent Service creates agents (the private agent that private first makes when Tinfoil connects, any agent the
owner adds) through a small host helper, `ops/agent-runtime/agent_runtime.py` in wizard-agent-service: the only
piece that touches Docker and Runtipi for agents, because the Service never gets the Docker socket. Until now only
hand-provisioned boxes had it, so on a standard install every agent creation failed with `runtime_unavailable`.

`wam agent-runtime` (run by install.sh on every fresh install and rerun, so `wam updates --apply` brings it too):
- fetches the Agent Service commit the release pins (`components.wizard-agent-service`), or its main on an unpinned
  box, proves the checkout is exactly that commit, and copies `ops/agent-runtime` to
  ~/.local/share/wizard-agent-runtime/releases/<commit> (`current` points at it; the running helper never sees a
  half-copied tree);
- makes the helper's bearer token once (~/.wizard-os/agent-runtime/token);
- writes the systemd user unit `wizard-agent-runtime.service`: the runtipi driver with this App Manager checkout,
  the Agent Service's own agents folder, and a Unix socket (~/.wizard-os/agent-runtime/sock/runtime.sock) instead of
  a TCP port. The fresh-mode Agent Service sits in Runtipi's network and can't reach the host's loopback; a socket
  folder mounted into that one container is the narrowest way in, and nothing new listens on any network;
- writes its own marked block (`x-wizard-agent-runtime`) into the Agent Service's Runtipi user-config override:
  WIZARD_AGENT_RUNTIME_URL=unix:..., the token file (read-only), WIZARD_AGENT_UPSTREAM=network (agents are reached
  by their app name on the same network), and restarts the Agent Service when that block changed;
- leaves alone a hand-installed helper (a unit this command did not write) and alongside boxes, whose Agent Service
  runs on the host network and keeps whatever its owner set up.

WIZARD_AGENT_RUNTIME=0 skips it. Status lands in ~/.wizard-os/agent-runtime.json.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import browser_boundary as _bb

REPO = "humanitylabs-org/wizard-agent-service"
UNIT = "wizard-agent-runtime.service"
MARK = "x-wizard-agent-runtime"
MARKER = "# Written by `wam agent-runtime` (wizard-app-manager); rerun the installer to update."
ROLES = ("service", "bundle")   # the apps that run an Agent Service (alongside.role_of)
SOCKET_MOUNT = "/run/wizard-agent-runtime"
TOKEN_MOUNT = "/run/wizard-agent-runtime-token"
KEEP_RELEASES = 3
_SAFE = re.compile(r"^/[A-Za-z0-9._/@+-]+$")
_HEX = re.compile(r"[0-9a-f]{64}")


def _run(args, timeout=120, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)


def paths(home: Path | None = None, wizard_home: Path | None = None) -> dict:
    h = Path(home or Path.home())
    w = Path(wizard_home or h / ".wizard-os")
    share = h / ".local/share/wizard-agent-runtime"
    own = w / "agent-runtime"
    return {"share": share, "src": share / "src", "releases": share / "releases", "current": share / "current",
            "units": h / ".config/systemd/user", "own": own, "token": own / "token", "sock_dir": own / "sock",
            "socket": own / "sock" / "runtime.sock", "status": w / "agent-runtime.json"}


def pinned_source() -> tuple[str, str, bool]:
    """(repo, ref, pinned). A release pins the Agent Service's full commit; an unpinned box follows main."""
    from . import release_pins
    rel = release_pins.load() or {}
    row = (rel.get("components") or {}).get("wizard-agent-service") or {}
    repo, commit = str(row.get("repo") or REPO), str(row.get("commit") or "")
    if row:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("release components.wizard-agent-service needs owner/repo and a full commit")
        return repo, commit, True
    return REPO, "main", False


def fetch(repo: str, ref: str, dest: Path) -> str:
    """Exactly `ref` of a public repo in dest (detached, clean, only ops/agent-runtime checked out). Returns the commit."""
    url = os.environ.get("WAM_AGENT_RUNTIME_REPO_URL") or f"https://github.com/{repo}.git"
    if not (dest / ".git").is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        if _run(["git", "init", "-q", str(dest)]).returncode:
            raise ValueError("git init failed")
    g = ["git", "-C", str(dest)]
    _run([*g, "remote", "remove", "origin"])
    _run([*g, "remote", "add", "origin", url])
    _run([*g, "sparse-checkout", "set", "--no-cone", "/ops/agent-runtime/"])
    if (p := _run([*g, "fetch", "-q", "--depth", "1", "origin", ref], 300)).returncode:
        raise ValueError(f"could not fetch {repo} {ref[:12]}: {(p.stderr or p.stdout).strip()[-200:]}")
    if (p := _run([*g, "checkout", "-q", "--force", "--detach", "FETCH_HEAD"])).returncode:
        raise ValueError(f"checkout failed: {p.stderr.strip()[-200:]}")
    _run([*g, "clean", "-qfdx"])
    head = _run([*g, "rev-parse", "HEAD"]).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", ref) and head != ref:
        raise ValueError(f"checkout is {head[:12]}, not the pinned {ref[:12]}")
    if _run([*g, "status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        raise ValueError("checkout is not clean")
    if not (dest / "ops/agent-runtime/agent_runtime.py").is_file():
        raise ValueError("fetched tree has no ops/agent-runtime/agent_runtime.py")
    return head


def supports_socket(tree: Path) -> bool:
    """An Agent Service release older than wizard-agent-service#593 has no --socket listener."""
    try:
        return '"--socket"' in (tree / "agent_runtime.py").read_text()
    except OSError:
        return False


def supports_tinfoil_volume(tree: Path) -> bool:
    """wizard-agent-service#595: the helper can read the key the owner saved in the Tinfoil app from its volume."""
    try:
        return 'add_argument("--tinfoil-key-volume"' in (tree / "agent_runtime.py").read_text()
    except OSError:
        return False


# The Tinfoil app's fixed data volume (wizard-tinfoil runtipi/docker-compose.yml); its private/connection.json holds
# the key the owner pasted. The helper reads only that key, on every apply, and puts it in the private agent's .env.
TINFOIL_VOLUME = "wizard-tinfoil-private-data"

UNIX_CLIENT = b"unixSocketFetch"   # wizard-agent-service#593: the Service's client for a unix: runtime URL


def service_reads_unix(store: str, app_id: str, docker: list[str] | None = None) -> bool | None:
    """Whether the Agent Service this app runs can use a `unix:` runtime URL. An older image throws at start on one
    ("agent runtime must be a loopback or box-network http address") and the Service never comes up, so the block
    must only be written for an image that has the client. Reads server/agents.mjs out of the app's container
    (`docker cp` works on a stopped or restarting one too). None when it can't be told (no container yet)."""
    if docker is None:
        from .network import docker_cmd
        docker = docker_cmd()
    name = f"{app_id}_{store}-{app_id}-1"   # Runtipi's compose project naming
    try:
        p = subprocess.run([*docker, "cp", f"{name}:/app/server/agents.mjs", "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode or not p.stdout:
        return None
    return UNIX_CLIENT in p.stdout


def foreign_writer(agents: Path) -> int | None:
    """The host uid the Agent Service writes its agent folders as, when that is not this user. The Service keeps
    each agent's folder 0700 and its files 0600, so a helper running as another uid can't read them (it answers
    agent_not_found). That happens where the box owner is not uid 1000 (the Service image's user) or under rootless
    Docker. None when every file the Service wrote here is ours, or it wrote none yet."""
    me = os.getuid()
    try:
        for f in sorted(agents.iterdir()):
            if f.name.startswith(".") and f.is_file() and not f.is_symlink() and f.stat().st_uid != me:
                return f.stat().st_uid
    except OSError:
        return None
    return None


def stage_release(p: dict, commit: str) -> tuple[Path, bool]:
    """Copy ops/agent-runtime of the checkout into releases/<commit> once and point `current` at it atomically.
    (release dir, whether `current` moved)."""
    rel = p["releases"] / commit
    if not (rel / "agent_runtime.py").is_file():
        tmp = p["releases"] / f".{commit}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(p["src"] / "ops/agent-runtime", tmp, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.rmtree(rel, ignore_errors=True)
        tmp.rename(rel)
    try:
        moved = os.readlink(p["current"]) != str(rel)
    except OSError:
        moved = True
    if moved:
        link = p["share"] / ".current.tmp"
        link.unlink(missing_ok=True)
        link.symlink_to(rel)
        link.replace(p["current"])
    old = sorted((d for d in p["releases"].iterdir() if d.is_dir() and not d.name.startswith(".") and d != rel),
                 key=lambda d: d.stat().st_mtime, reverse=True)
    for d in old[KEEP_RELEASES - 1:]:
        shutil.rmtree(d, ignore_errors=True)
    return rel, moved


def ensure_token(p: dict) -> bool:
    """The helper's bearer token, made once (32 random bytes, hex). Its folder is 0700; the file is 0644 so the Agent
    Service's container user can read its own read-only mount of it, whatever host uid it maps to. True when it was
    (re)made: a new file is a new inode, so the running helper (token in memory) and the Agent Service (a bind mount
    of the old inode) both have to start again to see it."""
    t = p["token"]
    t.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(t.parent, 0o700)
    try:
        if _HEX.fullmatch(t.read_text().strip()):
            return False
    except OSError:
        pass
    tmp = t.with_name(t.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(32) + "\n")
    os.chmod(tmp, 0o644)
    tmp.replace(t)
    return True


def agents_dir(runtipi_root: Path, store: str, app_id: str) -> Path:
    """The fresh-mode Agent Service's own agents folder on the host (its /state/wizard-os/agents)."""
    return Path(runtipi_root) / "app-data" / store / app_id / "data" / "wizard-os" / "agents"


def unit_text(p: dict, python: str, wam_dir: Path, agents: Path, runtipi_root: Path, tinfoil_volume: str = "") -> str:
    for v in (python, str(wam_dir), str(agents), str(runtipi_root), str(p["token"]), str(p["socket"]), str(p["current"])):
        if not _SAFE.fullmatch(v):
            raise ValueError(f"path {v!r} has characters a unit file can't carry safely")
    current, token, sock = p["current"], p["token"], p["socket"]
    return f"""{MARKER}
# The agent runtime helper (wizard-agent-service ops/agent-runtime): creates and runs agent containers for the
# Agent Service, which reaches it only through the Unix socket below (mounted into its container).
[Unit]
Description=WizardOS agent runtime helper (agent containers for the Agent Service)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
Environment=RUNTIPI_ROOT={runtipi_root}
Environment=PYTHONDONTWRITEBYTECODE=1
WorkingDirectory={current}
ExecStart={python} {current}/agent_runtime.py --driver runtipi --wam {wam_dir} --agents-dir {agents} --token-file {token} --socket {sock} --home-mount alongside{f" --tinfoil-key-volume {tinfoil_volume}" if tinfoil_volume else ""}
Restart=on-failure
RestartSec=3
UMask=0077
# No NoNewPrivileges, seccomp or path sandboxing: a user unit may reach Docker through setgid `sg docker`, which
# those options block (wizard-agent-service ops/host-container/README.md).

[Install]
WantedBy=default.target
"""


def hand_installed(p: dict) -> bool:
    f = p["units"] / UNIT
    try:
        return f.is_file() and MARKER not in f.read_text()
    except OSError:
        return True


def overlay(p: dict, service: str) -> dict:
    """The Agent Service's block: where the helper's socket and token are, and agents on the shared network."""
    for v in (str(p["sock_dir"]), str(p["token"])):
        if not _SAFE.fullmatch(v):
            raise ValueError(f"path {v!r} has characters a compose file can't carry safely")
    return {"service": service,
            "environment": {"WIZARD_AGENT_RUNTIME_URL": f"unix:{SOCKET_MOUNT}/runtime.sock",
                            "WIZARD_AGENT_RUNTIME_TOKEN_FILE": TOKEN_MOUNT,
                            "WIZARD_AGENT_UPSTREAM": "network"},
            "volumes": [f"{p['sock_dir']}:{SOCKET_MOUNT}:ro", f"{p['token']}:{TOKEN_MOUNT}:ro"]}


def sync_app(home, runtipi_root, store: str, app_id: str, wizard_home=None) -> bool:
    """Keep the block in an Agent Service app's override while this command manages the helper (called when the app
    is (re)installed, so a reinstall never drops it). True when the file changed."""
    p = paths(home, wizard_home)
    try:
        st = json.loads(p["status"].read_text())
    except (OSError, ValueError):
        return False
    if st.get("state") != "ready" or st.get("app") != app_id:
        return False
    return _bb.write_overlay(_bb.override_path(runtipi_root, store, app_id), overlay(p, app_id), MARK)


def healthy(p: dict, timeout: float = 3.0) -> bool:
    """GET /healthz over the socket with the token answers 200."""
    try:
        token = p["token"].read_text().strip()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(p["socket"]))
        s.sendall(f"GET /healthz HTTP/1.0\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n\r\n".encode())
        head = s.recv(64).decode("latin-1", "replace")
        s.close()
        return head.startswith("HTTP/1.") and head.split(" ", 2)[1] == "200"
    except (OSError, IndexError):
        return False


def install(wizard_home: Path, runtipi_root: Path, app_ids, store: str = "wizardapps", alongside_box: bool = False,
            apply: bool = True, home: Path | None = None, systemctl=None, wait_s: float = 10.0,
            reads_unix=None) -> dict:
    """Install or update the helper and wire the one Agent Service app to it. Returns the status (also saved)."""
    import time
    p = paths(home, wizard_home)
    sc = systemctl or (lambda *a: _run(["systemctl", "--user", *a], 60))

    def done(state, **kw):
        res = {"state": state, **kw}
        if apply:
            p["status"].parent.mkdir(parents=True, exist_ok=True)
            tmp = p["status"].with_name(p["status"].name + ".tmp")
            tmp.write_text(json.dumps(res, indent=2) + "\n")
            tmp.replace(p["status"])
        return res

    if os.environ.get("WIZARD_AGENT_RUNTIME", "").strip() == "0":
        return done("skipped", reason="WIZARD_AGENT_RUNTIME=0")
    if alongside_box:
        return done("skipped", reason="alongside box: its Agent Service runs on the host and keeps its own runtime setup")
    ids = sorted(app_ids)
    if len(ids) != 1:
        return done("skipped", reason=f"needs exactly one installed Agent Service app, found {ids or 'none'}")
    app_id = ids[0]
    if hand_installed(p):
        return done("kept", app=app_id, reason=f"a hand-installed {UNIT} exists; left exactly as it is")
    ok = (reads_unix or service_reads_unix)(store, app_id)
    if not ok:
        # Never point an Agent Service at a socket it can't read: it would not start at all. Take back a block an
        # earlier run wrote (a newer image went back), and wire it on the first run after the image has the client.
        try:
            removed = apply and _bb.write_overlay(_bb.override_path(runtipi_root, store, app_id), None, MARK)
        except ValueError as e:
            return done("failed", app=app_id, reason=str(e)[:300])
        why = ("its Agent Service could not be read (not installed or no container yet)" if ok is None else
               "the installed Agent Service is older than wizard-agent-service#593 and can't use the helper's socket")
        return done("skipped", app=app_id, reason=f"{why}; rerun after the Agent Service updates",
                    override_changed=bool(removed))
    other = foreign_writer(agents_dir(runtipi_root, store, app_id))
    if other is not None:
        # Wiring it anyway would look connected while every agent it creates stays unreadable to the helper; a
        # plain refusal keeps private first's runtime_unavailable answer visible and logged instead.
        try:
            removed = apply and _bb.write_overlay(_bb.override_path(runtipi_root, store, app_id), None, MARK)
        except ValueError as e:
            return done("failed", app=app_id, reason=str(e)[:300])
        return done("failed", app=app_id, override_changed=bool(removed),
                    reason=(f"the Agent Service writes its agent folders as uid {other} and this user is uid {os.getuid()}, "
                            "so the helper could not read them; not wired"))
    try:
        repo, ref, pinned = pinned_source()
    except (ValueError, OSError) as e:
        return done("failed", app=app_id, reason=str(e))
    if len(str(p["socket"]).encode()) > 100:
        return done("failed", app=app_id, reason=f"the socket path {p['socket']} is too long for a Unix socket")
    if not apply:
        return done("dry-run", app=app_id, repo=repo, ref=ref, pinned=pinned)
    try:
        commit = fetch(repo, ref, p["src"])
    except (ValueError, OSError, subprocess.TimeoutExpired) as e:
        return done("failed", app=app_id, reason=str(e)[:300])
    if not supports_socket(p["src"] / "ops/agent-runtime"):
        return done("skipped", app=app_id, commit=commit,
                    reason="this release's Agent Service has no socket listener yet (wizard-agent-service#593)")
    try:
        p["releases"].mkdir(parents=True, exist_ok=True)
        _rel, moved = stage_release(p, commit)
        token_new = ensure_token(p)
        p["sock_dir"].mkdir(parents=True, exist_ok=True)
        os.chmod(p["sock_dir"], 0o711)   # the container user only needs to reach the socket inside
        text = unit_text(p, sys.executable, Path(__file__).resolve().parents[1], agents_dir(runtipi_root, store, app_id), Path(runtipi_root),
                         TINFOIL_VOLUME if supports_tinfoil_volume(p["current"]) else "")
    except (ValueError, OSError) as e:
        return done("failed", app=app_id, commit=commit, reason=str(e)[:300])
    p["units"].mkdir(parents=True, exist_ok=True)
    unit = p["units"] / UNIT
    unit_changed = not unit.is_file() or unit.read_text() != text
    if unit_changed:
        tmp = unit.with_name(UNIT + ".tmp")
        tmp.write_text(text)
        tmp.replace(unit)
    sc("daemon-reload")
    if (r := sc("enable", UNIT)).returncode:
        return done("failed", app=app_id, commit=commit, reason=f"systemctl enable failed: {(r.stderr or r.stdout).strip()[-200:]}")
    sc("restart" if unit_changed or moved or token_new else "start", UNIT)
    deadline = time.monotonic() + wait_s
    up = healthy(p)
    while not up and time.monotonic() < deadline:
        time.sleep(0.5)
        up = healthy(p)
    if not up:
        return done("failed", app=app_id, commit=commit,
                    reason=f"the helper did not answer on its socket (systemctl --user status {UNIT})")
    try:
        changed = _bb.write_overlay(_bb.override_path(runtipi_root, store, app_id), overlay(p, app_id), MARK)
    except ValueError as e:
        return done("failed", app=app_id, commit=commit, reason=str(e)[:300])
    # a new token reaches a running Agent Service only through a recreate (its bind mount still holds the old file)
    return done("ready", app=app_id, commit=commit, pinned=pinned, override_changed=changed or token_new,
                token_replaced=token_new, socket=str(p["socket"]))


def describe(res: dict) -> str:
    line = f"agent runtime: {res.get('state')}"
    if res.get("commit"):
        line += f" (wizard-agent-service {res['commit'][:12]})"
    if res.get("reason"):
        line += f": {res['reason']}"
    if res.get("restart"):
        line += f"; Agent Service {res['restart']}"
    return line
