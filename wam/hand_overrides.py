"""Box overrides written by hand: retired by the update, never by hand (wizard-os#805).

A Runtipi user-config compose override (<runtipi>/user-config/<store>/<app>/docker-compose.yml) is the box-local way
to change how an app runs, and the App Manager writes its own marked blocks there (`x-wizard-*`). Three hand-written
ones were found on a live box; each is now either gone or an official behaviour, and every installer run (fresh or
upgrade, `wam bootstrap`) retires them, so no box depends on a hand edit:

1. `wizard-shell` (the pre-split bundle): a lock-down override for a backup box left behind on a box that no longer
   runs the bundle. When the bundle is not installed its override is moved aside: a later install of the bundle
   would otherwise start locked down for reasons nobody remembers.
2. `hermes-agent`: a published Hermes API port (container port 8642, e.g. 127.0.0.1:18642) so a second WizardOS shell
   on the box could reach it. No second shell is used; the App Manager never publishes the agent's API, so any such
   port entry is removed (only that entry; the App Manager's own blocks stay as they are).
3. `second-hand`: a bind mount of a host marketplace ledger folder over the app's ledger. Second Hand owns its data
   in its own app-data folder (`DATA_DIR/ledger`, `/data/private/ledger` in the app): once, the host folder is
   copied there under the ledger's own lock (the app stopped), the host folder is renamed aside and replaced by a
   link to the app's folder (so host scripts that write the ledger keep working), and the mount is removed.

Nothing is deleted. A removed entry is undone by putting back `docker-compose.yml.hand-<stamp>` (the original file,
kept beside it); the moved ledger by deleting the link and renaming the `*.moved-to-second-hand-<stamp>` folder back.
What was done is recorded in ~/.wizard-os/hand-overrides.json. Each step only acts on what it recognises; anything
else (a file that does not parse, a clash) is reported and left exactly as it was.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

SHELL_BUNDLE = "wizard-shell"
AGENT_APP = "hermes-agent"
HERMES_API_PORT = 8642
SECOND_HAND = "second-hand"
LEDGER_IN_APP = "/data/private/ledger"   # the recipe's DATA_DIR=/data/private and ${APP_DATA_DIR}/data:/data
LEDGER_ON_HOST = ("data", "private", "ledger")   # the same folder under app-data/<store>/second-hand
STATE_FILE = "hand-overrides.json"
STOP_WAIT_S = 180


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def override_path(runtipi_root, store: str, app_id: str) -> Path:
    return Path(runtipi_root) / "user-config" / store / app_id / "docker-compose.yml"


def _aside_name(path: Path, stamp: str) -> Path:
    """`docker-compose.yml.hand-<stamp>`: Runtipi reads only docker-compose.yml, so the copy beside it is inert."""
    dst = path.with_name(f"{path.name}.hand-{stamp}")
    n = 1
    while dst.exists():
        n += 1
        dst = path.with_name(f"{path.name}.hand-{stamp}-{n}")
    return dst


def _keep_original(path: Path, stamp: str) -> Path:
    dst = _aside_name(path, stamp)
    shutil.copy2(path, dst)
    return dst


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".wam-tmp")
    tmp.write_text(text)
    os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)


def _load(path: Path):
    """(text, doc) of an override; raises ValueError when it does not parse as a mapping."""
    from .network import load_yaml
    text = path.read_text()
    try:
        doc = load_yaml(text) or {}
    except Exception as e:  # noqa: BLE001 - yaml raises many kinds
        raise ValueError(f"{path} does not parse ({str(e)[:80]}); left as it is") from None
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a compose mapping; left as it is")
    return text, doc


def _rewrite(path: Path, doc: dict, stamp: str) -> dict:
    """Keep the original beside it, then write `doc` (or move the whole file aside when nothing is left)."""
    from .network import dump_yaml
    services = doc.get("services")
    if isinstance(services, dict):
        for name in [n for n, s in services.items() if s == {}]:
            del services[name]
        if not services:
            del doc["services"]
    if not doc:
        dst = _aside_name(path, stamp)
        os.replace(path, dst)
        return {"moved_aside": str(dst)}
    kept = _keep_original(path, stamp)
    _write(path, dump_yaml(doc))
    return {"rewritten": str(path), "original": str(kept)}


# ---------------------------------------------------------------- 1. the pre-split bundle's leftover override

def shell_bundle(runtipi_root, store: str, installed: set[str], *, dry_run: bool = False,
                 stamp: str | None = None) -> dict | None:
    p = override_path(runtipi_root, store, SHELL_BUNDLE)
    if not p.is_file() or SHELL_BUNDLE in installed:
        return None
    if dry_run:
        return {"app": SHELL_BUNDLE, "would": "move its override aside (the bundle is not installed)"}
    dst = _aside_name(p, stamp or _stamp())
    os.replace(p, dst)
    return {"app": SHELL_BUNDLE, "moved_aside": str(dst), "why": "the pre-split bundle is not installed"}


# ---------------------------------------------------------------- 2. a published Hermes API port

def _container_port(entry) -> int | None:
    """The container port of one compose `ports` entry (short "[ip:][host:]container[/proto]" or long form)."""
    if isinstance(entry, dict):
        target = entry.get("target")
    elif isinstance(entry, (str, int)):
        target = str(entry).split("/", 1)[0].rsplit(":", 1)[-1]
    else:
        return None
    try:
        return int(str(target).strip())
    except (TypeError, ValueError):
        return None   # a range or a variable: not ours to judge


def agent_port(runtipi_root, store: str, *, dry_run: bool = False, stamp: str | None = None) -> dict | None:
    p = override_path(runtipi_root, store, AGENT_APP)
    if not p.is_file():
        return None
    _, doc = _load(p)
    removed = []
    for name, svc in (doc.get("services") or {}).items() if isinstance(doc.get("services"), dict) else []:
        ports = svc.get("ports") if isinstance(svc, dict) else None
        if not isinstance(ports, list):   # absent, or a tagged `!reset []` the App Manager may write: leave it
            continue
        keep = [e for e in ports if _container_port(e) != HERMES_API_PORT]
        if len(keep) == len(ports):
            continue
        removed += [f"{name}: {e}" for e in ports if e not in keep]
        if keep:
            svc["ports"] = keep
        else:
            del svc["ports"]
    if not removed:
        return None
    if dry_run:
        return {"app": AGENT_APP, "would": "remove the published Hermes API port", "ports": removed}
    return {"app": AGENT_APP, "removed_ports": removed, **_rewrite(p, doc, stamp or _stamp())}


# ---------------------------------------------------------------- 3. Second Hand's host ledger mount

def _env_get(env, key: str):
    if isinstance(env, dict):
        return env.get(key)
    if isinstance(env, list):
        for e in env:
            if isinstance(e, str) and e.split("=", 1)[0] == key:
                return e.split("=", 1)[1] if "=" in e else None
    return None


def _env_drop(svc: dict, key: str) -> None:
    env = svc.get("environment")
    if isinstance(env, dict):
        env.pop(key, None)
    elif isinstance(env, list):
        svc["environment"] = [e for e in env if not (isinstance(e, str) and e.split("=", 1)[0] == key)]
    if svc.get("environment") in ({}, []):
        del svc["environment"]


def _bind(entry) -> tuple[str, str] | None:
    """(host source, container target) of a bind mount entry with an absolute host path, else None."""
    if isinstance(entry, dict):
        if entry.get("type") not in (None, "bind"):
            return None
        src, dst = entry.get("source"), entry.get("target")
    elif isinstance(entry, str) and ":" in entry:
        parts = entry.split(":")
        if len(parts) < 2:
            return None
        src, dst = parts[0], parts[1]
    else:
        return None
    if not isinstance(src, str) or not isinstance(dst, str) or not src.startswith("/"):
        return None
    return src, dst.rstrip("/") or "/"


def _ledger_mount(doc: dict):
    """(service name, volume entry, host folder) of the bind mount over Second Hand's ledger, else None."""
    hits = []
    for name, svc in (doc.get("services") or {}).items() if isinstance(doc.get("services"), dict) else []:
        if not isinstance(svc, dict) or not isinstance(svc.get("volumes"), list):
            continue
        target = (_env_get(svc.get("environment"), "LEDGER_DIR") or LEDGER_IN_APP).rstrip("/")
        for v in svc["volumes"]:
            b = _bind(v)
            if b and b[1] == target:
                hits.append((name, v, Path(b[0])))
    if len(hits) > 1:
        raise ValueError("more than one host folder is mounted over Second Hand's ledger; left as it is")
    return hits[0] if hits else None


def _files(root: Path) -> dict[str, int]:
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            if not p.is_symlink():
                out[str(p.relative_to(root))] = p.stat().st_size
    return out


def _app_uid(runtipi_root, store: str) -> int:
    try:
        return int(json.loads((Path(runtipi_root) / "apps" / store / SECOND_HAND / "config.json").read_text())["uid"])
    except (OSError, ValueError, KeyError, TypeError):
        return 1000


def _open_up(root: Path) -> None:
    """What Runtipi's own app-data walk does on install/update (a+rwx folders, a+rw files), for a copy the app's
    user (another uid) must write. Never follows links."""
    for dirpath, dirs, files in os.walk(root):
        os.chmod(dirpath, 0o777)
        for f in files:
            p = Path(dirpath) / f
            if not p.is_symlink():
                os.chmod(p, (p.stat().st_mode & 0o777) | 0o666)


@contextlib.contextmanager
def _ledger_lock(folder: Path):
    """The same exclusive lock Second Hand and the host scripts take for every load -> change -> save."""
    if fcntl is None:
        yield
        return
    with open(folder / "ledger.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _status(rt, urn: str) -> str | None:
    try:
        return ((rt.app(urn) or {}).get("app") or {}).get("status")
    except Exception:  # noqa: BLE001
        return None


def second_hand(runtipi_root, store: str, rt=None, *, dry_run: bool = False, stamp: str | None = None) -> dict | None:
    p = override_path(runtipi_root, store, SECOND_HAND)
    if not p.is_file():
        return None
    _, doc = _load(p)
    hit = _ledger_mount(doc)
    if not hit:
        return None
    name, entry, src = hit
    dest = Path(runtipi_root) / "app-data" / store / SECOND_HAND / Path(*LEDGER_ON_HOST)
    base = {"app": SECOND_HAND, "from": str(src), "to": str(dest)}
    if src.is_symlink() or not src.is_dir():
        return {**base, "skipped": "the mounted host folder is missing or already a link; left as it is"}
    if (dest / "ledger.json").exists() or (dest.is_dir() and any(dest.iterdir())):
        return {**base, "skipped": "Second Hand's own ledger folder is not empty; left as it is (move it by hand)"}
    probe = dest
    while not probe.exists():
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK) or not os.access(src.parent, os.W_OK | os.X_OK):
        return {**base, "skipped": "no write access to the ledger folders; left as it is"}
    if dry_run:
        return {**base, "would": "move the host ledger into Second Hand's own data folder"}
    stamp = stamp or _stamp()
    urn = f"{SECOND_HAND}:{store}"
    was = _status(rt, urn) if rt is not None else None
    if was == "running":
        try:
            rt.stop(urn)
            now = rt.wait_status(urn, {"stopped"}, timeout=STOP_WAIT_S)
            if now != "stopped":
                raise RuntimeError(f"still {now}")
        except Exception as e:  # noqa: BLE001 - it did not stop: change nothing, and leave it running
            return {**base, "error": f"could not stop it first ({str(e)[:160]})", "start": _start(rt, urn)}
    aside = src.with_name(f"{src.name}.moved-to-second-hand-{stamp}")
    made_dest = not dest.exists()
    done: list[str] = []
    kept = None
    try:
        with _ledger_lock(src):
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
            done.append("copied")
            if _files(src) != _files(dest):
                raise OSError("the copy does not match the host folder")
            os.rename(src, aside)
            done.append("renamed")
            os.symlink(dest, src)
            done.append("linked")
        if os.getuid() != _app_uid(runtipi_root, store):
            _open_up(dest)
        svc = doc["services"][name]
        svc["volumes"] = [v for v in svc["volumes"] if v is not entry]
        if not svc["volumes"]:
            del svc["volumes"]
        _env_drop(svc, "LEDGER_DIR")
        kept = _rewrite(p, doc, stamp)
        done.append("override")
    except Exception as e:  # noqa: BLE001 - undo every step, then report
        if "linked" in done:
            os.unlink(src)
        if "renamed" in done:
            os.rename(aside, src)
        if "copied" in done or dest.exists():
            if made_dest:
                shutil.rmtree(dest, ignore_errors=True)
            else:
                for child in dest.iterdir():
                    shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        if was == "running":
            _start(rt, urn)
        return {**base, "error": f"{type(e).__name__}: {e}"[:300], "rolled_back": True}
    started = _start(rt, urn) if was == "running" else None
    return {**base, "moved": True, "host_folder_kept_as": str(aside), "host_link": str(src), **(kept or {}),
            **({"start": started} if started else {})}


def _start(rt, urn: str) -> str | None:
    try:
        rt.start(urn)
        return "started"
    except Exception as e:  # noqa: BLE001
        return f"error: {str(e)[:160]}"


# ---------------------------------------------------------------- all three

def installed_ids(rt, store: str) -> set[str]:
    out = set()
    for row in rt.installed() or []:
        app = row.get("app") or {}
        if app.get("appStoreSlug") == store and app.get("appName"):
            out.add(app["appName"])
    return out


def retire(runtipi_root, store: str, rt, home: Path | None = None, *, dry_run: bool = False) -> list[dict]:
    """Retire every hand-written override this module knows. Never raises: each step reports on its own."""
    stamp = _stamp()
    out = []
    try:
        installed = installed_ids(rt, store)
    except Exception as e:  # noqa: BLE001 - without the installed list the bundle step can't tell; skip only it
        installed = None
        out.append({"app": SHELL_BUNDLE, "skipped": f"installed apps not readable ({str(e)[:80]})"})
    steps = [("agent", lambda: agent_port(runtipi_root, store, dry_run=dry_run, stamp=stamp)),
             ("second-hand", lambda: second_hand(runtipi_root, store, rt, dry_run=dry_run, stamp=stamp))]
    if installed is not None:
        steps.insert(0, ("shell", lambda: shell_bundle(runtipi_root, store, installed, dry_run=dry_run, stamp=stamp)))
    for label, step in steps:
        try:
            res = step()
        except Exception as e:  # noqa: BLE001
            res = {"app": label, "error": f"{type(e).__name__}: {e}"[:300]}
        if res:
            out.append(res)
    if out and not dry_run and home is not None:
        _record(Path(home), stamp, out)
    return out


def _record(home: Path, stamp: str, rows: list[dict]) -> None:
    p = home / STATE_FILE
    try:
        hist = json.loads(p.read_text()) if p.is_file() else []
    except (OSError, ValueError):
        hist = []
    hist = (hist if isinstance(hist, list) else [])[-19:] + [{"at": stamp, "steps": rows}]
    try:
        home.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(hist, indent=2) + "\n")
    except OSError as e:
        print(f"WARNING: {p} not written: {e}", file=sys.stderr)
