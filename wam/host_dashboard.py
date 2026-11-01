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
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import subprocess
from pathlib import Path

UNIT = "wizard-host-dashboard.service"
PORT = 9119
USER = "hermes"
PASSWORD_VAR = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"
ACCOUNT_CHANGES_VAR = "WIZARD_ACCOUNT_CHANGES"
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
    return (f"[Unit]\nDescription=Hermes dashboard for Wizard alongside apps (tailnet IP only, password required)\n"
            f"After=network-online.target tailscaled.service\n\n[Service]\nEnvironmentFile={env_path(wizard_home)}\n"
            f"Environment=HERMES_HOME={hermes_home}\n"
            f"ExecStart={hermes} dashboard --host {host} --port {port} --skip-build --no-open --isolated\n"
            f"Restart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)


def ensure(wizard_home: Path, runtipi_root: Path, store: str, installed: set[str], hermes: str | None,
           hermes_home: str, write_user_env, unit_dir: Path | None = None, apply: bool = True) -> dict:
    """Run the dashboard and point every consumer app at it. Returns {state, ...}; `changed_apps` need an update."""
    apps = consumers(runtipi_root, store, installed)
    if not apps:
        return {"state": "skipped", "note": "no installed app signs in through the agent's dashboard"}
    host = tailnet_ip()
    if not host:
        return {"state": "error", "error": "no Tailscale IPv4 on this box; apps can't reach the host dashboard"}
    if not hermes:
        return {"state": "error", "error": "the `hermes` command was not found"}
    url = f"http://{host}:{PORT}"
    out = {"state": "ready", "url": url, "apps": sorted(apps), "changed_apps": []}
    if not apply:
        return out
    creds, env_changed = _credentials(wizard_home)
    unit_dir = unit_dir or Path.home() / ".config/systemd/user"
    unit = unit_dir / UNIT
    text = unit_text(hermes, hermes_home, host, wizard_home)
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
    for app_id, url_vars in apps.items():
        want = {PASSWORD_VAR: creds[PASSWORD_VAR], "HERMES_DASHBOARD_BASIC_AUTH_USERNAME":
                creds.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or USER, **{v: url for v in url_vars}}
        p = runtipi_root / "user-config" / store / app_id / "app.env"
        if any(read_env(p).get(k) != v for k, v in want.items()):
            write_user_env(runtipi_root, app_id, want)
            out["changed_apps"].append(app_id)
        elif stale_running(app_id, store, want):   # address and login, wherever the container carries them
            out["changed_apps"].append(app_id)   # app.env is current, the running container is not
    return out


def disable(unit_dir: Path | None = None) -> bool:
    unit = (unit_dir or Path.home() / ".config/systemd/user") / UNIT
    if not unit.is_file():
        return False
    _systemctl("disable", "--now", UNIT)
    unit.unlink()
    _systemctl("daemon-reload")
    return True
