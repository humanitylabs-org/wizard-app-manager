"""Is this box running what the Store ships? (`wam standard`)

Every Wizard should run the published release of each app: the recipe the index lists, with images any box can
pull. Box-only changes (a hand-built image, a `pull_policy: never` image, a user-config override that swaps an
image or replaces a command, an app installed from a private source) work on one box and silently drift from
everyone else's, so fixes and bugs stop being shared. This check names each one so it can be moved back.

Allowed without a finding: wam's own override blocks (app networks, alongside wiring) and a release pin, i.e. an
override image that is a public registry reference pinned by digest. The owner's personal shell build
(my-release) is reported as `personal`, never as drift.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

STORE = "wizardapps"
STATES = ("standard", "custom", "unpublished", "personal")
REGISTRY_PIN = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}(?::\d+)?/[a-z0-9._/-]+(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
VAR = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?-([^}]*))?\}")


class _Loader(yaml.SafeLoader):
    """Compose files use !override / !reset; read them as plain values."""


def _any_tag(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_Loader.add_multi_constructor("!", _any_tag)


def _load(p: Path) -> dict:
    try:
        return yaml.load(p.read_text(), Loader=_Loader) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _resolve(image: str) -> str:
    return VAR.sub(lambda m: m.group(1) or "", str(image or "")).strip()


def _local_image(image: str, policy: str | None) -> bool:
    """An image only this box has: never pulled, or named by a bare local image id."""
    return policy == "never" or image.startswith("sha256:")


def check_app(runtipi_root: Path, app_id: str, personal: set[str] | frozenset = frozenset(),
              listed: dict[str, str] | None = None) -> dict:
    installed = runtipi_root / "apps" / STORE / app_id
    reasons: list[str] = []
    try:
        src = json.loads((installed / ".wizard-source").read_text())
    except (OSError, ValueError):
        src = {}
    origin = src.get("source") or ("owner-private" if src.get("private") else "standard")
    repo = str(src.get("repo") or "").lower()
    try:
        commit = (installed / ".wizard-commit").read_text().strip()
    except OSError:
        commit = ""
    notes: list[str] = []
    own = False
    if listed is not None:
        if repo in listed:
            # Older installs recorded no source; a repo the index lists is a Store install whatever the label.
            origin = "standard"
            if commit and listed[repo] != commit:
                notes.append(f"Store has a newer release ({listed[repo][:12]}); the nightly update moves it")
        else:
            own = True   # the owner's own app, not published: allowed, but nobody else gets its fixes
    if origin != "standard" and not own:
        reasons.append(f"installed from a {origin} source, not the Store listing")
    recipe = _load(installed / "docker-compose.yml")
    for name, svc in (recipe.get("services") or {}).items():
        svc = svc or {}
        img = _resolve(svc.get("image", ""))
        if svc.get("image") and svc.get("build") is None and _local_image(img, svc.get("pull_policy")):
            reasons.append(f"{name}: recipe image {img[:40]} exists only on this box")
    override = _load(runtipi_root / "user-config" / STORE / app_id / "docker-compose.yml")
    for name, svc in (override.get("services") or {}).items():
        svc = svc or {}
        if "image" in svc:
            img = _resolve(svc["image"])
            if _local_image(img, svc.get("pull_policy")) or not REGISTRY_PIN.match(img):
                reasons.append(f"{name}: override runs {img[:60]} instead of the Store image")
        for key in ("command", "entrypoint", "build"):
            if key in svc:
                reasons.append(f"{name}: override replaces {key}")
        if name not in (recipe.get("services") or {}) and set(svc) - {"networks"}:
            reasons.append(f"{name}: service exists only in this box's override")
    if app_id in personal:
        return {"id": app_id, "state": "personal", "reasons": reasons or ["owner's own build, kept on purpose"]}
    if own and not reasons:
        return {"id": app_id, "state": "unpublished", "reasons": ["not in the Store; only this box has it"], "notes": notes}
    return {"id": app_id, "state": "custom" if reasons else "standard", "reasons": reasons, "notes": notes}


def check(runtipi_root: Path, home: Path | None = None, index_url: str | None = None) -> dict:
    listed = None
    if index_url:
        try:
            from . import index
            listed = {str(a.get("repo", "")).lower(): (a.get("install") or {}).get("commit", "")
                      for a in index.all_listed(index_url)}
        except Exception:
            listed = None   # offline: judge only what is on disk
    personal: set[str] = set()
    # A my-release box (wizard-os#186) builds the owner's own shell on purpose.
    if home is not None and (home / "bin/my-release").is_file() and (home / "my-release.status").is_file():
        personal.add("wizard-ux")
    apps_dir = runtipi_root / "apps" / STORE
    rows = [check_app(runtipi_root, d.name, personal, listed) for d in sorted(apps_dir.iterdir()) if d.is_dir()] \
        if apps_dir.is_dir() else []
    count = {s: sum(r["state"] == s for r in rows) for s in STATES}
    return {"apps": rows, "index_checked": listed is not None, **count}


def describe(res: dict) -> str:
    lines = [", ".join(f"{res[s]} {s}" for s in STATES)]
    for r in res["apps"]:
        if r["state"] != "standard" or r.get("notes"):
            lines.append(f"{r['state'].upper():<12} {r['id']}")
            lines += [f"             - {x}" for x in r["reasons"] + r.get("notes", [])]
    return "\n".join(lines)
