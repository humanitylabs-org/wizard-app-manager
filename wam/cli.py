"""wizard-app-manager: stock Runtipi plus the Wizard App Index.

Every runtipi-cli command works as-is (passed straight through; app ids may drop the ":store" suffix):
  wam app start|stop|update|reset|uninstall|backup|restore|list-backups|delete-backup <app>
  wam app start-all|stop-all|available-updates      wam installed
  wam appstore add|list|remove|update               wam start|stop|restart|prepare|update|version|debug

Wizard additions:
  wam search [query]            search the index (apps.aiwizards.com)
  wam install <app>             <app> = id (transcriber), repo (owner/name) or a local folder with wizard.md
        [--set KEY=VALUE ...]   install-form values; required passwords are generated and saved if not given
  wam uninstall <id>            remove through Runtipi, rewrite apps.json
  wam apps [--json]             what is installed, with each app's MCP address (apps.json)
  wam sync                      mirror every index app into Runtipi's "Wizard Apps" store (the timer runs this)
  wam refresh                   re-register the local store with Runtipi and pull it
  wam doctor                    check the whole chain
  wam skin on|off               Wizard UX look for the stock Runtipi dashboard (a style layer; Runtipi is unchanged)
  wam serve                     HTTP API on 127.0.0.1:7790 (changes need the token in ~/.wizard-os/api-token)
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import platform
import secrets
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import index, registry, store, wizardmd
from .runtipi import Runtipi, RuntipiError, read_env


class Config:
    def __init__(self) -> None:
        self.home = registry.home()
        self.home.mkdir(parents=True, exist_ok=True)
        cfg_file = self.home / "app-manager.json"
        cfg = json.loads(cfg_file.read_text()) if cfg_file.is_file() else {}
        self.runtipi_root = Path(os.environ.get("RUNTIPI_ROOT", cfg.get("runtipi_root", Path.home() / "runtipi"))).expanduser()
        self.index = index.base(cfg)
        self.store_name = cfg.get("store_name", "wizardapps")   # Runtipi store slug, max 16 chars
        self.bind = os.environ.get("WAM_BIND", cfg.get("bind", "127.0.0.1"))
        self.port = int(os.environ.get("WAM_PORT", cfg.get("port", 7790)))
        self.out = self.home / "store"
        self.cache = self.home / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.secrets = self.home / "secrets"
        self.token_file = self.home / "api-token"

    def runtipi(self) -> Runtipi:
        rt = Runtipi.from_install(self.runtipi_root)
        if rt.ensure_operator(self.home / "runtipi-operator.json"):
            print(f"created Runtipi operator; sign-in saved to {self.home / 'runtipi-operator.json'}", file=sys.stderr)
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

def catalog(c: Config) -> dict:
    return store.read_catalog(c.out)


def sync_store(c: Config, metas: list[dict]) -> str:
    store.write_catalog(c.out, metas)
    head = store.commit_store(c.out)
    url = store.ensure_store_server(c.out)
    rt = c.runtipi()
    s = rt.ensure_store(c.store_name, url)
    c.store_name = s.get("slug") or c.store_name
    if s.get("slug") and s.get("name") != "Wizard Apps":
        try:
            rt.req("PATCH", f"/marketplace/{s['slug']}", {"name": "Wizard Apps", "enabled": True})
        except RuntipiError:
            pass
    rt.pull()
    return head


def sync_registry(c: Config) -> dict:
    rt = c.runtipi()
    reg = registry.build(catalog(c), rt.installed(), c.store_name, c.host_ip(), c.out / "skills")
    registry.write(reg, c.out / "skills", c.home)
    return reg


def port_free(port: int) -> bool:
    """True if nothing on this host listens on the port (any address)."""
    for fam, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if fam == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((addr, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def pick_port(wanted: int | None, lo: int = 20000, hi: int = 29999) -> int | None:
    """Keep the app's own port when it's free; otherwise the first free port in lo..hi."""
    if wanted and port_free(int(wanted)):
        return None
    for p in range(lo, hi + 1):
        if port_free(p):
            return p
    raise SystemExit("no free port in 20000-29999 for this app")


def build_form(c: Config, app_id: str, given: dict[str, str]) -> tuple[dict, list[str]]:
    """Install-form values. Defaults and `random` fields are left to Runtipi. Required passwords that the
    owner didn't give are generated here and saved 0600 under ~/.wizard-os/secrets/<id>.json (never printed)."""
    cfg = json.loads((c.out / "apps" / app_id / "config.json").read_text())
    form: dict = {"openPort": False, "exposed": False, "exposedLocal": False}   # loopback mapping is in the compose
    saved: dict = {}
    missing: list[str] = []
    for f in cfg.get("form_fields") or []:
        var = f.get("env_variable")
        if not var or f.get("type") == "random":
            continue
        if var in given:
            form[var] = given[var]
        elif "default" in f:
            continue
        elif f.get("required") and f.get("type") == "password":
            form[var] = saved[var] = secrets.token_urlsafe(max(24, int(f.get("min") or 16)))
        elif f.get("required"):
            missing.append(f"{var} ({f.get('label', '')}: {f.get('hint', '')})")
    alt = pick_port(cfg.get("port"))
    if alt:
        print(f"{app_id}: port {cfg.get('port')} is taken on this host; using {alt}", file=sys.stderr)
        form["port"] = alt
    if saved:
        c.secrets.mkdir(mode=0o700, exist_ok=True)
        p = c.secrets / f"{app_id}.json"
        p.write_text(json.dumps(saved, indent=2) + "\n")
        p.chmod(0o600)
        print(f"{app_id}: generated {', '.join(saved)}; saved to {p}", file=sys.stderr)
    return form, missing


def resolve(c: Config, ref: str) -> tuple[Path, str, str]:
    """-> (checked-out root, origin label, commit)."""
    p = Path(ref).expanduser()
    if p.is_dir() and (p / "wizard.md").is_file():
        return p.resolve(), str(p.resolve()), store.local_commit(p)
    rec = index.resolve(c.index, ref)
    root = store.fetch_commit(rec["install"]["git"], rec["install"]["commit"], c.cache)
    return root, rec["repo"], rec["install"]["commit"]


def installed_urns(rt: Runtipi) -> set[str]:
    return {((r.get("info") or {}).get("urn") or "") for r in rt.installed()}


# ---------------------------------------------------------------- commands

def install(c: Config, ref: str, given: dict[str, str] | None = None, wait: bool = True, _seen: set | None = None) -> dict:
    _seen = _seen if _seen is not None else set()
    root, origin, commit = resolve(c, ref)
    res = wizardmd.check(root)
    if not wizardmd.passed(res):
        raise SystemExit(f"{ref} fails the wizard.md check here: {', '.join(wizardmd.failures(res))}")
    m = store.meta(root, origin, commit)
    if m["id"] in _seen:
        return {"id": m["id"], "status": "already handled"}
    _seen.add(m["id"])
    if not store.matches_arch(m, platform.machine()):
        raise SystemExit(f"{m['id']} supports {m['architectures']}, this host is {platform.machine()}")
    rt = c.runtipi()
    for dep in m["uses"]:
        if f"{dep}:{c.store_name}" in installed_urns(rt) or dep in _seen:
            continue
        try:
            print(f"installing dependency {dep}", file=sys.stderr)
            install(c, dep, None, wait, _seen)
        except (index.IndexError_, SystemExit, RuntimeError) as e:
            print(f"note: dependency {dep} not installed ({e}); {m['id']} may need it", file=sys.stderr)
    store.add_app(c.out, root, m)
    metas = [a for a in catalog(c)["apps"] if a["id"] != m["id"]] + [m]
    sync_store(c, metas)
    urn = f"{m['id']}:{c.store_name}"
    if urn in installed_urns(rt):
        return {"id": m["id"], "urn": urn, "status": "already installed", "hint": f"wam update {m['id']}"}
    form, missing = build_form(c, m["id"], given or {})
    if missing:
        raise SystemExit(f"{m['id']} needs: " + "; ".join(missing) + f"\n  e.g. wam install {ref} --set KEY=VALUE")
    rt.install(urn, form)
    status = rt.wait_status(urn, {"running", "stopped"}) if wait else "installing"
    reg = sync_registry(c)
    entry = next((a for a in reg["apps"] if a["id"] == m["id"]), {})
    return {"id": m["id"], "urn": urn, "status": status, "commit": commit[:12], "from": origin,
            "open": (entry.get("view") or {}).get("upstream"), "mcp": (entry.get("mcp") or {}).get("url"),
            "installed": [a["id"] for a in reg["apps"]]}


def uninstall(c: Config, app_id: str) -> dict:
    rt = c.runtipi()
    urn = f"{app_id}:{c.store_name}"
    if urn in installed_urns(rt):
        rt.uninstall(urn)
        rt.wait_status(urn, {"missing"}, timeout=300)
    store.remove_app(c.out, app_id)
    sync_store(c, [a for a in catalog(c)["apps"] if a["id"] != app_id])
    reg = sync_registry(c)
    return {"id": app_id, "removed": True, "installed": [a["id"] for a in reg["apps"]]}


def update(c: Config, app_id: str | None) -> list[dict]:
    """Move installed apps to the commit the index lists now. Re-checked; Runtipi keeps app data."""
    rt = c.runtipi()
    out = []
    for a in catalog(c)["apps"]:
        if app_id and a["id"] != app_id:
            continue
        if "/" not in a["origin"] or Path(a["origin"]).exists():
            out.append({"id": a["id"], "status": "local app; reinstall from its folder"}); continue
        rec = index.resolve(c.index, a["origin"])
        if rec["install"]["commit"] == a["commit"]:
            out.append({"id": a["id"], "status": "up to date"}); continue
        root = store.fetch_commit(rec["install"]["git"], rec["install"]["commit"], c.cache)
        res = wizardmd.check(root)
        if not wizardmd.passed(res):
            out.append({"id": a["id"], "status": "new commit fails the check here", "failed": wizardmd.failures(res)}); continue
        m = store.meta(root, a["origin"], rec["install"]["commit"])
        store.add_app(c.out, root, m, bump_from=a["commit"])
        sync_store(c, [x for x in catalog(c)["apps"] if x["id"] != m["id"]] + [m])
        urn = f"{m['id']}:{c.store_name}"
        rt.req("PATCH", f"/app-lifecycle/{urn}/update", {"performBackup": True}, timeout=600)
        out.append({"id": m["id"], "status": rt.wait_status(urn, {"running", "stopped"}), "from": a["commit"][:12], "to": m["commit"][:12]})
    sync_registry(c)
    return out


def sync(c: Config) -> dict:
    """Mirror every listed index app (pinned commit, re-checked here) into the local store, so Runtipi's own
    App Store, install, update and uninstall buttons work for Wizard apps. Installed or local apps that left
    the index stay, so nothing installed loses its files."""
    have = {a["id"]: a for a in catalog(c)["apps"]}
    keep, added, updated, failed = {}, [], [], []
    for rec in index.all_listed(c.index):
        commit = rec["install"]["commit"]
        old = next((a for a in have.values() if a.get("origin") == rec["repo"]), None)
        try:
            root = store.fetch_commit(rec["install"]["git"], commit, c.cache)   # cached per commit
            res = wizardmd.check(root)
            if not wizardmd.passed(res):
                failed.append(f"{rec['repo']}: {', '.join(wizardmd.failures(res))}")
                if old:
                    keep[old["id"]] = old
                continue
            m = store.meta(root, rec["repo"], commit)
            if m["id"] in keep:
                failed.append(f"{rec['repo']}: id '{m['id']}' already taken by {keep[m['id']]['origin']}")
                continue
            store.add_app(c.out, root, m, bump_from=(old or {}).get("commit"))
            keep[m["id"]] = m
            if not old:
                added.append(m["id"])
            elif old.get("commit") != commit:
                updated.append(m["id"])
        except Exception as e:  # noqa: BLE001
            failed.append(f"{rec['repo']}: {str(e)[:200]}")
            if old:
                keep[old["id"]] = old
    installed = {((r.get("info") or {}).get("urn") or "").partition(":")[0] for r in c.runtipi().installed()}
    for a in have.values():
        if a["id"] not in keep and (a["id"] in installed or "/" not in a["origin"] or Path(a["origin"]).exists()):
            keep[a["id"]] = a
    for gone in set(have) - set(keep):
        store.remove_app(c.out, gone)
    head = sync_store(c, list(keep.values()))
    sync_registry(c)
    return {"apps": sorted(keep), "added": added, "updated": updated, "failed": failed, "store_head": head[:12]}


def doctor(c: Config) -> list[tuple[str, bool, str]]:
    out = []
    try:
        rt = c.runtipi()
        rt.installed()
        out.append(("Runtipi API reachable", True, rt.base))
        stores = {s.get("slug"): s for s in rt.stores()}
        s = stores.get(c.store_name)
        out.append(("Wizard store registered in Runtipi", bool(s), (s or {}).get("url", "missing (installs register it)")))
    except Exception as e:  # noqa: BLE001
        out.append(("Runtipi API reachable", False, str(e)[:200]))
    running = store._run(["docker", "inspect", "-f", "{{.State.Running}}", store.STORE_CONTAINER]).stdout.strip() == "true"
    out.append(("store server container running", running or not catalog(c)["apps"], store.STORE_CONTAINER if running else "starts on first install"))
    try:
        n = len(index.search(c.index, ""))
        out.append(("index reachable", True, f"{c.index} ({n} apps)"))
    except Exception as e:  # noqa: BLE001
        out.append(("index reachable", False, str(e)[:200]))
    out.append(("API is box-only", c.bind in ("127.0.0.1", "::1", "localhost"), f"{c.bind}:{c.port}"))
    out.append(("API token present (0600)", c.token_file.is_file() and (c.token_file.stat().st_mode & 0o077) == 0, str(c.token_file)))
    regp = c.home / "apps.json"
    out.append(("apps.json written", regp.is_file() or not catalog(c)["apps"], str(regp)))
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
        if u.path == "/api/apps":
            p = c.home / "apps.json"
            return self._json(200, json.loads(p.read_text()) if p.is_file() else {"apps": []})
        if u.path == "/api/catalog":
            return self._json(200, catalog(c))
        if u.path == "/api/search":
            q = parse_qs(u.query)
            try:
                return self._json(200, {"apps": index.search(c.index, (q.get("q") or [""])[0],
                                                             category=(q.get("category") or [None])[0])})
            except index.IndexError_ as e:
                return self._json(502, {"error": str(e)})
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
            if path.startswith("/api/apps/") and path.endswith("/install"):
                return self._json(200, install(c, path.split("/")[3]))
            if path == "/api/update":
                return self._json(200, update(c, self._body().get("app")))
            if path == "/api/sync":
                return self._json(200, sync(c))
            if path == "/api/refresh":
                return self._json(200, {"store_head": sync_store(c, catalog(c)["apps"])})
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


# ---------------------------------------------------------------- main

def _sets(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep:
            raise SystemExit(f"--set expects KEY=VALUE, got {p!r}")
        out[k] = v
    return out


# Everything runtipi-cli does, wam does too: these commands go straight to the stock runtipi-cli, unchanged.
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


def runtipi_cli(runtipi_root: Path, args: list[str]) -> int:
    exe = runtipi_root / "runtipi-cli"
    if not exe.is_file():
        print(f"error: {exe} not found (set RUNTIPI_ROOT)", file=sys.stderr)
        return 1
    import subprocess
    return subprocess.call([str(exe), *_short_ids(runtipi_root, args)], cwd=runtipi_root)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in RUNTIPI_COMMANDS:
        return runtipi_cli(Config().runtipi_root, argv)
    ap = argparse.ArgumentParser(prog="wam", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query", nargs="?", default=""); s.add_argument("--json", action="store_true")
    i = sub.add_parser("install"); i.add_argument("app"); i.add_argument("--set", action="append", default=[]); i.add_argument("--no-wait", action="store_true")
    u = sub.add_parser("uninstall"); u.add_argument("id")
    a = sub.add_parser("apps"); a.add_argument("--json", action="store_true")
    sub.add_parser("refresh"); sub.add_parser("doctor"); sub.add_parser("serve"); sub.add_parser("sync")
    sk = sub.add_parser("skin"); sk.add_argument("state", choices=["on", "off"])
    args = ap.parse_args(argv)
    c = Config()
    try:
        if args.cmd == "search":
            hits = index.search(c.index, args.query)
            print(json.dumps(hits, indent=2) if args.json else "\n".join(
                f"{h['id']:<20} {h['category']:<10} {h['repo']:<40} {h['summary']}" for h in hits) or "no matches")
        elif args.cmd == "install":
            print(json.dumps(install(c, args.app, _sets(args.set), wait=not args.no_wait), indent=2))
        elif args.cmd == "uninstall":
            print(json.dumps(uninstall(c, args.id), indent=2))
        elif args.cmd == "apps":
            p = c.home / "apps.json"
            reg = json.loads(p.read_text()) if p.is_file() else {"apps": []}
            print(json.dumps(reg, indent=2) if args.json else "\n".join(
                f"{x['id']:<20} {x['status'] or '?':<9} {(x['mcp'] or {}).get('url', '')}" for x in reg["apps"]) or "nothing installed")
        elif args.cmd == "sync":
            print(json.dumps(sync(c), indent=2))
        elif args.cmd == "refresh":
            print(f"store {c.out} @ {sync_store(c, catalog(c)['apps'])[:12]} -> Runtipi store '{c.store_name}' pulled")
        elif args.cmd == "doctor":
            c.api_token()
            res = doctor(c)
            for n, ok, d in res:
                print(f"{'PASS' if ok else 'FAIL'}  {n}  {d}")
            return 0 if all(ok for _, ok, _ in res) else 1
        elif args.cmd == "skin":
            if args.state == "on":
                print(f"Wizard UX skin on (route {store.skin_on(c.runtipi_root, c.home)}); reload the Runtipi dashboard")
            else:
                store.skin_off(c.runtipi_root)
                print("skin off; the Runtipi dashboard is stock again")
        elif args.cmd == "serve":
            Handler.cfg, Handler.token = c, c.api_token()
            srv = ThreadingHTTPServer((c.bind, c.port), Handler)
            print(f"wizard-app-manager API on http://{c.bind}:{c.port} (index {c.index})", flush=True)
            srv.serve_forever()
    except (index.IndexError_, RuntipiError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
