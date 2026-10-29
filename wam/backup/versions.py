"""Restore never downgrades an app (wizard-os#91).

A restore keeps the owner's data exactly and runs it on the destination box's current release of each app. When the
backup was made on a NEWER release than the one this box offers, that app is refused (its data is not put back, this
box's copy is left as it is) with a message telling the owner to update the box and restore again; other apps go on.

Versions are wizard.md versions: upstream semver, optionally followed by a Wizard patch level, e.g. `2.63.23-wizard1`.
The upstream part is compared numerically first, then the `-wizardN` number (a plain upstream release is wizard0),
and an ordinary pre-release (`-rc1`, `-beta.2`) sorts before its release, as in semver. When either side cannot be
parsed, the app commit decides only when it is the same commit (same release); otherwise the restore goes on with a
warning: an unknown version never blocks a restore.
"""
from __future__ import annotations

import json
import re
from .filesystem import Path

import yaml

_VERSION = re.compile(r"^\s*v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\s*$")
_WIZARD = re.compile(r"(?:^|[.-])wizard[.-]?(\d+)$", re.I)


def parse(v) -> tuple | None:
    """A sortable key for a version string, or None when it is not a version this understands."""
    if v is None or isinstance(v, bool):
        return None
    m = _VERSION.match(str(v))
    if not m:
        return None
    nums = [int(x) for x in m.group(1).split(".")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    pre_s = m.group(2) or ""
    wizard = 0
    w = _WIZARD.search(pre_s)
    if w:
        wizard = int(w.group(1))
        pre_s = pre_s[:w.start()]
    pre = []
    for part in [p for p in re.split(r"[.-]", pre_s) if p]:
        pre.append((0, int(part), "") if part.isdigit() else (1, 0, part.lower()))
    # no pre-release sorts after any pre-release of the same upstream version
    return (tuple(nums), (1,) if not pre else (0, tuple(pre)), wizard)


def compare(a, b) -> int | None:
    """-1, 0, 1 as a <, ==, > b; None when either cannot be parsed."""
    ka, kb = parse(a), parse(b)
    if ka is None or kb is None:
        return None
    return (ka > kb) - (ka < kb)


def _front(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    try:
        got = yaml.safe_load(m.group(1)) if m else {}
    except yaml.YAMLError:
        return {}
    return got if isinstance(got, dict) else {}


def package_release(d: Path) -> dict | None:
    """{version, commit} of an app package folder (wizard.md version, .wizard-commit), None when there is none."""
    d = Path(d)
    if not d.is_dir():
        return None
    version = commit = None
    try:
        if (d / "wizard.md").is_file():
            v = _front((d / "wizard.md").read_text()).get("version")
            version = str(v) if v not in (None, "") else None
        if version is None and (d / "config.json").is_file():
            v = (json.loads((d / "config.json").read_text()) or {}).get("version")
            version = str(v) if v not in (None, "") else None
        if (d / ".wizard-commit").is_file():
            commit = (d / ".wizard-commit").read_text().strip() or None
    except (OSError, ValueError, AttributeError):
        pass
    return {"version": version, "commit": commit} if (version or commit) else None


def backed_up(rec: dict, staged_package: Path | None = None) -> dict:
    """The release a backed-up app ran: its apps.json row (manifest), else the bundled package it carried."""
    version = str(rec["version"]) if rec.get("version") not in (None, "") else None
    commit = rec.get("commit") or None
    if (version is None or commit is None) and staged_package is not None:
        pkg = package_release(staged_package) or {}
        version = version or pkg.get("version")
        commit = commit or pkg.get("commit")
    return {"version": version, "commit": commit}


def _label(v: dict) -> str:
    return v.get("version") or (f"commit {v['commit'][:12]}" if v.get("commit") else "an unknown version")


def check(rec: dict, have: dict, here: dict | None) -> dict:
    """Decide one app: {"verdict": "ok" | "refuse" | "warn", "message", "backup", "here"}.

    `have` is the backed-up release, `here` what this box runs or will install (None: not known before install)."""
    name = rec.get("name") or rec.get("id")
    out = {"id": rec.get("id"), "name": name, "backup": have, "here": here}
    if here is None:
        return {**out, "verdict": "warn", "message": f"{name}: this box's release is not known before it is "
                                                     "installed; it is checked again after installing"}
    c = compare(have.get("version"), here.get("version"))
    if c is not None:
        if c > 0:
            return {**out, "verdict": "refuse",
                    "message": f"{name} not restored: your backup is from {name} {have['version']}, newer than this "
                               f"box's {here['version']}. Update this box, then restore again."}
        return {**out, "verdict": "ok", "message": None}
    if have.get("commit") and have.get("commit") == here.get("commit"):
        return {**out, "verdict": "ok", "message": None}      # same commit: the same release, whatever its label
    return {**out, "verdict": "warn",
            "message": f"{name}: cannot tell whether the backup's {_label(have)} is newer than this box's "
                       f"{_label(here)}; restoring it anyway"}


__all__ = ["parse", "compare", "package_release", "backed_up", "check"]
