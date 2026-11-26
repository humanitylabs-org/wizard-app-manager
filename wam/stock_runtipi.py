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
