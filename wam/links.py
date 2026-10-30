"""Private links: put every installed app that has a page on the owner's tailnet and say where to open it.

Stock Runtipi already gives each app a fixed port on 127.0.0.1. On Tailscale the matching address is
https://<box>.<tailnet>.ts.net:<port>, published with one stock `tailscale serve --bg --https=<port>` per app port
(wizard-app-manager#18). Nothing here invents a proxy, a subpath or a per-app hostname.

Rules:
- Only when Tailscale is connected (BackendState Running). An unpaired box gets no links and a plain explanation.
- Only apps whose wizard.md has a `view:` (a page). Services without a page are never published.
- The App Manager's own admin page stays box-only.
- Never touches Funnel, routes, exit nodes or anyone else's serve entries; an existing entry on a port is left alone.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ADMIN_PORTS = {8480, 8443}   # the App Manager itself: box-only


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a tailscale command as this user (works when it is the tailnet operator), else once more with sudo -n
    (the installer's wizard user has passwordless sudo). Never prompts."""
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0 and os.geteuid() != 0 and shutil.which("sudo"):
        q = subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=timeout)
        if q.returncode == 0 or not p.stdout:
            return q
    return p


def tailnet() -> dict:
    """{'state': BackendState, 'host': 'box.tailnet.ts.net' or ''}; state 'missing' when Tailscale isn't installed."""
    if not shutil.which("tailscale"):
        return {"state": "missing", "host": ""}
    try:
        r = _run(["tailscale", "status", "--json"])
        d = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"state": "unknown", "host": ""}
    host = ((d.get("Self") or {}).get("DNSName") or "").rstrip(".")
    return {"state": d.get("BackendState") or "unknown", "host": host}


def serve_status() -> dict:
    """Fail closed: an unreadable Serve configuration is not an empty one."""
    r = _run(["tailscale", "serve", "status", "--json"])
    if r.returncode:
        raise ValueError("could not read Tailscale Serve configuration")
    d = json.loads(r.stdout or "{}")
    if not isinstance(d, dict):
        raise ValueError("invalid Tailscale Serve configuration")
    return d


def route_status(config: dict, host: str, port: int, path: str, target: str) -> str:
    """Only reuse an exact private HTTPS proxy; never take over another handler."""
    tcp = (config.get("TCP") or {}).get(str(port))
    web = (config.get("Web") or {}).get(f"{host}:{port}") or {}
    # Tailscale normalizes /app/ to /app in status JSON.
    handlers = {(key.rstrip("/") or "/"): val for key, val in (web.get("Handlers") or {}).items()}
    path = path.rstrip("/") or "/"
    if (config.get("AllowFunnel") or {}).get(f"{host}:{port}"):
        return "conflict"
    if tcp and not tcp.get("HTTPS"):
        return "conflict"
    if path in handlers:
        return "match" if handlers[path] == {"Proxy": target} else "conflict"
    if tcp and not handlers:
        return "conflict"
    return "missing"


# An app mid-restart (or mid-update) is still installed and comes straight back: keep its page published. The
# registry is often written while an app is restarting (the network watcher rewrites it after every start), and
# dropping the app here for that moment took a published app (and, when it was the shell, every same-origin path)
# off links.json, so the shell got its raw port back as the view and could no longer open it in a window.
LIVE_STATUSES = (None, "running", "starting", "restarting", "updating")


def pages(reg: dict) -> list[dict]:
    """Installed apps with a page: id, name, category, local port and path, from apps.json."""
    out = []
    for a in reg.get("apps") or []:
        v = a.get("view") or {}
        url = v.get("local") or v.get("url") or ""
        if not url.startswith("http://") or a.get("status") not in LIVE_STATUSES:
            continue
        hostport, _, path = url[len("http://"):].partition("/")
        port = hostport.rpartition(":")[2]
        if hostport.rpartition(":")[0] not in ("127.0.0.1", "localhost", "[::1]"):
            continue
        if not port.isdigit() or not 1 <= int(port) <= 65535 or int(port) in ADMIN_PORTS:
            continue
        out.append({"id": a["id"], "name": a.get("name") or a["id"], "category": a.get("category"),
                    "port": int(port), "path": "/" + path})
    return out


def shell_first(rows: list[dict], shell_ids: set[str]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["id"] not in shell_ids, r["category"] != "interface", r["name"].lower()))


def gateway_paths(home: Path | None = None) -> dict[str, dict]:
    """Apps that declare a path on the Wizard origin (private test catalog `gateway`), e.g. /wisdom/. They are opened
    inside the shell at that path, so they share its origin (and its /wizard-os-home/core/app-client.js).
    Paths published before are kept (from links.json), so a later run without WAM_PRIVATE_CATALOG set (the API
    service, an installer re-run) doesn't move the app back to a port of its own."""
    # First-party defaults must work on a fresh box, without a private catalog
    # or a links.json copied from somebody else's installation.
    defaults = json.loads(Path(__file__).with_name("defaults.json").read_text())["slots"]
    out = {d["app"]: {"path": d["gateway"]["path"], "strip": d["gateway"].get("strip_prefix", True),
                      "local_path": d["gateway"]["path"]}
           for d in defaults.values() if d.get("app") and d.get("gateway")}
    if home:
        try:
            for l in json.loads((home / "links.json").read_text()).get("links") or []:
                o = str(l.get("open") or "")
                if l.get("gateway_implicit"):   # recomputed from the installed recipe on every run
                    continue
                if l.get("id") and o.startswith("/") and o.endswith("/") and l.get("gateway_strip") is not None:
                    out[l["id"]] = {"path": o, "strip": bool(l["gateway_strip"])}
        except (OSError, ValueError, AttributeError):
            pass
    try:
        from . import test_catalog
        rows = test_catalog.load()
    except (OSError, ValueError, KeyError, TypeError):
        return out
    for a in rows:
        g = a.get("gateway") or {}
        path = str(g.get("path") or "")
        if path.startswith("/") and path.endswith("/") and path.strip("/").replace("-", "").isalnum():
            out[a["id"]] = {"path": path, "strip": g.get("strip_prefix", True) is not False}
    return out


# Shell paths a same-origin app route must never shadow (the shell answers them through its own `/` proxy).
RESERVED_PATHS = {"wizard-os-home", "wizard", "api", "mcp", "v1", "troubleshoot", "login", "logout", "assets",
                  "static", "fonts", "health", "healthz"}


def implicit_gateway(row: dict, shell_ids: set[str], declared: dict | None = None) -> dict | None:
    """Same-origin path for an installed app page that has no gateway of its own (wizard-app-manager: Store apps
    open inside Wizard on fresh boxes). The shell only opens views on its own origin, so every app page also gets a
    path on the shell's tailnet origin, exactly like the alongside gateway: a recipe whose view lives at the root is
    served at /<id>/ with the prefix stripped, and a recipe that declares its own `view.path` (e.g. /files/) is served
    at that path unchanged (strip_prefix false). The agent dashboard (category `agent`) keeps its own port: it uses
    root-absolute URLs. Returns None when the app should keep only its own port."""
    if declared or row["id"] in shell_ids or row.get("category") == "agent":
        return None
    path = row["path"] if row["path"].endswith("/") else row["path"] + "/"
    if path == "/":
        path, strip = f"/{row['id']}/", True
    else:
        strip = False
    top = path.strip("/").split("/")[0]
    if not top or top in RESERVED_PATHS or not path.strip("/").replace("-", "").replace("/", "").isalnum():
        return None
    return {"path": path, "strip": strip, "implicit": True}


def gateway_shell_ports(config: dict, host: str, shell_port: int) -> list[int]:
    """Owner-configured shell aliases, never arbitrary ports supplied by a browser."""
    import re
    from .store_install import PREFIX, PORT
    ports = {shell_port}
    for authority, site in (config.get("Web") or {}).items():
        name, _, port = authority.rpartition(":")
        if name != host or not port.isdigit() or not 1 <= int(port) <= 65535:
            continue
        if (config.get("AllowFunnel") or {}).get(authority) or not (config.get("TCP", {}).get(port) or {}).get("HTTPS"):
            continue
        handlers = site.get("Handlers") or {}
        root = (handlers.get("/") or {}).get("Proxy", "")
        if root == f"http://127.0.0.1:{shell_port}" or (
            re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", root) and
            (handlers.get(PREFIX) or {}).get("Proxy") == f"http://127.0.0.1:{PORT}"
        ):
            ports.add(int(port))
    return sorted(ports)


def publish(reg: dict, home: Path, shell_ids: set[str] | None = None, apply: bool = True,
            api_port: int = 7790) -> dict:
    """Serve each app page on the tailnet (when connected) and write home/links.json. Returns the result."""
    ts = tailnet()
    rows = shell_first(pages(reg), shell_ids or set())
    gw = gateway_paths(home)
    result: dict = {"provider": "tailscale", "state": ts["state"], "host": ts["host"], "links": [], "errors": {}}
    if ts["state"] != "Running" or not ts["host"]:
        result["note"] = ("Tailscale isn't connected yet, so nothing is on your private network. "
                          "Run `sudo tailscale up`, open the sign-in link with YOUR Tailscale account, then `wam links`.")
    else:
        sids = shell_ids or set()
        shell_port = next((r["port"] for r in rows if r["id"] in sids), None)

        def ensure(publish_port: int, path: str, target: str, set_path: bool) -> None:
            """Publish one private route, or raise ValueError; an existing different route is never replaced."""
            status = route_status(serve_status(), ts["host"], publish_port, path, target)
            if status == "conflict":
                raise ValueError(f"existing Serve route on {publish_port}{path} differs; left untouched")
            if status == "missing":
                args = ["tailscale", "serve", "--bg", f"--https={publish_port}"]
                if set_path:
                    args.append(f"--set-path={path}")
                p = _run([*args, target], 60)
                if p.returncode != 0:
                    raise ValueError((p.stderr or p.stdout).strip() or "Tailscale Serve failed")
                if route_status(serve_status(), ts["host"], publish_port, path, target) != "match":
                    raise ValueError("Serve route readback did not match requested private proxy")

        for r in rows:
            g = gw.get(r["id"]) if shell_port else None
            # Old release-pinned recipes still serve at /. Do not route those
            # through /files/ until their matching app update is installed.
            if g and g.get("local_path") and r["path"] != g["local_path"]:
                g = None
            implicit = implicit_gateway(r, sids, gw.get(r["id"])) if shell_port and not g else None
            port = int(shell_port or r["port"]) if g else int(r["port"])
            path = g["path"] if g else "/"
            target = f"http://127.0.0.1:{r['port']}" + (g["path"] if g and not g["strip"] else "")
            if apply:
                try:
                    publish_ports = gateway_shell_ports(serve_status(), ts["host"], port) if g else [port]
                except (OSError, subprocess.TimeoutExpired, ValueError) as e:
                    result["errors"][r["id"]] = str(e)[:200]
                    continue
                for publish_port in publish_ports:
                    try:
                        ensure(publish_port, path, target, bool(g))
                    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
                        result["errors"][r["id"] if publish_port == port else f"{r['id']}@{publish_port}"] = str(e)[:200]
                if r["id"] in result["errors"]:
                    continue
            if implicit:
                # Also on the shell's origin, so Wizard (same-origin views only) opens it in a window. The app keeps
                # its own port too. A different route already on the shell's own port means the app keeps opening
                # at its own address, exactly as before; other shell aliases are best effort.
                itarget = f"http://127.0.0.1:{r['port']}" + ("" if implicit["strip"] else implicit["path"])
                if apply:
                    try:
                        iports = gateway_shell_ports(serve_status(), ts["host"], int(shell_port))
                    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
                        iports, implicit = [], None
                        result.setdefault("gateway_skipped", {})[r["id"]] = str(e)[:200]
                    for publish_port in iports:
                        try:
                            ensure(publish_port, implicit["path"], itarget, True)
                        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
                            result.setdefault("gateway_skipped", {})[f"{r['id']}@{publish_port}"] = str(e)[:200]
                            if publish_port == int(shell_port):
                                implicit = None
                                break
            if implicit:
                result["links"].append({**r, "url": f"https://{ts['host']}:{shell_port}{implicit['path']}",
                                        "open": implicit["path"], "gateway_strip": implicit["strip"],
                                        "gateway_implicit": True, "own": f"https://{ts['host']}:{r['port']}{r['path']}"})
                continue
            url = f"https://{ts['host']}:{port}{g['path'] if g else r['path']}"
            result["links"].append({**r, "url": url, "open": g["path"] if g else url,
                                    **({"gateway_strip": g["strip"]} if g else {})})
        if shell_port:
            from . import resources
            try:
                result["resources"] = resources.publish(ts["host"], shell_port, api_port, apply)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                result["errors"]["resources"] = "Private resource route unavailable; existing routes left untouched"
    if not apply:
        return {**result, "dry_run": True}
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / "links.json.tmp"
    tmp.write_text(json.dumps(result, indent=2) + "\n")
    tmp.replace(home / "links.json")
    return result


def apply_to_registry(reg: dict, home: Path) -> dict:
    """Give each published app the address a browser can open (links.json) as its view url, which is what the
    Agent Service hands the shell. The box-only address stays in view.local for `wam links` and health checks."""
    try:
        saved = json.loads((home / "links.json").read_text())
    except (OSError, ValueError):
        return reg
    opens = {l["id"]: l.get("open") or l.get("url") for l in saved.get("links") or [] if l.get("id")}
    for a in reg.get("apps") or []:
        v = a.get("view")
        if v and opens.get(a["id"]):
            v.setdefault("local", v.get("url"))
            v["url"] = opens[a["id"]]
    return reg


def describe(result: dict) -> str:
    if not result["links"]:
        return result.get("note") or "No app with a page is installed yet."
    lines = ["Open on any device signed in to your Tailscale:"]
    for l in result["links"]:
        lines.append(f"  {l['name']:<28} {l['url']}")
    for app_id, err in result["errors"].items():
        lines.append(f"  {app_id}: not published ({err})")
    return "\n".join(lines)
