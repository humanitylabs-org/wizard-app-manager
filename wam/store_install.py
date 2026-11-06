"""Store "Get": install an app from the Wizard App Store window, without giving the browser the API token.

Route: `wam serve` answers on its own loopback port (7791). `tailscale serve` maps the shell's tailnet origin
`/wizard-os-home/store-install/<app>` to it and sets `Tailscale-User-Login` itself. A connection is served only
when its local peer socket belongs to root, i.e. tailscaled: the owner's programs (and rootless containers, whose
root maps to the owner) can't reach it with a forged identity header. Unix-socket serving would need root. A request is accepted only when ALL hold:
  - it came through Tailscale Serve as the box owner (`Tailscale-User-Login` == the owner's tailnet login),
  - it came from another tailnet device: Serve labels connections from this node's own address with the owner's
    login, and every app container reaches Serve from that address, so a change from the box itself is refused
    (reads stay allowed),
  - its Origin is the shell's own tailnet origin (no cross-site posts),
  - it is JSON with the `X-Wizard-Store: 1` header (a simple form or image tag can't send that),
  - an exact selection resolves server-side against a catalog/subscription, or a link's pinned GitHub
    manifest passes the wizard.md checker and matches the app id. No browser URLs or settings.
    A second tap while an install runs joins it instead of starting another.
Removal ("Remove app") is a separate exact-identity route behind the same gate: store_remove.py.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
PREFIX = "/wizard-os-home/store-install"
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def tailscale_cmd(*args: str) -> list[str]:
    """argv for the tailscale CLI, [] when there is none. `WAM_TAILSCALE_SOCKET` points it at an isolated userspace
    daemon (the private gateway's) and `WAM_TAILSCALE_BIN` at a binary that is not on the service's PATH."""
    binary = os.environ.get("WAM_TAILSCALE_BIN") or shutil.which("tailscale") or ""
    if not binary:
        return []
    sock = os.environ.get("WAM_TAILSCALE_SOCKET") or ""
    return [binary, *(["--socket", sock] if sock else []), *args]


def _status() -> dict:
    cmd = tailscale_cmd("status", "--json")
    if not cmd:
        return {}
    try:
        return json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def owner_login() -> str:
    """The tailnet login of this box's owner (the node's user), or '' when Tailscale isn't running."""
    d = _status()
    uid = str((d.get("Self") or {}).get("UserID") or "")
    return str(((d.get("User") or {}).get(uid) or {}).get("LoginName") or "")


def own_ips() -> set[str]:
    """This node's own Tailscale addresses (empty when Tailscale isn't running)."""
    return {str(ip) for ip in ((_status().get("Self") or {}).get("TailscaleIPs") or [])}


LOOPBACK = {"127.0.0.1", "::1"}
FROM_BOX = ("changes are refused from this Wizard itself: its apps reach Tailscale Serve with your identity too. "
            "Use your phone or computer, or `wam` on the server")


def from_box(headers, ips: set[str]) -> bool:
    """True when the request reached Tailscale Serve from this box itself (or the client is unknown).

    Serve sets X-Forwarded-For to the connecting tailnet address (it replaces whatever the client sent) and sets
    Tailscale-User-Login for this node's own address to the node's user: the owner. Every program on the box,
    including every app container (rootless Docker connects out from the host), therefore arrives as the owner,
    from the node's own address. Only the owner's other devices have another address. Fails closed: without this
    node's addresses (Tailscale didn't answer) nothing can be told apart, so every request counts as the box's."""
    hop = (headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
    return not ips or not hop or hop in LOOPBACK or hop in ips


def check_request(headers, owner: str, origins: set[str], ips: set[str] | None = None) -> str | None:
    """None when allowed, else the reason it isn't."""
    if not owner:
        return "store installs need Tailscale (no owner login known)"
    if (headers.get("Tailscale-User-Login") or "").lower() != owner.lower():
        return "only the box owner can install from the Store"
    if from_box(headers, own_ips() if ips is None else ips):
        return FROM_BOX
    if (headers.get("Origin") or "") not in origins:
        return "wrong origin"
    if headers.get("X-Wizard-Store") != "1" or "application/json" not in (headers.get("Content-Type") or ""):
        return "bad request"
    return None


def served_origins(host: str) -> set[str]:
    """Only owner-configured Serve mounts to this listener, not arbitrary browser ports."""
    try:
        r = subprocess.run(tailscale_cmd('serve', 'status', '--json') or ['tailscale', 'serve', 'status', '--json'],
                           capture_output=True, text=True, timeout=15)
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
    from . import store_remove
    store_remove.removing(app_id, c)   # read an unknown removal back now, outside the lock
    with _lock:
        job = _jobs.get(app_id)
        if job and job["state"] == "installing":
            if selection and job.get("selection") != {k: selection[k] for k in ("source", "repo", "commit", "operation")}:
                return 409, {"error": "another release operation is already running for this app"}
            return 202, job
        if store_remove.removing(app_id):
            return 409, {"error": "this app is being removed; refresh when that finishes"}
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
            job.update(state="failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        job["finished"] = int(time.time())

    threading.Thread(target=run, name=f"store-install-{app_id}", daemon=True).start()
    return 202, job


def _links(c) -> None:
    """Publish the new app on the tailnet so Wizard can open it (best effort)."""
    try:
        from . import links
        from .cli import write_registry, share_registry
        known = links.registry_addresses(c.home)   # before write_registry can replace them with loopback
        reg = write_registry(c)
        links.publish(reg, c.home, {a["id"] for a in reg.get("apps") or [] if "shell" in (a.get("provides") or [])},
                      known=known)
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

# --- Gateway listener (loopback :7792) --------------------------------------------------------------------------
# The private gateway (a user-owned Caddy + userspace tailscaled) is not the root-owned tailscaled the :7791 peer gate
# trusts, so that gate can never admit it, and it stays exactly as it is. The gateway reaches Store Get through this
# second listener instead. It admits a request only when ALL hold:
#   - it carries the gateway's own proof header, equal to a secret file only the gateway and this service read
#     (`<wizard home>/store-gateway-proof`, 0600). The gateway sets the header itself after its verified-owner check
#     and strips any client copy, so a browser, an app container or an agent cannot supply it;
#   - the app path is a plain app id (no `_` control paths: updates, removals, connections stay on :7791);
#   - then the same owner / other-device / origin / JSON / catalog checks as :7791 (check_request, start).
# No proof file, no listener.
GATEWAY_PORT = 7792
GATEWAY_PROOF_FILE = "store-gateway-proof"


def gateway_proof(home) -> str:
    """The shared secret, '' when the gateway listener is not set up (missing, empty or too short to trust)."""
    try:
        v = (Path(home) / GATEWAY_PROOF_FILE).read_text().strip()
    except OSError:
        return ""
    return v if len(v) >= 32 else ""


def gateway_proof_ok(headers, home) -> bool:
    want, got = gateway_proof(home), (headers.get("X-Wizard-Store-Proof") or "").strip()
    return bool(want) and hmac.compare_digest(want.encode(), got.encode())


def gateway_origins(home) -> set[str]:
    """The Wizard's own browser origin(s): settings `network.base_url` and its aliases. Nothing else is ever allowed."""
    try:
        net = (json.loads((Path(home) / "settings.json").read_text()).get("network") or {})
    except (OSError, ValueError, AttributeError):
        return set()
    urls = [net.get("base_url"), *(net.get("aliases") or [])]
    return {u.rstrip("/") for u in urls if isinstance(u, str) and re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?/?", u)}


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
