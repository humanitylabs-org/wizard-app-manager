"""Browser owner boundary for the App Manager's shell-origin routes (wizard-os#133, slice 3).

Off unless WIZARD_BROWSER_BOUNDARY=1. Then every Store/Settings listener route and the owner-only host API route
`/api/system/devices` also need the shell-only token in `X-Wizard-Shell-Token`, read from WIZARD_SHELL_TOKEN_FILE
(the same file and header as the Agent Service gate, wizard-agent-service `server/browser-boundary.mjs`).

Why: an app page is loaded same-origin with the shell, so it carries the owner's Tailscale login, the shell's
Origin and `Sec-Fetch-Site: same-origin` too. Those prove the device, not that the shell asked. The token is held
only by the shell document; a custom header also forces a CORS preflight for any cross-origin caller.

This is an ADDITIONAL gate. It never replaces the owner login, peer, origin or from-box checks that already run.
With the switch off nothing here runs and every route behaves exactly as before.
"""
from __future__ import annotations

import hmac
import os
import re

HEADER = "X-Wizard-Shell-Token"
SWITCH = "WIZARD_BROWSER_BOUNDARY"
TOKEN_FILE = "WIZARD_SHELL_TOKEN_FILE"
REFUSED = "this needs the Wizard shell itself; an app page can't do it"
FRAME_DESTS = {"iframe", "frame", "embed", "object"}
_HEX64 = re.compile(r"[a-f0-9]{64}")


class BoundaryError(RuntimeError):
    """Switch on but no usable token: fail closed (every gated request is refused)."""


def enabled(env=None) -> bool:
    return (os.environ if env is None else env).get(SWITCH) == "1"


def token(env=None) -> str:
    """The shell token, or BoundaryError when the switch is on and the file is missing or malformed."""
    env = os.environ if env is None else env
    try:
        with open(env.get(TOKEN_FILE) or "", encoding="utf-8") as f:
            got = f.read().strip()
    except (OSError, UnicodeError) as e:
        raise BoundaryError(f"{SWITCH}=1 needs a readable {TOKEN_FILE}") from e
    if not _HEX64.fullmatch(got):
        raise BoundaryError("the shell token must be 32 random bytes as lowercase hex")
    return got


def refusal(headers, env=None) -> str | None:
    """None when the request may continue, else the reason it is refused. Always None with the switch off."""
    if not enabled(env):
        return None
    try:
        want = token(env)
    except BoundaryError:
        return REFUSED
    origin = (headers.get("Origin") or "").strip().lower()
    site = headers.get("Sec-Fetch-Site")
    # Even a stolen token may not authorise an opaque (sandboxed) frame, another site or a framed load.
    if origin == "null" or (site is not None and site != "same-origin"):
        return REFUSED
    if (headers.get("Sec-Fetch-Dest") or "").lower() in FRAME_DESTS:
        return REFUSED
    got = headers.get(HEADER) or ""
    if not hmac.compare_digest(got.encode(), want.encode()):
        return REFUSED
    return None


# ---------------------------------------------------------------- provisioning the shell token on a box (slice 4)
#
# `wam browser-boundary on|off|status`. The App Manager mints one random token, keeps it in its own home
# (<home>/browser-boundary/shell-token, the folder 0700) and hands it, read-only, to the apps that must agree on it:
# the Agent Service and the shell (wizard-ux, or the pre-split wizard-shell bundle), through a block of their stock
# Runtipi user-config override marked `x-wizard-browser-boundary` (only that block is ever rewritten or removed).
# `wam serve` reads the same file (apply_box_state at start). Off (the default) means no file, no block, no change.
# Turning it on for anyone is Miguel's call after the hostile-app browser test (release stop on wizard-os#133).

MARK = "x-wizard-browser-boundary"
MOUNT = "/run/wizard-shell/token"
STATE_DIR = "browser-boundary"
_SAFE = re.compile(r"^/[A-Za-z0-9._/@+-]+$")


def token_path(home) -> "Path":
    from pathlib import Path
    return Path(home) / STATE_DIR / "shell-token"


def state_path(home) -> "Path":
    from pathlib import Path
    return Path(home) / STATE_DIR / "state.json"


def box_on(home) -> bool:
    """True when the owner turned the boundary on for this box (`wam browser-boundary on`)."""
    import json
    try:
        return json.loads(state_path(home).read_text()).get("on") is True
    except (OSError, ValueError, AttributeError):
        return False


def ensure_token(home, minted: list | None = None) -> "Path":
    """Mint the token once (32 random bytes, lowercase hex). The folder is 0700, so no other host user can reach
    it; the file itself is 0644 so a container user other than the owner's mapped uid can read its own mount."""
    import secrets
    p = token_path(home)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    try:
        if _HEX64.fullmatch(p.read_text().strip()):
            return p
    except OSError:
        pass
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(32) + "\n")
    os.chmod(tmp, 0o644)
    tmp.replace(p)
    if minted is not None:   # a new file is a new inode: running containers still hold the old one
        minted.append(p)
    return p


def overlay(home, service: str) -> dict:
    """The block for one app's main service: the switch, the token file's path, and its read-only mount."""
    src = str(token_path(home))
    if not _SAFE.fullmatch(src):
        raise ValueError(f"path {src!r} has characters a compose file can't carry safely")
    return {"service": service, "environment": {SWITCH: "1", TOKEN_FILE: MOUNT}, "volumes": [f"{src}:{MOUNT}:ro"]}


def _strip(doc: dict) -> dict:
    mark = doc.pop(MARK, None)
    if not isinstance(mark, dict):
        return doc
    svcs = doc.get("services") if isinstance(doc.get("services"), dict) else {}
    s = svcs.get(mark.get("service"))
    if isinstance(s, dict):
        env = s.get("environment")
        if isinstance(env, dict):
            for k in mark.get("environment") or []:
                env.pop(k, None)
            if not env:
                s.pop("environment")
        vols = s.get("volumes")
        if isinstance(vols, list):
            s["volumes"] = [v for v in vols if v not in (mark.get("volumes") or [])]
            if not s["volumes"]:
                s.pop("volumes")
        if not s:
            svcs.pop(mark.get("service"))
    if isinstance(doc.get("services"), dict) and not doc["services"]:
        doc.pop("services")
    return doc


def check_overlay(path, block: dict | None) -> None:
    """Raise ValueError when write_overlay would refuse, without writing anything."""
    from pathlib import Path
    from .network import load_yaml
    path = Path(path)
    if not block or not path.is_file():
        return
    doc = load_yaml(path.read_text()) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a compose mapping")
    doc = _strip(doc)
    svc = ((doc.get("services") or {}) if isinstance(doc.get("services"), dict) else {}).get(block["service"]) or {}
    env = svc.get("environment") if isinstance(svc, dict) else None
    if env is not None and not isinstance(env, dict):
        raise ValueError(f"{path}: {block['service']}.environment must be a mapping to add the browser boundary")
    clash = [k for k in block["environment"] if k in (env or {}) and env[k] != block["environment"][k]]
    if clash:
        raise ValueError(f"{path}: hand-written {', '.join(clash)} present; remove it first")


def write_overlay(path, block: dict | None) -> bool:
    """Put (or with None remove) the boundary block in one app's user-config override; keep every other line's
    meaning. Returns True when the file changed. Never touches a file without the block when removing."""
    from pathlib import Path
    from .network import load_yaml, dump_yaml
    path = Path(path)
    before = path.read_text() if path.is_file() else None
    if block is None and (before is None or MARK not in before):
        return False
    doc = (load_yaml(before) or {}) if before else {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a compose mapping")
    doc = _strip(doc)
    if block:
        svc = doc.setdefault("services", {}).setdefault(block["service"], {})
        env = svc.get("environment")
        if env is None:
            env = svc["environment"] = {}
        if not isinstance(env, dict):
            raise ValueError(f"{path}: {block['service']}.environment must be a mapping to add the browser boundary")
        clash = [k for k in block["environment"] if k in env and env[k] != block["environment"][k]]
        if clash:
            raise ValueError(f"{path}: hand-written {', '.join(clash)} present; remove it first")
        env.update(block["environment"])
        vols = svc.setdefault("volumes", [])
        vols.extend(v for v in block["volumes"] if v not in vols)
        doc[MARK] = {"service": block["service"], "environment": sorted(block["environment"]),
                     "volumes": list(block["volumes"])}
    head = []   # keep the leading comment header exactly as other writers left it
    for line in (before or "").splitlines():
        if not line.startswith("#"):
            break
        head.append(line + "\n")
    text = "".join(head) + dump_yaml(doc) if doc else ""
    if text == (before or ""):
        return False
    if not doc:
        path.unlink(missing_ok=True)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".wam-tmp")
    tmp.write_text(text)
    if path.is_file():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)
    return True


def override_path(runtipi_root, store: str, app_id: str):
    from pathlib import Path
    return Path(runtipi_root) / "user-config" / store / app_id / "docker-compose.yml"


def sync_app(home, runtipi_root, store: str, app_id: str) -> bool:
    """Bring one shell/service app's override in line with the box state (block when on, none when off). Called
    after any writer that rewrites the whole override (alongside mode) and on install. True when it changed."""
    on = box_on(home)
    if on:
        ensure_token(home)
    return write_overlay(override_path(runtipi_root, store, app_id), overlay(home, app_id) if on else None)


def apply_box_state(home, env=None) -> bool:
    """At `wam serve` start: with the box switched on, gate this process too (same file as the apps). An explicit
    WIZARD_BROWSER_BOUNDARY in the environment always wins. True when it set the switch."""
    env = os.environ if env is None else env
    if SWITCH in env or not box_on(home):
        return False
    env[SWITCH] = "1"
    env[TOKEN_FILE] = str(ensure_token(home))
    return True


def set_box(home, runtipi_root, store: str, app_ids, on: bool, dry_run: bool = False) -> dict:
    """`wam browser-boundary on|off`: record the choice, mint (or drop) the token and (re)write each app's block.
    Restarting the apps and `wam serve` is the caller's."""
    import json
    out = {"on": on, "changed": []}
    if dry_run:
        out["would_change"] = list(app_ids)
        return out
    # Every override must accept the block before anything is recorded or written, so a refusal never leaves a
    # half-on box (the App Manager gating while the shell has no token).
    for app_id in app_ids:
        check_overlay(override_path(runtipi_root, store, app_id), overlay(home, app_id) if on else None)
    sp = state_path(home)
    sp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sp.write_text(json.dumps({"on": on}) + "\n")
    minted: list = []
    if on:
        ensure_token(home, minted)
    for app_id in app_ids:
        if write_overlay(override_path(runtipi_root, store, app_id), overlay(home, app_id) if on else None) \
                or (minted and status_one(runtipi_root, store, app_id)):
            out["changed"].append(app_id)
    if not on:
        token_path(home).unlink(missing_ok=True)
    return out


def status_one(runtipi_root, store: str, app_id: str) -> bool:
    p = override_path(runtipi_root, store, app_id)
    return p.is_file() and MARK in p.read_text()


def status(home, runtipi_root, store: str, app_ids) -> dict:
    tok = token_path(home)
    try:
        usable = bool(_HEX64.fullmatch(tok.read_text().strip()))
    except OSError:
        usable = False
    apps = {app_id: status_one(runtipi_root, store, app_id) for app_id in app_ids}
    return {"on": box_on(home), "token": usable, "apps": apps}
