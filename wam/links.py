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


def served_ports() -> set[int]:
    try:
        r = _run(["tailscale", "serve", "status", "--json"])
        d = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return set()
    return {int(p) for p in (d.get("TCP") or {}) if str(p).isdigit()}


def pages(reg: dict) -> list[dict]:
    """Installed apps with a page: id, name, category, local port and path, from apps.json."""
    out = []
    for a in reg.get("apps") or []:
        v = a.get("view") or {}
        url = v.get("local") or v.get("url") or ""
        if not url.startswith("http://") or a.get("status") not in (None, "running"):
            continue
        hostport, _, path = url[len("http://"):].partition("/")
        port = hostport.rpartition(":")[2]
        if not port.isdigit() or int(port) in ADMIN_PORTS:
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
    out = {}
    if home:
        try:
            for l in json.loads((home / "links.json").read_text()).get("links") or []:
                o = str(l.get("open") or "")
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


def publish(reg: dict, home: Path, shell_ids: set[str] | None = None, apply: bool = True) -> dict:
    """Serve each app page on the tailnet (when connected) and write home/links.json. Returns the result."""
    ts = tailnet()
    rows = shell_first(pages(reg), shell_ids or set())
    gw = gateway_paths(home)
    result: dict = {"provider": "tailscale", "state": ts["state"], "host": ts["host"], "links": [], "errors": {}}
    if ts["state"] != "Running" or not ts["host"]:
        result["note"] = ("Tailscale isn't connected yet, so nothing is on your private network. "
                          "Run `sudo tailscale up`, open the sign-in link with YOUR Tailscale account, then `wam links`.")
    else:
        already = served_ports() if apply else set()
        shell_port = next((r["port"] for r in rows if r["id"] in (shell_ids or set())), None)
        for r in rows:
            g = gw.get(r["id"])
            if g and shell_port:
                # Same origin as the shell: one path on the shell's tailnet port.
                target = f"http://127.0.0.1:{r['port']}" + ("" if g["strip"] else g["path"])
                if apply:
                    try:
                        p = _run(["tailscale", "serve", "--bg", f"--https={shell_port}", f"--set-path={g['path']}", target], 60)
                        if p.returncode != 0:
                            result["errors"][r["id"]] = (p.stderr or p.stdout).strip()[:200]
                            continue
                    except (OSError, subprocess.TimeoutExpired) as e:
                        result["errors"][r["id"]] = str(e)[:200]
                        continue
                result["links"].append({**r, "url": f"https://{ts['host']}:{shell_port}{g['path']}", "open": g["path"],
                                        "gateway_strip": g["strip"]})
                continue
            if apply and r["port"] not in already:
                try:
                    p = _run(["tailscale", "serve", "--bg", f"--https={r['port']}", f"http://127.0.0.1:{r['port']}"], 60)
                    if p.returncode != 0:
                        result["errors"][r["id"]] = (p.stderr or p.stdout).strip()[:200]
                        continue
                except (OSError, subprocess.TimeoutExpired) as e:
                    result["errors"][r["id"]] = str(e)[:200]
                    continue
            url = f"https://{ts['host']}:{r['port']}{r['path']}"
            result["links"].append({**r, "url": url, "open": url})
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
