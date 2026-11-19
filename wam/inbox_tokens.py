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
The token opens only that app's own inbox routes and its delivery route, never the owner's. Nothing here prints a
token. Bug lane (always on): without it every app call to the inbox answers 403 on a real box.
"""
from __future__ import annotations

import os
import re
import secrets
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


def service_home(runtipi_root: Path, store: str = STORE) -> Path | None:
    """The Agent Service's own WIZARD_OS_HOME on the host (gateway_keys.path's lookup), None when no Service data
    folder is here yet."""
    from .gateway_keys import AGENT_SERVICE_APPS
    for app in AGENT_SERVICE_APPS:
        data = Path(runtipi_root) / "app-data" / store / app / "data"
        try:
            if data.is_dir():
                return data / "wizard-os"
        except OSError:
            continue
    return None


def _user_env(runtipi_root: Path, app_id: str, store: str = STORE) -> dict[str, str]:
    from .runtipi import read_env
    try:
        return read_env(Path(runtipi_root) / "user-config" / store / app_id / "app.env")
    except OSError:
        return {}


def _write_user_env(runtipi_root: Path, app_id: str, values: dict[str, str], store: str = STORE) -> None:
    """Merge KEY=VALUE pairs into the app's user-config app.env, 0600 from the start (it holds a token)."""
    p = Path(runtipi_root) / "user-config" / store / app_id / "app.env"
    p.parent.mkdir(parents=True, exist_ok=True)
    keep = [l for l in (p.read_text().splitlines() if p.is_file() else []) if l.partition("=")[0].strip() not in values]
    tmp = p.with_name(p.name + ".wam-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(keep + [f"{k}={v}" for k, v in values.items()]) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def sync_app(runtipi_root: Path, app_id: str, *, store: str = STORE, prefer: str = "installed", files=None,
             mint=lambda: secrets.token_urlsafe(32)) -> dict:
    """Give one installed app its inbox token. Returns {"vars": [...], "token": "kept|created|adopted",
    "changed": bool (its user-config app.env changed)} or {"skipped": reason}. Never prints the token.
    One sync at a time per box: registry writers run concurrently (API, watchers, installer), and two first-time
    syncs must never mint different tokens for one app (the Service would accept one, the app send the other)."""
    import fcntl
    lock = Path(runtipi_root) / "user-config" / store / ".wam-inbox-tokens.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            return _sync_app(runtipi_root, app_id, store=store, prefer=prefer, files=files, mint=mint)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _sync_app(runtipi_root: Path, app_id: str, *, store: str, prefer: str, files, mint) -> dict:
    if not APP_ID.fullmatch(app_id or ""):
        return {"skipped": "not an app id the Agent Service accepts"}
    names = token_vars(recipe_text(runtipi_root, app_id, store, prefer))
    if not names:
        return {"skipped": "its recipe takes no agent inbox token"}
    os_home = service_home(runtipi_root, store)
    if os_home is None:
        return {"skipped": "no Agent Service data folder on this box yet"}
    if files is None:
        from .gateway_keys import RuntipiFiles
        files = RuntipiFiles(Path(runtipi_root))
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


def sync(reg: dict, runtipi_root: Path, *, store: str = STORE, files=None, mint=None) -> dict:
    """sync_app for every installed app of this store: {"apps": {id: result}}. Never raises per app."""
    out: dict = {"apps": {}}
    for a in (reg or {}).get("apps") or []:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        if str(a.get("urn") or f"_:{store}").partition(":")[2] != store:
            continue
        try:
            kw = {"mint": mint} if mint else {}
            r = sync_app(runtipi_root, a["id"], store=store, files=files, **kw)
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
        res = sync(reg, Path(c.runtipi_root), files=files)
        for app_id in changed_running(res, rt):
            try:
                rt.restart(f"{app_id}:{STORE}")
                res["apps"][app_id]["restarted"] = True
            except Exception as e:  # noqa: BLE001
                res["apps"][app_id]["restart_error"] = str(e)[:160]
        for app_id, r in res["apps"].items():
            if r.get("error"):
                print(f"note: agent inbox token not handed to {app_id} ({r['error']})", file=sys.stderr)
        return res
    except Exception as e:  # noqa: BLE001 - a registry write must never fail on this
        print(f"note: agent inbox tokens not checked ({str(e)[:160]})", file=sys.stderr)
        return None


def before_start(c, app_id: str, prefer: str = "store") -> None:
    """Install/update hook, before Runtipi generates the app's env: the token is in user-config by then, so the app
    starts with it. Never raises (the registry hook after the install/update retries)."""
    try:
        r = sync_app(Path(c.runtipi_root), app_id, prefer=prefer)
        if r.get("error"):
            print(f"note: agent inbox token not handed to {app_id} ({r['error']})", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"note: agent inbox token not handed to {app_id} ({str(e)[:160]})", file=sys.stderr)
