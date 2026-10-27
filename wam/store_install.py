"""Store "Get": install an app from the Wizard App Store window, without giving the browser the API token.

Route: `wam serve` answers on its own loopback port (7791). `tailscale serve` maps the shell's tailnet origin
`/wizard-os-home/store-install/<app>` to it and sets `Tailscale-User-Login` itself. A connection is served only
when its local peer socket belongs to root, i.e. tailscaled: the owner's programs (and rootless containers, whose
root maps to the owner) can't reach it with a forged identity header. Unix-socket serving would need root. A request is accepted only when ALL hold:
  - it came through Tailscale Serve as the box owner (`Tailscale-User-Login` == the owner's tailnet login),
  - its Origin is the shell's own tailnet origin (no cross-site posts),
  - it is JSON with the `X-Wizard-Store: 1` header (a simple form or image tag can't send that),
  - an exact selection resolves server-side against a catalog/subscription, or a link's pinned GitHub
    manifest passes the wizard.md checker and matches the app id. No browser URLs, settings or uninstall.
    A second tap while an install runs joins it instead of starting another.
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
        if (d.get("Self") or {}).get("Tags"):
            return ""  # tagged nodes have no human owner, even if UserID is also populated
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


def served_origins(host: str) -> set[str]:
    """Only owner-configured Serve mounts to this listener, not arbitrary browser ports."""
    try:
        r = subprocess.run(['tailscale', 'serve', 'status', '--json'], capture_output=True, text=True, timeout=15)
        web = json.loads(r.stdout).get('Web', {})
        return {f'https://{authority}' for authority, site in web.items()
                if authority.rsplit(':', 1)[0] == host
                and site.get('Handlers', {}).get(PREFIX, {}).get('Proxy') == f'http://127.0.0.1:{PORT}'}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return set()


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


def start(c, app_id: str, install_fn, installed_fn, *, selection=None) -> tuple[int, dict]:
    if not ID.match(app_id or ""):
        return 400, {"error": "bad app id"}
    with _lock:
        job = _jobs.get(app_id)
        if job and job["state"] == "installing":
            if selection and job.get("selection") != {k: selection[k] for k in ("source", "repo", "commit", "operation")}:
                return 409, {"error": "another release operation is already running for this app"}
            return 202, job
    if not selection and app_id in installed_fn():
        return 200, {"app": app_id, "state": "installed", "note": "already installed"}
    if not selection and not allowed(c, app_id):
        return 404, {"error": f"{app_id} is not in the App Store"}
    with _lock:
        job = _jobs.get(app_id)
        if job and job["state"] == "installing":
            if selection and job.get("selection") != {k: selection[k] for k in ("source", "repo", "commit", "operation")}:
                return 409, {"error": "another release operation is already running for this app"}
            return 202, job
        job = _jobs[app_id] = {"app": app_id, "state": "installing", "started": int(time.time())}
        if selection:
            job["selection"] = {k: selection[k] for k in ("source", "repo", "commit", "operation")}

    def run():
        try:
            res = install_fn(c, app_id)
            ok = res.get("status") in ("running", "stopped", "already installed")
            if ok:
                # Publish the same-origin route and rewrite/share the registry BEFORE reporting `installed`: the
                # Store then asks the shell to re-read its apps, and must find the app's openable URL already there.
                _links(c)
            job.update(state="installed" if ok else "failed", status=res.get("status"),
                       **({} if ok else {"error": f"finished as {res.get('status')}"}))
        except SystemExit as e:
            job.update(state="failed", error=str(e)[:400])
        except Exception as e:  # noqa: BLE001 - report, never crash the API server
            from .host_policy import NeedsOwnerApproval
            if isinstance(e, NeedsOwnerApproval):
                job.update(state="needs_owner_approval", **e.payload)
            else:
                job.update(state="failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        job["finished"] = int(time.time())

    threading.Thread(target=run, name=f"store-install-{app_id}", daemon=True).start()
    return 202, job


def _links(c) -> None:
    """Publish the new app on the tailnet so Wizard can open it (best effort)."""
    try:
        from . import links
        from .cli import write_registry, share_registry
        reg = write_registry(c)
        links.publish(reg, c.home, {a["id"] for a in reg.get("apps") or [] if "shell" in (a.get("provides") or [])})
        links.apply_to_registry(reg, c.home)
        from . import registry
        registry.write(reg, c.home)
        # The Agent Service consumes the shared projection, not the owner's file.
        # Publish it again after URL rewriting, including the very last install.
        share_registry(c)
    except Exception:  # noqa: BLE001
        pass


def status(app_id: str) -> tuple[int, dict]:
    job = _jobs.get(app_id)
    return (200, job) if job else (404, {"error": "no install for that app"})


PORT = 7791


def peer_uid(peer_port: int, local_port: int = PORT) -> int | None:
    """Owner uid of the loopback socket connected to us from peer_port (from /proc/net/tcp{,6})."""
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = open(f).read().splitlines()[1:]
        except OSError:
            continue
        for r in rows:
            p = r.split()
            if len(p) < 8:
                continue
            lport = int(p[1].rsplit(":", 1)[1], 16)
            rport = int(p[2].rsplit(":", 1)[1], 16)
            if lport == peer_port and rport == local_port:
                return int(p[7])
    return None


def route(shell_port: int, port: int = PORT) -> list[str]:
    return ["tailscale", "serve", "--bg", f"--https={shell_port}", f"--set-path={PREFIX}", f"http://127.0.0.1:{port}"]
