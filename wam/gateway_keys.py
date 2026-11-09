"""`wam doctor`: does the Agent Service's gateway hold a grant key for every app with tools? (#387)

The gateway reads its app keys from `<WIZARD_OS_HOME>/grants/agent-service.json` (`{"outgoing": {"<app id>": "<key>"}}`,
spec/PERMISSIONS.md layer 2) and sends `X-Wizard-Grant`. Without a key the app treats every agent as unknown and its
tools silently vanish, while Settings still shows the owner's chosen level. Nothing in the App Manager writes that file
yet (the producer is separate work on #387), so this check only makes the gap visible. Read-only: it never creates,
changes or prints a key; it reports app ids only.

Behind the off-by-default feature switch `wizard/doctor-gateway-keys` (Settings › Try new features, For testers).

The producer (`sync`, below) is behind its own off-by-default switch `wizard/gateway-keys`: after every registry write
(install, update, uninstall) it gives every installed Wizard Apps app with tools one key, publishes the key's hash to the
app as a `{"from": "host:agent-service", "sha256": ...}` row in its `wizard/grants.json` `incoming` (the row the App
Manager backend's reconcile keeps as an unmanaged row), and only then writes the key to the gateway file (0600, owned by
the Agent Service's user). Hand-written keys and rows are adopted unchanged. When an app it manages is gone, its row and
key are removed. It never prints a key.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path

SWITCH = "wizard/doctor-gateway-keys"
PRODUCER_SWITCH = "wizard/gateway-keys"
NAME = "agent connections: gateway holds a key for every app with tools"
KEY = re.compile(r"[0-9a-f]{64}")
READER_KEY = re.compile(r"[A-Za-z0-9_-]{32,256}")      # what the gateway accepts (agent-bridge.mjs GRANT_KEY)
HOST = "host:agent-service"                            # outside the app-id namespace (spec/PERMISSIONS.md)
STATE = "gateway-keys.json"                            # in the Wizard home: {"managed": [app ids]}, never keys
STORE = "wizardapps"
AGENT_SERVICE_APPS = ("wizard-agent-service", "wizard-shell")   # the gateway itself (and the pre-split bundle)


def path(home: Path | None, runtipi_root: Path | None, store: str = "wizardapps") -> Path | None:
    """The gateway file under the Agent Service's own WIZARD_OS_HOME (its data mount, e.g. /state/wizard-os inside
    the container = app-data/<store>/wizard-agent-service/data/wizard-os on the host). The installed service's own data
    folder wins, even before its agents/ folder exists, so a legacy ~/.wizard-os/agents can't redirect the check to the
    wrong file; otherwise the same lookup as its agents (kinds.agents_dir). None when no Agent Service data is here."""
    from .kinds import agents_dir
    if runtipi_root:
        for app in AGENT_SERVICE_APPS:
            data = Path(runtipi_root) / "app-data" / store / app / "data"
            try:
                if data.is_dir():
                    return data / "wizard-os" / "grants" / "agent-service.json"
            except OSError:
                continue
    d = agents_dir(home, runtipi_root, store)
    if d:
        return d.parent / "grants" / "agent-service.json"
    return None


def _read(p: Path, runtipi_root: Path | None, rt_read=None) -> bytes | None:
    """The file's bytes, None when it does not exist. It is 0600 and owned by the Agent Service's user (often root),
    so when the CLI user can't open it, read it through the runtipi container like app_rename does."""
    try:
        return p.read_bytes() if p.is_file() else None
    except PermissionError:
        pass
    if rt_read is None:
        from .app_rename import rt_read
    try:
        if runtipi_root is None:
            raise ValueError
        inner = "/data/" + str(p.relative_to(Path(runtipi_root)))
    except ValueError:
        raise PermissionError(f"can't read {p}")
    return rt_read(inner)


def check(reg: dict, home: Path | None, runtipi_root: Path | None, rt_read=None) -> tuple[str, bool, str] | None:
    """One doctor row, or None when there is nothing to check (no Agent Service installed or no app with tools)."""
    apps = [a for a in (reg or {}).get("apps") or [] if isinstance(a, dict)]
    if not any(a.get("id") in AGENT_SERVICE_APPS for a in apps):
        return None
    tools = sorted({a["id"] for a in apps if a.get("mcp") and a.get("id") and a["id"] not in AGENT_SERVICE_APPS})
    if not tools:
        return None
    p = path(home, runtipi_root)
    if p is None:
        return (NAME, False, "can't find the Agent Service's data folder")
    try:
        raw = _read(p, runtipi_root, rt_read)
    except Exception as e:  # noqa: BLE001 - a doctor row, never a crash
        return (NAME, False, f"can't read {p}: {str(e)[:120]}")
    keys: dict = {}
    if raw is not None:
        try:
            doc = json.loads(raw)
            og = doc.get("outgoing") if isinstance(doc, dict) else None
            keys = og if isinstance(og, dict) else {}
        except ValueError:
            return (NAME, False, f"{p} is not valid JSON")
    missing = [i for i in tools if not (isinstance(keys.get(i), str) and READER_KEY.fullmatch(keys[i]))]
    if missing:
        where = "no gateway key file" if raw is None else "no valid key"
        return (NAME, False, f"{where} for {', '.join(missing)[:300]}: agents can't use these apps' tools even though "
                             f"Settings shows a level (#387; {p})")
    # The key only works when the app holds its hash: a `host:agent-service` row in the app's wizard/grants.json.
    norow = []
    for i in tools:
        pf = _provider_file(runtipi_root, i) if runtipi_root else None
        try:
            praw = _read(pf, runtipi_root, rt_read) if pf else None
            rows = (json.loads(praw) or {}).get("incoming") if praw is not None else None
        except Exception:  # noqa: BLE001 - unreadable counts as missing
            rows = None
        if not (isinstance(rows, list) and any(isinstance(r, dict) and r.get("from") == HOST
                                               and r.get("sha256") == digest(keys[i]) for r in rows)):
            norow.append(i)
    if norow:
        return (NAME, False, f"the app does not hold the gateway's key ({HOST} row) for {', '.join(norow)[:300]}: "
                             f"agents can't use these apps' tools even though Settings shows a level (#387)")
    return (NAME, True, f"{len(tools)} apps ({p})")


# ---------------------------------------------------------------- the producer (#387), switch wizard/gateway-keys

def switch_on(home: Path | None = None) -> bool:
    """Whether the owner turned the producer on (tests monkeypatch this)."""
    from .feature_switches import is_on
    return is_on(PRODUCER_SWITCH, home)


def digest(key: str) -> str:
    """The hash an app stores for a key: lowercase hex SHA-256 of the key's text (same as the backend's reconcile)."""
    return hashlib.sha256(key.encode()).hexdigest()


class RuntipiFiles:
    """Files under the Runtipi root, which the App Manager and the Agent Service own (root or the service's uid):
    read, stat and atomically replaced through the runtipi container, as `/data/...` (like app_rename)."""

    def __init__(self, runtipi_root: Path):
        self.root = Path(runtipi_root)

    def _inner(self, p: Path) -> str:
        return "/data/" + str(Path(p).relative_to(self.root))

    def read(self, p: Path) -> bytes | None:
        from .app_rename import rt_read
        return rt_read(self._inner(p))

    def stat(self, p: Path) -> tuple[int, int, int] | None:
        """(mode bits, uid, gid) of a file or folder, None when missing. Never follows a final symlink."""
        from .app_rename import _ok, _sh
        r = _sh('[ -e "$1" ] && [ ! -L "$1" ] || exit 3; stat -c "%a %u %g" "$1"', self._inner(p))
        if r.returncode == 3:
            return None
        mode, uid, gid = _ok(r, f"stat {p}").stdout.decode().split()
        return int(mode, 8), int(uid), int(gid)

    def write(self, p: Path, data: bytes, mode: int, uid: int, gid: int) -> None:
        """Replace `p` atomically: a private temp file beside it, mode and owner set before it is renamed in."""
        from .app_rename import _ok, _sh
        _ok(_sh('d=$(dirname "$1"); [ -d "$d" ] || { mkdir -p "$d" && chown "$3:$4" "$d" && chmod 700 "$d"; } || exit 1; '
                't="$1.wam-gk.$$"; rm -f "$t"; (umask 077; cat > "$t") && chown "$3:$4" "$t" && chmod "$2" "$t" '
                '&& mv -f "$t" "$1" || { rm -f "$t"; exit 1; }',
                self._inner(p), format(mode, "o"), str(uid), str(gid), stdin=data), f"write {p}")


def _provider_file(runtipi_root: Path, app_id: str) -> Path:
    """The app's layer-2 file, written by the backend's reconcile (wizard-app-grants.ts): app-data/<store>/<id>/wizard."""
    return Path(runtipi_root) / "app-data" / STORE / app_id / "wizard" / "grants.json"


def _valid_app_file(doc) -> bool:
    """Same shape the backend's reconcile accepts (`read` in wizard-app-grants.ts); anything else is never replaced."""
    return (isinstance(doc, dict) and isinstance(doc.get("outgoing"), dict)
            and all(isinstance(v, str) and v for v in doc["outgoing"].values())
            and isinstance(doc.get("incoming"), list)
            and all(isinstance(r, dict) and isinstance(r.get("from"), str) and isinstance(r.get("sha256"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", r["sha256"]) for r in doc["incoming"])
            and isinstance(doc.get("enforce"), bool))


def _dump(doc) -> bytes:
    return (json.dumps(doc, indent=2) + "\n").encode()


def _load_state(home: Path) -> set[str]:
    try:
        v = json.loads((Path(home) / STATE).read_text()).get("managed")
        return {x for x in v if isinstance(x, str)} if isinstance(v, list) else set()
    except (OSError, ValueError, AttributeError):
        return set()


def _save_state(home: Path, managed: set[str]) -> None:
    p = Path(home) / STATE
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"managed": sorted(managed)}) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def sync(reg: dict, home: Path, runtipi_root: Path, files=None, mint=lambda: secrets.token_hex(32)) -> dict:
    """Give the gateway one key for every installed Wizard Apps app with tools. Returns a summary with app ids only.

    Order (spec/PERMISSIONS.md layer 2, providers first): every app's `host:agent-service` row is published before the
    gateway file holds its key, so the gateway never sends a key the app can't check yet. Removal runs the same way
    round: the app's row goes first, then the key. Only apps this producer managed lose a key; a hand-written key for an
    app it never saw stays. The registry never holds two installed apps with one id (registry.build refuses that), so the
    gateway's bare app id names exactly one installation."""
    files = files or RuntipiFiles(runtipi_root)
    out: dict = {"keys": [], "minted": [], "rows_added": [], "removed": [], "skipped": {}}
    apps = [a for a in (reg or {}).get("apps") or [] if isinstance(a, dict) and a.get("id")]
    if not any(a["id"] == "wizard-agent-service" and str(a.get("urn", "")).endswith(f":{STORE}") for a in apps):
        return {**out, "skipped": {"*": "no Agent Service from Wizard Apps installed"}}
    data = Path(runtipi_root) / "app-data" / STORE / "wizard-agent-service" / "data"
    os_home = data / "wizard-os"
    st = files.stat(os_home)
    if st is None:
        # The service creates its WIZARD_OS_HOME as the user it runs as; until it has, we don't know that user.
        return {**out, "skipped": {"*": "the Agent Service has not created its data folder yet"}}
    _, uid, gid = st
    gw = os_home / "grants" / "agent-service.json"
    raw = files.read(gw)
    try:
        gdoc = json.loads(raw) if raw is not None else {}
    except ValueError:
        return {**out, "skipped": {"*": f"{gw} is not valid JSON: left alone"}}
    if not isinstance(gdoc, dict) or not isinstance(gdoc.get("outgoing", {}), dict):
        return {**out, "skipped": {"*": f"{gw} has no outgoing map: left alone"}}
    outgoing = dict(gdoc.get("outgoing") or {})
    managed = _load_state(home)

    tools = {}
    for a in apps:
        i = a["id"]
        if i in AGENT_SERVICE_APPS or not a.get("mcp"):
            continue
        if not str(a.get("urn", "")).endswith(f":{STORE}"):
            out["skipped"][i] = "not from Wizard Apps (its grants are kept by its own store)"
            continue
        tools[i] = a

    # 1. Providers first: publish (or keep) each app's row for the gateway's key.
    for i in sorted(tools):
        pf = _provider_file(runtipi_root, i)
        try:
            praw, pst = files.read(pf), files.stat(pf)
            pdoc = json.loads(praw) if praw is not None else None
        except (ValueError, RuntimeError, OSError) as e:
            out["skipped"][i] = f"can't read its grants file: {str(e)[:80]}"
            continue
        if pdoc is None or pst is None:
            out["skipped"][i] = "no grants file yet (the App Manager writes it on install)"
            continue
        if not _valid_app_file(pdoc):
            out["skipped"][i] = "its grants file is not in the expected shape: left alone"
            continue
        key = outgoing.get(i)
        if not (isinstance(key, str) and READER_KEY.fullmatch(key)):
            key = mint()
            out["minted"].append(i)
        h = digest(key)
        if not any(r["from"] == HOST and r["sha256"] == h for r in pdoc["incoming"]):
            pdoc["incoming"].append({"from": HOST, "sha256": h})
            files.write(pf, _dump(pdoc), pst[0], pst[1], pst[2])
            out["rows_added"].append(i)
        outgoing[i] = key
        managed.add(i)
        out["keys"].append(i)

    # 2. Gone: an app this producer managed that is no longer installed with tools loses its row, then its key.
    for i in sorted(managed - set(tools)):
        key = outgoing.get(i)
        pf = _provider_file(runtipi_root, i)
        if isinstance(key, str):
            try:
                praw, pst = files.read(pf), files.stat(pf)
                pdoc = json.loads(praw) if praw is not None else None
                if pdoc is not None and pst is not None and _valid_app_file(pdoc):
                    keep = [r for r in pdoc["incoming"] if not (r["from"] == HOST and r["sha256"] == digest(key))]
                    if len(keep) != len(pdoc["incoming"]):
                        files.write(pf, _dump({**pdoc, "incoming": keep}), pst[0], pst[1], pst[2])
            except (ValueError, RuntimeError, OSError) as e:
                out["skipped"][i] = f"can't remove its row: {str(e)[:80]}; key kept"
                continue
        outgoing.pop(i, None)
        managed.discard(i)
        out["removed"].append(i)

    # 3. Then the gateway file: 0600, owned by the user the Agent Service runs as (the gateway refuses anything else).
    new = {**gdoc, "outgoing": outgoing}
    gst = files.stat(gw)
    if raw is None or new != gdoc or gst is None or gst[0] != 0o600 or gst[1] != uid:
        files.write(gw, _dump(new), 0o600, uid, gid)
    _save_state(home, managed)
    return out


def sync_if_on(c, reg: dict, files=None) -> dict | None:
    """The registry hook: run `sync` when the owner turned `wizard/gateway-keys` on, else do nothing. Never raises."""
    try:
        if not switch_on(c.home):
            return None
        return sync(reg, Path(c.home), Path(c.runtipi_root), files=files)
    except Exception as e:  # noqa: BLE001 - a registry write must never fail on this
        return {"error": str(e)[:200]}
