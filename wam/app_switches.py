"""App feature switches in the box's one switch catalog (wizard.app-switches/1; Miguel, 10 Nov 2026).

Every new feature, in WizardOS core or in any installed app, ships behind a switch that is off by default and is
flipped with one click from the WizardOS Tracker's Try it toggle. Core switches are the shell's tweaks
(feature_switches.py). This module adds the switches of every installed app that implements the app-switch contract
(wizard-os docs/APP_SWITCHES.md), so `feature_switches.listing()` serves ONE catalog and the Tracker reads one place.

Discovery: an installed app declares its switch endpoint in its wizard.md front matter:

    switches: /api/switches                 # shorthand, or
    switches:
      contract: wizard.app-switches/1
      path: /api/view/switches

The App Manager reads the installed recipe's wizard.md (`<runtipi>/apps/<store>/<id>/wizard.md`), else a `switches`
key in the app's registry entry. Apps that declare nothing are skipped; an app added tomorrow appears as soon as it is
installed or updated with the declaration.

Calls are server-side, on the app's loopback port (its registry `view.local`), with the OWNER's authority: the platform
owner proof (wizard-os spec/OWNER_PROOF.md, `X-Wizard-App-Owner-Proof`, hmac-v1, bound to the app id, method and
target) signed with the app's proof key, plus the box's own Origin and `X-Wizard-Switch: 1`. So a flip works the same
from every browser and device, with no app cookie in the browser. The proof key is the edge key the App Manager mints
for apps that declare `owner_proof` (owner_proof.edge_key_path), or a box's adopted hand-built one
(`<home>/<app>-owner/proof`). An app without a proof key is never called through the box's own Tailscale Serve URL:
Serve names the owner for every request that starts on this box, which spec/OWNER_PROOF.md section 3 rules out
(wizard-os#133). It is listed in `apps` with `ok: false` and the Tracker falls back to its same-origin browser call
(Tracker 0.21), where Serve names the real owner of that browser.

Ids in the merged catalog are `<app id>:<switch id>` (core ids are `<author>/<id>`), so the two never collide.
App switches follow the app's own state; they are not locked by the box channel (the app keeps them off by default).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CONTRACT = "wizard.app-switches/1"
APP_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
SWITCH_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
FULL_ID = re.compile(r"([a-z0-9][a-z0-9-]{0,62}):([a-z0-9][a-z0-9-]{0,63})")
ISSUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}#\d{1,7}")
PATH = re.compile(r"/[A-Za-z0-9._~/-]{0,120}")
TIMEOUT = 4.0


class AppSwitchError(RuntimeError):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def _home(home):
    from .updates import _home as h
    return h(home)


def _runtipi_root(home: Path) -> Path:
    try:
        cfg = json.loads((home / "app-manager.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    return Path(os.environ.get("RUNTIPI_ROOT") or cfg.get("runtipi_root") or Path.home() / "runtipi").expanduser()


def parse(raw) -> str | None:
    """The switch endpoint path from a `switches:` declaration, None when absent or malformed."""
    if isinstance(raw, dict):
        if raw.get("contract", CONTRACT) != CONTRACT:
            return None
        raw = raw.get("path")
    if isinstance(raw, str) and PATH.fullmatch(raw) and ".." not in raw:
        return raw
    return None


def _wizard_md(root: Path, store: str, app_id: str) -> dict:
    from .network import _front
    try:
        return _front((root / "apps" / store / app_id / "wizard.md").read_text())
    except OSError:
        return {}


def declared(home=None) -> list[dict]:
    """Installed apps that declare a switch endpoint: [{id, name, path, local}]."""
    h = _home(home)
    try:
        reg = json.loads((h / "apps.json").read_text())
    except (OSError, ValueError):
        return []
    root = _runtipi_root(h)
    out = []
    for a in reg.get("apps") or []:
        if not isinstance(a, dict) or not APP_ID.fullmatch(str(a.get("id") or "")):
            continue
        store = (str(a.get("urn") or "").split(":", 1) + ["wizardapps"])[1] or "wizardapps"
        if not APP_ID.fullmatch(store):
            store = "wizardapps"
        path = parse(_wizard_md(root, store, a["id"]).get("switches")) or parse(a.get("switches"))
        view = a.get("view") or {}
        local = str(view.get("local") or "")
        prefix = str(view.get("url") or "")
        if not path or not re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}(/[a-z0-9][a-z0-9-]{0,62})?/?", local):
            continue
        out.append({"id": a["id"], "name": str(a.get("name") or a["id"])[:80], "path": path, "local": local.rstrip("/"),
                    "prefix": prefix.rstrip("/") if re.fullmatch(r"/[a-z0-9][a-z0-9-]{0,62}/?", prefix) else ""})
    return out


def missing(home=None) -> list[dict]:
    """Installed apps with a page that declare no switch endpoint (Miguel, 11 Nov 2026: every Wizard app ships
    wizard.app-switches/1, even with an empty list). `wam check --installed` and `wam doctor` warn about them."""
    h = _home(home)
    try:
        reg = json.loads((h / "apps.json").read_text())
    except (OSError, ValueError):
        return []
    root = _runtipi_root(h)
    have = {a["id"] for a in declared(h)}
    out = []
    for a in reg.get("apps") or []:
        if not isinstance(a, dict) or not APP_ID.fullmatch(str(a.get("id") or "")) or a["id"] in have:
            continue
        view = a.get("view") or {}
        if not view.get("url") and not view.get("local"):
            continue   # no page, nothing for Try it to reach
        store = (str(a.get("urn") or "").split(":", 1) + ["wizardapps"])[1] or "wizardapps"
        if not APP_ID.fullmatch(store):
            store = "wizardapps"
        doc = _wizard_md(root, store, a["id"])
        why = "wizard.md has no `switches:`" if not parse(doc.get("switches")) and not parse(a.get("switches")) \
            else "no loopback view to call"
        out.append({"id": a["id"], "name": str(a.get("name") or a["id"])[:80], "version": str(doc.get("version") or a.get("version") or ""),
                    "why": why})
    return out


def proof_key(home: Path, app_id: str) -> bytes | None:
    """The app's owner-proof key: the platform edge key, else a box's adopted hand-built one."""
    from . import owner_proof
    for p in (owner_proof.edge_key_path(home, app_id), home / f"{app_id}-owner" / "proof"):
        try:
            return owner_proof.read_private_key(p)
        except (OSError, ValueError):
            continue
    return None


_origin_cache: dict = {}
_origin_lock = threading.Lock()


def box_origin(home: Path) -> str | None:
    """The Wizard's own browser origin (what an owner page on this box sends as Origin)."""
    from . import store_install
    got = sorted(store_install.gateway_origins(home))
    if got:
        return got[0]
    with _origin_lock:
        hit = _origin_cache.get(str(home))
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
    try:
        from . import cli
        origins = sorted(cli.shell_origins(cli.Config()))
    except (Exception, SystemExit):            # noqa: BLE001 - discovery only; the app then refuses plainly
        origins = []
    pick = next((o for o in origins if o.endswith(":8444")), origins[0] if origins else None)
    with _origin_lock:
        _origin_cache[str(home)] = (time.monotonic(), pick)
    return pick


def _call(home: Path, app: dict, body: dict | None = None, origin: str | None = None, opener=None) -> dict:
    """One switch request to the app as the owner: its loopback port, signed with the app's owner-proof key, with the
    box Origin and `X-Wizard-Switch: 1`. Without a key there is no server-side call (a box-local request through
    Serve would carry the owner's login for any caller on the box; wizard-os#133)."""
    from . import owner_proof
    key = proof_key(home, app["id"])
    method = "POST" if body is not None else "GET"
    o = origin or box_origin(home)
    headers = {"Accept": "application/json", "X-Wizard-Switch": "1"}
    if key is None:
        raise AppSwitchError(f"{app['name']} has no owner proof key; its switches are flipped from the owner's browser",
                             503)
    headers[owner_proof.HEADER] = owner_proof.sign(key, app["id"], method, app["path"])
    url = app["local"] + app["path"]
    if o:
        headers["Origin"] = o
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT) as r:
            j = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = (json.loads(e.read() or b"{}") or {}).get("error")
        except ValueError:
            msg = None
        raise AppSwitchError(f"{app['name']}: {msg if isinstance(msg, str) else 'HTTP ' + str(e.code)}"[:200],
                             e.code if e.code in (400, 401, 403, 404, 409) else 502) from None
    except (OSError, ValueError, urllib.error.URLError) as e:
        raise AppSwitchError(f"can't reach {app['name']} ({str(e)[:100]})", 503) from None
    if not isinstance(j, dict) or not isinstance(j.get("switches"), list):
        raise AppSwitchError(f"{app['name']} did not answer with {CONTRACT}", 502)
    return j


def _rows(app: dict, j: dict) -> list[dict]:
    out = []
    for s in j.get("switches") or []:
        if not isinstance(s, dict) or not SWITCH_ID.fullmatch(str(s.get("id") or "")):
            continue
        issues = [str(i) for i in (s.get("issues") or []) if isinstance(i, str) and ISSUE.fullmatch(i)][:12]
        out.append({"id": f"{app['id']}:{s['id']}", "kind": "app", "app": app["id"], "app_name": app["name"],
                    "switch": s["id"], "name": str(s.get("name") or s["id"])[:120],
                    "description": str(s.get("description") or "")[:600], "author": app["id"],
                    "on": s.get("on") is True, "default": s.get("default") is True, "issues": issues, "locked": False})
    return out


def listing(home=None, apps=None, opener=None) -> tuple[list[dict], list[dict]]:
    """(switch rows, per-app status [{id, name, ok, error?}]) for every installed app that declares switches."""
    h = _home(home)
    apps = declared(h) if apps is None else apps
    if not apps:
        return [], []

    def one(app):
        try:
            return _rows(app, _call(h, app, opener=opener)), {"id": app["id"], "name": app["name"], "ok": True}
        except AppSwitchError as e:
            return [], {"id": app["id"], "name": app["name"], "ok": False, "error": str(e), "status": e.code}

    with ThreadPoolExecutor(max_workers=min(8, len(apps))) as ex:
        got = list(ex.map(one, apps))
    rows = [r for rs, _ in got for r in rs]
    return sorted(rows, key=lambda t: (t["app_name"].lower(), t["name"].lower())), [s for _, s in got]


def set_switch(full_id: str, on: bool, home=None, origin: str | None = None, apps=None, opener=None) -> dict:
    """Flip `<app id>:<switch id>` in that app, server-side, as the owner. Returns the app's changed row."""
    m = FULL_ID.fullmatch(full_id or "")
    if not m:
        raise AppSwitchError("send {\"id\": \"<app>:<switch>\", \"on\": true|false}")
    if not isinstance(on, bool):
        raise AppSwitchError("\"on\" must be true or false")
    h = _home(home)
    app = next((a for a in (declared(h) if apps is None else apps) if a["id"] == m.group(1)), None)
    if app is None:
        raise AppSwitchError(f"{m.group(1)} is not an installed app with switches", 404)
    j = _call(h, app, {"id": m.group(2), "on": on}, origin=origin, opener=opener)
    row = next((r for r in _rows(app, j) if r["switch"] == m.group(2)), None)
    if row is None:
        raise AppSwitchError(f"{full_id} is not a switch of {app['name']}", 404)
    return row
