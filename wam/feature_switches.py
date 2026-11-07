"""Feature switches for one box: the Wizard OS Tracker app's 'Try it' and Settings' "Try new features" (Miguel, 7 Nov 2026).

Feature switches live in Settings › Appearance ("Try new features", For testers only) and in the Wizard OS Tracker
app, one shared state. Only on the For testers channel (`latest`): on For everyone (`stable`) every switch stays off
and none can be turned on (the shell enforces the same: wizard-ux core/experience.js).

Where the state lives (unchanged; this module adds no second copy):
  the owner's Files, Pages/wizard-look/look.json, read and written through the Agent Service Pages file API on the
  box's loopback (`GET/PUT /wizard/v1/pages/wizard-look/look.json`: versioned, a stale save is 409 with the newer text,
  every save kept in Pages history). Its `tweaks` map is `{"<author>/<id>": {"on": bool, "at": n}}`; every shell
  merges per choice, newest `at` wins (wizard-ux core/look-sync.js), and repaints within ~30 s. A switch set here is
  written with `at` = the file's highest revision + 1, so it wins over every earlier device choice.
Which switches exist: the running shell's own catalog (`<wizard-ux>/wizard-os-home/tweaks/catalog.json` and each
  `<id>/manifest.json`), minus built-in features (catalog `defaults`) and shell views (manifest `view`), which are not
  feature switches.

Who may call (the owner, nothing else; no app key, no container networking, nothing to install):
  - in a browser: the Tracker's page runs inside the owner's Wizard on the shell's origin
    (`https://<box>:8444/wizard-os-tracker/`), so it calls the same owner-only, same-origin route Settings › Updates
    already uses: `/wizard-os-home/store-install/_feature-switches`, which Tailscale Serve proxies to the wam store
    listener (`StoreHandler`, :7791) with the owner's Tailscale identity. POST needs the shell's Origin,
    `X-Wizard-Store: 1` and JSON (store_install.check_request), like every other owner change there.
  - on the server: `wam feature-switches [--on ID|--off ID]`, or `Authorization: Bearer <~/.wizard-os/api-token>` on
    `wam serve` (127.0.0.1:7790) `/api/feature-switches`.

Both HTTP routes: GET -> listing(); POST {"id": "<author>/<id>", "on": true|false} -> set_switch().
401/403 without the owner, 409 on For everyone.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

STORE_PATH = "_feature-switches"     # under /wizard-os-home/store-install/ (StoreHandler)
ID = re.compile(r"[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*")
LOOK = "/wizard/v1/pages/wizard-look/look.json"


class SwitchError(RuntimeError):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def _home(home: Path | None) -> Path:
    from .updates import _home as h
    return h(home)


# --- what exists and what is on ----------------------------------------------------------------------------------

def shell_origin(home: Path | None = None) -> str | None:
    """Loopback origin of this box's Wizard UX shell (its registry health URL)."""
    try:
        for a in json.loads((_home(home) / "apps.json").read_text()).get("apps") or []:
            health = str(a.get("health") or "")
            if a.get("id") == "wizard-ux" and health.startswith("http://127.0.0.1:"):
                return health.rsplit("/", 1)[0]
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _get(url: str, timeout: float = 8):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                timeout=timeout) as r:
        return json.loads(r.read())


def catalog(home: Path | None = None, fetch=_get) -> list[dict]:
    """The feature switches the running shell ships: catalog tweaks minus built-ins and views."""
    origin = shell_origin(home)
    if not origin:
        raise SwitchError("can't find this Wizard's shell (wizard-ux) in apps.json", 503)
    base = f"{origin}/wizard-os-home/tweaks"
    try:
        cat = fetch(f"{base}/catalog.json")
    except (OSError, ValueError, urllib.error.URLError) as e:
        raise SwitchError(f"can't read the shell's feature list ({str(e)[:120]})", 503) from None
    builtin = set(cat.get("defaults") or [])
    out = []
    for tid in cat.get("tweaks") or []:
        if not isinstance(tid, str) or not ID.fullmatch(tid) or tid in builtin:
            continue
        try:
            m = fetch(f"{base}/{tid}/manifest.json")
        except (OSError, ValueError, urllib.error.URLError):
            m = {}
        if not isinstance(m, dict) or m.get("view"):
            continue                                   # a shell view is a way to see the shell, chosen in Settings
        out.append({"id": tid, "name": str(m.get("name") or tid)[:120], "description": str(m.get("description") or "")[:600],
                    "author": tid.split("/")[0]})
    return sorted(out, key=lambda t: (t["name"].lower(), t["id"]))


class Pages:
    """look.json through the Agent Service Pages file API on loopback (the shell's own path, the same history)."""

    def __init__(self, origin: str | None):
        if not origin:
            raise SwitchError("can't reach the Agent Service (Pages)", 503)
        self.origin = origin

    def _req(self, method: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.origin + LOOK, data=data, method=method,
                                   headers={"Accept": "application/json",
                                            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (OSError, urllib.error.URLError) as e:
            raise SwitchError(f"can't reach the Agent Service ({str(e)[:120]})", 503) from None

    def get(self) -> tuple[dict, str | None]:
        code, j = self._req("GET")
        if code == 200 and isinstance(j.get("text"), str):
            try:
                doc = json.loads(j["text"])
            except ValueError:
                doc = {}
            return (doc if isinstance(doc, dict) and doc.get("version") == 1 else {"version": 1}), j.get("version")
        if code == 404 and j.get("error") == "file_not_found":
            return {"version": 1}, None
        raise SwitchError(f"can't read look.json (HTTP {code})", 503)

    def put(self, doc: dict, version: str | None) -> tuple[bool, dict | None, str | None]:
        code, j = self._req("PUT", {"text": json.dumps(doc, indent=1) + "\n", "version": version})
        if 200 <= code < 300:
            return True, None, j.get("version")
        cur = j.get("current") if isinstance(j, dict) else None
        if code == 409 and isinstance(cur, dict):
            try:
                newer = json.loads(cur.get("text") or "{}") if cur.get("exists") else {"version": 1}
            except ValueError:
                newer = {"version": 1}
            return False, newer, cur.get("version")
        raise SwitchError(f"can't save look.json (HTTP {code})", 503)


def _pages(home: Path | None) -> Pages:
    from .updates import service_origin
    return Pages(service_origin(home))


def _tweaks(doc: dict) -> dict:
    t = doc.get("tweaks")
    return {k: v for k, v in (t.items() if isinstance(t, dict) else []) if ID.fullmatch(k) and isinstance(v, dict)
            and isinstance(v.get("on"), bool)}


def _revision(doc: dict) -> int:
    ats = [doc.get("skin", {}).get("at") if isinstance(doc.get("skin"), dict) else 0]
    for k in ("tweaks", "defaults", "views"):
        if isinstance(doc.get(k), dict):
            ats += [v.get("at") for v in doc[k].values() if isinstance(v, dict)]
    if isinstance(doc.get("desktopBackdrop"), dict):
        ats.append(doc["desktopBackdrop"].get("at"))
    return max([int(a) for a in ats if isinstance(a, (int, float)) and a >= 0] or [0])


def _channel(home: Path | None) -> str:
    from .updates import channel
    return channel(home)


def listing(home: Path | None = None, fetch=_get, pages: Pages | None = None) -> dict:
    """Every feature switch with whether it is on for this box. On For everyone all are off and locked."""
    chan = _channel(home)
    locked = chan != "latest"
    doc, _ = (pages or _pages(home)).get()
    saved = _tweaks(doc)
    rows = [{**t, "on": (not locked) and saved.get(t["id"], {}).get("on") is True, "locked": locked}
            for t in catalog(home, fetch)]
    return {"channel": chan, "channel_name": "For testers" if chan == "latest" else "For everyone", "locked": locked,
            "note": ("Feature switches are for testers: choose For testers in Settings › About & updates."
                     if locked else ""), "switches": rows}


def set_switch(tid: str, on: bool, home: Path | None = None, fetch=_get, pages: Pages | None = None,
               by: str = "owner") -> dict:
    """Turn one feature switch on or off for this box (every device of the owner follows within ~30 s)."""
    if not isinstance(tid, str) or not ID.fullmatch(tid):
        raise SwitchError("send {\"id\": \"<author>/<id>\", \"on\": true|false}")
    if not isinstance(on, bool):
        raise SwitchError("\"on\" must be true or false")
    if _channel(home) != "latest":
        raise SwitchError("Feature switches are for testers: this Wizard is on For everyone, so they stay off", 409)
    if tid not in {t["id"] for t in catalog(home, fetch)}:
        raise SwitchError(f"{tid} is not a feature switch on this Wizard", 404)
    p = pages or _pages(home)
    doc, version = p.get()
    for _ in range(4):                                  # a device saved meanwhile: merge onto its newer text, retry
        doc = {**doc, "version": 1}
        doc["tweaks"] = {**_tweaks(doc), tid: {"on": on, "at": _revision(doc) + 1}}
        ok, newer, version = p.put(doc, version)
        if ok:
            out = listing(home, fetch, p)
            return {**out, "changed": {"id": tid, "on": on, "by": by}}
        doc = newer or {"version": 1}
    raise SwitchError("look.json kept changing; try again", 409)
