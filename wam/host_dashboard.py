"""The host Hermes dashboard for alongside boxes.

Some apps sign in through the agent's own dashboard API (e.g. ChatGPT: Hermes' device-code login, and the
Wizard OS plugin's read-only usage route). In fresh mode they reach the Hermes Agent app by name; on an alongside
box Hermes runs on the host and has no dashboard running. This module runs stock `hermes dashboard` as a user
service and points those apps at it:

- bound to the box's Tailscale IPv4 only (rootless Docker containers reach it; the public interface does not),
- stock basic auth required (password generated once, 0600 in ~/.wizard-os/secrets/host-dashboard.env),
- each consumer app gets the address and password through its Runtipi user-config app.env (kept across updates).

A consumer is any installed app whose recipe has the HERMES_DASHBOARD_BASIC_AUTH_PASSWORD form field; its
dashboard URL variable is whatever `${VAR:-http://hermes-agent:9119}` the recipe compose uses. No Hermes core change.
"""
from __future__ import annotations

import re
import secrets
import shutil
import subprocess
from pathlib import Path

UNIT = "wizard-host-dashboard.service"
PORT = 9119
USER = "hermes"
PASSWORD_VAR = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"
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


def credentials(wizard_home: Path) -> dict[str, str]:
    """The dashboard login (created once, 0600). An existing file is kept as is, so apps keep working."""
    p = env_path(wizard_home)
    env = read_env(p)
    if env.get(PASSWORD_VAR) and env.get("HERMES_DASHBOARD_BASIC_AUTH_SECRET"):
        return env
    env = {"HERMES_DASHBOARD_BASIC_AUTH_USERNAME": env.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or USER,
           PASSWORD_VAR: env.get(PASSWORD_VAR) or secrets.token_urlsafe(24),
           "HERMES_DASHBOARD_BASIC_AUTH_SECRET": secrets.token_hex(32)}
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    p.chmod(0o600)
    return env


def tailnet_ip() -> str | None:
    if not shutil.which("tailscale"):
        return None
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    ip = out.strip().splitlines()[0].strip() if out.strip() else ""
    return ip if re.fullmatch(r"100\.\d{1,3}\.\d{1,3}\.\d{1,3}", ip) else None


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
    creds = credentials(wizard_home)
    unit_dir = unit_dir or Path.home() / ".config/systemd/user"
    unit = unit_dir / UNIT
    text = unit_text(hermes, hermes_home, host, wizard_home)
    changed = not unit.is_file() or unit.read_text() != text
    unit_dir.mkdir(parents=True, exist_ok=True)
    if changed:
        unit.write_text(text)
        _systemctl("daemon-reload")
    _systemctl("enable", UNIT)
    r = _systemctl("restart", UNIT) if changed else _systemctl("start", UNIT)
    if r.returncode != 0:
        out.update(state="error", error=(r.stderr or r.stdout).strip()[:300])
    for app_id, url_vars in apps.items():
        want = {PASSWORD_VAR: creds[PASSWORD_VAR], "HERMES_DASHBOARD_BASIC_AUTH_USERNAME":
                creds.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or USER, **{v: url for v in url_vars}}
        p = runtipi_root / "user-config" / store / app_id / "app.env"
        if any(read_env(p).get(k) != v for k, v in want.items()):
            write_user_env(runtipi_root, app_id, want)
            out["changed_apps"].append(app_id)
    return out


def disable(unit_dir: Path | None = None) -> bool:
    unit = (unit_dir or Path.home() / ".config/systemd/user") / UNIT
    if not unit.is_file():
        return False
    _systemctl("disable", "--now", UNIT)
    unit.unlink()
    _systemctl("daemon-reload")
    return True
