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
# `wam serve` reads the same file (apply_box_state at start). No state file means no token, no block, no change.
# A new box starts with it on (settle_new_box, Miguel 23 Nov on wizard-os#133, after the automated hostile-app check
# on a real box, wizard-ux tools/boundary-box.sh); a box installed before follows one release later
# (existing_box_reason, run by `wam bootstrap` after the release moved the apps).

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


def settle_new_box(home, installed, core, defaults, env=None) -> dict | None:
    """Every installer run (`wam bootstrap`, before anything is installed): a new box starts with the boundary on.

    New box: the owner never chose (no state file), no shell / Agent Service / pre-split bundle is installed yet and
    nothing but Wizard's own defaults is. Then the choice is recorded as on and the token minted, so the shell and the
    Agent Service get their block when bootstrap installs them. A box that already runs Wizard is left alone (it follows
    one release later: apps that keep data in the browser move it over first). An owner's choice is never changed, and
    WIZARD_BROWSER_BOUNDARY=0 in the installer's environment keeps a new box off. `wam browser-boundary off` is the way
    back. Returns what it decided, or None when it changed nothing."""
    import json
    import time
    env = os.environ if env is None else env
    if state_path(home).exists() or (env.get(SWITCH) or "1") != "1":
        return None
    installed = set(installed)
    if installed & set(core) or installed - set(defaults):
        return None
    sp = state_path(home)
    sp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(sp.parent, 0o700)
    ensure_token(home)
    sp.write_text(json.dumps({"on": True, "why": "new box", "at": int(time.time())}) + "\n")
    return {"on": True, "why": "new box"}


def mac_guest() -> bool:
    """try-wizard's Linux VM on a Mac. Its Mac app still calls the guest's owner routes without the token (the update
    lease, backups), so a Mac guest is never switched on by itself; `wam browser-boundary on` still works there."""
    import subprocess
    from .disk import MAC_GUEST_MARK, MAC_HOST_FILE
    if MAC_HOST_FILE.is_file() or MAC_GUEST_MARK.exists():
        return True
    try:
        r = subprocess.run(["systemd-detect-virt", "--vm"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "apple"


def existing_box_reason(home, installed_before, core, legacy, current, mac=False, env=None) -> str | None:
    """Every installer run (`wam bootstrap`, after the release moved the apps): a box that already ran Wizard follows
    the new boxes one release later (Miguel 23 Nov on wizard-os#133, option A). None when it may be switched on now,
    else why it is left as it is (tried again on the next installer run).

    On only when: the owner never chose (no state file) and the installer was not told WIZARD_BROWSER_BOUNDARY=0; the
    box ran the split shell or Agent Service before this run (a new box is settle_new_box's; a pre-split box runs the
    bundle's own shell, which never sends the token); every shell/Agent Service app it has is on the release's own commit now (`current`: an
    older shell sends no token, so it must have moved first); and it is not a Mac guest (mac_guest)."""
    env = os.environ if env is None else env
    installed_before = set(installed_before)
    if state_path(home).exists():
        return "the owner chose"
    if (env.get(SWITCH) or "1") != "1":
        return f"{SWITCH}={env.get(SWITCH)} in the installer's environment"
    if not installed_before & set(core):
        # a pre-split box runs the bundle's own shell, which never sends the token (a split box keeps the stopped
        # bundle only as rollback material, beside the shell and Agent Service that run)
        return "the pre-split Wizard shell bundle runs" if legacy in installed_before else "not a box that already ran Wizard"
    if not current:
        return "the shell or Agent Service is not on this release yet"
    if mac:
        return "a Mac guest (its Mac app does not send the token yet)"
    return None


API_UNIT = "wizard-app-manager-api.service"


def restart_serve(run=None) -> str:
    """`wam serve` gates its own routes only from its next start (apply_box_state): restart its user unit if it runs
    (try-restart: a box without the unit is left alone). "restarted", or "error: ..." for the caller to report."""
    import subprocess
    run = run or (lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True, timeout=60))
    try:
        r = run("try-restart", API_UNIT)
    except (OSError, subprocess.SubprocessError) as e:
        return f"error: {str(e)[:160]}"
    return "restarted" if r.returncode == 0 else f"error: {(r.stderr or '').strip()[:160] or r.returncode}"


def owner_headers(home=None) -> dict:
    """The shell token header for this box's own programs that call the owner API on its loopback (`wam` agent
    access, feature switches and agent restarts; `wizard contact`), or {} when the box has no token. The boundary
    walls off app pages in the browser, not programs on the box: those are the owner on every box today (wizard-os
    spec/OWNER_PROOF.md) and can read this file anyway. The token never leaves the box this way."""
    if home is None:
        from . import registry
        home = registry.home()
    try:
        got = token_path(home).read_text().strip()
    except (OSError, UnicodeError):
        return {}
    return {HEADER: got} if _HEX64.fullmatch(got) else {}


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


def _strip(doc: dict, marker: str = MARK) -> dict:
    mark = doc.pop(marker, None)
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


def _already(doc: dict, block: dict, marker: str) -> bool:
    """The block is in the override exactly as asked (its record, its values and its mounts), wherever it sits."""
    want = {"service": block["service"], "environment": sorted(block["environment"]), "volumes": list(block["volumes"])}
    if doc.get(marker) != want:
        return False
    svc = (doc.get("services") or {}).get(block["service"])
    if not isinstance(svc, dict):
        return False
    env = svc.get("environment") if isinstance(svc.get("environment"), dict) else {}
    vols = svc.get("volumes") if isinstance(svc.get("volumes"), list) else []
    return all(env.get(k) == v for k, v in block["environment"].items()) and all(v in vols for v in block["volumes"])


def render(path, block: dict | None, marker: str = MARK):
    """What write_overlay would write: (before, after) texts, after None for "remove the file", or None when nothing
    changes. Raises ValueError for any override it can't safely edit, so callers can check every app first.
    `marker` names the block, so another owner-chosen setting (private_first) can keep its own block beside this one."""
    from pathlib import Path
    from .network import load_yaml, dump_yaml
    path = Path(path)
    before = path.read_text() if path.is_file() else None
    if block is None and (before is None or marker not in before):
        return None
    try:
        doc = (load_yaml(before) or {}) if before else {}
    except Exception as e:   # noqa: BLE001 - a broken override is refused, never half-edited
        raise ValueError(f"{path}: not readable compose YAML ({str(e)[:120]})") from e
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a compose mapping")
    if not isinstance(doc.get("services", {}), dict):
        raise ValueError(f"{path}: services must be a mapping")
    mark = doc.get(marker)
    if mark is not None and not (isinstance(mark, dict) and isinstance(doc.get("services", {}).get(mark.get("service"), {}), dict)):
        raise ValueError(f"{path}: the {marker} block is malformed; fix it by hand")
    if block and _already(doc, block, marker):
        return None   # the block is there as asked: never reorder it (a rerun must not recreate the app)
    doc = _strip(doc, marker)
    if block:
        svcs = doc.setdefault("services", {})
        svc = svcs.setdefault(block["service"], {})
        if not isinstance(svc, dict):
            raise ValueError(f"{path}: services.{block['service']} must be a mapping")
        env = svc.get("environment")
        if env is None:
            env = svc["environment"] = {}
        if not isinstance(env, dict):
            raise ValueError(f"{path}: {block['service']}.environment must be a mapping to add the {marker} block")
        clash = [k for k in block["environment"] if k in env and env[k] != block["environment"][k]]
        if clash:
            raise ValueError(f"{path}: hand-written {', '.join(clash)} present; remove it first")
        vols = svc.setdefault("volumes", []) if block["volumes"] else svc.get("volumes", [])
        if not isinstance(vols, list):
            raise ValueError(f"{path}: {block['service']}.volumes must be a list")
        env.update(block["environment"])
        vols.extend(v for v in block["volumes"] if v not in vols)   # an env-only block adds no volumes key
        doc[marker] = {"service": block["service"], "environment": sorted(block["environment"]),
                     "volumes": list(block["volumes"])}
    head = []   # keep the leading comment header exactly as other writers left it
    for line in (before or "").splitlines():
        if not line.startswith("#"):
            break
        head.append(line + "\n")
    text = "".join(head) + dump_yaml(doc) if doc else ""
    if text == (before or ""):
        return None
    return before, (text if doc else None)


def write_overlay(path, block: dict | None, marker: str = MARK) -> bool:
    """Put (or with None remove) the boundary block in one app's user-config override; keep every other line's
    meaning. Returns True when the file changed. Never touches a file without the block when removing."""
    from pathlib import Path
    path = Path(path)
    r = render(path, block, marker)
    if r is None:
        return False
    text = r[1]
    if text is None:
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


def set_box(home, runtipi_root, store: str, app_ids, on: bool, dry_run: bool = False, why: str = "") -> dict:
    """`wam browser-boundary on|off`: record the choice, mint (or drop) the token and (re)write each app's block.
    Restarting the apps and `wam serve` is the caller's. `why` is noted in the state file when given."""
    import json
    import time
    out = {"on": on, "changed": []}
    if dry_run:
        out["would_change"] = list(app_ids)
        return out
    # Every override must accept the block before anything is recorded or written, so a refusal never leaves a
    # half-on box (the App Manager gating while the shell has no token).
    for app_id in app_ids:
        render(override_path(runtipi_root, store, app_id), overlay(home, app_id) if on else None)
    sp = state_path(home)
    sp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sp.write_text(json.dumps({"on": on, **({"why": why, "at": int(time.time())} if why else {})}) + "\n")
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
