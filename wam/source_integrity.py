"""Accidental owner-edit protection for managed source copies; not a hostile-owner attestation."""
import hashlib
import json
import os
from pathlib import Path
import yaml

LIMIT = 256 * 1024 * 1024


def inventory(root: Path) -> dict:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source is not a regular directory")
    entries, total = {}, 0

    def walk(here):
        nonlocal total
        for p in sorted(here.iterdir()):
            if here == root and p.name == ".git":
                continue
            key = p.relative_to(root).as_posix()
            if p.is_symlink():
                entries[key] = {"link": os.readlink(p)}
            elif p.is_dir():
                entries[key] = {"directory": True}
                walk(p)
            elif p.is_file():
                total += p.stat().st_size
                if total > LIMIT:
                    raise ValueError("source inventory exceeds size limit")
                digest = hashlib.sha256()
                with p.open("rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        digest.update(block)
                entries[key] = {"sha256": digest.hexdigest()}
            else:
                raise ValueError("unsupported source file")
    walk(root)
    return entries


def record_path(root: Path) -> Path:
    return root.with_name(root.name + ".inventory.json")


def record(root: Path, expected: dict | None = None) -> None:
    record_path(root).write_text(json.dumps(inventory(root) if expected is None else expected, sort_keys=True) + "\n")


def pristine(root: Path) -> bool:
    try:
        expected = json.loads(record_path(root).read_text())
        return isinstance(expected, dict) and expected == inventory(root)
    except (OSError, ValueError):
        return False


def update_block(runtipi_root: Path, store: str, app: str) -> str | None:
    """Read only the installed recipe; a store check can already prepare the next upstream version."""
    from . import packaging
    from .wizardmd import front_matter
    installed = Path(runtipi_root) / "apps" / store / app
    manifest = installed / "wizard.md"
    if not manifest.is_file():
        return None  # legacy image recipe, no managed source copy
    try:
        doc = front_matter(manifest.read_text())
        # Read only the installed front matter, never execute app code.
        if (doc.get("runtime") or {}).get("package") not in ("source", "build"):
            return None
        commit = (installed / ".wizard-commit").read_text().strip()
        if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            return "source-unverified"
        source = packaging.source_dir(packaging.app_data_root(runtipi_root), store, app, commit)
        return verify_installed(source, installed, commit)
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        return "source-unverified"


def verify_installed(source: Path, installed: Path, commit: str) -> str | None:
    """Migrate legacy copies only against a fresh, exact installed-commit checkout.

    The prepared store recipe/cache can already be newer or locally edited. Neither
    is evidence. Never create/replace source here; only save trusted expected bytes.
    """
    import re
    import tempfile
    from . import private
    try:
        path = record_path(source)
        if path.exists() or path.is_symlink():
            expected = json.loads(path.read_text())
            if not isinstance(expected, dict):
                return "source-unverified"
            return None if expected == inventory(source) else "owner-source"
        # Missing/unreadable source is not proof of owner edits.
        actual = inventory(source)
        repo = json.loads((installed / '.wizard-source').read_text()).get('repo')
        if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            return "source-unverified"
        with tempfile.TemporaryDirectory(prefix='wam-verify-') as tmp:
            upstream = Path(tmp) / 'source'
            private.fetch(repo, commit, upstream, private.token())  # verifies fetched HEAD == commit
            expected = inventory(upstream)
        if actual != expected or inventory(source) != expected:
            return "owner-source"
        # Persist the trusted expected inventory, never re-snapshot arbitrary bytes.
        path.write_text(json.dumps(expected, sort_keys=True) + '\n')
        return None
    except (OSError, ValueError, TypeError, AttributeError, private.PrivateError):
        return "source-unverified"
