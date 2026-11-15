"""Feature switches for one box: the WizardOS Tracker app's 'Try it' and Settings' "Try new features" (Miguel, 7 Nov 2026).

Feature switches live in Settings › Appearance ("Try new features", For testers only) and in the WizardOS Tracker
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

One catalog for core AND apps (Miguel, 10 Nov 2026): listing() also carries the switches of every installed app that
implements wizard.app-switches/1 (app_switches.py; ids `<app id>:<switch id>`, `kind: "app"`, each with the `issues`
it delivers), and set_switch() flips those in the app, server-side with the owner's proof. Core rows say `kind: "core"`
and carry `issues` from their manifest when it names them. `apps` lists each switch-declaring app and whether it
answered.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

STORE_PATH = "_feature-switches"     # under /wizard-os-home/store-install/ (StoreHandler)
ID = re.compile(r"[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*")
ISSUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}#\d{1,7}")
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
        issues = [str(i) for i in (m.get("issues") or []) if isinstance(i, str) and ISSUE.fullmatch(i)][:12]
        out.append({"id": tid, "name": str(m.get("name") or tid)[:120], "description": str(m.get("description") or "")[:600],
                    "author": tid.split("/")[0], "kind": "core", "issues": issues})
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
                doc = None
            # Unreadable or another schema version: hand back an UNREADABLE marker so set_switch refuses to write
            # over it (the owner's skin, views and backdrop live in this file); listing reads it as "nothing on".
            return (doc if _readable(doc) else UNREADABLE), j.get("version")
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
                newer = None
            return False, (newer if _readable(newer) else UNREADABLE), cur.get("version")
        raise SwitchError(f"can't save look.json (HTTP {code})", 503)


UNREADABLE = {"version": None, "unreadable": True}   # look.json exists but is not a version-1 document we understand


def _readable(doc) -> bool:
    return isinstance(doc, dict) and doc.get("version") == 1


def _refuse_unreadable(doc) -> None:
    if not _readable(doc):
        raise SwitchError("look.json is unreadable or from a newer WizardOS, so it was left alone; "
                          "fix or update it first", 409)


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


def _app_rows(home, app_rows):
    if app_rows is not None:
        return app_rows
    from . import app_switches
    try:
        return app_switches.listing(home)
    except Exception as e:                      # noqa: BLE001 - an app never takes the core catalog down
        return [], [{"id": "", "ok": False, "error": f"app switches: {str(e)[:120]}"}]


def listing(home: Path | None = None, fetch=_get, pages: Pages | None = None, app_rows=None) -> dict:
    """Every feature switch with whether it is on for this box. On For everyone all core ones are off and locked;
    app switches (`kind: "app"`) follow the app's own state."""
    chan = _channel(home)
    locked = chan != "latest"
    doc, _ = (pages or _pages(home)).get()
    saved = _tweaks(doc) if _readable(doc) else {}
    rows = [{**t, "on": (not locked) and saved.get(t["id"], {}).get("on") is True, "locked": locked}
            for t in catalog(home, fetch)]
    apps, status = _app_rows(home, app_rows)
    return {"channel": chan, "channel_name": "For testers" if chan == "latest" else "For everyone", "locked": locked,
            "note": ("Feature switches are for testers: choose For testers in Settings › About & updates."
                     if locked else ""), "switches": rows + list(apps), "apps": list(status)}


def is_on(tid: str, home: Path | None = None, pages: Pages | None = None) -> bool:
    """Whether the owner turned feature switch `tid` on, for box-side code gated on a switch (#400). Reads the same
    look.json `tweaks` map as listing(); always False on For everyone and whenever look.json can't be read."""
    try:
        if not ID.fullmatch(tid) or _channel(home) != "latest":
            return False
        doc, _ = (pages or _pages(home)).get()
        return _readable(doc) and _tweaks(doc).get(tid, {}).get("on") is True
    except (SwitchError, OSError, ValueError):
        return False


def set_switch(tid: str, on: bool, home: Path | None = None, fetch=_get, pages: Pages | None = None,
               by: str = "owner", origin: str | None = None) -> dict:
    """Turn one feature switch on or off for this box (every device of the owner follows within ~30 s).
    `<app id>:<switch id>` flips that app's own switch in the app (app_switches.set_switch), as the owner."""
    if isinstance(tid, str) and ":" in tid:
        from . import app_switches
        try:
            row = app_switches.set_switch(tid, on, home, origin=origin)
        except app_switches.AppSwitchError as e:
            raise SwitchError(str(e), e.code) from None
        try:
            out = listing(home, fetch, pages)
        except SwitchError:                     # the app flipped; the core half can't be read right now
            apps, status = _app_rows(home, None)
            out = {"switches": list(apps), "apps": list(status)}
        return {**out, "changed": {"id": tid, "on": row["on"], "by": by}}
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
        _refuse_unreadable(doc)                         # never overwrite the owner's skin/views with an empty file
        doc = {**doc, "version": 1}
        doc["tweaks"] = {**_tweaks(doc), tid: {"on": on, "at": _revision(doc) + 1}}
        ok, newer, version = p.put(doc, version)
        if ok:
            out = listing(home, fetch, p)
            return {**out, "changed": {"id": tid, "on": on, "by": by}}
        doc = newer or {"version": 1}
    raise SwitchError("look.json kept changing; try again", 409)
