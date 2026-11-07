"""Feature switches (formerly Settings › Tweaks) for one box, for the Wizard OS Tracker app (Miguel, 7 Nov 2026).

Feature switches left Settings: an owner turns new features on or off only in the Wizard OS Tracker app
(app id `wizard-os-tracker`), and only on the For testers channel (`latest`). On For everyone (`stable`) every switch
stays off and none can be turned on (the shell enforces the same: wizard-ux core/experience.js).

Where the state lives (unchanged; this module adds no second copy):
  the owner's Files, Pages/wizard-look/look.json, read and written through the Agent Service Pages file API on the
  box's loopback (`GET/PUT /wizard/v1/pages/wizard-look/look.json`: versioned, a stale save is 409 with the newer text,
  every save kept in Pages history). Its `tweaks` map is `{"<author>/<id>": {"on": bool, "at": n}}`; every shell
  merges per choice, newest `at` wins (wizard-ux core/look-sync.js), and repaints within ~30 s. A switch set here is
  written with `at` = the file's highest revision + 1, so it wins over every earlier device choice.
Which switches exist: the running shell's own catalog (`<wizard-ux>/wizard-os-home/tweaks/catalog.json` and each
  `<id>/manifest.json`), minus built-in features (catalog `defaults`) and shell views (manifest `view`), which are not
  feature switches.

Who may call (two callers, nothing else):
  - the owner on the server (`wam feature-switches`, or `Authorization: Bearer <~/.wizard-os/api-token>`);
  - an app holding a key minted for it by `wam feature-switches --grant <app>`. Only apps in APPS may get one. The
    key (32 random bytes, hex) is written to the app's `<app-data>/wizard/feature-switches.key`, which its services
    already mount read-only at /wizard (spec/PERMISSIONS.md layer 2); the box keeps only its SHA-256 in
    `~/.wizard-os/grants/feature-switches.json` (0600). The caller sends it as `X-Wizard-Grant`; identity comes from
    the hash match, never from the request. `--revoke <app>` removes it at once.

HTTP (`wam serve`): GET /api/feature-switches -> listing(); POST /api/feature-switches {"id": "<author>/<id>",
"on": true|false} -> set_switch(). 401 without a credential, 403 for an app not allowed, 409 on For everyone.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from pathlib import Path

APPS = ("wizard-os-tracker",)      # apps that may hold a feature-switch key
ID = re.compile(r"[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*")
APP = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
LOOK = "/wizard/v1/pages/wizard-look/look.json"
KEYFILE = "feature-switches.key"


class SwitchError(RuntimeError):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def _home(home: Path | None) -> Path:
    from .updates import _home as h
    return h(home)


# --- who may call ------------------------------------------------------------------------------------------------

def _grants(home: Path | None) -> Path:
    return _home(home) / "grants" / "feature-switches.json"


def _read_grants(home: Path | None) -> list[dict]:
    try:
        rows = json.loads(_grants(home).read_text()).get("incoming")
    except (OSError, ValueError, AttributeError):
        return []
    return [r for r in rows or [] if isinstance(r, dict) and r.get("from") in APPS
            and isinstance(r.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", r["sha256"])]


def _write_grants(home: Path | None, rows: list[dict]) -> None:
    p = _grants(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"incoming": rows}, f, indent=2)
    tmp.replace(p)


def caller(key: str | None, home: Path | None = None) -> str | None:
    """The app a presented X-Wizard-Grant belongs to, or None. Constant-time compare on the hash."""
    if not key or not re.fullmatch(r"[0-9a-f]{64}", key):
        return None
    got = hashlib.sha256(key.encode()).hexdigest()
    for r in _read_grants(home):
        if hmac.compare_digest(got, r["sha256"]):
            return r["from"]
    return None


def grant(app: str, key_dir: Path, home: Path | None = None) -> dict:
    """Mint a key for an allowed app: the hash stays on the box, the key goes to the app's /wizard mount."""
    if app not in APPS:
        raise SwitchError(f"only {', '.join(APPS)} may switch features", 403)
    key = secrets.token_hex(32)
    key_dir.mkdir(parents=True, exist_ok=True)
    kf = key_dir / KEYFILE
    tmp = kf.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)   # read-only inside the app's own mount
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    tmp.replace(kf)
    rows = [r for r in _read_grants(home) if r["from"] != app]
    _write_grants(home, rows + [{"from": app, "sha256": hashlib.sha256(key.encode()).hexdigest()}])
    return {"app": app, "granted": True, "key_file": str(kf)}


def revoke(app: str, key_dir: Path | None = None, home: Path | None = None) -> dict:
    _write_grants(home, [r for r in _read_grants(home) if r["from"] != app])
    if key_dir is not None:
        (key_dir / KEYFILE).unlink(missing_ok=True)
    return {"app": app, "granted": False}


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
