"""Alongside: the Claude connection app holds the exact Claude Code sign-in the host Hermes uses.

On a fresh box the Hermes Agent app and the Claude app share one named volume, so they already hold one sign-in.
Alongside, the host Hermes (Claude subscription provider) runs the official Claude Code against the owner's own login
folder (`~/.claude`, or the folder named by CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR / CLAUDE_CONFIG_DIR), while the
Claude app's recipe mounts its own volume: two sign-ins, and the app renewed and tested the one nobody uses. When that
host login expired, chats failed with a generic error and nothing on the box could renew it.

This module points the app's engine at the agent's login folder through a stock Runtipi user-config override:
- `/claude` becomes a bind mount of that folder, so the app's auto-renew and Connect/Test act on the agent's sign-in.
  Tokens are never copied: Claude refresh tokens are single-use, so two copies sign each other out.
- the engine runs as the owner: on rootless Docker that is container uid 0 (the owner, no capabilities, files stay
  owner-owned); on rootful Docker it is the owner's own uid:gid (container root would leave root-owned files in the
  owner's folder, and the recipe's uid 1000 can't read the owner's 0600 credentials unless the owner happens to be 1000).
Everything else in an existing override (owner image pins, environment) is kept. `wam alongside --off` removes only
what this wrote, and the app goes back to its own volume.
"""
from __future__ import annotations

import os
import pwd
import re
from pathlib import Path

import yaml

from .runtipi import read_env

PROVIDES = "claude-subscription"
TARGET = "/claude"
MARK = "x-wizard-claude-login"   # top-level compose extension: the folder wam bound (and the user it set)
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/@+-]+$")


# ---------------------------------------------------------------- does the host Hermes use a Claude subscription?

def _provider_names(cfg: dict) -> set[str]:
    out: set[str] = set()
    model = cfg.get("model")
    if isinstance(model, dict) and isinstance(model.get("provider"), str):
        out.add(model["provider"])
    for key in ("fallback_providers", "fallback_model"):
        v = cfg.get(key)
        for row in (v if isinstance(v, list) else [v]):
            if isinstance(row, dict) and isinstance(row.get("provider"), str):
                out.add(row["provider"])
            elif isinstance(row, str):
                out.add(row)
    return out


def uses_claude_subscription(cfg: dict) -> bool:
    """Hermes runs Claude (main model or a fallback) through a Claude subscription provider, i.e. the official
    Claude Code login (e.g. claude-subscription-directsdk-experimental)."""
    return any(n.startswith("claude-subscription") for n in _provider_names(cfg if isinstance(cfg, dict) else {}))


def owner_home(uid: int | None) -> Path:
    try:
        return Path(pwd.getpwuid(int(uid)).pw_dir) if uid is not None else Path.home()
    except (KeyError, ValueError, TypeError):
        return Path.home()


def login_dir(hermes_home: Path, uid: int | None = None, env: dict | None = None) -> Path:
    """The folder the host Hermes' Claude Code reads its sign-in from, exactly as the provider resolves it:
    CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR, else CLAUDE_CONFIG_DIR (Hermes' .env first, then its process
    environment), else the Hermes owner's ~/.claude."""
    henv = read_env(hermes_home / ".env") if (hermes_home / ".env").is_file() else {}
    penv: dict = dict(os.environ) if env is None else env
    home = owner_home(uid)
    for var in ("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", "CLAUDE_CONFIG_DIR"):
        v = str(henv.get(var) or penv.get(var) or "").strip()
        if v:
            p = Path(v.replace("~", str(home), 1) if v.startswith("~") else v)
            return p if p.is_absolute() else home / p
    return home / ".claude"


def engine_user(uid: int, gid: int, rootless: bool) -> str:
    return "0:0" if rootless else f"{int(uid)}:{int(gid)}"


# ---------------------------------------------------------------- the override (tag-preserving YAML merge)

class _Tagged:
    def __init__(self, tag, value):
        self.tag, self.value = tag, value


class _Loader(yaml.SafeLoader):
    pass


class _Dumper(yaml.SafeDumper):
    pass


def _tagged(loader, tag, node):
    make = {yaml.ScalarNode: loader.construct_scalar, yaml.SequenceNode: loader.construct_sequence,
            yaml.MappingNode: loader.construct_mapping}[type(node)]
    return _Tagged(tag, make(node))


def _represent(dumper, value):
    node = dumper.represent_data(value.value)
    node.tag = value.tag
    return node


_Loader.add_multi_constructor("!", lambda loader, tag, node: _tagged(loader, "!" + tag, node))
_Dumper.add_representer(_Tagged, _represent)


def _target(entry) -> str | None:
    if isinstance(entry, str):
        parts = entry.split(":")
        return parts[1] if len(parts) >= 2 else parts[0]
    if isinstance(entry, dict):
        return entry.get("target")
    return None


def recipe_compose(runtipi_root: Path, store: str, app_id: str) -> dict:
    for p in (runtipi_root / "apps" / store / app_id / "docker-compose.yml",
              runtipi_root / "repos" / store / "apps" / app_id / "docker-compose.yml"):
        try:
            return yaml.load(p.read_text(), Loader=_Loader) or {}
        except (OSError, yaml.YAMLError):
            continue
    return {}


def engine_services(compose: dict, app_id: str) -> list[str]:
    """Services in the app recipe that mount the Claude sign-in at /claude (the engine), else `<id>-engine`."""
    found = [name for name, svc in ((compose or {}).get("services") or {}).items()
             if isinstance(svc, dict) and any(_target(v) == TARGET for v in (svc.get("volumes") or []))]
    return found or [f"{app_id}-engine"]


DEFAULT_APP = "wizard-claude"


def is_consumer(app_id: str, provides: list | None) -> bool:
    """A Claude connection app: it provides `claude-subscription` (the default app counts before its record says
    so). Other apps that happen to mount /claude (the retired AI Accounts) are left alone."""
    return PROVIDES in (provides or []) or app_id == DEFAULT_APP


def _load(path: Path) -> dict:
    try:
        doc = yaml.load(path.read_text(), Loader=_Loader) if path.is_file() else {}
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: not valid YAML ({e}); fix or remove it, then run `wam alongside` again") from None
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a compose mapping")
    return doc


def apply(override: Path, services: list[str], folder: Path, user: str) -> bool:
    """Bind `folder` at /claude and run as `user` in every engine service; keep the rest of the override.
    Returns True when the file changed (the app then needs `wam app update`)."""
    folder = Path(folder)
    if not _SAFE_PATH.match(str(folder)) or ":" in str(folder):
        raise ValueError(f"Claude login folder {str(folder)!r} has characters a compose mount can't carry safely")
    if not re.fullmatch(r"\d+:\d+", user):
        raise ValueError(f"bad engine user {user!r}")
    before = override.read_text() if override.is_file() else None
    doc = _load(override)
    svcs = doc.setdefault("services", {})
    if not isinstance(svcs, dict):
        raise ValueError(f"{override}: services must be a mapping")
    for name in services:
        svc = svcs.setdefault(name, {})
        if not isinstance(svc, dict):
            raise ValueError(f"{override}: service {name} must be a mapping")
        vols = [v for v in (svc.get("volumes") or []) if _target(v) != TARGET]
        svc["volumes"] = vols + [f"{folder}:{TARGET}"]
        svc["user"] = user
    doc[MARK] = {"folder": str(folder), "user": user, "services": list(services)}
    text = ("# Claude login: written by wam (alongside). The Claude app holds the host Hermes' own Claude Code sign-in.\n"
            "# Remove with `wam alongside --off`, then `wam app update <app>`.\n"
            + yaml.dump(doc, Dumper=_Dumper, sort_keys=False))
    if text == before:
        return False
    override.parent.mkdir(parents=True, exist_ok=True)
    tmp = override.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(override)
    return True


def remove(override: Path) -> bool:
    """Undo `apply`: drop only the /claude bind and user it set. Returns True when the file changed."""
    if not override.is_file():
        return False
    doc = _load(override)
    mark = doc.pop(MARK, None)
    if not isinstance(mark, dict):
        return False
    svcs = doc.get("services") or {}
    for name in mark.get("services") or []:
        svc = svcs.get(name)
        if not isinstance(svc, dict):
            continue
        svc["volumes"] = [v for v in (svc.get("volumes") or []) if v != f"{mark.get('folder')}:{TARGET}"]
        if not svc["volumes"]:
            svc.pop("volumes")
        if svc.get("user") == mark.get("user"):
            svc.pop("user")
        if not svc:
            svcs.pop(name)
    if not svcs:
        doc.pop("services", None)
    if doc:
        override.write_text(yaml.dump(doc, Dumper=_Dumper, sort_keys=False))
    else:
        override.unlink()
    return True


def ensure(runtipi_root: Path, store: str, app_id: str, provides: list | None, hermes_home: Path,
           uid: int, gid: int, rootless: bool, cfg: dict) -> dict:
    """Point one Claude connection app at the host Hermes' Claude login. {state, folder?, changed}."""
    if not is_consumer(app_id, provides):
        return {"state": "skipped", "changed": False, "note": "not a Claude connection app"}
    if not uses_claude_subscription(cfg):
        return {"state": "skipped", "changed": False,
                "note": "the host Hermes does not use a Claude subscription; the app keeps its own sign-in"}
    override = runtipi_root / "user-config" / store / app_id / "docker-compose.yml"
    # Share-aware apps use the generic owner-granted folder mechanism (#79/#80).
    # Preserve a pre-existing alongside binding: silently copying/moving the host's
    # live, single-use refresh token would invalidate the owner's other client.
    for base in (runtipi_root / 'apps' / store / app_id,
                 runtipi_root / 'repos' / store / 'apps' / app_id):
        manifest = base / 'wizard.md'
        if not manifest.is_file():
            continue
        text = manifest.read_text()
        match = re.match(r'^---\r?\n(.*?)\r?\n---', text, re.S)
        front = yaml.safe_load(match.group(1)) if match else {}
        if isinstance(front, dict) and front.get('shares'):
            return {'state': 'legacy' if override.is_file() and MARK in _load(override) else 'shared',
                    'changed': False, 'note': 'App-owned sign-in uses owner share grants; an existing host alongside binding is retained until explicitly disconnected'}
        break
    folder = login_dir(hermes_home, uid)
    # The owner's folder must exist before the mount, or the Docker daemon creates it (root-owned on rootful Docker).
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    changed = apply(override, engine_services(recipe_compose(runtipi_root, store, app_id), app_id), folder, engine_user(uid, gid, rootless))
    return {"state": "ready", "folder": str(folder), "changed": changed}
