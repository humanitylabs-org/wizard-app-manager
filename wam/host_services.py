"""Host services that fill a system spot from outside Runtipi (wizard-agent-service issue 463).

A box may run the Wizard Voice Service on the host itself (loopback :8864, the Agent Service's default STT origin),
not as a Runtipi app. `wam slots` then read Voice and Transcription as `empty` while that service was serving every
voice note, so the box's own record of what it can do was wrong and a careful agent was told "nothing here".

Behind the owner's switch `wizard/capability-registry` (For testers only, like every feature switch): when the voice
service answers healthy on loopback and no installed app already provides the spot, Voice and Transcription are
recorded in apps.json `external` as filled by the host service. `slots.table` shows them `filled`, `parking` carries
them, and `wam bootstrap` doesn't install a second speech stack beside it. Switch off, or the service is unhealthy:
nothing is added, exactly as before.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

SWITCH = "wizard/capability-registry"
VOICE_ID = "wizard-voice-service"
VOICE_NAME = "host Wizard Voice Service"
VOICE_SLOTS = ("voice", "transcription")
DEFAULT_VOICE_ORIGIN = "http://127.0.0.1:8864"
ORIGIN_ENV = "WIZARD_STT_ORIGIN"
LOOPBACK = re.compile(r"http://(?:127\.0\.0\.1|localhost|\[::1\]):\d{1,5}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def switch_on(home: Path | None = None) -> bool:
    from . import feature_switches
    return feature_switches.is_on(SWITCH, home, timeout=2)


def voice_origin() -> str:
    """The host voice service's loopback origin (WIZARD_STT_ORIGIN when it is loopback, else the default)."""
    value = (os.environ.get(ORIGIN_ENV) or "").rstrip("/")
    return value if LOOPBACK.fullmatch(value) else DEFAULT_VOICE_ORIGIN


def voice_health(origin: str, timeout: float = 1.5) -> dict | None:
    """{provider, model} when the voice service's /health says ok (the Agent Service's own readiness rule), else None."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(f"{origin}/health", timeout=timeout) as res:
            value = json.loads(res.read(64 * 1024).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("status") != "ok" or not isinstance(value.get("provider"), str) or not value["provider"]:
        return None
    return {"provider": value["provider"][:80], "model": str(value.get("model") or "")[:120]}


def external_slots(reg: dict, home: Path | None = None, *, on=None, health=None) -> dict[str, dict]:
    """Spots a healthy host voice service fills (only those no installed app provides). {} when the switch is off."""
    if not (switch_on(home) if on is None else on()):
        return {}
    provided = {s for a in reg.get("apps") or [] for s in (a.get("provides") or [])}
    free = [s for s in VOICE_SLOTS if s not in provided]
    if not free:
        return {}
    origin = voice_origin()
    found = (health or voice_health)(origin)
    if not found:
        return {}
    return {s: {"external": True, "host_service": True, "id": VOICE_ID, "name": VOICE_NAME, "origin": origin,
                "provider": found["provider"]} for s in free}
