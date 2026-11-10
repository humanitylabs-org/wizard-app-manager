"""What a Wizard OS release touches, and what applying it on this box will stop.

`impact` is about the release: the strongest part its pins move, compared with the installed release.
`interrupts` is about this box: the update path can stop more than the release touches (today every installer run
restarts runtipi, and Hermes too while the builder link isn't idempotent). Waiting decisions use `interrupts`.

Levels, weakest first:
  none     only words differ (name, about, notes, date)        nothing restarts, pages don't reload
  ui       the Wizard interface                                 its container; open pages reload
  service  App Manager, Agent Service, troubleshooter, apps     their containers / units; chat runs in flight
  hermes   the agent (or anything this module doesn't know)     agent turns, sub-agents, scheduled runs, its tasks
  os       the operating system / a reboot                      everything
"""
from __future__ import annotations

LEVELS = ("none", "ui", "service", "hermes", "os")
META = frozenset({"version", "name", "date", "about", "notes"})
UI_PARTS = frozenset({"wizard-ux"})
SERVICE_PARTS = frozenset({
    "wizard-app-manager", "wizard-troubleshooter", "wizard-agent-service", "wizard-chatgpt", "wizard-claude",
    "files", "transcriber", "browser", "secret-drop", "notifications", "wizard-voice-service",
    "wizard-connection-router", "install",   # `install` holds the installer's commands and modes: it runs anyway
})
HERMES_PARTS = frozenset({"hermes", "hermes-agent"})
OS_PARTS = frozenset({"os", "guest-image", "kernel"})   # none in the release format yet; reserved


def rank(level: str | None) -> int:
    return LEVELS.index(level) if level in LEVELS else LEVELS.index("hermes")


def strongest(*levels: str | None) -> str:
    return max((lv for lv in levels if lv), key=rank, default="none")


def changed_parts(have: dict | None, want: dict | None) -> set[str]:
    """Names of the parts whose pins differ: component/image names, default-app *app* names, or top-level keys.
    Words (META) never count. A missing or malformed side counts as everything changed ({"?"})."""
    if not isinstance(have, dict) or not isinstance(want, dict):
        return {"?"}
    parts: set[str] = set()
    for key in set(have) | set(want):
        a, b = have.get(key), want.get(key)
        if key in META or a == b:
            continue
        if key in ("components", "images") and isinstance(a, dict) and isinstance(b, dict):
            parts |= {name for name in set(a) | set(b) if a.get(name) != b.get(name)}
        elif key == "default_apps" and isinstance(a, dict) and isinstance(b, dict):
            for slot in set(a) | set(b):
                ra, rb = a.get(slot), b.get(slot)
                if ra != rb:
                    apps = {r["app"] for r in (ra, rb) if isinstance(r, dict) and r.get("app")}
                    parts |= apps or {f"slot:{slot}"}
        else:
            parts.add(key)
    return parts


def part_level(part: str) -> str:
    if part in UI_PARTS:
        return "ui"
    if part in SERVICE_PARTS:
        return "service"
    if part in OS_PARTS:
        return "os"
    return "hermes"          # Hermes itself, or anything unknown: the safe side


def impact(have: dict | None, want: dict | None) -> str:
    return strongest(*(part_level(p) for p in changed_parts(have, want)))


def installer_keeps_hermes() -> bool:
    """True once this App Manager's builder link leaves an unchanged Hermes alone (wizard-app-manager#581).
    Before that, every installer run stops and starts Hermes, whatever the release changes."""
    try:
        from . import builder_bundle
    except Exception:
        return False
    return bool(getattr(builder_bundle, "LINK_IS_IDEMPOTENT", False))


def interrupts(level: str, path: str, *, keeps_hermes: bool | None = None) -> str:
    """What applying a release of `level` stops on this box when it goes through `path`:
    "quick" (swap only the changed images), "pin" (words only: move the receipt) or "installer"."""
    if path == "pin":
        return "none"
    if path == "quick":
        return level
    floor = "service"                                     # the installer recreates runtipi on every run
    if not (installer_keeps_hermes() if keeps_hermes is None else keeps_hermes):
        floor = "hermes"
    return strongest(level, floor)


def summary(have: dict | None, want: dict | None, path: str = "installer") -> dict:
    parts = changed_parts(have, want)
    level = strongest(*(part_level(p) for p in parts))
    return {"impact": level, "interrupts": interrupts(level, path), "parts": sorted(parts),
            "reload": bool(parts & (UI_PARTS | {"?"}))}
