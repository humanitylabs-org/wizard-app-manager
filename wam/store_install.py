"""Store "Get": install an app from the Wizard App Store window, without giving the browser the API token.

Route: `wam serve` answers on a 0600 unix socket (~/.wizard-os/store.sock), not on the loopback API port, so no
other local program can fake the identity header. `tailscale serve` maps the shell's tailnet origin
`/wizard-os-home/store-install/<app>` to it and sets `Tailscale-User-Login` itself. A request is accepted only when ALL hold:
  - it came through Tailscale Serve as the box owner (`Tailscale-User-Login` == the owner's tailnet login),
  - its Origin is the shell's own tailnet origin (no cross-site posts),
  - it is JSON with the `X-Wizard-Store: 1` header (a simple form or image tag can't send that),
  - the app id is listed in the public App Index or the pinned private test catalog. Nothing else: no settings,
    no repo URLs, no uninstall. A second tap while an install runs joins it instead of starting another.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
PREFIX = "/wizard-os-home/store-install"
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def owner_login() -> str:
    """The tailnet login of this box's owner (the node's user), or '' when Tailscale isn't running."""
    if not shutil.which("tailscale"):
        return ""
    try:
        d = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                                      timeout=15).stdout or "{}")
        uid = str((d.get("Self") or {}).get("UserID") or "")
        return str(((d.get("User") or {}).get(uid) or {}).get("LoginName") or "")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def check_request(headers, owner: str, origins: set[str]) -> str | None:
    """None when allowed, else the reason it isn't."""
    if not owner:
        return "store installs need Tailscale (no owner login known)"
    if (headers.get("Tailscale-User-Login") or "").lower() != owner.lower():
        return "only the box owner can install from the Store"
    if (headers.get("Origin") or "") not in origins:
        return "wrong origin"
    if headers.get("X-Wizard-Store") != "1" or "application/json" not in (headers.get("Content-Type") or ""):
        return "bad request"
    return None


def allowed(c, app_id: str) -> bool:
    """Listed in the pinned private test catalog or the public App Index."""
    from . import index, test_catalog
    try:
        if test_catalog.resolve(app_id):
            return True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        return bool(index.resolve(c.index, app_id))
    except Exception:  # noqa: BLE001 - not in the index
        return False


def start(c, app_id: str, install_fn, installed_fn) -> tuple[int, dict]:
    if not ID.match(app_id or ""):
        return 400, {"error": "bad app id"}
    with _lock:
        job = _jobs.get(app_id)
        if job and job["state"] == "installing":
            return 202, job
    if app_id in installed_fn():
        return 200, {"app": app_id, "state": "installed", "note": "already installed"}
    if not allowed(c, app_id):
        return 404, {"error": f"{app_id} is not in the App Store"}
    with _lock:
        job = _jobs.get(app_id)
        if job and job["state"] == "installing":
            return 202, job
        job = _jobs[app_id] = {"app": app_id, "state": "installing", "started": int(time.time())}

    def run():
        try:
            res = install_fn(c, app_id)
            ok = res.get("status") in ("running", "stopped", "already installed")
            job.update(state="installed" if ok else "failed", status=res.get("status"),
                       **({} if ok else {"error": f"finished as {res.get('status')}"}))
            if ok:
                _links(c)
        except SystemExit as e:
            job.update(state="failed", error=str(e)[:400])
        except Exception as e:  # noqa: BLE001 - report, never crash the API server
            job.update(state="failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        job["finished"] = int(time.time())

    threading.Thread(target=run, name=f"store-install-{app_id}", daemon=True).start()
    return 202, job


def _links(c) -> None:
    """Publish the new app on the tailnet so Wizard can open it (best effort)."""
    try:
        from . import links
        from .cli import write_registry
        reg = write_registry(c)
        links.publish(reg, c.home, {a["id"] for a in reg.get("apps") or [] if "shell" in (a.get("provides") or [])})
        links.apply_to_registry(reg, c.home)
        from . import registry
        registry.write(reg, c.home)
    except Exception:  # noqa: BLE001
        pass


def status(app_id: str) -> tuple[int, dict]:
    job = _jobs.get(app_id)
    return (200, job) if job else (404, {"error": "no install for that app"})


def socket_path(home):
    return home / "store.sock"


def route(shell_port: int, home) -> list[str]:
    return ["tailscale", "serve", "--bg", f"--https={shell_port}", f"--set-path={PREFIX}", f"unix:{socket_path(home)}"]
