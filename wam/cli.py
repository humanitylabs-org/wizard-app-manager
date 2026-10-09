"""wizard-app-manager CLI and HTTP API.

  wam refresh                 find apps, check them, compile the store, tell Runtipi to pull it
  wam catalog [--json]        what can be installed
  wam install <id>            install through Runtipi, then rewrite apps.json
  wam uninstall <id>          remove (data kept by Runtipi's backups), then rewrite apps.json
  wam apps [--json]           what is installed (apps.json)
  wam bootstrap               refresh + install every system app
  wam doctor                  check the whole chain
  wam serve                   HTTP API + the compiled store over git smart-HTTP for Runtipi
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import registry, store
from .runtipi import Runtipi, RuntipiError


class Config:
    def __init__(self) -> None:
        self.home = registry.home()
        cfg_file = self.home / "app-manager.json"
        cfg = json.loads(cfg_file.read_text()) if cfg_file.is_file() else {}
        self.runtipi_root = Path(os.environ.get("RUNTIPI_ROOT", cfg.get("runtipi_root", Path.home() / "runtipi"))).expanduser()
        self.sources = cfg.get("sources") or [{"topic": t} for t in store.DEFAULT_TOPICS]
        self.store_name = cfg.get("store_name", "wizard")          # Runtipi store slug, max 16 chars
        self.bind = os.environ.get("WAM_BIND", cfg.get("bind", "0.0.0.0"))
        self.port = int(os.environ.get("WAM_PORT", cfg.get("port", 8790)))
        self.out = self.home / "store"
        self.cache = self.home / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)

    def runtipi(self) -> Runtipi:
        rt = Runtipi.from_install(self.runtipi_root)
        if rt.ensure_operator(self.home / "runtipi-operator.json"):
            print(f"created Runtipi operator; sign-in saved to {self.home / 'runtipi-operator.json'}", file=sys.stderr)
        return rt

    def host_ip(self) -> str:
        try:
            from .runtipi import read_env
            return read_env(self.runtipi_root / ".env").get("INTERNAL_IP") or "127.0.0.1"
        except Exception:  # noqa: BLE001
            return "127.0.0.1"

    def store_url(self) -> str:
        return f"http://{self.host_ip()}:{self.port}/store.git"


def catalog(c: Config) -> dict:
    p = c.out / "catalog.json"
    return json.loads(p.read_text()) if p.is_file() else {"apps": [], "skipped": []}


def refresh(c: Config, pull: bool = True) -> dict:
    cands, errors = store.discover(c.sources, c.cache, os.environ.get("GITHUB_TOKEN"))
    cat = store.compile_store(cands, c.out)
    head = store.commit_store(c.out)
    cat["errors"] = errors
    cat["store_head"] = head
    if pull:
        rt = c.runtipi()
        rt.ensure_store(c.store_name, c.store_url())
        rt.pull()
    return cat


def sync_registry(c: Config) -> dict:
    rt = c.runtipi()
    reg = registry.build(catalog(c), rt.installed(), c.store_name, c.host_ip(), c.out / "skills")
    registry.write(reg, c.out / "skills", c.home)
    return reg


def form_defaults(app_id: str, c: Config) -> dict:
    cfg = json.loads((c.out / "apps" / app_id / "config.json").read_text())
    form = {}
    for f in cfg.get("form_fields") or []:
        if "default" in f and f.get("env_variable"):
            form[f["env_variable"]] = f["default"]
    return form


def install(c: Config, app_id: str, wait: bool = True) -> dict:
    cat = {a["id"]: a for a in catalog(c)["apps"]}
    if app_id not in cat:
        raise SystemExit(f"{app_id} is not in the compiled store; run `wam refresh` (skipped apps: `wam catalog --json`)")
    if not store.matches_arch(cat[app_id], platform.machine()):
        raise SystemExit(f"{app_id} supports {cat[app_id]['architectures']}, this host is {platform.machine()}")
    rt = c.runtipi()
    urn = f"{app_id}:{c.store_name}"
    for dep in cat[app_id].get("uses") or []:
        if dep in cat and not any(((r.get('info') or {}).get('urn') or '') == f"{dep}:{c.store_name}" for r in rt.installed()):
            print(f"installing dependency {dep}", file=sys.stderr)
            install(c, dep, wait)
    rt.install(urn, form_defaults(app_id, c))
    status = rt.wait_status(urn, {"running", "stopped"}) if wait else "installing"
    reg = sync_registry(c)
    return {"id": app_id, "urn": urn, "status": status, "apps_json": str(c.home / "apps.json"), "installed": [a["id"] for a in reg["apps"]]}


def uninstall(c: Config, app_id: str) -> dict:
    rt = c.runtipi()
    urn = f"{app_id}:{c.store_name}"
    rt.uninstall(urn)
    rt.wait_status(urn, {"missing"}, timeout=300)
    reg = sync_registry(c)
    return {"id": app_id, "removed": True, "installed": [a["id"] for a in reg["apps"]]}


def doctor(c: Config) -> list[tuple[str, bool, str]]:
    out = []
    try:
        rt = c.runtipi()
        rt.installed()
        out.append(("Runtipi API reachable with CLI token", True, rt.base))
        stores = {s.get("slug"): s for s in rt.stores()}
        s = stores.get(c.store_name)
        out.append(("Wizard store registered in Runtipi", bool(s), (s or {}).get("url", "missing")))
    except Exception as e:  # noqa: BLE001
        out.append(("Runtipi API reachable with CLI token", False, str(e)[:200]))
    cat = catalog(c)
    out.append(("store compiled", (c.out / ".git").is_dir(), f"{len(cat['apps'])} apps, {len(cat['skipped'])} skipped"))
    regp = c.home / "apps.json"
    out.append(("apps.json written", regp.is_file(), str(regp)))
    if regp.is_file():
        reg = json.loads(regp.read_text())
        for col in registry.collisions(reg):
            out.append(("slot collision", False, col))
    return out


class Handler(BaseHTTPRequestHandler):
    cfg: Config

    def log_message(self, fmt, *args):  # noqa: D401
        sys.stderr.write("wam: " + (fmt % args) + "\n")

    def _json(self, code: int, obj) -> None:
        b = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _git(self) -> None:
        """Serve the compiled store with git's own smart-HTTP backend (isomorphic-git needs smart HTTP)."""
        path, _, query = self.path.partition("?")
        body = b""
        if self.command == "POST":
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        env = dict(os.environ, GIT_PROJECT_ROOT=str(self.cfg.out.parent), GIT_HTTP_EXPORT_ALL="1",
                   PATH_INFO=path.replace("/store.git", "/store", 1), REQUEST_METHOD=self.command, QUERY_STRING=query,
                   CONTENT_TYPE=self.headers.get("Content-Type", ""), CONTENT_LENGTH=str(len(body)),
                   REMOTE_ADDR=self.client_address[0],
                   GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0=str(self.cfg.out))
        r = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True, timeout=120)
        if r.returncode or b"Status: 5" in r.stdout[:200]:
            print(f"git http-backend: {r.stderr.decode(errors='replace')[:500]}", file=sys.stderr)
        head, _, payload = r.stdout.partition(b"\r\n\r\n")
        if not _:
            head, _, payload = r.stdout.partition(b"\n\n")
        code, headers = 200, []
        for line in head.decode(errors="replace").splitlines():
            k, _, v = line.partition(":")
            if k.lower() == "status":
                code = int(v.strip().split()[0])
            elif k:
                headers.append((k, v.strip()))
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        c = self.cfg
        if self.path.startswith("/store.git/"):
            return self._git()
        if self.path == "/healthz":
            return self._json(200, {"ok": True})
        if self.path == "/api/catalog":
            return self._json(200, catalog(c))
        if self.path == "/api/apps":
            p = c.home / "apps.json"
            return self._json(200, json.loads(p.read_text()) if p.is_file() else {"apps": []})
        if self.path == "/api/doctor":
            return self._json(200, [{"check": n, "ok": ok, "detail": d} for n, ok, d in doctor(c)])
        return self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        c = self.cfg
        if self.path.startswith("/store.git/"):
            return self._git()
        try:
            if self.path == "/api/refresh":
                return self._json(200, refresh(c))
            if self.path.startswith("/api/apps/") and self.path.endswith("/install"):
                return self._json(200, install(c, self.path.split("/")[3]))
        except (RuntipiError, SystemExit) as e:
            return self._json(400, {"error": str(e)})
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):  # noqa: N802
        if self.path.startswith("/api/apps/"):
            try:
                return self._json(200, uninstall(self.cfg, self.path.split("/")[3]))
            except (RuntipiError, SystemExit) as e:
                return self._json(400, {"error": str(e)})
        return self._json(404, {"error": "not found"})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wam", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh"); r.add_argument("--no-pull", action="store_true")
    cg = sub.add_parser("catalog"); cg.add_argument("--json", action="store_true")
    i = sub.add_parser("install"); i.add_argument("id"); i.add_argument("--no-wait", action="store_true")
    u = sub.add_parser("uninstall"); u.add_argument("id")
    a = sub.add_parser("apps"); a.add_argument("--json", action="store_true")
    sub.add_parser("bootstrap"); sub.add_parser("doctor"); sub.add_parser("serve")
    args = ap.parse_args(argv)
    c = Config()

    if args.cmd == "refresh":
        cat = refresh(c, pull=not args.no_pull)
        for app in cat["apps"]:
            print(f"OK    {app['id']:<16} {app['version']:<14} {app['type']:<8} {app['origin']}")
        for s in cat["skipped"]:
            print(f"SKIP  {s['origin']}  ({', '.join(s['failed'])})")
        for e in cat["errors"]:
            print(f"ERR   {e}")
        print(f"store {c.out} @ {cat['store_head'][:10]}" + ("" if args.no_pull else f" -> Runtipi store '{c.store_name}' pulled"))
    elif args.cmd == "catalog":
        cat = catalog(c)
        print(json.dumps(cat, indent=2) if args.json else "\n".join(f"{a['id']:<16} {a['type']:<8} {'system ' if a['system'] else ''}{a['summary']}" for a in cat["apps"]))
    elif args.cmd == "install":
        print(json.dumps(install(c, args.id, wait=not args.no_wait), indent=2))
    elif args.cmd == "uninstall":
        print(json.dumps(uninstall(c, args.id), indent=2))
    elif args.cmd == "apps":
        p = c.home / "apps.json"
        reg = json.loads(p.read_text()) if p.is_file() else {"apps": []}
        print(json.dumps(reg, indent=2) if args.json else "\n".join(
            f"{x['id']:<16} {x['status'] or '?':<9} {'(system) ' if x['system'] else ''}{(x['mcp'] or {}).get('url', '')}" for x in reg["apps"]))
    elif args.cmd == "bootstrap":
        refresh(c)
        for app in catalog(c)["apps"]:
            if app["system"]:
                print(json.dumps(install(c, app["id"]), indent=2))
    elif args.cmd == "doctor":
        res = doctor(c)
        for n, ok, d in res:
            print(f"{'PASS' if ok else 'FAIL'}  {n}  {d}")
        return 0 if all(ok for _, ok, _ in res) else 1
    elif args.cmd == "serve":
        Handler.cfg = c
        srv = ThreadingHTTPServer((c.bind, c.port), Handler)
        print(f"wizard-app-manager on http://{c.bind}:{c.port}  store {c.store_url()}", flush=True)
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
