"""wam: the Wizard App Manager command line.

The Wizard App Manager is Runtipi with its app store replaced by the Wizard App Index: browsing searches the index
live, and an app is downloaded only when it is opened, installed or updated. `wam` is its command line.

Every runtipi-cli command works as-is (passed straight through; app ids may drop the ":store" suffix):
  wam app start|stop|update|reset|uninstall|backup|restore|list-backups|delete-backup <app>
  wam app start-all|stop-all|available-updates      wam installed
  wam appstore add|list|remove|update               wam start|stop|restart|prepare|update|version|debug

Wizard additions:
  wam search [query]            search the index (apps.aiwizards.com)
  wam install <app>             install by id (plus the apps it `uses`), passwords generated + saved 0600
        [--set KEY=VALUE ...]   install-form values
  wam uninstall <id>            remove (backups are kept unless you delete them)
  wam apps [--json]             what is installed, with each app's MCP address (writes ~/.wizard-os/apps.json)
  wam slots [--json]            system app parking spots: which app fills each, which are empty
  wam bootstrap                 install the default app for every empty spot (skips spots with no default yet)
  wam links [--json] [--dry-run] put every installed app page on your Tailscale and print the links
  wam troubleshooter [--json] [--dry-run]  install/update the native Troubleshooter (/troubleshoot/, works when chat is down)
  wam doctor [--fix]            check the whole chain
  wam hermes status [--json]    host Hermes version vs the box's release pin
  wam hermes update [--json] [--dry-run] [--wait-quiet SECONDS]  explicit owner update, with backup and rollback
  wam alongside [--detect|--off] [--hermes-home DIR] [--shell-port N] [--share-history|--no-share-history]
                [--share-sessions|--no-share-sessions]
                                run next to this box's existing Hermes: it fills the agent spot (never migrated).
                                --share-history lets the shell read the Hermes home (titles, archive; exposes credentials)
                                --share-sessions shares only state.db read-only (helper status, titles, archive; no credentials)
  wam serve                     HTTP API on 127.0.0.1:7790 (changes need the token in ~/.wizard-os/api-token)
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from . import alongside, index, private, registry, slots, release_pins
from .runtipi import Runtipi, RuntipiError, read_env

STORE = "wizardapps"
STORE_NAME = "Wizard Apps"


class Config:
    def __init__(self) -> None:
        release_pins.load()  # reject malformed pins before creating state or contacting Runtipi
        self.home = registry.home()
        self.home.mkdir(parents=True, exist_ok=True)
        cfg_file = self.home / "app-manager.json"
        cfg = json.loads(cfg_file.read_text()) if cfg_file.is_file() else {}
        self.runtipi_root = Path(os.environ.get("RUNTIPI_ROOT", cfg.get("runtipi_root", Path.home() / "runtipi"))).expanduser()
        self.index = index.base(cfg)
        self.private_apps: dict[str, str] = {}
        pa = cfg.get("private_apps", {})
        if not isinstance(pa, dict):
            raise SystemExit("private_apps must be an object of app-id: owner/repo aliases")
        for k, v in pa.items():
            if not isinstance(k, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", k):
                raise SystemExit(f"invalid private_apps alias: {k!r}")
            # Accept the documented URL form, but never repair malformed identifiers.
            repo = v.removeprefix("https://github.com/") if isinstance(v, str) else ""
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo) or repo.split("/")[-1] in (".", ".."):
                raise SystemExit(f"private_apps[{k}] must be owner/repo or https://github.com/owner/repo")
            self.private_apps[k] = repo
        self.bind = os.environ.get("WAM_BIND", cfg.get("bind", "127.0.0.1"))
        self.port = int(os.environ.get("WAM_PORT", cfg.get("port", 7790)))
        self.secrets = self.home / "secrets"
        self.token_file = self.home / "api-token"

    def runtipi(self) -> Runtipi:
        rt = Runtipi.from_install(self.runtipi_root)
        if rt.ensure_operator(self.home / "runtipi-operator.json"):
            print(f"created an operator account; sign-in saved to {self.home / 'runtipi-operator.json'}", file=sys.stderr)
        return rt

    def host_ip(self) -> str:
        try:
            return read_env(self.runtipi_root / ".env").get("INTERNAL_IP") or "127.0.0.1"
        except Exception:  # noqa: BLE001
            return "127.0.0.1"

    def api_token(self) -> str:
        if not self.token_file.is_file():
            self.token_file.write_text(secrets.token_urlsafe(32) + "\n")
            self.token_file.chmod(0o600)
        return self.token_file.read_text().strip()


# ---------------------------------------------------------------- helpers

def store_ok(c: Config, s: dict | None) -> bool:
    """The App Manager treats the `wizardapps` store as the index whatever URL an old install left in it."""
    return bool(s) and bool(s.get("enabled"))


def ensure_store(c: Config, rt: Runtipi) -> None:
    """The Wizard store is the index itself (no git catalog): it must exist and be enabled."""
    s = {x.get("slug"): x for x in rt.stores()}.get(STORE)
    if store_ok(c, s):
        return
    if s:
        rt.req("PATCH", f"/marketplace/{STORE}", {"name": s.get("name") or STORE_NAME, "enabled": True})
        return
    # Runtipi derives the slug from the name, so create it as "wizardapps" (the slug every urn uses), then rename.
    # Earlier wam versions created it as "Wizard Apps" (slug wizard-apps); with that copy present the create is a
    # 409 (same URL), so remove only that known legacy slug. The API refuses deletion when apps exist
    # or this is the last store; let that failure stop migration rather than bypassing its safeguards.
    want = c.index.rstrip("/")
    for x in rt.stores():
        if x.get("slug") == "wizard-apps" and str(x.get("url") or "").rstrip("/") == want:
            rt.req("DELETE", "/marketplace/wizard-apps")
    rt.req("POST", "/marketplace/create", {"name": STORE, "url": c.index})
    rt.req("PATCH", f"/marketplace/{STORE}", {"name": STORE_NAME, "enabled": True})


def write_registry(c: Config, rt: Runtipi | None = None) -> dict:
    rt = rt or c.runtipi()
    reg = registry.build(rt.installed(), STORE, c.host_ip(), c.runtipi_root)
    ext = alongside.external_slots(c.home)
    if ext:
        reg["external"] = ext   # spots filled from outside Runtipi (alongside mode: the host's own Hermes)
    from . import links
    links.apply_to_registry(reg, c.home)   # published apps open at their private-network address, not loopback
    registry.write(reg, c.home)
    slots.merge_into_registry(reg)
    registry.write(reg, c.home)   # second write adds `parking` (every spot, filled or not)
    share_registry(c)
    # Return the exact owner-route projection we persisted, including staged view IDs.
    # Internal _skill_src remains available for both copy passes, never in API/CLI output.
    return json.loads((c.home / "apps.json").read_text())


def share_registry(c: Config) -> bool:
    """Give app containers (the Wizard shell) a read-only copy of apps.json + skills, never secrets or tokens, in
    Runtipi's app-data, where an app's compose mounts it: ${ROOT_FOLDER_HOST}/app-data/wizard-os (same as wam on stock)."""
    import shutil
    shared = c.runtipi_root / "app-data" / "wizard-os"
    try:
        shared.mkdir(parents=True, exist_ok=True)
        # The share holds no secrets and is read by app containers running as other users, so its
        # modes are explicit: a caller's restrictive umask (0600 apps.json) must not hide every app
        # from the Agent Service. The owner's own ~/.wizard-os copy keeps whatever mode it has.
        os.chmod(shared, 0o755)
        tmp = shared / "apps.json.tmp"
        shutil.copyfile(c.home / "apps.json", tmp)
        os.chmod(tmp, 0o644)
        tmp.replace(shared / "apps.json")
        if (c.home / "skills").is_dir():
            shutil.rmtree(shared / "skills", ignore_errors=True)
            # Links stay links (never copied as their target's contents) and are never chmod-ed,
            # so widening modes can only ever touch the regular skill files WAM itself wrote.
            shutil.copytree(c.home / "skills", shared / "skills", symlinks=True, copy_function=shutil.copyfile)
            for dirpath, dirnames, filenames in os.walk(shared / "skills"):
                os.chmod(dirpath, 0o755)
                for name in filenames:
                    f = os.path.join(dirpath, name)
                    if not os.path.islink(f):
                        os.chmod(f, 0o644)
        from .view_share import publish
        from . import chats_views
        chats_reg = json.loads((c.home / 'apps.json').read_text())
        for app in chats_reg.get('apps', []):
            view = app.get('chats_view')
            if view:
                app['_chats_src'] = str(c.home / 'chats-views' / app['id'] / (view['sha256'] + '.js'))
        chats_views.publish(chats_reg, shared)
        for metadata in (c.home / "views").glob("*/view.json"):
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", metadata.parent.name):
                publish(metadata.parent.name, c.home, shared)
        return True
    except OSError as e:
        print(f"note: could not share apps.json at {shared} ({e}); apps in containers won't see it", file=sys.stderr)
        return False


def installed_ids(rt: Runtipi) -> set[str]:
    out = set()
    for r in rt.installed():
        name, _, st = (((r.get("info") or {}).get("urn")) or "").partition(":")
        if st == STORE:
            out.add(name)
    return out


def shared_value(c: Config, var: str, uses: list[str]) -> str | None:
    """A secret this app shares with an app it uses, e.g. the shell's WIZARD_HERMES_API_KEY is hermes-agent's
    HERMES_API_KEY: same name, or same name without a WIZARD_ prefix, in that app's Runtipi app.env."""
    from .runtipi import read_env
    if getattr(c, "home", None) and (v := alongside.shared_secret(c.home, var)):
        return v
    for dep in uses:
        envp = c.runtipi_root / "app-data" / STORE / dep / "app.env"
        if not envp.is_file():   # not installed (or, alongside, filled by the host)
            continue
        env = read_env(envp)
        for k in (var, var.removeprefix("WIZARD_")):
            if env.get(k):
                return env[k]
    return None


def shell_origins(c: Config) -> set[str]:
    """Existing private HTTPS shell mounts, never inferred from a request Origin."""
    from . import links, store_install
    try:
        host = json.loads((c.home / "links.json").read_text()).get("host") or ""
        port = int(((alongside.load(c.home) or {}).get("shell") or {}).get("port") or 0)
        config = links.serve_status()
        origins = store_install.served_origins(host)
        # Also support CLI-only boxes without the Store listener mount.
        for authority, site in config.get("Web", {}).items():
            if authority.rsplit(":", 1)[0] == host and port and site.get("Handlers", {}).get("/", {}).get("Proxy") == f"http://127.0.0.1:{port}":
                origins.add(f"https://{authority}")
        return {o for o in origins if not config.get("AllowFunnel", {}).get(o.removeprefix("https://"))}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return set()


def is_origin_list(f: dict) -> bool:
    """A required list of trusted browser origins with no real default, e.g. `X_ALLOWED_ORIGINS` (url) or Wisdom's
    `WISDOM_ORIGINS` (text, default ''). Runtipi takes an empty default as the value, so the app would start
    trusting no Wizard origin and answer 'Unknown host' inside the shell."""
    var = str(f.get("env_variable") or "")
    return f.get("type") in ("url", "text") and var.endswith("_ORIGINS") and f.get("default", "") == ""


def build_form(c: Config, app_id: str, info: dict, given: dict[str, str], uses: list[str] | None = None) -> tuple[dict, list[str]]:
    """Defaults and `random` fields are left to the App Manager. Required passwords the owner didn't give are
    generated and saved 0600 in ~/.wizard-os/secrets/<id>.json (never printed). Ports stay box-only: the app's
    compose publishes on 127.0.0.1, so openPort/exposed stay off."""
    form: dict = {"openPort": False, "exposed": False, "exposedLocal": False}
    saved: dict = {}
    missing: list[str] = []
    for f in info.get("form_fields") or []:
        var = f.get("env_variable")
        if not var or f.get("type") == "random":
            continue
        if var in given:
            form[var] = given[var]
        elif f.get("required") and (v := shared_value(c, var, uses or [])):
            form[var] = v
        elif f.get("required") and is_origin_list(f) and (origins := shell_origins(c)):
            # Origin-list configuration is distinct from arbitrary required URLs
            # (provider endpoints etc.). Only existing owner Serve mounts qualify.
            form[var] = ",".join(sorted(origins))
        elif "default" in f:
            continue
        elif f.get("required") and f.get("type") == "password":
            form[var] = saved[var] = secrets.token_urlsafe(max(24, int(f.get("min") or 16)))
        elif f.get("required"):
            missing.append(f"{var} ({f.get('label', '')}: {f.get('hint', '')})")
    if saved:
        c.secrets.mkdir(mode=0o700, exist_ok=True)
        p = c.secrets / f"{app_id}.json"
        p.write_text(json.dumps(saved, indent=2) + "\n")
        p.chmod(0o600)
        print(f"{app_id}: generated {', '.join(saved)}; saved to {p}", file=sys.stderr)
    return form, missing


# ---------------------------------------------------------------- commands

def install(c: Config, app_id: str, given: dict[str, str] | None = None, wait: bool = True, _seen: set | None = None,
            _split: bool = False, _record: dict | None = None) -> dict:
    release_pins.load()  # also covers callers that supply their own Config
    _seen = _seen if _seen is not None else set()
    if app_id in _seen:
        return {"id": app_id, "status": "already handled"}
    _seen.add(app_id)
    rt = c.runtipi()
    rec = _record if _record is not None else resolve_any(c, app_id)
    app_id = rec["id"]
    ensure_store(c, rt)
    have = installed_ids(rt)
    external_apps = {d.get("app") for s_, d in slots.load_defaults().items() if s_ in alongside.external_slots(c.home)}
    defaults = slots.load_defaults()
    for dep in rec.get("uses") or []:
        # `uses` may name a parking spot (e.g. agent-service) rather than an app: install the spot's default app,
        # unless an installed app already provides that spot.
        if dep in defaults:
            spot = dep
            providers = slots.providers(write_registry(c, rt)).get(spot) or []
            if providers or spot in alongside.external_slots(c.home):
                continue
            dep = (defaults[spot] or {}).get("app") or dep
        if dep in have or dep in _seen:
            continue
        if dep in external_apps:   # alongside mode: the host's own Hermes fills this spot
            print(f"skipping dependency {dep}: its spot is filled by the host (alongside mode)", file=sys.stderr)
            continue
        try:
            print(f"installing dependency {dep}", file=sys.stderr)
            install(c, dep, None, wait, _seen)
        except (index.IndexError_, SystemExit, RuntimeError, RuntipiError) as e:
            print(f"note: dependency {dep} not installed ({e}); {app_id} may need it", file=sys.stderr)
    urn = f"{app_id}:{STORE}"
    if app_id in installed_ids(rt):
        return {"id": app_id, "urn": urn, "status": "already installed", "hint": f"wam app update {app_id}"}
    if app_id == "hermes-agent" and not alongside.load(c.home):
        from .builder_bundle import fresh
        fresh(c, app_id, STORE)
    info = (rt.app(urn) or {}).get("info") or {}   # the App Manager fetches and checks this one app now
    if not info:
        raise SystemExit(f"{app_id}: the App Manager could not fetch it from the index (see its logs)")
    form, missing = build_form(c, app_id, info, given or {}, uses=rec.get("uses") or [])
    role = alongside_role(rec, app_id)
    if role in ("service", "shell") and alongside.LEGACY_BUNDLE in have and not _split:
        raise SystemExit(f"this box runs the {alongside.LEGACY_BUNDLE} bundle (the shell and the Agent Service in one). "
                         f"Split it with `wam shell-split` (stops the bundle, copies its state, installs both apps); "
                         f"installing {app_id} beside it would start a second Agent Service")
    if (al := alongside.load(c.home)) and role:
        alongside.write_shell_override(c.runtipi_root, STORE, app_id, c.home, role)
        # fixed loopback ports: the agent's `wizard` MCP entry names the shell's; the shell names the service's
        form["port"] = alongside.service_port(al) if role == "service" else int(al["shell"]["port"])
    if "port" not in form:
        # The recipe's default port may already be taken on this box (e.g. two test apps both default to 8799):
        # Runtipi then fails `compose up` with "port is already allocated". Pick the next free one instead.
        # Dependencies can be installed before the shell/service starts listening. Reserve their
        # recorded ports as well as live listeners, or a dependency can steal the host bridge port.
        reserved = {int((al.get(part) or {}).get("port") or 0) for part in ("shell", "service")} if al else set()
        want = int(info.get("port") or 0)
        if want and (want in reserved or not port_free(want)):
            form["port"] = free_port(want + 1, avoid=reserved)
            print(f"note: port {want} is in use or reserved; {app_id} gets {form['port']}", file=sys.stderr)
    trust_env = trusted_origin_env(c, app_id, int(form.get("port") or info.get("port") or 0))
    if trust_env:
        write_user_env(c.runtipi_root, app_id, trust_env)
    if al:   # e.g. Claude: the app must hold the host Hermes' own Claude Code sign-in, not a second one
        try:
            for name, r in claude_login_cmd(c, {app_id: rec.get("provides") or []}).get("apps", {}).items():
                if r.get("state") == "error":
                    print(f"note: {name} keeps its own Claude sign-in ({r.get('error')}); run `wam alongside`",
                          file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - never fail the install for it; `wam alongside` repairs it
            print(f"note: Claude login not linked ({str(e)[:120]}); run `wam alongside`", file=sys.stderr)
    if missing:
        raise SystemExit(f"{app_id} needs: " + "; ".join(missing) + f"\n  e.g. wam install {app_id} --set KEY=VALUE")
    rt.install(urn, form)
    if role == "service" and alongside.load(c.home):
        alongside.set_service_active(c.home, True)   # the agent's gateway now lives on the Agent Service
    # A failed install (e.g. an image that can't be pulled) deletes the app record, so "missing" means failed:
    # stop waiting then instead of polling for the full timeout.
    status = rt.wait_status(urn, {"running", "stopped", "missing"}) if wait else "installing"
    if status == "missing":
        reason = install_failure_reason(urn)
        raise SystemExit(f"{app_id}: install failed" + (f": {reason}" if reason else "") +
                         " (the App Manager removed it; see `docker logs runtipi`)")
    reg = write_registry(c, rt)
    if status == "running" and alongside.load(c.home) and app_id in host_dashboard_consumers(c, {app_id}):
        try:   # e.g. ChatGPT: its sign-in runs through the agent's dashboard, which alongside lives on the host
            dash = host_dashboard_cmd(c, rt)
            if app_id in (dash.get("changed_apps") or []):
                rt.update(urn, perform_backup=False)   # restart it with the dashboard address and login
                rt.wait_status(urn, {"running", "stopped", "missing"})
        except Exception as e:  # noqa: BLE001 - never fail the install for it
            print(f"note: host dashboard not set up ({str(e)[:120]}); run `wam alongside`", file=sys.stderr)
    if app_id in ("hermes-agent", "wizard-agent-service") and status == "running" and not alongside.load(c.home) \
            and {"hermes-agent", "wizard-agent-service"} <= {a["id"] for a in reg["apps"]}:
        try:   # fresh mode: the agent app gets the `wizard` gateway and pointer skills (alongside: `wizard link`)
            from .agent_link import link as agent_link
            agent_link(c, STORE)
        except Exception as e:  # noqa: BLE001 - never fail the install for it; doctor/agent-link repair it
            print(f"note: agent not linked to the app gateway ({str(e)[:160]}); run `wam agent-link`", file=sys.stderr)
    if role == "shell" and status == "running":
        try:   # alongside and fresh: the Store data adapter follows the installed shell's commit
            store_data_cmd(c)
        except Exception as e:  # noqa: BLE001 - the Store window is optional; never fail the shell install for it
            print(f"note: App Store data not set up ({str(e)[:120]}); run `wam store-data`", file=sys.stderr)
    entry = next((a for a in reg["apps"] if a["id"] == app_id), {})
    return {"id": app_id, "urn": urn, "status": status, "commit": (entry.get("commit") or "")[:12], "from": rec["repo"],
            "open": (entry.get("view") or {}).get("url"), "mcp": (entry.get("mcp") or {}).get("url"),
            "installed": [a["id"] for a in reg["apps"]], **({"private": True} if rec.get("private") else {})}


def trusted_origin_env(c: Config, app_id: str, app_port: int | None = None) -> dict[str, str]:
    """Apps built with the Wizard builder answer only hosts/origins they are told to trust
    (WIZARD_TRUSTED_HOSTS / WIZARD_TRUSTED_ORIGINS). With Tailscale, trust the shell's tailnet origin so the app
    opens inside Wizard instead of answering 'untrusted_host_or_site'. A fresh box (Runtipi shell, no alongside.json)
    takes the shell port from apps.json and also trusts the app's own private address, `https://<box>:<app port>`,
    which is where `wam links` publishes it there (alongside boxes route such apps under the shell origin)."""
    # Before install the recipe is only in Runtipi's store copy (repos/), after it also in apps/.
    text = ""
    for comp in (c.runtipi_root / "repos" / STORE / "apps" / app_id / "docker-compose.yml",
                 c.runtipi_root / "apps" / STORE / app_id / "docker-compose.yml"):
        try:
            text = comp.read_text()
            break
        except OSError:
            continue
    if "WIZARD_TRUSTED_HOSTS" not in text:
        return {}
    al = alongside.load(c.home)
    own = 0
    if al:
        port = int((al.get("shell") or {}).get("port") or 0)
    else:
        from . import store_data
        try:
            reg = json.loads((c.home / "apps.json").read_text())
        except (OSError, ValueError):
            reg = {}
        port = store_data.fresh_ports(reg)[0]
        own = int(app_port or 0) if app_port and 0 < int(app_port) < 65536 and int(app_port) != port else 0
    try:
        host = json.loads((c.home / "links.json").read_text()).get("host") or ""
    except (OSError, ValueError):
        host = ""
    if not host or not port or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        return {}
    hosts, origins = [f"{host}:{port}", host], [f"https://{host}:{port}"]
    if own:
        hosts.insert(1, f"{host}:{own}")
        origins.append(f"https://{host}:{own}")
    env = {"WIZARD_TRUSTED_HOSTS": ",".join(hosts), "WIZARD_TRUSTED_ORIGINS": ",".join(origins)}
    # Cloudflare and other owner-login recipes need the box owner's tailnet identity.
    if "WIZARD_OWNER_LOGINS" in text:
        from . import store_install
        owner = store_install.owner_login()
        if owner:
            env["WIZARD_OWNER_LOGINS"] = owner
    return env


def write_user_env(runtipi_root: Path, app_id: str, values: dict[str, str]) -> Path:
    """Merge KEY=VALUE pairs into the app's Runtipi user-config app.env (kept across updates), owner-only."""
    p = runtipi_root / "user-config" / STORE / app_id / "app.env"
    p.parent.mkdir(parents=True, exist_ok=True)
    keep = [l for l in (p.read_text().splitlines() if p.is_file() else [])
            if l.partition("=")[0].strip() not in values]
    p.write_text("\n".join(keep + [f"{k}={v}" for k, v in values.items()]) + "\n")
    p.chmod(0o600)
    return p


def port_free(port: int) -> bool:
    """Nothing listens on this TCP port on any local address (loopback, tailnet, all)."""
    import socket
    for fam, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET, "127.0.0.1")):
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((addr, port))
            except OSError:
                return False
    try:
        rows = [l.split() for f in ("/proc/net/tcp", "/proc/net/tcp6") for l in open(f).read().splitlines()[1:]]
    except OSError:
        rows = []
    return not any(len(r) > 3 and r[3] == "0A" and int(r[1].rsplit(":", 1)[1], 16) == port for r in rows)


def free_port(start: int, stop: int = 65000, *, avoid: set[int] | None = None) -> int:
    for p in range(max(1024, start), stop):
        if p not in (avoid or set()) and port_free(p):
            return p
    raise SystemExit("no free port for this app")


def install_failure_reason(urn: str) -> str:
    """Best effort: the App Manager's last logged error line for this app."""
    try:
        p = subprocess.run(["docker", "logs", "--since", "30m", "runtipi"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    name = urn.split(":")[0]
    hits = [l.strip() for l in (p.stdout + p.stderr).splitlines()
            if name in l and any(w in l.lower() for w in ("error", "denied", "failed", "not found", "manifest"))]
    return hits[-1][-300:] if hits else ""


def carry_bundle_state(runtipi_root: Path, app_id: str, store: str = STORE, uid: int | None = None,
                       gid: int | None = None) -> Path | None:
    """Splitting the pre-split wizard-shell bundle (stopped first, see shell_split): the new Agent Service starts
    with the bundle's state (thread dispositions, files, run queue) instead of an empty folder. Copied, never moved,
    and only into an empty folder, so the bundle can still be started again (rollback) and a re-run changes nothing.
    The copy is built beside the target and renamed into place only when complete, owned by the user the Agent
    Service container runs as (`uid`/`gid`); on any error nothing is left behind."""
    src = runtipi_root / "app-data" / store / alongside.LEGACY_BUNDLE / "data"
    dst = runtipi_root / "app-data" / store / app_id / "data"
    if app_id == alongside.LEGACY_BUNDLE or src.is_symlink() or not src.is_dir() or not any(src.iterdir()):
        return None
    if dst.is_dir() and any(dst.iterdir()):
        return None
    for stale in dst.parent.glob(".data.carry-*") if dst.parent.is_dir() else []:   # a copy killed midway
        shutil.rmtree(stale, ignore_errors=True)
    tmp = dst.parent / f".data.carry-{os.getpid()}"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, tmp, symlinks=True)
        if uid is not None and gid is not None:
            if os.geteuid() == 0:
                for p in [tmp, *tmp.rglob("*")]:
                    os.lchown(p, uid, gid)
            elif os.getuid() != uid:
                raise PermissionError(f"the Agent Service runs as uid {uid}, this command as uid {os.getuid()}")
        if dst.is_dir():
            dst.rmdir()   # empty (checked above)
        tmp.rename(dst)
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"could not copy the Wizard shell's state to {app_id} ({e}); nothing was changed. "
                         f"Run it as root (sudo), or as the owner of {src}") from None
    return src


LIFECYCLE_WANT = {"start": {"running"}, "restart": {"running"}, "stop": {"stopped"}}


def app_lifecycle(c: "Config", verb: str, app_id: str, timeout: int = 300) -> dict:
    """start/stop/restart one app through the authenticated App Manager API (the path the web UI uses).
    runtipi-cli talks to the queue directly on [::1]:5672, which the stock compose never publishes (#53)."""
    rt = c.runtipi()
    urn = app_id if ":" in app_id else f"{app_id}:{STORE}"
    if not ((rt.app(urn) or {}).get("app") or {}):
        raise SystemExit(f"{app_id} is not installed")
    getattr(rt, verb)(urn)
    time.sleep(2)
    status = rt.wait_status(urn, LIFECYCLE_WANT[verb] | {"start_error", "stop_error", "restart_error", "missing"}, timeout)
    out = {"id": app_id, "urn": urn, "action": verb, "status": status}
    if status not in LIFECYCLE_WANT[verb]:
        out["error"] = f"{verb} did not finish (status {status}); see `docker logs runtipi`"
    return out


def _app(c: Config, verb: str, app_id: str) -> None:
    """Start/stop one app through the App Manager API (not runtipi-cli; see app_lifecycle)."""
    res = app_lifecycle(c, verb, app_id)
    if res.get("error"):
        raise SystemExit(f"wam app {verb} {app_id} failed: {res['error']}")


def _bundle_queue(port: int) -> dict | None:
    """The bundle's run queue from its health, or None when that can't be read (no answer, or no queue field)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/wizard/health", timeout=5) as r:  # noqa: S310 - loopback
            q = (json.loads(r.read()) or {}).get("queue")
            return q if isinstance(q, dict) else None
    except Exception:  # noqa: BLE001
        return None


def can_carry(runtipi_root: Path, uid: int, gid: int, store: str = STORE) -> str:
    """Why the bundle's state could not be copied for the Agent Service ('' when it can): checked before anything
    is stopped."""
    src = runtipi_root / "app-data" / store / alongside.LEGACY_BUNDLE / "data"
    if not src.is_dir() or src.is_symlink():
        return ""
    if os.geteuid() != 0 and os.getuid() != uid:
        return f"the Agent Service runs as uid {uid}, this command as uid {os.getuid()}: run it with sudo"
    for p in [src, *src.rglob("*")]:
        if p.is_symlink():
            continue
        if not os.access(p, os.R_OK | (os.X_OK if p.is_dir() else 0)):
            return f"{p} is not readable by this user: run it with sudo"
    return ""


def _restore_bundle(c: Config, rt: Runtipi, al: dict | None, remove: list[str] = (), stop: list[str] = ()) -> dict:
    """Best effort, every step tried: uninstall the apps a failed split installed (`remove`; stopped instead if the
    uninstall fails), stop the others (`stop`), point alongside back at the bundle and start it. The bundle is started
    only when no Agent Service can still be running (one writer); otherwise that is reported and it stays stopped."""
    errors, done, still_up = [], [], []
    for app in remove:
        try:
            rt.uninstall(f"{app}:{STORE}")
            if rt.wait_status(f"{app}:{STORE}", {"missing"}, 120) != "missing":
                raise RuntimeError("still installed after 120 s")
            done.append(f"uninstalled {app}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"uninstall {app}: {str(e)[:160]}")
            stop = [*stop, app]
    for app in stop:
        try:
            _app(c, "stop", app)
            if rt.wait_status(f"{app}:{STORE}", {"stopped", "missing"}, 120) not in ("stopped", "missing"):
                raise RuntimeError("did not stop")
            done.append(f"stopped {app}")
        except BaseException as e:  # noqa: BLE001 - SystemExit from runtipi-cli too
            errors.append(f"stop {app}: {str(e)[:160]}")
            if app == "wizard-agent-service":
                still_up.append(app)
    if al:
        try:
            alongside.write_shell_override(c.runtipi_root, STORE, alongside.LEGACY_BUNDLE, c.home, "bundle")
            alongside.set_service_active(c.home, False)
        except Exception as e:  # noqa: BLE001
            errors.append(f"alongside override: {str(e)[:160]}")
    status = "stopped (not started: an Agent Service may still be running)" if still_up else "unknown"
    if not still_up:
        try:
            _app(c, "start", alongside.LEGACY_BUNDLE)
            status = rt.wait_status(f"{alongside.LEGACY_BUNDLE}:{STORE}", {"running"}, 180)
        except BaseException as e:  # noqa: BLE001
            errors.append(f"start {alongside.LEGACY_BUNDLE}: {str(e)[:160]}")
    return {"bundle": status, "done": done, "errors": errors}


def _stopped(rt: Runtipi, app: str) -> bool:
    return rt.wait_status(f"{app}:{STORE}", {"stopped"}, 1) == "stopped"


def shell_split(c: Config, rollback: bool = False, force: bool = False) -> dict:
    """The one ordered upgrade from the wizard-shell bundle to the split pair, and its rollback.
    Split: check the state copy can succeed and refuse while the bundle has live runs (nothing changed yet) -> stop
    the bundle and confirm it stopped -> copy its state -> install the Agent Service, then the shell. Any failure
    after the stop uninstalls what this run installed (or stops it) and starts the bundle again, so a re-run starts
    clean. A run killed after the service install (bundle stopped, service there, no shell) is finished by the next
    run. Splitting again after a rollback (service installed, bundle running) stops the service, sets its earlier
    state aside, copies the bundle's current state and starts it again. Exactly one Agent Service (and so one
    follow-through writer) runs at any time. Rollback: stop both, start the bundle on its own state again (anything
    made after the split stays in the Agent Service's folder). The agent's `wizard` MCP entry is never edited: it
    names the shell's loopback port, which serves the gateway throughout."""
    rt = c.runtipi()
    have = installed_ids(rt)
    bundle, svc, ux = alongside.LEGACY_BUNDLE, "wizard-agent-service", "wizard-ux"
    al = alongside.load(c.home)
    port = int(al["shell"]["port"]) if al else alongside.DEFAULT_SHELL_PORT
    if bundle not in have:
        raise SystemExit(f"no {bundle} bundle on this box: nothing to split or go back to")
    if rollback:
        r = _restore_bundle(c, rt, al, stop=[a for a in (ux, svc) if a in have])
        if r["errors"]:
            raise SystemExit("rollback incomplete: " + "; ".join(r["errors"]) + f" (done: {', '.join(r['done']) or 'nothing'}"
                             f"; bundle {r['bundle']})")
        return {"rolled_back": True, "stopped": r["done"], "bundle": r["bundle"],
                "note": "threads, files and runs made after the split stay in the Agent Service's folder"}
    if ux in have:
        raise SystemExit(f"{ux} is already installed next to the bundle; `wam shell-split --rollback` stops it, "
                         f"`wam uninstall {ux}` removes it")
    bundle_stopped = _stopped(rt, bundle)
    # Killed after the service install: the service still runs and the bundle is stopped. Anything else with the
    # service installed (it was stopped by a rollback) is a split again: the bundle's current state is copied.
    resume = svc in have and bundle_stopped and not _stopped(rt, svc)
    again = svc in have and not resume
    owner = (int(al["agent"]["uid"]), int(al["agent"]["gid"])) if al else (1000, 1000)
    if not resume and (why := can_carry(c.runtipi_root, *owner)):
        raise SystemExit(f"can't copy the Wizard shell's state: {why}. Nothing was changed")
    if not bundle_stopped:
        q = _bundle_queue(port)
        if not force and q is None:
            raise SystemExit(f"the bundle's health ({port}) does not report its run queue, so live runs can't be "
                             "checked; pass --force once you know nothing is running. Nothing was changed")
        if not force and (q.get("active") or q.get("queued")):
            raise SystemExit(f"the bundle has {q.get('active', 0)} running and {q.get('queued', 0)} queued runs; "
                             "split when they finish (or --force). Nothing was changed")
        if again and not _stopped(rt, svc):
            raise SystemExit(f"{svc} is running next to the bundle; `wam shell-split --rollback` first. Nothing was changed")
        _app(c, "stop", bundle)
        if (st := rt.wait_status(f"{bundle}:{STORE}", {"stopped"}, 180)) != "stopped":
            r = _restore_bundle(c, rt, al)
            raise SystemExit(f"the bundle did not stop (status {st}); it was started again ({r['bundle']}), "
                             "nothing else changed")
    carried, installed_now = None, []
    step = "copying the bundle's state"
    try:
        if not resume:
            old = c.runtipi_root / "app-data" / STORE / svc / "data"
            if old.is_dir() and any(old.iterdir()):   # an earlier split's state: keep it aside, start from the bundle's
                old.rename(old.with_name(f"data.before-split-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"))
            carried = carry_bundle_state(c.runtipi_root, svc, uid=owner[0], gid=owner[1])
        if again:
            step = f"starting {svc} again"
            if al:
                alongside.write_shell_override(c.runtipi_root, STORE, svc, c.home, "service")
                alongside.set_service_active(c.home, True)
            _app(c, "start", svc)
            r1 = {"status": rt.wait_status(f"{svc}:{STORE}", {"running"}, 180)}
        elif resume:
            r1 = {"status": "already installed (resumed)"}
        else:
            step = f"installing {svc}"
            r1 = install(c, svc, _split=True)
            installed_now.append(svc)
        step = f"installing {ux}"
        r2 = install(c, ux, _split=True)
    except (SystemExit, RuntimeError, RuntipiError, OSError) as e:
        now = installed_ids_safe(rt)
        remove = [a for a in (ux, *installed_now) if a in now]
        r = _restore_bundle(c, rt, al, remove=remove, stop=[svc] if svc in now and svc not in installed_now else [])
        tail = ("" if not r["errors"] else " Recovery had problems: " + "; ".join(r["errors"])
                + ". Run `wam shell-split --rollback` once they are fixed.")
        raise SystemExit(f"the split stopped while {step} ({str(e)[:300]}). Recovery: {', '.join(r['done']) or 'nothing to undo'}"
                         f"; bundle {r['bundle']}.{tail} Fix the cause and re-run `wam shell-split`") from None
    return {"split": True, "resumed": resume, "again": again, "carried_from": str(carried) if carried else None,
            "service": r1.get("status"), "shell": r2.get("status"),
            # the agent's `wizard` MCP entry keeps its URL: the shell's loopback port passes the gateway through
            "rollback": "wam shell-split --rollback  (the bundle and its state are kept)"}


def installed_ids_safe(rt: Runtipi) -> set[str]:
    try:
        return installed_ids(rt)
    except Exception:  # noqa: BLE001
        return set()


def alongside_side(rt: Runtipi, apps: dict[str, str], saved: bool) -> bool:
    """True when the Agent Service is the side this box runs: the service if it is running, the bundle if it is
    running, else the saved choice (a reboot or an update can catch both stopped for a moment)."""
    def up(role: str) -> bool:
        return any(rt.wait_status(f"{a}:{STORE}", {"running"}, 1) == "running" for a, r in apps.items() if r == role)
    if not any(r == "service" for r in apps.values()):
        return False
    if up("service"):
        return True
    if up("bundle"):
        return False
    # Neither runs (a reboot, an update): the saved choice; a box split in fresh mode has none, but its shell is
    # installed, which is only allowed after the split.
    return saved or any(r == "shell" for r in apps.values())


def alongside_role(rec: dict, app_id: str) -> str | None:
    """'service', 'shell' or 'bundle' (the pre-split wizard-shell) for the apps alongside mode reconfigures."""
    provides = list(rec.get("provides") or [])
    defaults = slots.load_defaults()
    for slot in (alongside.SHELL_SLOT, alongside.SERVICE_SLOT):   # a default fills its spot before its record says so
        if app_id == (defaults.get(slot) or {}).get("app") and slot not in provides:
            provides.append(slot)
    return alongside.role_of(app_id, provides)


def is_shell(rec: dict, app_id: str) -> bool:
    return alongside_role(rec, app_id) is not None


def _alongside_apps(c: Config, rt: Runtipi | None = None) -> dict[str, str]:
    """app id -> role, for the shell/service apps alongside mode reconfigures: the installed ones when the App
    Manager answers, else the slot defaults."""
    defaults = slots.load_defaults()
    want = {d.get("app") for s, d in defaults.items() if s in (alongside.SHELL_SLOT, alongside.SERVICE_SLOT)}
    want.add(alongside.LEGACY_BUNDLE)
    out = {}
    reg = registry_apps(c)
    have = installed_ids(rt) if rt is not None else (set(reg) if reg else None)
    for app_id in sorted(a for a in want if a):
        if have is not None and app_id not in have:
            continue
        role = alongside_role(reg.get(app_id) or {}, app_id)
        if role:
            out[app_id] = role
    return out


def registry_apps(c: Config) -> dict[str, dict]:
    try:
        return {a["id"]: a for a in json.loads((c.home / "apps.json").read_text()).get("apps") or []}
    except (OSError, ValueError, KeyError):
        return {}


def _app_image(c: Config, st: dict, app_id: str) -> str:
    """The image an installed app runs: its alongside pin, else the first image in its installed compose."""
    if alongside.pin(st, app_id):
        return alongside.pin(st, app_id)
    comp = c.runtipi_root / "apps" / STORE / app_id / "docker-compose.yml"
    try:
        imgs = private.images(yaml.safe_load(comp.read_text()) or {})
    except (OSError, ValueError, yaml.YAMLError):
        return ""
    return imgs[0] if imgs else ""


def alongside_cmd(c: Config, detect_only: bool, off: bool, hermes_home: str | None, shell_port: int | None,
                  share_history: bool | None = None, share_sessions: bool | None = None) -> dict:
    home = alongside.hermes_home(hermes_home)
    if detect_only:
        return alongside.detect(home)
    if off:
        was = alongside.disable(c.home)
        from . import host_dashboard
        host_dashboard.disable()
        apps = sorted(_alongside_apps(c))
        removed = [a for a in apps if alongside.remove_shell_override(c.runtipi_root, STORE, a)]
        removed += [a for a in claude_login_off(c) if a not in removed]
        return {"mode": "fresh", "was_alongside": was, "shell_override_removed": bool(removed), "overrides_removed": removed,
                "next": " && ".join(f"wam app update {a}" for a in removed) or "nothing to update"}
    st = alongside.enable(c.home, home, shell_port, share_history, share_sessions)
    out = {k: v for k, v in st.items() if k != "detected"}
    out["detected"] = st["detected"]
    try:
        rt = c.runtipi()
        apps = _alongside_apps(c, rt)
        # Only the side that runs: after the split the bundle stays stopped (rollback material), and before it the
        # split apps aren't started. Naming the other side in `next` would start a second Agent Service.
        # Which side runs is decided by run status, not install status: after a rollback the Agent Service is
        # installed but stopped. (A box split before alongside mode was turned on has no saved flag yet.)
        active = alongside_side(rt, apps, bool((st.get("service") or {}).get("active")))
        if active != bool((st.get("service") or {}).get("active")):
            alongside.set_service_active(c.home, active)
        apps = {a: r for a, r in apps.items() if (r != "bundle") == active} or apps
        for app_id, role in apps.items():
            alongside.write_shell_override(c.runtipi_root, STORE, app_id, c.home, role)
            if role != "shell":   # it now runs as the owner: hand it the state the recipe's uid wrote
                if alongside.adopt_state(c.runtipi_root, STORE, app_id, _app_image(c, st, app_id)):
                    out.setdefault("state_adopted", []).append(app_id)
                if alongside.adopt_media(c.runtipi_root, _app_image(c, st, app_id), alongside.container_user(st["agent"])):
                    out.setdefault("media_adopted", []).append(app_id)
        order = sorted(apps, key=lambda a: apps[a] != "service") if apps else []   # the service first
        dash = host_dashboard_cmd(c, rt)
        if dash.get("state") != "skipped":
            out["host_dashboard"] = {k: v for k, v in dash.items() if k != "changed_apps"}
        order += [a for a in dash.get("changed_apps") or [] if a not in order]
        reg_apps = registry_apps(c)
        login = claude_login_cmd(c, {a: (reg_apps.get(a) or {}).get("provides") or [] for a in installed_ids(rt)})
        if login.get("apps"):
            out["claude_login"] = login
        order += [a for a in login.get("changed_apps") or [] if a not in order]
        if order:
            out["next"] = " && ".join(f"wam app update {a}" for a in order) + "  (applies the alongside settings)"
        write_registry(c, rt)
    except (RuntipiError, OSError) as e:
        out["note"] = f"App Manager not reachable yet ({str(e)[:120]}); settings are saved and apply on install"
    return out


def claude_login_cmd(c: Config, apps: dict[str, list]) -> dict:
    """Alongside: point each installed Claude connection app at the host Hermes' own Claude Code login folder
    (wam/claude_login.py), so its auto-renew and Connect/Test keep the agent signed in. Never copies a token.
    `apps` = app id -> provides. Returns {apps: {id: result}, changed_apps: [...]} (changed apps need an update)."""
    from . import claude_login
    al = alongside.load(c.home)
    if not al:
        return {"state": "skipped", "note": "not an alongside box (fresh boxes share the Claude volume)"}
    a = al.get("agent") or {}
    if not a.get("hermes_home") or a.get("uid") is None or a.get("gid") is None:
        return {"state": "skipped", "note": "alongside state names no host Hermes; run `wam alongside`"}
    hh = Path(a["hermes_home"])
    cfg = alongside._config(hh)
    out: dict = {"apps": {}, "changed_apps": []}
    rootless = None
    for app_id, provides in sorted(apps.items()):
        if not claude_login.is_consumer(app_id, provides):
            continue
        if rootless is None:
            rootless = alongside.docker_rootless()
        try:
            r = claude_login.ensure(c.runtipi_root, STORE, app_id, provides, hh, int(a["uid"]), int(a["gid"]),
                                    rootless, cfg)
        except (ValueError, OSError) as e:
            r = {"state": "error", "error": str(e)[:200], "changed": False}
        out["apps"][app_id] = {k: v for k, v in r.items() if k != "changed"}
        if r.get("changed"):
            out["changed_apps"].append(app_id)
    return out


def claude_login_off(c: Config) -> list[str]:
    """`wam alongside --off`: Claude apps go back to their own volume (only what claude_login wrote is removed)."""
    from . import claude_login
    base = c.runtipi_root / "user-config" / STORE
    return [d.name for d in sorted(base.iterdir()) if d.is_dir() and claude_login.remove(d / "docker-compose.yml")] \
        if base.is_dir() else []


def host_dashboard_consumers(c: Config, ids: set[str]) -> dict[str, list[str]]:
    from . import host_dashboard
    return host_dashboard.consumers(c.runtipi_root, STORE, ids)


def host_dashboard_cmd(c: Config, rt: Runtipi | None = None, apply: bool = True) -> dict:
    """Alongside: run the host Hermes dashboard for apps that sign in through it (e.g. ChatGPT) and point them at it."""
    from . import host_dashboard
    al = alongside.load(c.home)
    if not al:
        return {"state": "skipped", "note": "not an alongside box (fresh boxes use the Hermes Agent app's dashboard)"}
    rt = rt or c.runtipi()
    try:
        return host_dashboard.ensure(c.home, c.runtipi_root, STORE, installed_ids(rt), alongside.hermes_binary(),
                                     al["agent"]["hermes_home"], write_user_env, apply=apply)
    except OSError as e:
        return {"state": "error", "error": str(e)[:200]}


def store_data_cmd(c: Config, apply: bool = True) -> dict:
    """Install/refresh the App Store browse-data service for the installed shell and route it, with the owner-gated
    Store Get listener, on the shell's tailnet ports. Alongside boxes name their ports in alongside.json; fresh boxes
    (Runtipi apps on loopback ports) take them from apps.json."""
    from . import links, store_data
    al = alongside.load(c.home)
    reg = write_registry(c, c.runtipi())
    if al:
        shell_port = int((al.get("shell") or {}).get("port") or 0)
        svc_port = int((al.get("service") or {}).get("port") or 0)
    else:
        shell_port, svc_port = store_data.fresh_ports(reg)
        if not shell_port:
            return {"state": "skipped", "note": "no Wizard shell with a loopback port is installed"}
    out = store_data.ensure(reg, apply=apply, service=f"http://127.0.0.1:{svc_port}" if svc_port else None)
    out["mode"] = "alongside" if al else "fresh"
    if out.get("state") == "ready" and apply and shutil.which("tailscale") and shell_port:
        ts = links.tailnet()
        if ts["state"] != "Running" or not ts["host"]:
            out["tailnet"] = "Tailscale isn't connected yet; rerun after `sudo tailscale up`"
        else:
            out.update(store_data.publish_routes(ts["host"], shell_port))
    return out


def resolve_any(c: Config, ref: str) -> dict:
    """Explicit pinned catalog first, owner aliases next, then the public index/private repo fallback.

    An alias cannot silently replace an opted-in immutable catalog release. Its manifest identity is checked
    inside prepare before any image load/pull or store mutation.
    """
    pinned = release_pins.resolve(ref)
    if pinned:
        return private.prepare(c.runtipi_root, STORE, pinned['repo'], source_pin=pinned)
    from . import test_catalog
    pinned = test_catalog.resolve(ref)
    if pinned:
        return private.prepare(c.runtipi_root, STORE, pinned['repo'], release=pinned)
    if ref in c.private_apps:
        repo = c.private_apps[ref]
        try:
            return private.prepare(c.runtipi_root, STORE, repo, expected_id=ref)
        except private.PrivateError as pe:
            raise SystemExit(f"{ref}: private_apps alias to {repo} failed: {pe}") from None
    try:
        return index.resolve(c.index, ref)
    except index.IndexError_ as e:
        repo = ref if "/" in ref else slots.repo_for(ref)
        if not repo:
            raise
        try:
            rec = private.prepare(c.runtipi_root, STORE, repo.removeprefix("https://github.com/"))
        except private.PrivateError as pe:
            raise SystemExit(f"{ref}: not in the public index ({e}); private install failed: {pe}") from None
        print(f"{rec['id']}: installing from private repo {rec['repo']} (only boxes with access can do this)", file=sys.stderr)
        return rec


def uninstall(c: Config, app_id: str, force: bool = False) -> dict:
    rt = c.runtipi()
    if not force:
        empty = slots.blocks_uninstall(write_registry(c, rt), app_id)
        if empty:
            raise SystemExit(f"{app_id} is the only app in required spot(s) {', '.join(empty)}; install another app that "
                             f"provides it first (or pass --force)")
    urn = f"{app_id}:{STORE}"
    if app_id in installed_ids(rt):
        rt.uninstall(urn)
        rt.wait_status(urn, {"missing"}, timeout=300)
    reg = write_registry(c, rt)
    return {"id": app_id, "removed": True, "installed": [a["id"] for a in reg["apps"]]}


BOOTSTRAP_RETRY_DELAY = 10


def bootstrap(c: Config) -> dict:
    """Fill every empty parking spot with its default app. Spots that already hold any provider are left alone."""
    release_pins.load()
    rt = c.runtipi()
    reg = write_registry(c, rt)
    done, failed = [], {}
    for app_id in slots.to_install(reg):
        try:
            done.append(install(c, app_id)["id"])
        except (SystemExit, RuntimeError, RuntipiError, index.IndexError_) as e:
            failed[app_id] = str(e)[:200]
    # One retry for apps the App Manager removed after a failed `compose up` (seen live: a pull cancelled mid-way
    # on a fresh box); a second attempt on the same box then succeeded. Anything failing twice is reported.
    for app_id in [a for a in list(failed) if "install failed" in failed[a]]:
        time.sleep(BOOTSTRAP_RETRY_DELAY)
        try:
            print(f"retrying {app_id} once", file=sys.stderr)
            done.append(install(c, app_id)["id"])
            del failed[app_id]
        except (SystemExit, RuntimeError, RuntipiError, index.IndexError_) as e:
            failed[app_id] = str(e)[:200]
    updated = release_updates(c, rt, failed)
    reg = write_registry(c, rt)
    return {"installed": done, "updated": updated, "failed": failed, "spots": slots.table(reg)}


def release_updates(c: Config, rt: Runtipi, failed: dict) -> list[str]:
    """Move installed release defaults to the release's source commit on an upgrade rerun.

    A release-installed default is locked to its commit (`.wizard-selection`), so filling empty spots alone left
    e.g. Files on the 0.3.12 commit after an upgrade to 0.3.13+, and the read-back failed its source check.
    Only apps installed from the release's own repo are moved; anything else the owner chose stays.
    """
    doc = release_pins.load() or {}
    if not doc.get("default_apps"):
        return []
    have = installed_ids(rt)
    out = []
    for row in (doc.get("default_apps") or {}).values():
        if not row or not row.get("commit") or row.get("app") not in have:
            continue
        app_id = row["app"]
        live = c.runtipi_root / "apps" / STORE / app_id
        try:
            commit = (live / ".wizard-commit").read_text().strip()
            source = json.loads((live / ".wizard-source").read_text()).get("repo")
        except (OSError, ValueError):
            continue
        if commit == row["commit"] or source != row["repo"]:
            continue
        try:
            print(f"updating {app_id} to the release's source {row['commit'][:12]}", file=sys.stderr)
            resolve_any(c, app_id)   # prepares the pinned recipe (and its selection) in the store folder
            res = app_update(c, app_id)
            if res.get("error"):
                raise RuntimeError(res["error"])
            out.append(app_id)
        except (SystemExit, RuntimeError, RuntipiError, OSError, private.PrivateError) as e:
            failed[app_id] = f"update to release source failed: {e}"[:200]
    return out


def _app_container_states(urn: str) -> list[str]:
    """Docker states of the containers Runtipi labelled for one app (empty when they are all gone)."""
    from .agent_link import _docker
    p = subprocess.run(_docker() + ["ps", "-a", "--filter", f"label=runtipi.appurn={urn}", "--format", "{{.State}}"],
                       capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise RuntimeError(f"docker ps failed: {(p.stderr or p.stdout).strip()[:160]}")
    return p.stdout.split()


def recover(c: Config, running: list[str] | tuple[str, ...] = ()) -> dict:
    """wam#95: start installed apps whose containers are missing or not running while their records survive.
    Only the Wizard shell / Agent Service and the apps named in `running` (recorded running before this rerun) are
    started: an app the owner stopped stays stopped. Apps whose containers all run are left alone (healthy no-op)."""
    rt = c.runtipi()
    defaults = slots.load_defaults()
    core = [(defaults.get(s) or {}).get("app") for s in ("shell", "agent-service")]
    core = [a for a in core if a]
    installed = {}
    for row in rt.installed():
        urn = (row.get("info") or {}).get("urn") or ""
        if urn:
            installed[urn] = (row.get("app") or {}).get("status")
    want = [f"{a}:{STORE}" for a in core] + [u if ":" in u else f"{u}:{STORE}" for u in running]
    started, failed, skipped = [], {}, []
    for urn in dict.fromkeys(want):
        app_id = urn.split(":", 1)[0]
        if urn not in installed:
            skipped.append(app_id)
            continue
        try:
            states = _app_container_states(urn)
            if states and all(s == "running" for s in states):
                continue
            res = app_lifecycle(c, "start", urn)
            if res.get("error"):
                failed[app_id] = res["error"][:200]
            else:
                started.append(app_id)
        except (SystemExit, RuntimeError, RuntipiError, OSError, subprocess.SubprocessError) as e:
            failed[app_id] = str(e)[:200]
    if started:
        write_registry(c, rt)
    return {"started": started, "failed": failed, "not_installed": skipped, "core": core}


def doctor(c: Config, fix: bool = False) -> list[tuple[str, bool, str]]:
    out = []
    from . import builder_bundle
    if fix:
        try:
            builder_bundle.install(c.home)
            if not alongside.load(c.home) and os.environ.get('WIZARD_MODE') != 'alongside':
                builder_bundle.fresh(c)
        except (OSError, ValueError) as e:
            out.append(("Wizard builder repair", False, str(e)))
    al = alongside.load(c.home)
    hh = Path(al['agent']['hermes_home']) if al else c.runtipi_root / 'app-data' / STORE / 'hermes-agent/data'
    runtime = None if al else builder_bundle.RUNTIME + '/current/system-skills'
    out.extend(builder_bundle.health(c.home, hh, runtime))
    if not al:
        out.extend(builder_bundle.runtime_health(c))
    try:
        rt = c.runtipi()
        rt.installed()
        out.append(("App Manager API reachable", True, rt.base))
        s = {x.get("slug"): x for x in rt.stores()}.get(STORE)
        ok = store_ok(c, s)
        if not ok and fix:
            ensure_store(c, rt)
            s = {x.get("slug"): x for x in rt.stores()}.get(STORE)
            ok = store_ok(c, s)
        out.append(("Wizard store reads the index", ok, (s or {}).get("url", "missing (wam install creates it)")))
        try:
            hits = rt.req("GET", f"/marketplace/apps/search?pageSize=3&storeId={STORE}").get("data") or []
            out.append(("App Store lists index apps", bool(hits), f"{len(hits)} shown from a live index search"))
        except RuntipiError as e:
            out.append(("App Store lists index apps", False, str(e)[:160]))
    except Exception as e:  # noqa: BLE001
        out.append(("App Manager API reachable", False, str(e)[:200]))
    try:
        n = len(index.search(c.index, ""))
        out.append(("index reachable", True, f"{c.index} ({n} apps on the first page)"))
    except Exception as e:  # noqa: BLE001
        out.append(("index reachable", False, str(e)[:200]))
    out.append(("API is box-only", c.bind in ("127.0.0.1", "::1", "localhost"), f"{c.bind}:{c.port}"))
    out.append(("API token present (0600)", c.token_file.is_file() and (c.token_file.stat().st_mode & 0o077) == 0, str(c.token_file)))
    regp = c.home / "apps.json"
    out.append(("apps.json written", regp.is_file(), str(regp)))
    st = alongside.load(c.home)
    if st:
        a = st["agent"]
        out.append(("alongside: host Hermes API answers", alongside.api_up(a["origin"]), a["origin"]))
        out.append(("alongside: host Hermes has an API key", bool(alongside.api_key(Path(a["hermes_home"]))),
                    "API_SERVER_KEY in its .env (`wizard link` adds one if missing)"))
        apps = _alongside_apps(c)
        active = bool((st.get("service") or {}).get("active"))
        agent_side = next((x for x, r in apps.items() if r == ("service" if active else "bundle")),
                          "wizard-agent-service" if active else alongside.LEGACY_BUNDLE)
        ovr = c.runtipi_root / "user-config" / STORE / agent_side / "docker-compose.yml"
        label = "Agent Service" if apps.get(agent_side) == "service" else "shell"
        out.append((f"alongside: {label} points at the host Hermes", ovr.is_file() and a["origin"] in ovr.read_text(), str(ovr)))
        ux = next((x for x, r in apps.items() if r == "shell"), None)
        if ux and apps.get(agent_side) == "service":
            uo = c.runtipi_root / "user-config" / STORE / ux / "docker-compose.yml"
            want = f"http://127.0.0.1:{alongside.service_port(st)}"
            out.append(("alongside: shell points at the Agent Service", uo.is_file() and want in uo.read_text(), str(uo)))
        if st["shell"].get("share_history"):
            db = Path(a["hermes_home"]) / "state.db"
            readable = db.is_file() and os.access(db, os.R_OK) and os.stat(db).st_uid == os.getuid()
            out.append(("alongside: session history readable by the shell", readable,
                        f"{db} (shell runs as uid {a['uid']}, read-only; it can read every file in the Hermes home)"))
        elif st["shell"].get("share_sessions"):
            db = Path(a["hermes_home"]) / "state.db"
            text = ovr.read_text() if ovr.is_file() else ""
            mounted = 'WIZARD_HERMES_STATE_PATH: "/hermes/state.db"' in text
            stale = alongside.stale_session_mounts(text, a["hermes_home"]) if mounted else []
            out.append(("alongside: session database shared read-only (no credentials)", mounted and db.is_file() and not stale,
                        f"{db} -> /hermes/state.db" if mounted and not stale else
                        f"Hermes replaced {', '.join(stale)} since the mount: run `wam app update {agent_side}`" if stale else
                        f"not in the override yet: run `wam alongside` then `wam app update {agent_side}`"))
        else:
            out.append(("alongside: Hermes home not shared with the shell", True,
                        "chats over the Hermes API (opt in: wam alongside --share-sessions)"))
    if regp.is_file():
        for col in registry.collisions(json.loads(regp.read_text())):
            out.append(("slot collision", False, col))
    return out


# ---------------------------------------------------------------- HTTP API

class Handler(BaseHTTPRequestHandler):
    cfg: Config
    token: str

    def log_message(self, fmt, *args):  # noqa: D401
        sys.stderr.write("wam: " + (fmt % args) + "\n")

    def _json(self, code: int, obj) -> None:
        b = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _authorized(self) -> bool:
        got = self.headers.get("Authorization", "")
        if hmac.compare_digest(got.encode(), f"Bearer {self.token}".encode()):
            return True
        self._json(401, {"error": "send Authorization: Bearer <token from ~/.wizard-os/api-token>"})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802
        c, u = self.cfg, urlparse(self.path)
        if u.path == "/healthz":
            return self._json(200, {"ok": True})
        if u.path == "/api/system/load":
            # Exact read-only numeric projection; never proxy arbitrary manager paths or credentials.
            from . import resources
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self._json(403, {"error": "cross_site_refused"})
            try:
                return self._json(200, resources.read(c.runtipi(), getattr(c, "runtipi_root", None)))
            except Exception:
                return self._json(503, {"error": "server_resources_unavailable"})
        if u.path == "/api/apps":
            p = c.home / "apps.json"
            return self._json(200, json.loads(p.read_text()) if p.is_file() else {"apps": []})
        if u.path == "/api/search":
            q = parse_qs(u.query)
            try:
                return self._json(200, {"apps": index.search(c.index, (q.get("q") or [""])[0], category=(q.get("category") or [None])[0])})
            except index.IndexError_ as e:
                return self._json(502, {"error": str(e)})
        if u.path == "/api/slots":
            p = c.home / "apps.json"
            return self._json(200, {"spots": slots.table(json.loads(p.read_text()) if p.is_file() else {})})
        if u.path == "/api/updates":
            from . import updates
            return self._json(200, updates.status())
        if u.path == "/api/doctor":
            return self._json(200, [{"check": n, "ok": ok, "detail": d} for n, ok, d in doctor(c)])
        return self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return
        c, path = self.cfg, urlparse(self.path).path
        try:
            if path == "/api/install":
                b = self._body()
                return self._json(200, install(c, str(b.get("app", "")), {k: str(v) for k, v in (b.get("set") or {}).items()}))
            if path == "/api/bootstrap":
                return self._json(200, bootstrap(c))
            if path == "/api/registry":
                return self._json(200, write_registry(c))
            if path == "/api/updates":            # {"mode": "auto"|"ask"} or {"channel": "stable"|"latest"}
                from . import updates
                body = self._body()
                if set(body) == {"channel"}:
                    return self._json(200, updates.set_channel(str(body.get("channel", ""))))
                return self._json(200, updates.set_mode(str(body.get("mode", ""))))
            if path == "/api/updates/apply":      # the Update button; {"force": true} is the only way on unpinned
                from . import updates
                return self._json(200, updates.apply(force=self._body().get("force") is True))
        except (RuntipiError, SystemExit, RuntimeError, index.IndexError_) as e:
            return self._json(400, {"error": str(e)})
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):  # noqa: N802
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if path.startswith("/api/apps/"):
            try:
                return self._json(200, uninstall(self.cfg, path.split("/")[3]))
            except (RuntipiError, SystemExit, RuntimeError) as e:
                return self._json(400, {"error": str(e)})
        return self._json(404, {"error": "not found"})


class StoreHandler(BaseHTTPRequestHandler):
    """Store Get on loopback :7791, served only to tailscaled (peer socket owned by root). The path is the app id
    (Serve strips the /wizard-os-home/store-install mount). Authorised by the owner's Tailscale identity."""
    cfg: Config

    def log_message(self, fmt, *args):  # noqa: D401
        sys.stderr.write("wam store: " + (fmt % args) + "\n")

    _json = Handler._json

    def _go(self, method: str) -> None:
        from . import store_install as si
        if si.peer_uid(self.client_address[1]) != 0:
            return self._json(403, {"error": "only reachable through tailscale serve"})
        c = self.cfg
        lp = c.home / "links.json"
        ts = (json.loads(lp.read_text()) if lp.is_file() else {}).get("host")
        origins = si.served_origins(ts) if ts else set()
        owner = si.owner_login()
        if method == "POST":
            why = si.check_request(self.headers, owner, origins)
        else:
            why = None if owner and (self.headers.get("Tailscale-User-Login") or "").lower() == owner.lower() else "owner only"
        if why:
            return self._json(403, {"error": why})
        app_id = urlparse(self.path).path.strip("/")
        if app_id == UPDATES_PATH:   # Settings → Updates (app ids can't start with "_", so this never shadows one)
            return self._updates(method)
        if app_id.startswith('_views/'):
            return self._view_revert(method, app_id.removeprefix('_views/'))
        if app_id == '_slots/chats':
            return self._chats_default(method)
        if app_id == '_recommended':
            from . import recommended
            if method == 'GET':
                return self._json(200, recommended.listing(c))
            return self._json(202, recommended.start(c))
        if method == "POST":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    return self._json(400, {"error": "invalid selection body size"})
                selection = json.loads(self.rfile.read(size))
                from . import store_selection
                if not si.ID.fullmatch(app_id):
                    raise ValueError("bad app id")
                selection = store_selection.resolve(c, app_id, selection)
                code, body = si.start(c, app_id, lambda c_, a: store_selection.execute(c_, a, selection),
                                      lambda: installed_ids(c.runtipi()), selection=selection)
            except (ValueError, OSError, index.IndexError_) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 - answer with the reason; a dropped connection shows as a bare 502
                sys.stderr.write(f"wam store: {app_id} selection failed: {type(e).__name__}: {e}\n")
                return self._json(500, {"error": f"The App Manager couldn't prepare this install ({type(e).__name__}). "
                                                 "Update Wizard and try again."})
        else:
            code, body = si.status(app_id)
        return self._json(code, body)

    def _chats_default(self, method: str) -> None:
        from . import chats_views
        try:
            if method == 'POST':
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 256:
                    raise ValueError('invalid body size')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or set(body) != {'default'}:
                    raise ValueError('send {default: installed-provider-id}')
                chats_views.choose(self.cfg.home, body['default'])
                share_registry(self.cfg)
            reg = json.loads((self.cfg.home / 'apps.json').read_text())
            return self._json(200, (reg.get('slots') or {}).get('chats', {'default': chats_views.BUILTIN}))
        except (ValueError, OSError) as e:
            return self._json(400, {'error': str(e)[:200]})

    def _view_revert(self, method: str, app_id: str) -> None:
        """Shell-owned Use original action, behind exactly the Settings/Store owner gate."""
        from . import views
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,40}', app_id):
            return self._json(404, {'error': 'not found'})
        try:
            if method == 'POST':
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 128:
                    raise ValueError('invalid body size')
                if json.loads(self.rfile.read(size)) != {'action': 'revert'}:
                    raise ValueError('send action: revert')
                views.revert(app_id, self.cfg.home)
            view = views.load(app_id, self.cfg.home)
            if not view:
                return self._json(404, {'error': 'not customized'})
            return self._json(200, {'app': app_id, 'active': view['active']})
        except (ValueError, OSError) as e:
            return self._json(400, {'error': str(e)[:200]})

    def _updates(self, method: str) -> None:
        """GET: installed vs the channel's release and the owner's mode. POST {"mode": "auto"|"ask"} saves the choice;
        POST {"channel": "stable"|"latest"} saves the channel;
        POST {"action": "apply"} starts the update (the installer's detached rerun). Never forced from a browser:
        an unpinned box refuses."""
        from . import updates
        if method == "GET":
            return self._json(200, updates.status())
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024:
                return self._json(400, {"error": "invalid body size"})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("expected an object")
        except (ValueError, OSError) as e:
            return self._json(400, {"error": str(e)[:200]})
        if body.get("action") == "apply" and set(body) == {"action"}:
            res = updates.apply()
            return self._json(200 if res.get("started") or not res.get("error") else 409, res)
        if set(body) == {"mode"} and body["mode"] in updates.MODES:
            return self._json(200, {**updates.set_mode(body["mode"]), **updates.status()})
        if set(body) == {"channel"} and body["channel"] in updates.CHANNELS:
            return self._json(200, {**updates.set_channel(body["channel"]), **updates.status()})
        return self._json(400, {"error": 'send {"mode": "auto"|"ask"}, {"channel": "stable"|"latest"} or '
                                         '{"action": "apply"}'})

    def do_GET(self):  # noqa: N802
        self._go("GET")

    def do_POST(self):  # noqa: N802
        self._go("POST")


UPDATES_PATH = "_updates"


# ---------------------------------------------------------------- main

def _sets(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep:
            raise SystemExit(f"--set expects KEY=VALUE, got {p!r}")
        out[k] = v
    return out


# Everything runtipi-cli does, wam does too: these commands go straight to runtipi-cli, unchanged.
RUNTIPI_COMMANDS = {"app", "appstore", "installed", "start", "stop", "restart", "prepare", "debug",
                    "reset-password", "update", "version", "completion"}


def _short_ids(runtipi_root: Path, args: list[str]) -> list[str]:
    """`wam app update hermes-frontend-3` -> `... hermes-frontend-3:wizardapps` when exactly one store has it."""
    if not args or args[0] != "app":
        return args
    out = list(args)
    for i, tok in enumerate(out[2:], start=2):
        if tok.startswith("-") or ":" in tok:
            continue
        hits = sorted(p.parent.name for p in (runtipi_root / "apps").glob(f"*/{tok}") if p.is_dir())
        if len(hits) == 1:
            out[i] = f"{tok}:{hits[0]}"
    return out


def app_update(c: "Config", app_id: str, backup: bool = True, timeout: int = 1200) -> dict:
    """Update one app through the App Manager API (the queued, rolled-back path the web UI uses), not runtipi-cli,
    whose direct queue call can fail and leave the app stopped. A running app is running again afterwards."""
    rt = c.runtipi()
    urn = app_id if ":" in app_id else f"{app_id}:{STORE}"
    before = ((rt.app(urn) or {}).get("app") or {})
    if not before:
        raise SystemExit(f"{app_id} is not installed")
    was_running = before.get("status") == "running"
    app, store = urn.split(":", 1)
    if store == STORE and app in alongside.PIN_VARS and alongside.load(c.home):
        # Runtipi merges user-config after the recipe: a stale alongside image wins unless refreshed BEFORE update.
        role = {alongside.LEGACY_BUNDLE: "bundle", "wizard-ux": "shell", "wizard-agent-service": "service"}[app]
        alongside.write_shell_override(c.runtipi_root, store, app, c.home, role)
    rt.update(urn, perform_backup=backup)
    time.sleep(3)
    status = rt.wait_status(urn, {"running", "stopped", "update_error", "missing"}, timeout)
    after = ((rt.app(urn) or {}).get("app") or {})
    restarted = False
    if was_running and status != "running":
        rt.start(urn)
        status = rt.wait_status(urn, {"running"}, 300)
        restarted = True
    out = {"id": app_id, "urn": urn, "status": status, "version": after.get("version"),
           "from_version": before.get("version"), "restarted": restarted}
    if status != "running" and was_running:
        out["error"] = "the app did not come back; see `docker logs runtipi`"
    return out


def runtipi_cli(runtipi_root: Path, args: list[str]) -> int:
    exe = runtipi_root / "runtipi-cli"
    if not exe.is_file():
        print(f"error: {exe} not found (set RUNTIPI_ROOT)", file=sys.stderr)
        return 1
    return subprocess.call([str(exe), *_short_ids(runtipi_root, args)], cwd=runtipi_root)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Builder lives in its own module; lifecycle/global registry remains separate.
    if argv and argv[0] == "hermes":
        from .hermes_update import main as hermes_main
        return hermes_main(argv[1:])
    if argv and argv[0] == "agent-link":
        from .agent_link import main as agent_link_main
        return agent_link_main(argv[1:])
    if argv and argv[0] == "builder-bundle":
        from .builder_bundle import main as bundle_main
        return bundle_main(argv[1:])
    if argv and argv[0] in {"new", "check", "install-local", "update-local", "publish", "builder-skill", "package-inline"}:
        from .builder import main as builder_main
        return builder_main(argv)
    if argv and argv[0] == "views":
        from .views import main as views_main
        return views_main(argv[1:])
    if len(argv) >= 3 and argv[0] == "app" and argv[1] in LIFECYCLE_WANT and not argv[2].startswith("-"):
        res = app_lifecycle(Config(), argv[1], argv[2])
        print(json.dumps(res, indent=2))
        return 1 if res.get("error") else 0
    if argv[:2] == ["app", "update"] and len(argv) >= 3 and not argv[2].startswith("-"):
        res = app_update(Config(), argv[2], backup="--no-backup" not in argv)
        print(json.dumps(res, indent=2))
        return 0 if res["status"] == "running" or not res.get("error") else 1
    if argv and argv[0] in RUNTIPI_COMMANDS:
        return runtipi_cli(Config().runtipi_root, argv)
    ap = argparse.ArgumentParser(prog="wam", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query", nargs="?", default=""); s.add_argument("--json", action="store_true")
    i = sub.add_parser("install"); i.add_argument("app"); i.add_argument("--set", action="append", default=[]); i.add_argument("--no-wait", action="store_true")
    u = sub.add_parser("uninstall"); u.add_argument("id"); u.add_argument("--force", action="store_true")
    sl = sub.add_parser("slots"); sl.add_argument("--json", action="store_true")
    sub.add_parser("bootstrap")
    rc = sub.add_parser("recover", help="start the shell / Agent Service and recorded-running apps whose containers are missing")
    rc.add_argument("--running", action="append", default=[], help="app URN recorded running before this rerun")
    ss = sub.add_parser("shell-split", help="move this box from the wizard-shell bundle to the split Agent Service + shell")
    ss.add_argument("--rollback", action="store_true"); ss.add_argument("--force", action="store_true")
    a = sub.add_parser("apps"); a.add_argument("--json", action="store_true")
    d = sub.add_parser("doctor"); d.add_argument("--fix", action="store_true")
    sub.add_parser("serve")
    sub.add_parser("prefetch", help="cache recommended images only; never install or start apps")
    sd = sub.add_parser("store-data", help="set up the App Store browse data and Store Get routes for the installed shell")
    sd.add_argument("--dry-run", action="store_true")
    ln = sub.add_parser("links"); ln.add_argument("--json", action="store_true"); ln.add_argument("--dry-run", action="store_true")
    tr = sub.add_parser("troubleshooter", help="install/update the native Troubleshooter (/troubleshoot/) for this box")
    tr.add_argument("--json", action="store_true"); tr.add_argument("--dry-run", action="store_true")
    al = sub.add_parser("alongside"); al.add_argument("--detect", action="store_true"); al.add_argument("--off", action="store_true")
    al.add_argument("--hermes-home"); al.add_argument("--shell-port", type=int)
    al.add_argument("--share-history", dest="share_history", action="store_true", default=None)
    al.add_argument("--no-share-history", dest="share_history", action="store_false")
    al.add_argument("--share-sessions", dest="share_sessions", action="store_true", default=None)
    al.add_argument("--no-share-sessions", dest="share_sessions", action="store_false")
    up = sub.add_parser("updates", help="update check, the owner's choice (--ask default, --auto) and channel "
                                        "(--channel stable default, or latest)")
    upg = up.add_mutually_exclusive_group()
    upg.add_argument("--auto", action="store_true"); upg.add_argument("--ask", action="store_true")
    upg.add_argument("--apply", action="store_true"); upg.add_argument("--apply-if-auto", action="store_true")
    upg.add_argument("--ensure-timer", action="store_true", help="installer: make the nightly timer match the mode")
    upg.add_argument("--channel", choices=("stable", "latest"),
                     help="stable: tested releases (default); latest: every release as soon as it is cut")
    up.add_argument("--force", action="store_true", help="with --apply: also update an unpinned (hand-updated) box")
    args = ap.parse_args(argv)
    if args.cmd == "updates":
        from . import updates
        if args.force and not args.apply:
            print("--force only goes with --apply", file=sys.stderr)
            return 2
        res = (updates.set_channel(args.channel) if args.channel
               else updates.set_mode("auto") if args.auto else updates.set_mode("ask") if args.ask
               else updates.apply(force=args.force) if args.apply else updates.apply_if_auto() if args.apply_if_auto
               else updates.ensure_timer() if args.ensure_timer else updates.status(fresh=True))
        print(json.dumps(res, indent=2))
        return 1 if res.get("error") else 0
    c = Config()
    try:
        if args.cmd == "search":
            hits = index.search(c.index, args.query)
            print(json.dumps(hits, indent=2) if args.json else "\n".join(
                f"{h['id']:<20} {h['category']:<10} {h['repo']:<40} {h['summary']}" for h in hits) or "no matches")
        elif args.cmd == "install":
            print(json.dumps(install(c, args.app, _sets(args.set), wait=not args.no_wait), indent=2))
        elif args.cmd == "uninstall":
            print(json.dumps(uninstall(c, args.id, force=args.force), indent=2))
        elif args.cmd == "slots":
            rows = slots.table(write_registry(c))
            print(json.dumps(rows, indent=2) if args.json else "\n".join(
                f"{r['name']:<20} {(r['active'] or '-'):<20} {r['state']:<15} default {r['default_app'] or '(none yet)'}"
                + ("  required" if r["required"] else "") for r in rows))
        elif args.cmd == "bootstrap":
            print(json.dumps(bootstrap(c), indent=2))
        elif args.cmd == "prefetch":
            from . import recommended
            print(json.dumps(recommended.prefetch(c), indent=2))
        elif args.cmd == "recover":
            print(json.dumps(recover(c, args.running), indent=2))
        elif args.cmd == "shell-split":
            print(json.dumps(shell_split(c, rollback=args.rollback, force=args.force), indent=2))
        elif args.cmd == "apps":
            reg = write_registry(c)
            print(json.dumps(reg, indent=2) if args.json else "\n".join(
                f"{x['id']:<20} {x['status'] or '?':<9} {(x['mcp'] or {}).get('url', '')}" for x in reg["apps"]) or "nothing installed")
        elif args.cmd == "doctor":
            c.api_token()
            res = doctor(c, fix=args.fix)
            for n, ok, det in res:
                print(f"{'PASS' if ok else 'FAIL'}  {n}  {det}")
            return 0 if all(ok for _, ok, _ in res) else 1
        elif args.cmd == "store-data":
            res = store_data_cmd(c, apply=not args.dry_run)
            print(json.dumps(res, indent=2))
            return 0 if res.get("state") in ("ready", "skipped") else 1
        elif args.cmd == "alongside":
            res = alongside_cmd(c, args.detect, args.off, args.hermes_home, args.shell_port, args.share_history,
                                args.share_sessions)
            print(json.dumps(res, indent=2))
            if args.detect:
                return 0 if res["found"] else 3
        elif args.cmd == "troubleshooter":
            from . import troubleshooter
            res = troubleshooter.install(c.home, c.runtipi_root, apply=not args.dry_run)
            print(json.dumps(res, indent=2) if args.json else troubleshooter.describe(res))
            return 1 if res["state"] in ("failed", "degraded") else 0
        elif args.cmd == "links":
            from . import links
            if args.dry_run:
                rt = Runtipi.from_install(c.runtipi_root)  # no operator creation or registry writes on a dry run
                reg = registry.build(rt.installed(), STORE, c.host_ip(), c.runtipi_root)
            else:
                reg = write_registry(c)
            shell_ids = {a["id"] for a in reg["apps"] if is_shell(a, a["id"])}
            res = links.publish(reg, c.home, shell_ids, apply=not args.dry_run, api_port=c.port)
            if not args.dry_run:
                # The installer calls links last. Rebuild/share after publishing,
                # otherwise a fresh Agent Service still receives loopback URLs.
                write_registry(c)
            print(json.dumps(res, indent=2) if args.json else links.describe(res))
            return 0 if not res["errors"] else 1
        elif args.cmd == "serve":
            from . import recommended
            recommended.resume(c)
            Handler.cfg, Handler.token = c, c.api_token()
            StoreHandler.cfg = c
            try:
                import threading
                from . import store_install
                ssrv = ThreadingHTTPServer(("127.0.0.1", store_install.PORT), StoreHandler)
                threading.Thread(target=ssrv.serve_forever, daemon=True).start()
                print(f"store installs on 127.0.0.1:{store_install.PORT} (tailscaled only, owner only)", flush=True)
            except OSError as e:
                print(f"note: store install listener not started ({e})", file=sys.stderr)
            srv = ThreadingHTTPServer((c.bind, c.port), Handler)
            print(f"wizard-app-manager API on http://{c.bind}:{c.port} (index {c.index})", flush=True)
            srv.serve_forever()
    except (index.IndexError_, RuntipiError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
