"""App Store browse data for every box (alongside and fresh).

The Wizard UX Store window reads `/wizard-os-home/store-data/` (the public App Index, this box's installed apps,
logos). Its read-only adapter ships as `tools/store-data-origin.mjs` in the wizard-ux repo, but the shell image does
not run it, so the Store was empty (alongside) or said "Installed status is incomplete" (fresh). This module installs
it from the SAME commit as the installed shell, runs it as a loopback user service, and routes only that prefix (plus
the App Manager's owner-gated Store Get listener) on the shell's own tailnet ports.

Nothing is invented here: the adapter files are fetched from the public repo at the pinned commit and checked
before use; the service binds 127.0.0.1 only; the browser never sees the Runtipi credential (the adapter keeps it).
"""
from __future__ import annotations

import glob
import json
import os
import posixpath
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

REPO = "humanitylabs-org/wizard-ux"
FILES = ("store-data-origin.mjs", "store-index-views.mjs", "system-overview.mjs", "store-private-catalog.mjs")
REQUIRED = {"store-data-origin.mjs", "store-index-views.mjs", "system-overview.mjs"}
PORT = 9584
PREFIX = "/wizard-os-home/store-data"
UNIT = "wizard-store-data.service"
SHA = re.compile(r"^[0-9a-f]{40}$")
SERVICE = re.compile(r"^http://127\.0\.0\.1:\d{1,5}$")
# Relative module imports in the adapter: `from './x.mjs'`, `import('./x.mjs')`, `import './x.mjs'`, and modules the
# adapter shares with the shell such as `from '../web/store/install-link.js'` (wizard-ux #529). Names are kept relative
# to tools/ and resolved inside the repo; the cache mirrors the repo layout (<sha>/tools, <sha>/web/...).
LOCAL_IMPORT = re.compile(r"""(?:from|import\s*\(?)\s*['"](\.{1,2}/[A-Za-z0-9_./-]+\.m?js)['"]""")


def _resolve(importer: str, spec: str) -> str:
    """The module `spec` imported by `importer` (both relative to tools/), as a tools/-relative name; it must stay inside
    the repo (raises ValueError otherwise)."""
    name = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    repo_path = posixpath.normpath(posixpath.join("tools", name))
    if repo_path.startswith("../") or repo_path == ".." or posixpath.isabs(repo_path):
        raise ValueError(f"adapter import {spec!r} in {importer} leaves the wizard-ux repo")
    return name


def _imports(importer: str, text: str) -> list[str]:
    return [_resolve(importer, spec) for spec in LOCAL_IMPORT.findall(text)]


def find_node() -> str | None:
    """A Node.js 18+ binary: PATH, the copies the Hermes installer leaves in ~/.hermes, then the official,
    sha256-checked Node the Troubleshooter installs (a fresh box has no host Hermes and often no system Node)."""
    home = Path.home()
    cands = [shutil.which("node"), str(home / ".hermes/node/bin/node")]
    cands += sorted(glob.glob(str(home / ".hermes/tools/node-*/bin/node")), reverse=True)
    cands.append(str(home / ".local/share/wizard-troubleshooter/node/bin/node"))
    for c in cands:
        if not c or not os.access(c, os.X_OK):
            continue
        try:
            v = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        m = re.match(r"v(\d+)\.", v)
        if m and int(m.group(1)) >= 18:
            return c
    return None


def ensure_node() -> str | None:
    """find_node(), else install the official Node build the Troubleshooter pins (published sha256 checked)."""
    node = find_node()
    if node:
        return node
    from . import troubleshooter
    try:
        return troubleshooter.node(troubleshooter.paths())
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        import sys
        print(f"note: could not install Node.js for the Store data service ({str(e)[:160]})", file=sys.stderr)
        return None


def _local_port(url) -> int | None:
    m = re.match(r"^http://(?:127\.0\.0\.1|localhost):(\d{1,5})(?:/|$)", str(url or ""))
    return int(m.group(1)) if m else None


def fresh_ports(reg: dict) -> tuple[int, int]:
    """(shell port, Agent Service port) on a fresh box, from the App Manager's own apps.json (loopback only)."""
    shell = svc = 0
    for a in reg.get("apps") or []:
        provides = a.get("provides") or []
        urls = (a.get("health"), (a.get("view") or {}).get("local"), (a.get("view") or {}).get("url"),
                (a.get("mcp") or {}).get("url"))
        port = next((p for p in map(_local_port, urls) if p), 0)
        if not shell and ("shell" in provides or a.get("id") == "wizard-ux"):
            shell = port
        if not svc and ("agent-service" in provides or a.get("id") == "wizard-agent-service"):
            svc = port
    return shell, svc


def shell_commit(reg: dict) -> str | None:
    for a in reg.get("apps") or []:
        if "shell" in (a.get("provides") or []) or a.get("id") == "wizard-ux":
            c = str(a.get("commit") or "")
            if SHA.match(c):
                return c
    return None


def _fetch(sha: str, name: str, timeout: int = 20) -> bytes | None:
    """`name` is relative to tools/ (e.g. `store-data-origin.mjs` or `../web/store/install-link.js`)."""
    url = f"https://raw.githubusercontent.com/{REPO}/{sha}/{posixpath.normpath(posixpath.join('tools', name))}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed https host, pinned sha
            return r.read(2 * 1024 * 1024)
    except Exception:  # noqa: BLE001 - optional files may not exist at older commits
        return None


def _complete(tools: Path) -> bool:
    """A cached adapter is usable only if the required files AND every module they import, transitively, are present
    (a cache written before sibling imports were fetched lacks wizard-alpha.mjs; one written before shared shell
    modules were followed lacks ../web/store/install-link.js; either fails at node start)."""
    if not all((tools / f).is_file() for f in REQUIRED):
        return False
    queue, seen = [f for f in FILES if (tools / f).is_file()], set()
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        try:
            text = (tools / name).read_text(errors="replace")
            queue += _imports(name, text)
        except (OSError, ValueError):
            return False
    return True


def fetch_tools(sha: str, dest_root: Path, fetch=None) -> Path:
    """Download the adapter at `sha` into dest_root/<sha>/tools (idempotent, atomic). Modules it imports from elsewhere
    in the repo land at their repo paths under dest_root/<sha>/, so relative imports resolve as in the repo."""
    fetch = fetch or _fetch
    if not SHA.match(sha):
        raise ValueError("shell commit must be a full 40-char sha")
    final_root = dest_root / sha
    final = final_root / "tools"
    if _complete(final):
        return final
    tmp = dest_root / f".{sha}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "tools").mkdir(parents=True)
    queue, seen = list(FILES), set()
    try:
        while queue:   # the listed files plus every module they import (e.g. wizard-alpha.mjs), at the same sha
            name = queue.pop(0)
            if name in seen:
                continue
            seen.add(name)
            body = fetch(sha, name)
            if body is None:
                if name in REQUIRED or name not in FILES:   # an imported module is required by its importer
                    raise RuntimeError(f"wizard-ux {sha[:9]} has no tools/{name}; this shell version has no Store data")
                continue
            dest = tmp / posixpath.normpath(posixpath.join("tools", name))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            queue += [m for m in _imports(name, body.decode("utf-8", "replace")) if m not in seen]
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    final_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(final_root, ignore_errors=True)
    tmp.rename(final_root)
    return final


def unit_text(node: str, tools: Path, sha: str, port: int = PORT, catalog: str | None = None,
              service: str | None = None) -> str:
    env = f"Environment=WAM_PRIVATE_CATALOG={catalog}\n" if catalog else ""
    # The adapter reads the shell app list from the Agent Service; alongside boxes run it on their own port.
    if service and SERVICE.match(service):
        env += f"Environment=WIZARD_STORE_SERVICE_ORIGIN={service}\n"
    return (f"[Unit]\nDescription=Wizard App Store browse data (read-only, loopback :{port}) from wizard-ux {sha}\n"
            f"After=network-online.target\n\n[Service]\n{env}"
            f"ExecStart={node} {tools}/store-data-origin.mjs {port}\nRestart=on-failure\nNoNewPrivileges=true\n\n"
            f"[Install]\nWantedBy=default.target\n")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)


def ensure(reg: dict, data_root: Path | None = None, unit_dir: Path | None = None, port: int = PORT,
           apply: bool = True, service: str | None = None) -> dict:
    """Install/refresh the Store data service for the installed shell's commit. Returns a result dict."""
    sha = shell_commit(reg)
    if not sha:
        return {"state": "skipped", "note": "no Wizard shell with a pinned commit is installed"}
    node = ensure_node()
    if not node:
        return {"state": "error", "error": "Node.js 18+ not found (PATH, ~/.hermes/node, ~/.hermes/tools/node-*, "
                                           "the Troubleshooter's Node) and could not be installed"}
    data_root = data_root or Path.home() / ".local/share/wizard-store-data"
    unit_dir = unit_dir or Path.home() / ".config/systemd/user"
    try:
        tools = fetch_tools(sha, data_root)
    except (RuntimeError, ValueError, OSError) as e:
        return {"state": "error", "error": str(e)}
    cat = os.environ.get("WAM_PRIVATE_CATALOG")
    text = unit_text(node, tools, sha, port, cat if cat and Path(cat).is_file() else None, service)
    unit = unit_dir / UNIT
    changed = not unit.is_file() or unit.read_text() != text
    out = {"state": "ready", "commit": sha, "port": port, "node": node, "unit": str(unit), "changed": changed}
    if not apply:
        return out
    unit_dir.mkdir(parents=True, exist_ok=True)
    if changed:
        unit.write_text(text)
        _systemctl("daemon-reload")
    _systemctl("enable", UNIT)
    r = _systemctl("restart", UNIT) if changed else _systemctl("start", UNIT)
    if r.returncode != 0:
        out.update(state="error", error=(r.stderr or r.stdout).strip()[:300])
    return out


def publish_routes(host: str, shell_port: int, port: int = PORT) -> dict:
    """Route the Store data prefix and the Store Get listener on every owner shell port (the shell's own port plus
    owner-configured aliases). An existing different handler is never replaced; readback must match exactly."""
    from . import links, store_install
    wanted = ((PREFIX, f"http://127.0.0.1:{port}{PREFIX}", "tailnet"),
              (store_install.PREFIX, f"http://127.0.0.1:{store_install.PORT}", "get"))
    out: dict = {"tailnet": "routed", "get": "routed", "ports": []}
    try:
        ports = links.gateway_shell_ports(links.serve_status(), host, shell_port)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {"tailnet": str(e)[:200], "get": str(e)[:200], "ports": []}
    for p in ports:
        for path, target, key in wanted:
            try:
                st = links.route_status(links.serve_status(), host, p, path, target)
                if st == "conflict":
                    raise ValueError(f"existing Serve route on {p}{path} differs; left untouched")
                if st == "missing":
                    r = links._run(["tailscale", "serve", "--bg", f"--https={p}", f"--set-path={path}", target], 60)
                    if r.returncode:
                        raise ValueError((r.stderr or r.stdout).strip()[-200:] or "Tailscale Serve failed")
                    if links.route_status(links.serve_status(), host, p, path, target) != "match":
                        raise ValueError("Serve route readback did not match")
            except (OSError, subprocess.TimeoutExpired, ValueError) as e:
                out[key] = f"{p}: {str(e)[:200]}"
        out["ports"].append(p)
    return out


def route(shell_port: int, port: int = PORT) -> list[str]:
    """The `tailscale serve` command that routes only the Store data prefix on the shell's tailnet port."""
    return ["tailscale", "serve", "--bg", f"--https={shell_port}", f"--set-path={PREFIX}",
            f"http://127.0.0.1:{port}{PREFIX}"]
