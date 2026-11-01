"""Bounded retention for the backups an update makes.

`wam app update` asks Runtipi for a per-app backup (a tar.gz of the app's data) and `wam hermes update` snapshots
Hermes into `<wizard-os home>/backups/hermes-*`. Nothing removed old copies, so every update added another full copy
of the app's data (and backup tools then mirrored them all). After a successful update we keep the newest copy, so a
fresh rollback point always survives, and remove older copies past a minimum age, so copies made minutes apart are
never raced. Pruning is best effort: it never fails the update.

Defaults can be changed in `<wizard-os home>/app-manager.json`:
    "update_backups": {"keep": 1, "app_max_age_hours": 12, "hermes_max_age_days": 30}
"""
from __future__ import annotations

import calendar
import json
import re
import shutil
import time
from pathlib import Path

KEEP = 1                    # newest copies always kept
APP_MAX_AGE_HOURS = 12      # Runtipi per-app backups older than this (beyond the newest) are removed
HERMES_MAX_AGE_DAYS = 30    # wam's Hermes update snapshots older than this (beyond the newest) are removed
FRESH_SLACK_MS = 120_000    # the newest app backup must be this recent relative to the update start (clock skew)

HERMES_DIR = re.compile(r"hermes-(\d{8}T\d{6}Z)-[0-9a-f]{8}")


def settings(cfg: dict | None) -> dict:
    """Retention settings from app-manager.json's `update_backups`, falling back to the defaults on bad values."""
    raw = (cfg or {}).get("update_backups") if isinstance(cfg, dict) else None
    raw = raw if isinstance(raw, dict) else {}

    def num(key, default, lo):
        v = raw.get(key, default)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= lo else default

    return {"keep": int(num("keep", KEEP, 1)),
            "app_max_age_hours": num("app_max_age_hours", APP_MAX_AGE_HOURS, 0),
            "hermes_max_age_days": num("hermes_max_age_days", HERMES_MAX_AGE_DAYS, 0)}


def journal_backups(home: Path | None, urn: str) -> set[str]:
    """Runtipi backups of `urn` named by a restore journal (`<home>/restores/*/journal.json`, written before
    appdata.py): `wam backup undo` restores exactly that file, so it must never be pruned. Raises OSError/ValueError
    when a journal cannot be read, so the caller prunes nothing rather than guess."""
    names: set[str] = set()
    root = Path(home) / "restores" if home else None
    if not root or not root.is_dir():
        return names
    for journal in root.glob("*/journal.json"):
        doc = json.loads(journal.read_text())
        for s in (doc.get("steps") or []) if isinstance(doc, dict) else []:
            if isinstance(s, dict) and s.get("urn") == urn and s.get("backup"):
                names.add(str(s["backup"]))
    return names


def prune_app(rt, urn: str, started_ms: int, opts: dict | None = None, now_ms: int | None = None,
              home: Path | None = None) -> dict:
    """Prune one app's Runtipi backups through Runtipi's API, after an update of `urn` that began at `started_ms`.

    Only `urn`'s backups are listed or deleted. Nothing is deleted unless the newest backup was made by this update
    (no fresh rollback point, no pruning), and a backup a restore journal under `home` names is always kept. Returns {"deleted": [...], "kept": [...], "errors": [...]}; never raises.
    """
    o = opts or settings(None)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    out: dict = {"deleted": [], "kept": [], "errors": []}
    try:
        protected = journal_backups(home, urn)
    except (OSError, ValueError) as e:
        out["errors"].append(f"could not read the restore journals, so no backup was removed: {e}"[:300])
        return out
    try:
        backups = [b for b in (rt.list_backups(urn) or []) if isinstance(b, dict) and b.get("id")]
    except Exception as e:  # noqa: BLE001 - pruning must never fail the update
        out["errors"].append(f"could not list backups: {e}"[:300])
        return out
    backups.sort(key=lambda b: b.get("date") or 0, reverse=True)
    if not backups or (backups[0].get("date") or 0) < started_ms - FRESH_SLACK_MS:
        out["skipped"] = "this update made no new backup"
        out["kept"] = [b["id"] for b in backups]
        return out
    cutoff = now_ms - o["app_max_age_hours"] * 3600 * 1000
    for i, b in enumerate(backups):
        if (i < o["keep"] or b["id"] in protected or not isinstance(b.get("date"), (int, float))
                or b["date"] >= cutoff):
            out["kept"].append(b["id"])
            continue
        try:
            rt.delete_backup(urn, b["id"])
            out["deleted"].append(b["id"])
        except Exception as e:  # noqa: BLE001
            out["kept"].append(b["id"])
            out["errors"].append(f"could not delete {b['id']}: {e}"[:300])
    return out


def prune_hermes(backups_dir: Path, opts: dict | None = None, now: float | None = None,
                 protect: str | Path | None = None) -> dict:
    """Prune wam's own Hermes update snapshots in `backups_dir` after a successful `wam hermes update`.

    Only real directories named like wam's snapshots (`hermes-<UTC stamp>-<8 hex>`) are considered; the age comes from
    the name. The newest `keep` and `protect` (the snapshot this update made) always stay. Never raises.
    """
    o = opts or settings(None)
    now = time.time() if now is None else now
    out: dict = {"deleted": [], "kept": [], "errors": []}
    found = []
    try:
        for p in Path(backups_dir).iterdir():
            m = HERMES_DIR.fullmatch(p.name)
            if not m or p.is_symlink() or not p.is_dir():
                continue
            try:
                stamp = calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%SZ"))
            except (ValueError, OverflowError):
                continue
            found.append((stamp, p))
    except OSError as e:
        out["errors"].append(f"could not list {backups_dir}: {e}"[:300])
        return out
    found.sort(key=lambda t: (t[0], t[1].name), reverse=True)
    keep_path = Path(protect).resolve() if protect else None
    cutoff = now - o["hermes_max_age_days"] * 86400
    for i, (stamp, p) in enumerate(found):
        if i < o["keep"] or stamp >= cutoff or (keep_path and p.resolve() == keep_path):
            out["kept"].append(p.name)
            continue
        try:
            shutil.rmtree(p)
            out["deleted"].append(p.name)
        except OSError as e:
            out["kept"].append(p.name)
            out["errors"].append(f"could not delete {p.name}: {e}"[:300])
    return out
