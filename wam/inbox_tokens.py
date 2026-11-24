"""Hand each agent-inbox app its own Agent Service app token (#804).

The Agent Service accepts an installed app's own app token as that app's door to the agent inbox
(wizard-agent-service#569, docs/agent-inbox.md "App doors"). The token is the Service's file
`<its WIZARD_OS_HOME>/app-tokens/<app id>` (the same file the app delivery route checks), which in a box is
app-data/<store>/wizard-agent-service/data/wizard-os/app-tokens/<app id> on the host (/state/wizard-os inside the
container, like grants/agent-service.json in gateway_keys.py).

An app asks for its token by naming a `<NAME>_AGENT_INBOX_TOKEN` variable in its recipe compose (Mail:
`${MAIL_AGENT_INBOX_TOKEN:-}`, Messenger: `${MESSENGER_AGENT_INBOX_TOKEN:-}`). For each such installed app the App
Manager:
  1. keeps the Service's token file, or creates it when missing (0600, owned by the Service's user, 32 random bytes
     base64url, like the Service's own `ensureToken`); a token the app already holds is adopted when the Service lost
     its file (e.g. a reinstalled Service), so the app keeps working without a restart;
  2. writes it to the app's Runtipi user-config app.env under the recipe's variable name, which Runtipi merges into
     the app's env on every install, update, start and restart (so it is kept across updates; after a restore that
     brought back another token, the next sync writes that one).
On owner boxes Runtipi's user-config is root-owned while wam runs as the owner's user (#931): app.env is then read
and written as root through the runtipi container, and the sync lock lives in the Wizard home.
The token opens only that app's own inbox routes and its delivery route, never the owner's. Nothing here prints a
token. Bug lane (always on): without it every app call to the inbox answers 403 on a real box.
"""
from __future__ import annotations

import os
import re
import secrets
import stat
import sys
from pathlib import Path

STORE = "wizardapps"
VAR = re.compile(r"\$\{([A-Z][A-Z0-9_]*_AGENT_INBOX_TOKEN)(?:[:?+-][^}]*)?\}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{24,256}")
APP_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")    # the Service's APP_ID_RE (app-bindings.mjs)


def token_vars(compose_text: str) -> list[str]:
    """The `*_AGENT_INBOX_TOKEN` variables a recipe compose interpolates, in order, without duplicates."""
    out: list[str] = []
    for name in VAR.findall(compose_text or ""):
        if name not in out:
            out.append(name)
    return out


def recipe_text(runtipi_root: Path, app_id: str, store: str = STORE, prefer: str = "installed") -> str:
    """The app's recipe compose: the installed copy first (or the store copy first for an update/install, which is
    the recipe Runtipi is about to apply), the other as the fallback. '' when neither is readable."""
    root = Path(runtipi_root)
    installed = root / "apps" / store / app_id / "docker-compose.yml"
    repo = root / "repos" / store / "apps" / app_id / "docker-compose.yml"
    for p in ((installed, repo) if prefer == "installed" else (repo, installed)):
        try:
            return p.read_text()
        except OSError:
            continue
    return ""


def service_home(runtipi_root: Path, store: str = STORE, home: Path | None = None) -> Path | None:
    """The Agent Service's own WIZARD_OS_HOME on the host (gateway_keys.path's lookup), None when no Service data
    folder is here yet. A Service app in Runtipi wins; otherwise a Service that runs on the host itself (#931: no
    Runtipi Service app, e.g. a systemd Service on 127.0.0.1:8923) counts when the given Wizard home (WIZARD_OS_HOME,
    else ~/.wizard-os) already holds the Service's `app-tokens/` folder, the proof that this Service reads tokens
    from there."""
    from .gateway_keys import AGENT_SERVICE_APPS
    for app in AGENT_SERVICE_APPS:
        data = Path(runtipi_root) / "app-data" / store / app / "data"
        try:
            if data.is_dir():
                return data / "wizard-os"
        except OSError:
            continue
    return host_service_home(home)


def host_service_home(home: Path | None) -> Path | None:
    """The host Service's WIZARD_OS_HOME: `home` when it holds a real (not symlinked) `app-tokens/` folder."""
    if not home:
        return None
    h = Path(home).expanduser()
    try:
        tokens = h / "app-tokens"
        if h.is_dir() and not h.is_symlink() and tokens.is_dir() and not tokens.is_symlink():
            return h
    except OSError:
        pass
    return None


class HostFiles:
    """RuntipiFiles' read/stat/write for a Service home outside the Runtipi root: plain files on this host."""

    def read(self, p: Path) -> bytes | None:
        p = Path(p)
        if p.is_symlink() or not p.is_file():
            return None
        return p.read_bytes()

    def stat(self, p: Path) -> tuple[int, int, int] | None:
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
        return stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid

    def write(self, p: Path, data: bytes, mode: int, uid: int, gid: int) -> None:
        """Replace `p` atomically: a private temp file beside it, mode and owner set before it is renamed in."""
        p = Path(p)
        if not p.parent.is_dir():
            p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _own(p.parent, uid, gid)
        tmp = p.with_name(f"{p.name}.wam-it.{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, mode)
            _own(tmp, uid, gid)
            tmp.replace(p)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def _own(p: Path, uid: int, gid: int) -> None:
    st = os.lstat(p)
    if (st.st_uid, st.st_gid) != (uid, gid):
        os.chown(p, uid, gid, follow_symlinks=False)   # only root can give it away; anyone else fails loudly


NO_SERVICE = "no Agent Service data folder on this box yet"


def _env_path(runtipi_root: Path, app_id: str, store: str = STORE) -> Path:
    return Path(runtipi_root) / "user-config" / store / app_id / "app.env"


def _parse_env(text: str) -> dict[str, str]:
    """runtipi.read_env's parsing, for text read through the runtipi container."""
    env: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _rt_read(runtipi_root: Path, p: Path) -> bytes | None:
    """A root-owned user-config file, read as root through the runtipi container (#931: on owner boxes Runtipi's
    user-config is root:root and wam runs as the owner's own user)."""
    from .gateway_keys import RuntipiFiles
    return RuntipiFiles(Path(runtipi_root)).read(p)


def _rt_write(runtipi_root: Path, p: Path, data: bytes) -> None:
    """Replace a user-config file as root through the runtipi container: a 0600 temp file beside it, the old file's
    owner kept, renamed into place. A missing app folder is made the way Runtipi makes it (root, 755)."""
    from .app_rename import _ok, _sh
    from .gateway_keys import RuntipiFiles
    inner = RuntipiFiles(Path(runtipi_root))._inner(p)
    _ok(_sh('d=$(dirname "$1"); [ -d "$d" ] || mkdir -p "$d" || exit 1; [ ! -L "$1" ] || exit 1; '
            't="$1.wam-it.$$"; rm -f "$t"; (umask 077; cat > "$t") && chmod 600 "$t" '
            '&& { [ ! -e "$1" ] || chown "$(stat -c %u:%g "$1")" "$t"; } && mv -f "$t" "$1" || { rm -f "$t"; exit 1; }',
            inner, stdin=data), f"write {p}")


def _user_env_text(runtipi_root: Path, app_id: str, store: str = STORE) -> str | None:
    """The app's user-config app.env text (None when there is none). A file or folder the wam user may not read
    (root-owned, or the 0600 file the privileged write left) is read through the runtipi container."""
    p = _env_path(runtipi_root, app_id, store)
    try:
        return p.read_text()
    except FileNotFoundError:
        return None
    except PermissionError:
        raw = _rt_read(runtipi_root, p)
        return raw.decode("utf-8", "replace") if raw is not None else None


def _user_env(runtipi_root: Path, app_id: str, store: str = STORE) -> dict[str, str]:
    try:
        return _parse_env(_user_env_text(runtipi_root, app_id, store) or "")
    except Exception:  # noqa: BLE001 - unreadable counts as empty (the write then says what is wrong)
        return {}


def _write_user_env(runtipi_root: Path, app_id: str, values: dict[str, str], store: str = STORE) -> None:
    """Merge KEY=VALUE pairs into the app's user-config app.env, 0600 from the start (it holds a token). When the
    wam user may not write there (Runtipi's user-config is root-owned on owner boxes, #931), the same merge is
    written as root through the runtipi container."""
    p = _env_path(runtipi_root, app_id, store)

    def merged(text: str | None) -> str:
        keep = [l for l in (text or "").splitlines() if l.partition("=")[0].strip() not in values]
        return "\n".join(keep + [f"{k}={v}" for k, v in values.items()]) + "\n"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        text = merged(p.read_text() if p.is_file() else None)
        tmp = p.with_name(p.name + ".wam-tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    except PermissionError:
        _rt_write(runtipi_root, p, merged(_user_env_text(runtipi_root, app_id, store)).encode())
        return
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _lock(runtipi_root: Path, store: str, home: Path | None):
    """The open file the box-wide sync lock is taken on: in the Wizard home (wam's own folder, always writable by
    it), else beside user-config as before. An existing lock file another user made is opened read-only (flock
    works on any open file)."""
    places = [Path(home).expanduser() / "inbox-tokens.lock"] if home else []
    places.append(Path(runtipi_root) / "user-config" / store / ".wam-inbox-tokens.lock")
    last: OSError | None = None
    for lock in places:
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            return open(lock, "a")
        except PermissionError as e:
            last = e
            try:
                return open(lock, "r")
            except OSError:
                continue
        except OSError as e:
            last = e
    raise last or OSError("no place for the inbox token lock")


def sync_app(runtipi_root: Path, app_id: str, *, store: str = STORE, prefer: str = "installed", files=None,
             mint=lambda: secrets.token_urlsafe(32), home: Path | None = None) -> dict:
    """Give one installed app its inbox token. Returns {"vars": [...], "token": "kept|created|adopted",
    "changed": bool (its user-config app.env changed)} or {"skipped": reason}. Never prints the token.
    One sync at a time per box: registry writers run concurrently (API, watchers, installer), and two first-time
    syncs must never mint different tokens for one app (the Service would accept one, the app send the other)."""
    import fcntl
    if not APP_ID.fullmatch(app_id or ""):
        return {"skipped": "not an app id the Agent Service accepts"}
    names = token_vars(recipe_text(runtipi_root, app_id, store, prefer))
    if not names:
        return {"skipped": "its recipe takes no agent inbox token"}   # nothing written for any other app
    if service_home(runtipi_root, store, home) is None:
        return {"vars": names, "skipped": NO_SERVICE}
    with _lock(runtipi_root, store, home) as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            return _sync_app(runtipi_root, app_id, store=store, prefer=prefer, files=files, mint=mint, home=home)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _sync_app(runtipi_root: Path, app_id: str, *, store: str, prefer: str, files, mint, home=None) -> dict:
    if not APP_ID.fullmatch(app_id or ""):
        return {"skipped": "not an app id the Agent Service accepts"}
    names = token_vars(recipe_text(runtipi_root, app_id, store, prefer))
    if not names:
        return {"skipped": "its recipe takes no agent inbox token"}
    os_home = service_home(runtipi_root, store, home)
    if os_home is None:
        return {"vars": names, "skipped": NO_SERVICE}
    if files is None:
        from .gateway_keys import RuntipiFiles
        inside = Path(os_home).is_relative_to(Path(runtipi_root))
        files = RuntipiFiles(Path(runtipi_root)) if inside else HostFiles()
    owner = files.stat(os_home)
    if owner is None:
        # The Service creates its WIZARD_OS_HOME as the user it runs as; until it has, we don't know that user.
        return {"skipped": "the Agent Service has not created its data folder yet"}
    _, uid, gid = owner
    path = os_home / "app-tokens" / app_id
    raw = files.read(path)
    current = raw.decode("utf-8", "replace").strip() if raw is not None else ""
    env = _user_env(runtipi_root, app_id, store)
    held = next((env[n] for n in names if TOKEN.fullmatch(env.get(n, "") or "")), "")
    if TOKEN.fullmatch(current):
        token, how = current, "kept"
    elif current:
        # Something the Service would not accept (shorter than 24 or odd characters) that we didn't write: leave it.
        return {"vars": names, "skipped": "its app token file is not a token the Service accepts: left alone"}
    else:
        token, how = (held, "adopted") if held else (mint(), "created")
        if not TOKEN.fullmatch(token):
            raise ValueError("minted token has an unexpected shape")
        files.write(path, (token + "\n").encode(), 0o600, uid, gid)
    want = {n: token for n in names if env.get(n) != token}
    if want:
        _write_user_env(runtipi_root, app_id, want, store)
    return {"vars": names, "token": how, "changed": bool(want)}


def sync(reg: dict, runtipi_root: Path, *, store: str = STORE, files=None, mint=None, home: Path | None = None) -> dict:
    """sync_app for every installed app of this store: {"apps": {id: result}}. Never raises per app."""
    out: dict = {"apps": {}}
    for a in (reg or {}).get("apps") or []:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        if str(a.get("urn") or f"_:{store}").partition(":")[2] != store:
            continue
        try:
            kw = {"mint": mint} if mint else {}
            r = sync_app(runtipi_root, a["id"], store=store, files=files, home=home, **kw)
        except Exception as e:  # noqa: BLE001 - one app's trouble never stops the others
            r = {"error": str(e)[:160] or type(e).__name__}
        if "skipped" not in r or r.get("vars"):
            out["apps"][a["id"]] = r
    return out


def changed_running(result: dict, rt, store: str = STORE) -> list[str]:
    """Apps whose token was just written to their env and that run now: they need one restart to pick it up
    (Runtipi regenerates the env on restart). A stopped app picks it up on its next start."""
    ids = []
    for app_id, r in sorted((result or {}).get("apps", {}).items()):
        if not r.get("changed"):
            continue
        try:
            status = ((rt.app(f"{app_id}:{store}") or {}).get("app") or {}).get("status")
        except Exception:  # noqa: BLE001
            status = None
        if status == "running":
            ids.append(app_id)
    return ids


def sync_registry(c, rt, reg: dict, files=None) -> dict | None:
    """The registry hook (after every registry write): hand out missing tokens, and restart (once) a running app
    whose env just got its token, so its door works now rather than after its next update. Never raises."""
    try:
        res = sync(reg, Path(c.runtipi_root), files=files, home=getattr(c, "home", None))
        for app_id in changed_running(res, rt):
            try:
                rt.restart(f"{app_id}:{STORE}")
                res["apps"][app_id]["restarted"] = True
            except Exception as e:  # noqa: BLE001
                res["apps"][app_id]["restart_error"] = str(e)[:160]
        for app_id, r in res["apps"].items():
            if r.get("error"):
                print(f"note: agent inbox token not handed to {app_id} ({r['error']})", file=sys.stderr)
        waiting = sorted(i for i, r in res["apps"].items() if r.get("skipped") == NO_SERVICE)
        if waiting:   # one line per sync, never silent (#931)
            print(f"note: agent inbox tokens not handed to {', '.join(waiting)} ({NO_SERVICE}; "
                  f"looked in Runtipi app-data and {getattr(c, 'home', None) or 'no Wizard home'}/app-tokens)",
                  file=sys.stderr)
        return res
    except Exception as e:  # noqa: BLE001 - a registry write must never fail on this
        print(f"note: agent inbox tokens not checked ({str(e)[:160]})", file=sys.stderr)
        return None


LABEL = "agent inbox apps hold their Agent Service token"


def check(reg: dict, runtipi_root: Path, home: Path | None = None, store: str = STORE, files=None):
    """`wam doctor` row (read-only): every installed app whose recipe asks for an inbox token holds the very token
    the Service has in app-tokens/<id> (a different or missing Service copy still answers 403). None when no
    installed app asks. Names apps only, never a token."""
    asking = {}
    for a in (reg or {}).get("apps") or []:
        if not isinstance(a, dict) or not APP_ID.fullmatch(str(a.get("id") or "")):
            continue
        if str(a.get("urn") or f"_:{store}").partition(":")[2] != store:
            continue
        names = token_vars(recipe_text(runtipi_root, a["id"], store))
        if names:
            asking[a["id"]] = names
    if not asking:
        return None
    ids = ", ".join(sorted(asking))
    os_home = service_home(runtipi_root, store, home)
    if os_home is None:
        return (LABEL, False, f"{ids}: {NO_SERVICE} (looked in Runtipi app-data and {home or 'no Wizard home'}/app-tokens)")
    if files is None:
        from .gateway_keys import RuntipiFiles
        inside = Path(os_home).is_relative_to(Path(runtipi_root))
        files = RuntipiFiles(Path(runtipi_root)) if inside else HostFiles()
    missing = []
    for i, names in sorted(asking.items()):
        raw = files.read(os_home / "app-tokens" / i)
        held = raw.decode("utf-8", "replace").strip() if raw is not None else ""
        env = _user_env(runtipi_root, i, store)
        if not TOKEN.fullmatch(held) or any(env.get(n) != held for n in names):
            missing.append(i)
    if missing:
        return (LABEL, False, f"{', '.join(missing)}: no token matching the Agent Service's copy yet "
                              "(the next registry write hands it out)")
    return (LABEL, True, ids)


def before_start(c, app_id: str, prefer: str = "store") -> None:
    """Install/update hook, before Runtipi generates the app's env: the token is in user-config by then, so the app
    starts with it. Never raises (the registry hook after the install/update retries)."""
    try:
        r = sync_app(Path(c.runtipi_root), app_id, prefer=prefer, home=getattr(c, "home", None))
        if r.get("error"):
            print(f"note: agent inbox token not handed to {app_id} ({r['error']})", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"note: agent inbox token not handed to {app_id} ({str(e)[:160]})", file=sys.stderr)
