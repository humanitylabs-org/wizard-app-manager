"""The stock-Runtipi switch (wizard-os#822, cut phase 1: stock Runtipi replaces the App Manager fork).

Off by default. With it off nothing changes: wam talks to the App Manager (the Runtipi fork) exactly as before.
On, wam itself keeps the work the fork's backend did that stock Runtipi has no place for, starting with the app
grants and connections (wam/stock_grants.py): the owner API the fork served under /apps/wizard/... and
/apps/<id>:wizardapps/... is answered by wam, from the same state file the fork kept.

The switch is on when either
  - the environment says so: WIZARD_STOCK_RUNTIPI=1 (also true/on/yes), or
  - the Wizard home holds stock-runtipi.json with {"on": true} (what the installer writes on a stock box).
WIZARD_STOCK_RUNTIPI=0 (false/off/no) turns it off whatever the file says.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ENV = "WIZARD_STOCK_RUNTIPI"
FILE = "stock-runtipi.json"
_ON = ("1", "true", "on", "yes")
_OFF = ("0", "false", "off", "no")


def _home(home: Path | None) -> Path:
    if home is not None:
        return Path(home)
    return Path(os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def on(home: Path | None = None) -> bool:
    v = os.environ.get(ENV, "").strip().lower()
    if v in _ON:
        return True
    if v in _OFF:
        return False
    try:
        doc = json.loads((_home(home) / FILE).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("on") is True


# ------------------------------------------------------------------ the store a stock box installs from
# The installer switch (WIZARD_STOCK_RUNTIPI=1) adds this git store to stock Runtipi as `wizardapps`, the slug every
# Wizard urn uses. It is generated from the Wizard App Index by wizard-app-store (scripts/stock-store, its `stock`
# branch): every app's runtipi folder at its approved commit, plus wizard.md, .wizard-source and .wizard-commit.
# WIZARD_STOCK_STORE points a box at another branch or fork (a trial store).
STORE_ENV = "WIZARD_STOCK_STORE"
STORE_URL = "https://github.com/humanitylabs-org/wizard-app-store/tree/stock"


def store_url() -> str:
    return (os.environ.get(STORE_ENV) or "").strip() or STORE_URL


def write_marker(home: Path | None = None, store: str | None = None) -> Path:
    """What the installer leaves on a stock box: the switch (on) and the store it was installed from."""
    h = _home(home)
    h.mkdir(parents=True, exist_ok=True)
    p = h / FILE
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"on": True, "store": store or store_url()}, indent=2) + "\n")
    os.replace(tmp, p)
    return p


def marker_store(home: Path | None = None) -> str | None:
    try:
        doc = json.loads((_home(home) / FILE).read_text())
    except (OSError, ValueError):
        return None
    s = doc.get("store") if isinstance(doc, dict) else None
    return s if isinstance(s, str) and s.strip() else None


def box_store_url(home: Path | None = None) -> str:
    """The environment wins (an installer run), then the store this box was installed with, then the default."""
    return (os.environ.get(STORE_ENV) or "").strip() or marker_store(home) or STORE_URL


class NotInStore(LookupError):
    pass


def record(root: Path, store: str, app_id: str) -> dict:
    """An index-like record for one app, read from the git store Runtipi cloned (repos/<store>/apps/<id>), so an
    install on a stock box never asks the App Index: {id, repo, uses, provides, install: {commit}, commit, source}."""
    import re
    from . import wizardmd
    # Ids and store slugs are single path names (Runtipi's own rule), so the folder stays inside the store.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", app_id or "") or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", store or ""):
        raise NotInStore(f"'{app_id}' is not an app id in the Wizard store ({store})")
    base = (Path(root) / "repos" / store / "apps").resolve()
    d = (base / app_id).resolve()
    if d.parent != base:
        raise NotInStore(f"'{app_id}' is not an app id in the Wizard store ({store})")
    if not (d / "config.json").is_file():
        raise NotInStore(f"no app with id '{app_id}' in the Wizard store ({store})")
    try:
        manifest = wizardmd.front_matter((d / "wizard.md").read_text())
    except (OSError, ValueError):
        manifest = {}
    try:
        src = json.loads((d / ".wizard-source").read_text())
    except (OSError, ValueError):
        src = {}
    try:
        commit = (d / ".wizard-commit").read_text().strip()
    except OSError:
        commit = ""
    lst = lambda v: [str(x) for x in v] if isinstance(v, list) else []  # noqa: E731
    return {"id": app_id, "repo": str(src.get("repo") or manifest.get("repo") or ""),
            "name": manifest.get("name") or app_id, "uses": lst(manifest.get("uses")),
            "provides": lst(manifest.get("provides")), "install": {"commit": commit}, "commit": commit,
            "source": "stock-store"}
