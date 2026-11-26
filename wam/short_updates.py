"""Shorter app updates (wizard-app-manager#1074, box switch wizard/shorter-app-updates, off by default).

An update of a running app takes a backup first. Without the switch Runtipi stops the app for that backup, copies its
whole app data (twice), starts the old version again, and then stops it a second time to start the new one: James's
Messenger was down about 7 minutes, then again. With the switch on, wam asks Runtipi for the short path
(`wizardShortDowntime` in the update request): the new images are pulled and the app data is copied while the old
version still runs, the app stops once, only what changed since is copied, and the new version starts straight away;
the backup archive is packed while the new version runs. Runtipi images without it ignore the field (old path).
"""
from __future__ import annotations

from pathlib import Path

SWITCH = "wizard/shorter-app-updates"


def on(home: Path | None = None) -> bool:
    """Whether the owner turned wizard/shorter-app-updates on (False on For everyone or when it can't be read;
    tests monkeypatch this)."""
    from . import feature_switches
    return feature_switches.is_on(SWITCH, home, timeout=4)
