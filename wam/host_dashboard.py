"""The host Hermes dashboard for alongside boxes.

Some apps sign in through the agent's own dashboard API (e.g. ChatGPT: Hermes' device-code login, and the
Wizard OS plugin's read-only usage route). In fresh mode they reach the Hermes Agent app by name; on an alongside
box Hermes runs on the host and has no dashboard running. This module runs stock `hermes dashboard` as a user
service and points those apps at it:

- bound to the box's Tailscale IPv4 only (rootless Docker containers reach it; the public interface does not),
- stock basic auth required (password generated once, 0600 in ~/.wizard-os/secrets/host-dashboard.env),
- each consumer app gets the address and password through its Runtipi user-config app.env (kept across updates),
- WIZARD_ACCOUNT_CHANGES=1 turns on the wizard-os plugin's account broker (add/reconnect/remove subscriptions via
  stock `hermes auth`), which the Claude app uses for any number of Claude subscriptions.

A consumer is any installed app whose recipe has the HERMES_DASHBOARD_BASIC_AUTH_PASSWORD form field; its
dashboard URL variable is whatever `${VAR:-http://hermes-agent:9119}` the recipe compose uses. No Hermes core change.

Browser boundary (WIZARD_BROWSER_BOUNDARY=1, default-off; wizard-os issue 386, approved disposition): the password is
a full agent-admin credential and the tailnet address answers every tailnet device, so instead
- Hermes is bound to 127.0.0.2:19119, a host-only address Hermes still treats as public (password + Host check stay
  on; never 127.0.0.1, where Hermes drops its auth);
- a small Wizard front (wam/host_dashboard_front.py, its own user service) takes the tailnet address the apps already
  use. It answers box peers only, refuses browser and network markers, knows each consumer app by its OWN per-app key
  (written into the app's existing dashboard-password variable, so today's images keep working) and lets it reach
  only its reviewed path allowlist (CONSUMER_PATHS);
- the front alone holds the dashboard password, which is rotated when the front takes over, so copies the apps held
  before stop working;
- the owner's browser gets the dashboard as its own tab on its own origin (decision H2 = like H1): a Tailscale Serve
  https port to the front's owner listener, owner logins only, never framed, Hermes' own login kept.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

UNIT = "wizard-host-dashboard.service"
PORT = 9119
USER = "hermes"
PASSWORD_VAR = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"
ACCOUNT_CHANGES_VAR = "WIZARD_ACCOUNT_CHANGES"
FRONT_UNIT = "wizard-host-dashboard-front.service"
UPSTREAM_HOST = "127.0.0.2"   # host-only; Hermes treats it as a public bind (auth on), unlike 127.0.0.1
UPSTREAM_PORT = 19119
OWNER_PORT = 9120             # the owner's own-tab origin (Tailscale Serve https -> 127.0.0.1:9120)
# Distinct source addresses toward Hermes, so its per-IP login limit keeps the front's own sign-in and the owner's
# sign-ins in different buckets: a mistyped owner password never locks the apps out, and the reverse.
SOURCES = {"consumers": "127.0.0.3", "owner": "127.0.0.4"}
# What each consumer app may call on the dashboard (method, exact path or `/prefix/*`). Reviewed per app from its
# engine source (wizard-app-store images/wizard-chatgpt and images/wizard-claude). An app not listed gets nothing.
CONSUMER_PATHS = {
    "wizard-chatgpt": [
        ["GET", "/api/providers/oauth"],
        ["POST", "/api/providers/oauth/openai-codex/start"],
        ["GET", "/api/providers/oauth/openai-codex/poll/*"],
        ["DELETE", "/api/providers/oauth/openai-codex"],
        ["DELETE", "/api/providers/oauth/sessions/*"],
        ["GET", "/api/model/info"],
        ["GET", "/api/model/options"],
        ["GET", "/api/model/recommended-default"],
        ["POST", "/api/model/set"],
        ["GET", "/api/plugins/wizard-os/ai-usage/chatgpt"],
        ["GET", "/api/plugins/wizard-os/models/chatgpt"],
        ["POST", "/api/plugins/wizard-os/infer/chatgpt"],
        ["GET", "/api/plugins/wizard-os/provider-accounts/chatgpt"],
        ["GET", "/api/plugins/wizard-os/provider-accounts/chatgpt/*"],
        ["POST", "/api/plugins/wizard-os/provider-accounts/chatgpt/*"],
    ],
    "wizard-claude": [
        ["GET", "/api/plugins/wizard-os/provider-accounts/claude"],
        ["GET", "/api/plugins/wizard-os/provider-accounts/claude/*"],
        ["POST", "/api/plugins/wizard-os/provider-accounts/claude/*"],
    ],
}
URL_VAR = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-|-)https?://hermes-agent:9119/?\}")


def env_path(wizard_home: Path) -> Path:
    return wizard_home / "secrets" / "host-dashboard.env"


def read_env(p: Path) -> dict[str, str]:
    out = {}
    if p.is_file():
        for line in p.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() and not k.lstrip().startswith("#"):
                out[k.strip()] = v.strip()
    return out


def applied_path(wizard_home: Path) -> Path:
    """sha256 of the environment file the running dashboard was (re)started with."""
    return wizard_home / "state" / "host-dashboard-env.sha256"


def _env_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def credentials(wizard_home: Path) -> dict[str, str]:
    """The dashboard login (created once, 0600). An existing login is kept as is, so apps keep working."""
    return _credentials(wizard_home)[0]


def _credentials(wizard_home: Path) -> tuple[dict[str, str], bool]:
    """(dashboard environment, whether the file was written). Adds only what is missing; every existing value,
    including an owner's explicit WIZARD_ACCOUNT_CHANGES=0, is kept."""
    p = env_path(wizard_home)
    env = read_env(p)
    want = dict(env)
    if not (env.get(PASSWORD_VAR) and env.get("HERMES_DASHBOARD_BASIC_AUTH_SECRET")):
        want.update({"HERMES_DASHBOARD_BASIC_AUTH_USERNAME": env.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or USER,
                     PASSWORD_VAR: env.get(PASSWORD_VAR) or secrets.token_urlsafe(24),
                     "HERMES_DASHBOARD_BASIC_AUTH_SECRET": secrets.token_hex(32)})
    # The Claude app adds, reconnects and removes the agent's own subscriptions (any number) through the wizard-os
    # plugin's broker, which runs stock `hermes auth add|remove` and ships switched off unless its dashboard host
    # turns it on. New credentials land in Hermes' own pool; the existing Claude Code sign-in is never touched.
    want.setdefault(ACCOUNT_CHANGES_VAR, "1")
    if want == env and p.is_file():
        return env, False
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.write_text("".join(f"{k}={v}\n" for k, v in want.items()))
    p.chmod(0o600)
    return want, True


def tailnet_ip() -> str | None:
    if not shutil.which("tailscale"):
        return None
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    ip = out.strip().splitlines()[0].strip() if out.strip() else ""
    return ip if re.fullmatch(r"100\.\d{1,3}\.\d{1,3}\.\d{1,3}", ip) else None


def running_env(app_id: str, store: str) -> list[dict[str, str]] | None:
    """The environment of each running container of an installed app (Runtipi's compose project `<app>_<store>`),
    or None when Docker can't be read. Values stay in memory; nothing is printed."""
    docker = shutil.which("docker")
    if not docker:
        return None
    try:
        ps = subprocess.run([docker, "ps", "-q", "--filter", f"label=com.docker.compose.project={app_id}_{store}"],
                            capture_output=True, text=True, timeout=30)
        if ps.returncode:
            return None
        ids = ps.stdout.split()
        if not ids:
            return []
        r = subprocess.run([docker, "inspect", "--format", "{{json .Config.Env}}", *ids],
                           capture_output=True, text=True, timeout=30)
        if r.returncode:
            return None
        out = []
        for line in r.stdout.splitlines():
            env = {}
            for kv in json.loads(line) or []:
                k, _, v = str(kv).partition("=")
                env[k] = v
            out.append(env)
        return out
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def stale_running(app_id: str, store: str, want: dict[str, str]) -> bool:
    """A running container of the app still carries an older value of a variable it was pointed at (e.g. the
    dashboard address from before a tailnet move). Runtipi reads app.env only when it (re)creates a container, so
    such an app needs a recreate even when this run did not change its app.env (wizard-os: Heidi install)."""
    envs = running_env(app_id, store) or []
    return any(k in env and env[k] != v for env in envs for k, v in want.items())


def consumers(runtipi_root: Path, store: str, installed: set[str]) -> dict[str, list[str]]:
    """app id -> the dashboard URL variables its recipe reads, for installed apps that sign in via the dashboard."""
    out = {}
    for app_id in sorted(installed):
        d = runtipi_root / "apps" / store / app_id
        try:
            compose = (d / "docker-compose.yml").read_text()
            config = (d / "config.json").read_text()
        except OSError:
            continue
        if PASSWORD_VAR in config or PASSWORD_VAR in compose:
            out[app_id] = sorted(set(URL_VAR.findall(compose)))
    return out


def unit_text(hermes: str, hermes_home: str, host: str, wizard_home: Path, port: int = PORT) -> str:
    where = "host-only, behind the Wizard front" if host == UPSTREAM_HOST else "tailnet IP only"
    return (f"[Unit]\nDescription=Hermes dashboard for Wizard alongside apps ({where}, password required)\n"
            f"After=network-online.target tailscaled.service\n\n[Service]\nEnvironmentFile={env_path(wizard_home)}\n"
            f"Environment=HERMES_HOME={hermes_home}\n"
            f"ExecStart={hermes} dashboard --host {host} --port {port} --skip-build --no-open --isolated\n"
            f"Restart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n")


# ---------------------------------------------------------------- the boundary front

def front_config_path(wizard_home: Path) -> Path:
    return wizard_home / "secrets" / "host-dashboard-front.json"


def mode_path(wizard_home: Path) -> Path:
    return wizard_home / "state" / "host-dashboard-mode"


def keys_dir(wizard_home: Path) -> Path:
    return wizard_home / "secrets" / "host-dashboard-keys"


_APP = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_KEY = re.compile(r"[0-9a-f]{64}")


def app_key(wizard_home: Path, app_id: str) -> str:
    """The consumer app's own dashboard key (32 random bytes, hex), created once, 0600. Never the password."""
    if not _APP.fullmatch(app_id or ""):
        raise ValueError("bad app id")
    p = keys_dir(wizard_home) / f"{app_id}.key"
    try:
        key = p.read_text().strip()
        if _KEY.fullmatch(key):
            return key
    except FileNotFoundError:
        pass
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    key = secrets.token_hex(32)
    _atomic(p, key + "\n")
    return key


def prune_keys(wizard_home: Path, live: set[str]) -> list[str]:
    """Drop the keys of apps that are no longer consumers (uninstalled); their next install gets a new key."""
    d, gone = keys_dir(wizard_home), []
    for f in sorted(d.glob("*.key")) if d.is_dir() else []:
        if f.stem not in live:
            f.unlink(missing_ok=True)
            gone.append(f.stem)
    return gone


def _atomic(p: Path, text: str) -> None:
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def rotate(wizard_home: Path) -> dict[str, str]:
    """New dashboard password and session secret (old sessions die too). Other values are kept."""
    p = env_path(wizard_home)
    env = read_env(p)
    env.update({PASSWORD_VAR: secrets.token_urlsafe(24), "HERMES_DASHBOARD_BASIC_AUTH_SECRET": secrets.token_hex(32)})
    env.setdefault("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", USER)
    env.setdefault(ACCOUNT_CHANGES_VAR, "1")
    _atomic(p, "".join(f"{k}={v}\n" for k, v in env.items()))
    return env


def front_config(host: str, creds: dict[str, str], keys: dict[str, str], owner: dict | None) -> dict:
    """The front's 0600 configuration: where it listens, the dashboard login it alone holds, and each consumer's key
    hash with its allowlist. `owner` (optional): {"hosts": [...], "logins": [...]} for the own-tab listener."""
    doc = {"listen": {"host": host, "port": PORT},
           "upstream": {"host": UPSTREAM_HOST, "port": UPSTREAM_PORT},
           "source": dict(SOURCES),
           "login": {"username": creds.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or USER,
                     "password": creds[PASSWORD_VAR]},
           "consumers": {hashlib.sha256(k.encode()).hexdigest(): {"app": a, "allow": CONSUMER_PATHS.get(a, [])}
                         for a, k in sorted(keys.items())}}
    if owner and owner.get("hosts") and owner.get("logins"):
        doc["owner"] = {"listen": {"host": "127.0.0.1", "port": OWNER_PORT},
                        "hosts": sorted(owner["hosts"]), "logins": sorted(owner["logins"])}
    return doc


def front_unit_text(wizard_home: Path, python: str | None = None) -> str:
    pkg = Path(__file__).resolve().parent.parent
    return (f"[Unit]\nDescription=Wizard front for the host Hermes dashboard (box apps by their own key; owner tab)\n"
            f"After=network-online.target tailscaled.service {UNIT}\n\n[Service]\n"
            f"Environment=PYTHONPATH={pkg}\nEnvironment=PYTHONUNBUFFERED=1\n"
            f"ExecStart={python or sys.executable} -m wam.host_dashboard_front {front_config_path(wizard_home)}\n"
            f"Restart=on-failure\nRestartSec=5\nNoNewPrivileges=true\n\n[Install]\nWantedBy=default.target\n")


def publish_owner_tab(port: int = OWNER_PORT) -> str:
    """Tailscale Serve https:<port> -> the front's owner listener. 'published', 'present', 'taken' or 'unavailable'.
    Never touches another entry: a port already serving something else is left alone."""
    from . import links
    try:
        st = links.serve_status()
    except Exception:  # noqa: BLE001 - unreadable Serve is not an empty one
        return "unavailable"
    target = f"http://127.0.0.1:{port}"
    web = st.get("Web") or {}
    for hostport, h in web.items():
        if str(hostport).endswith(f":{port}"):
            proxies = [v.get("Proxy") for v in (h.get("Handlers") or {}).values()]
            return "present" if proxies == [target] else "taken"
    if str(port) in {str(k) for k in (st.get("TCP") or {})}:
        return "taken"
    r = links._run(["tailscale", "serve", "--bg", f"--https={port}", target])
    return "published" if r.returncode == 0 else "unavailable"


def unpublish_owner_tab(port: int = OWNER_PORT) -> str:
    """Remove the owner-tab Serve entry, only when it is ours (proxies to the front's owner listener)."""
    from . import links
    try:
        st = links.serve_status()
    except Exception:  # noqa: BLE001
        return "unavailable"
    for hostport, h in (st.get("Web") or {}).items():
        if str(hostport).endswith(f":{port}"):
            if [v.get("Proxy") for v in (h.get("Handlers") or {}).values()] != [f"http://127.0.0.1:{port}"]:
                return "not ours"
            links._run(["tailscale", "serve", f"--https={port}", "off"])
            return "removed"
    return "absent"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)


def ensure(wizard_home: Path, runtipi_root: Path, store: str, installed: set[str], hermes: str | None,
           hermes_home: str, write_user_env, unit_dir: Path | None = None, apply: bool = True,
           boundary: bool | None = None, owner: dict | None = None) -> dict:
    """Run the dashboard and point every consumer app at it. Returns {state, ...}; `changed_apps` need an update.

    `boundary` (default: WIZARD_BROWSER_BOUNDARY=1 in this process) puts the Wizard front in between; `owner`
    ({"hosts": [...], "logins": [...]}) adds the owner's own-tab listener to it."""
    if boundary is None:
        from . import browser_admission
        boundary = browser_admission.enabled()
    apps = consumers(runtipi_root, store, installed)
    if not apps:
        return {"state": "skipped", "note": "no installed app signs in through the agent's dashboard"}
    host = tailnet_ip()
    if not host:
        return {"state": "error", "error": "no Tailscale IPv4 on this box; apps can't reach the host dashboard"}
    if not hermes:
        return {"state": "error", "error": "the `hermes` command was not found"}
    url = f"http://{host}:{PORT}"
    out = {"state": "ready", "url": url, "apps": sorted(apps), "changed_apps": [],
           "front": "on" if boundary else "off"}
    if not apply:
        return out
    creds, env_changed = _credentials(wizard_home)
    mode = mode_path(wizard_home)
    was = mode.read_text().strip() if mode.is_file() else "direct"
    if boundary and was != "front":
        # The front takes over: every copy of the password the apps held so far stops working.
        creds, env_changed = rotate(wizard_home), True
    unit_dir = unit_dir or Path.home() / ".config/systemd/user"
    unit = unit_dir / UNIT
    text = unit_text(hermes, hermes_home, UPSTREAM_HOST if boundary else host, wizard_home,
                     UPSTREAM_PORT if boundary else PORT)
    changed = not unit.is_file() or unit.read_text() != text
    unit_dir.mkdir(parents=True, exist_ok=True)
    if changed:
        unit.write_text(text)
        _systemctl("daemon-reload")
    _systemctl("enable", UNIT)
    # The environment file is read only at start: a value this run added, or one the owner edited since the last
    # (re)start (e.g. WIZARD_ACCOUNT_CHANGES=0), needs a restart too.
    applied = applied_path(wizard_home)
    env_sha = _env_sha(env_path(wizard_home))
    stale = env_changed or (applied.read_text().strip() if applied.is_file() else "") != env_sha
    r = _systemctl("restart", UNIT) if changed or stale else _systemctl("start", UNIT)
    if r.returncode != 0:
        out.update(state="error", error=(r.stderr or r.stdout).strip()[:300])
    else:
        applied.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        applied.write_text(env_sha + "\n")
    keys = {}
    if boundary:
        keys = {a: app_key(wizard_home, a) for a in sorted(apps)}
        prune_keys(wizard_home, set(apps))
        cfg_path = front_config_path(wizard_home)
        doc = front_config(host, creds, keys, owner)
        before = None
        try:
            before = json.loads(cfg_path.read_text())
        except (OSError, ValueError):
            pass
        if before != doc:
            _atomic(cfg_path, json.dumps(doc, indent=2, sort_keys=True) + "\n")
        # Listeners are bound at start; keys, allowlists and the login are re-read live.
        relisten = not before or before.get("listen") != doc["listen"] or \
            (before.get("owner") or {}).get("listen") != (doc.get("owner") or {}).get("listen")
        funit = unit_dir / FRONT_UNIT
        ftext = front_unit_text(wizard_home)
        fchanged = not funit.is_file() or funit.read_text() != ftext
        if fchanged:
            funit.write_text(ftext)
            _systemctl("daemon-reload")
        _systemctl("enable", FRONT_UNIT)
        fr = _systemctl("restart", FRONT_UNIT) if fchanged or relisten or changed else _systemctl("start", FRONT_UNIT)
        if fr.returncode != 0 and out["state"] != "error":
            out.update(state="error", error=("front: " + (fr.stderr or fr.stdout).strip())[:300])
        out["owner_tab"] = bool(doc.get("owner"))
        mode.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode.write_text("front\n")
    elif was == "front":
        # Back to the stock setup: the front goes away with its copy of the password; apps get the password again.
        funit = unit_dir / FRONT_UNIT
        if funit.is_file():
            _systemctl("disable", "--now", FRONT_UNIT)
            funit.unlink()
            _systemctl("daemon-reload")
        front_config_path(wizard_home).unlink(missing_ok=True)
        prune_keys(wizard_home, set())
        mode.unlink(missing_ok=True)
    for app_id, url_vars in apps.items():
        login = keys[app_id] if boundary else creds[PASSWORD_VAR]
        want = {PASSWORD_VAR: login, "HERMES_DASHBOARD_BASIC_AUTH_USERNAME":
                creds.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or USER, **{v: url for v in url_vars}}
        p = runtipi_root / "user-config" / store / app_id / "app.env"
        if any(read_env(p).get(k) != v for k, v in want.items()):
            write_user_env(runtipi_root, app_id, want)
            out["changed_apps"].append(app_id)
        elif stale_running(app_id, store, want):   # address and login, wherever the container carries them
            out["changed_apps"].append(app_id)   # app.env is current, the running container is not
    return out


def login_for(wizard_home: Path, app_id: str, boundary: bool | None = None) -> str | None:
    """What an app's dashboard-password form field gets at install: its own key under the boundary (the front
    knows it), else None (the caller uses the shared dashboard password, as before)."""
    if boundary is None:
        from . import browser_admission
        boundary = browser_admission.enabled()
    return app_key(wizard_home, app_id) if boundary else None


def disable(unit_dir: Path | None = None) -> bool:
    funit = (unit_dir or Path.home() / ".config/systemd/user") / FRONT_UNIT
    if funit.is_file():
        _systemctl("disable", "--now", FRONT_UNIT)
        funit.unlink()
    unit = (unit_dir or Path.home() / ".config/systemd/user") / UNIT
    if not unit.is_file():
        return False
    _systemctl("disable", "--now", UNIT)
    unit.unlink()
    _systemctl("daemon-reload")
    return True
