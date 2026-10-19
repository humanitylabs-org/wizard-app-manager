"""App Store browse data for alongside boxes.

The Wizard UX Store window reads `/wizard-os-home/store-data/` (the public App Index, this box's installed apps,
logos). Its read-only adapter ships as `tools/store-data-origin.mjs` in the wizard-ux repo, but nothing installed it,
so the Store was empty on every alongside box. This module installs it from the SAME commit as the installed shell,
runs it as a loopback user service, and routes only that prefix on the shell's tailnet port.

Nothing is invented here: the adapter files are fetched from the public repo at the pinned commit and checked
before use; the service binds 127.0.0.1 only; the browser never sees the Runtipi credential (the adapter keeps it).
"""
from __future__ import annotations

import glob
import json
import os
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
# Sibling-module imports in the adapter: `from './x.mjs'` / `import('./x.mjs')`. Plain names only (no paths).
LOCAL_IMPORT = re.compile(r"""(?:from|import\s*\(?)\s*['"]\./([A-Za-z0-9_.-]+\.mjs)['"]""")


def find_node() -> str | None:
    """A Node.js 18+ binary: PATH, then the copies the Hermes installer leaves in ~/.hermes."""
    home = Path.home()
    cands = [shutil.which("node"), str(home / ".hermes/node/bin/node")]
    cands += sorted(glob.glob(str(home / ".hermes/tools/node-*/bin/node")), reverse=True)
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


def shell_commit(reg: dict) -> str | None:
    for a in reg.get("apps") or []:
        if "shell" in (a.get("provides") or []) or a.get("id") == "wizard-ux":
            c = str(a.get("commit") or "")
            if SHA.match(c):
                return c
    return None


def _fetch(sha: str, name: str, timeout: int = 20) -> bytes | None:
    url = f"https://raw.githubusercontent.com/{REPO}/{sha}/tools/{name}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed https host, pinned sha
            return r.read(2 * 1024 * 1024)
    except Exception:  # noqa: BLE001 - optional files may not exist at older commits
        return None


def _complete(tools: Path) -> bool:
    """A cached adapter is usable only if the required files AND every sibling module they import are present
    (a cache written before sibling imports were fetched lacks wizard-alpha.mjs and fails at node start)."""
    if not all((tools / f).is_file() for f in REQUIRED):
        return False
    for f in tools.glob("*.mjs"):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            return False
        if any(not (tools / m).is_file() for m in LOCAL_IMPORT.findall(text)):
            return False
    return True


def fetch_tools(sha: str, dest_root: Path, fetch=None) -> Path:
    """Download the adapter at `sha` into dest_root/<sha>/tools (idempotent, atomic)."""
    fetch = fetch or _fetch
    if not SHA.match(sha):
        raise ValueError("shell commit must be a full 40-char sha")
    final = dest_root / sha / "tools"
    if _complete(final):
        return final
    tmp = dest_root / f".{sha}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    queue, seen = list(FILES), set()
    while queue:   # the listed files plus every sibling module they import (e.g. wizard-alpha.mjs), at the same sha
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        body = fetch(sha, name)
        if body is None:
            if name in REQUIRED or name not in FILES:   # an imported module is required by its importer
                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError(f"wizard-ux {sha[:9]} has no tools/{name}; this shell version has no Store data")
            continue
        (tmp / name).write_bytes(body)
        queue += [m for m in LOCAL_IMPORT.findall(body.decode("utf-8", "replace")) if m not in seen]
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(final, ignore_errors=True)
    tmp.rename(final)
    return final


def unit_text(node: str, tools: Path, sha: str, port: int = PORT, catalog: str | None = None) -> str:
    env = f"Environment=WAM_PRIVATE_CATALOG={catalog}\n" if catalog else ""
    return (f"[Unit]\nDescription=Wizard App Store browse data (read-only, loopback :{port}) from wizard-ux {sha}\n"
            f"After=network-online.target\n\n[Service]\n{env}"
            f"ExecStart={node} {tools}/store-data-origin.mjs {port}\nRestart=on-failure\nNoNewPrivileges=true\n\n"
            f"[Install]\nWantedBy=default.target\n")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)


def ensure(reg: dict, data_root: Path | None = None, unit_dir: Path | None = None, port: int = PORT,
           apply: bool = True) -> dict:
    """Install/refresh the Store data service for the installed shell's commit. Returns a result dict."""
    sha = shell_commit(reg)
    if not sha:
        return {"state": "skipped", "note": "no Wizard shell with a pinned commit is installed"}
    node = find_node()
    if not node:
        return {"state": "error", "error": "Node.js 18+ not found (PATH, ~/.hermes/node, ~/.hermes/tools/node-*)"}
    data_root = data_root or Path.home() / ".local/share/wizard-store-data"
    unit_dir = unit_dir or Path.home() / ".config/systemd/user"
    try:
        tools = fetch_tools(sha, data_root)
    except (RuntimeError, ValueError, OSError) as e:
        return {"state": "error", "error": str(e)}
    cat = os.environ.get("WAM_PRIVATE_CATALOG")
    text = unit_text(node, tools, sha, port, cat if cat and Path(cat).is_file() else None)
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


def route(shell_port: int, port: int = PORT) -> list[str]:
    """The `tailscale serve` command that routes only the Store data prefix on the shell's tailnet port."""
    return ["tailscale", "serve", "--bg", f"--https={shell_port}", f"--set-path={PREFIX}",
            f"http://127.0.0.1:{port}{PREFIX}"]
