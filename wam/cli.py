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
  wam doctor [--fix]            check the whole chain
  wam serve                     HTTP API on 127.0.0.1:7790 (changes need the token in ~/.wizard-os/api-token)
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import index, registry
from .runtipi import Runtipi, RuntipiError, read_env

STORE = "wizardapps"
STORE_NAME = "Wizard Apps"


class Config:
    def __init__(self) -> None:
        self.home = registry.home()
        self.home.mkdir(parents=True, exist_ok=True)
        cfg_file = self.home / "app-manager.json"
        cfg = json.loads(cfg_file.read_text()) if cfg_file.is_file() else {}
        self.runtipi_root = Path(os.environ.get("RUNTIPI_ROOT", cfg.get("runtipi_root", Path.home() / "runtipi"))).expanduser()
        self.index = index.base(cfg)
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
    rt.req("POST", "/marketplace/create", {"name": STORE_NAME, "url": c.index})


def write_registry(c: Config, rt: Runtipi | None = None) -> dict:
    rt = rt or c.runtipi()
    reg = registry.build(rt.installed(), STORE, c.host_ip(), c.runtipi_root)
    registry.write(reg, c.home)
    return reg


def installed_ids(rt: Runtipi) -> set[str]:
    out = set()
    for r in rt.installed():
        name, _, st = (((r.get("info") or {}).get("urn")) or "").partition(":")
        if st == STORE:
            out.add(name)
    return out


def build_form(c: Config, app_id: str, info: dict, given: dict[str, str]) -> tuple[dict, list[str]]:
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

def install(c: Config, app_id: str, given: dict[str, str] | None = None, wait: bool = True, _seen: set | None = None) -> dict:
    _seen = _seen if _seen is not None else set()
    if app_id in _seen:
        return {"id": app_id, "status": "already handled"}
    _seen.add(app_id)
    rec = index.resolve(c.index, app_id)
    app_id = rec["id"]
    rt = c.runtipi()
    ensure_store(c, rt)
    have = installed_ids(rt)
    for dep in rec.get("uses") or []:
        if dep in have or dep in _seen:
            continue
        try:
            print(f"installing dependency {dep}", file=sys.stderr)
            install(c, dep, None, wait, _seen)
        except (index.IndexError_, SystemExit, RuntimeError, RuntipiError) as e:
            print(f"note: dependency {dep} not installed ({e}); {app_id} may need it", file=sys.stderr)
    urn = f"{app_id}:{STORE}"
    if app_id in installed_ids(rt):
        return {"id": app_id, "urn": urn, "status": "already installed", "hint": f"wam app update {app_id}"}
    info = (rt.app(urn) or {}).get("info") or {}   # the App Manager fetches and checks this one app now
    if not info:
        raise SystemExit(f"{app_id}: the App Manager could not fetch it from the index (see its logs)")
    form, missing = build_form(c, app_id, info, given or {})
    if missing:
        raise SystemExit(f"{app_id} needs: " + "; ".join(missing) + f"\n  e.g. wam install {app_id} --set KEY=VALUE")
    rt.install(urn, form)
    status = rt.wait_status(urn, {"running", "stopped"}) if wait else "installing"
    reg = write_registry(c, rt)
    entry = next((a for a in reg["apps"] if a["id"] == app_id), {})
    return {"id": app_id, "urn": urn, "status": status, "commit": (entry.get("commit") or "")[:12], "from": rec["repo"],
            "open": (entry.get("view") or {}).get("url"), "mcp": (entry.get("mcp") or {}).get("url"),
            "installed": [a["id"] for a in reg["apps"]]}


def uninstall(c: Config, app_id: str) -> dict:
    rt = c.runtipi()
    urn = f"{app_id}:{STORE}"
    if app_id in installed_ids(rt):
        rt.uninstall(urn)
        rt.wait_status(urn, {"missing"}, timeout=300)
    reg = write_registry(c, rt)
    return {"id": app_id, "removed": True, "installed": [a["id"] for a in reg["apps"]]}


def doctor(c: Config, fix: bool = False) -> list[tuple[str, bool, str]]:
    out = []
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
        if u.path == "/api/search":
            q = parse_qs(u.query)
            try:
                return self._json(200, {"apps": index.search(c.index, (q.get("q") or [""])[0], category=(q.get("category") or [None])[0])})
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
            if path == "/api/registry":
                return self._json(200, write_registry(c))
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


def runtipi_cli(runtipi_root: Path, args: list[str]) -> int:
    exe = runtipi_root / "runtipi-cli"
    if not exe.is_file():
        print(f"error: {exe} not found (set RUNTIPI_ROOT)", file=sys.stderr)
        return 1
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
    d = sub.add_parser("doctor"); d.add_argument("--fix", action="store_true")
    sub.add_parser("serve")
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
            reg = write_registry(c)
            print(json.dumps(reg, indent=2) if args.json else "\n".join(
                f"{x['id']:<20} {x['status'] or '?':<9} {(x['mcp'] or {}).get('url', '')}" for x in reg["apps"]) or "nothing installed")
        elif args.cmd == "doctor":
            c.api_token()
            res = doctor(c, fix=args.fix)
            for n, ok, det in res:
                print(f"{'PASS' if ok else 'FAIL'}  {n}  {det}")
            return 0 if all(ok for _, ok, _ in res) else 1
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
