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
