"""Host-only authenticated lifecycle bridge. Run: python3 -m wam.lifecycle serve.

The service proxy holds the dedicated token; browsers never receive it. The stock
Runtipi CLI JWT (sub=cli) stays entirely inside this host process.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import stat
import tempfile
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import alongside, cli, registry, slots
from .runtipi import Runtipi, RuntipiError, cli_token, read_env

ACTIONS = {"install": "POST", "start": "POST", "stop": "POST", "restart": "POST",
           "uninstall": "DELETE", "update": "PATCH"}
TERMINAL = {"install": {"running", "stopped"}, "start": {"running"}, "stop": {"stopped"},
            "restart": {"running"}, "update": {"running", "stopped"}, "uninstall": {"missing"}}
KEY = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")


class BridgeError(RuntimeError):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UpstreamFailure(RuntimeError):
    def __init__(self, *, rejected=False):
        super().__init__("upstream_rejected" if rejected else "upstream_unavailable")
        self.rejected = rejected


class BridgeRuntipi(Runtipi):
    """Use stock Runtipi CLI JWT/client helpers, but refuse redirects and retain safe rejection classification."""
    def req(self, method: str, path: str, body: dict | None = None, timeout: int = 60):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + cli_token(self.secret)}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            raise UpstreamFailure(rejected=400 <= e.code < 500 and e.code not in {408, 429}) from None
        except urllib.error.URLError as e:
            raise UpstreamFailure(rejected=isinstance(e.reason, ConnectionRefusedError)) from None
        return json.loads(raw) if raw else {}


def token_file(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and not path.is_symlink():
        fd, temporary = tempfile.mkstemp(prefix=".lifecycle-token.", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)  # Publish complete bytes without replacing a concurrent winner.
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:
        raise ValueError("lifecycle token must be a regular owner-only file (0600)")
    value = path.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", value):
        raise ValueError("lifecycle token must contain 32-256 header-safe characters")
    return value


def urn_value(value: str) -> str:
    name, sep, store = value.partition(":")
    if not registry.ID.fullmatch(name) or (sep and store != cli.STORE):
        raise BridgeError(400, "invalid_app")
    return f"{name}:{cli.STORE}"


class Completion:
    """Subscribe before submission so fast completion/rollback cannot be missed."""
    def __init__(self, rt: Runtipi, urn: str, action: str, timeout: float):
        self.urn, self.action, self.timeout = urn, action, timeout
        req = urllib.request.Request(rt.base + "/sse/app", headers={
            "Authorization": "Bearer " + cli_token(rt.secret), "Accept": "text/event-stream"})
        self.stream = urllib.request.build_opener(NoRedirect).open(req, timeout=timeout)
        if "text/event-stream" not in self.stream.headers.get("Content-Type", ""):
            self.stream.close()
            raise BridgeError(502, "completion_stream_unavailable")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stream.close()

    def wait(self) -> bool:
        deadline = time.monotonic() + self.timeout
        data = []
        size = 0
        while time.monotonic() < deadline:
            try:
                line = self.stream.readline(65537)
            except TimeoutError:
                raise BridgeError(504, "completion_unconfirmed") from None
            size += len(line)
            if not line or len(line) > 65536 or size > 1048576:
                raise BridgeError(504, "completion_unconfirmed")
            if line.strip() == b"":
                if data:
                    try:
                        event = json.loads(b"\n".join(data))
                    except ValueError:
                        raise BridgeError(502, "invalid_completion_event") from None
                    if not isinstance(event, dict):
                        raise BridgeError(502, "invalid_completion_event")
                    if event.get("appUrn") == self.urn:
                        if event.get("event") == self.action + "_success":
                            return True
                        if event.get("event") == self.action + "_error":
                            return False
                data, size = [], 0
            elif line.startswith(b"data:"):
                data.append(line[5:].strip())
        raise BridgeError(504, "completion_unconfirmed")


class Bridge:
    def __init__(self, cfg, token: str, *, rt=None, completion=Completion, timeout: float=900, poll=0.5):
        self.cfg, self.token = cfg, token
        self.rt = rt if rt is not None else BridgeRuntipi.from_install(cfg.runtipi_root)
        # Use .env only for the supported JWT_SECRET and port, never its LAN host.
        if rt is None:
            port = int(read_env(cfg.runtipi_root / ".env").get("NGINX_PORT") or 80)
            if not 1 <= port <= 65535:
                raise ValueError("invalid Runtipi port")
            self.rt.base = f"http://127.0.0.1:{port}/api"
        self.completion, self.timeout, self.poll = completion, timeout, poll
        config = registry._json_file(self.cfg.home / "app-manager.json")
        self.service_app_host = registry.service_app_host(config.get("service_app_host", "127.0.0.1"))
        self.lock = threading.RLock()
        self.cfg.home.mkdir(parents=True, exist_ok=True)
        self.operations = self.cfg.home / "lifecycle-operations.json"

    @contextlib.contextmanager
    def serialized(self, *, blocking=True):
        if not self.lock.acquire(blocking=blocking):
            raise BridgeError(409, "bridge_busy")
        try:
            with (self.cfg.home / "lifecycle.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError:
                    raise BridgeError(409, "bridge_busy") from None
                try:
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            self.lock.release()

    def clean(self, value: Any) -> Any:
        return self._redact(registry.sanitize(value))

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        if isinstance(value, str):
            for secret in (self.token, self.rt.secret):
                if secret and secret in value:
                    value = value.replace(secret, "[redacted]")
        return value

    def refresh(self) -> tuple[dict, list]:
        rows = self.rt.installed()
        reg = registry.build(rows, cli.STORE, self.service_app_host, self.cfg.runtipi_root)
        ext = alongside.external_slots(self.cfg.home)
        if ext:
            reg["external"] = ext
        slots.merge_into_registry(reg)
        # Sanitize known server secrets before either on-disk public projection is written.
        private = [{k: v for k, v in a.items() if k.startswith("_")} for a in reg["apps"]]
        reg = self.clean(reg)
        for app, internal in zip(reg["apps"], private):
            app.update(internal)
        registry.write(reg, self.cfg.home)
        # The shared copy goes through the same atomic ownership-aware writer, not cli.share_registry's rmtree.
        shared = self.cfg.runtipi_root / "app-data" / "wizard-os"
        registry.write(reg, shared, preserve_choices=False)
        upstreams = {a["id"]: a["_view_upstream"] for a in reg["apps"] if a.get("_view_upstream")}
        registry.atomic_write(self.cfg.home / "view-upstreams.json", json.dumps(upstreams).encode(), 0o600)
        return self.clean(reg), rows

    def read(self, path: str, query: str = ""):
        if path == "/api/system/load":
            return self.clean(self.rt.req("GET", "/system/load"))
        if path in {"/api/marketplace/all", "/api/marketplace/enabled"}:
            return self.clean(self.rt.req("GET", path[4:]))
        if path == "/api/marketplace/apps/search":
            params = parse_qsl(query, keep_blank_values=True)
            if any(k not in {"search", "pageSize", "cursor", "category", "storeId"} for k, _ in params):
                raise BridgeError(400, "invalid_query")
            params = [(k, v) for k, v in params if k != "storeId"] + [("storeId", cli.STORE)]
            return self.clean(self.rt.req("GET", path[4:] + "?" + urlencode(params)))
        try:
            with self.serialized(blocking=False):
                return self._read_projection(path)
        except BridgeError as e:
            if e.reason != "bridge_busy":
                raise
            return self._snapshot(path)

    def _read_projection(self, path: str):
        if path in {"/api/apps", "/api/apps/installed", "/api/slots"}:
            reg, rows = self.refresh()
            if path == "/api/apps":
                return reg
            if path == "/api/slots":
                return {"spots": slots.table(reg)}
            by_urn = {a["urn"]: a for a in reg["apps"]}
            installed = []
            for row in rows:
                urn = (row.get("info") or {}).get("urn")
                if urn not in by_urn:
                    continue
                installed.append(self._detail(row, by_urn[urn]))
            return {"installed": installed, "registry": reg}
        match = re.fullmatch(r"/api/internal/apps/([a-z0-9-]+)/view", path)
        if match:
            self.refresh()
            target = registry._json_file(self.cfg.home / "view-upstreams.json").get(match[1])
            if not target:
                raise BridgeError(404, "view_not_found")
            return {"upstream": target, "path": f"/apps/{match[1]}/"}
        match = re.fullmatch(r"/api/apps/([^/]+)", path)
        if match:
            urn = urn_value(match[1])
            reg, _ = self.refresh()
            entry = next((a for a in reg["apps"] if a["urn"] == urn), None)
            return self._detail(self.rt.app(urn), entry)
        raise BridgeError(404, "not_found")


    def _snapshot(self, path: str):
        if path not in {"/api/apps", "/api/apps/installed", "/api/slots"} and not re.fullmatch(
                r"/api/apps/[^/]+|/api/internal/apps/[a-z0-9-]+/view", path):
            raise BridgeError(404, "not_found")
        reg = registry._json_file(self.cfg.home / "apps.json")
        if not isinstance(reg, dict) or not isinstance(reg.get("apps"), list):
            raise BridgeError(503, "registry_not_ready")
        reg = self.clean(reg)
        if path == "/api/apps":
            return {**reg, "snapshot": True}
        if path == "/api/slots":
            return {"spots": slots.table(reg), "snapshot": True}
        match = re.fullmatch(r"/api/internal/apps/([a-z0-9-]+)/view", path)
        if match:
            target = registry._json_file(self.cfg.home / "view-upstreams.json").get(match[1])
            if not target:
                raise BridgeError(404, "view_not_found")
            return {"upstream": target, "path": f"/apps/{match[1]}/", "snapshot": True}
        def detail(app):
            return {"app": {"status": app["status"]},
                    "info": {"id": app["id"], "urn": app["urn"], "name": app["name"]},
                    "metadata": {}, "wizard": app}
        if path == "/api/apps/installed":
            return {"installed": [detail(a) for a in reg["apps"]], "registry": reg, "snapshot": True}
        urn = urn_value(path.removeprefix("/api/apps/"))
        app = next((a for a in reg["apps"] if a["urn"] == urn), None)
        if not app:
            raise BridgeError(503, "app_busy")
        return {**detail(app), "snapshot": True}

    def _detail(self, row: dict, entry: dict | None):
        app = row.get("app")
        safe_app = None if not app else {k: app[k] for k in (
            "id", "appName", "appStoreSlug", "status", "port", "version", "pendingRestart",
            "createdAt", "updatedAt", "isVisibleOnGuestDashboard") if k in app}
        info = dict(row.get("info") or {})
        # Metadata is public, but form defaults can contain deployment secrets regardless of field type.
        fields = info.get("form_fields")
        if isinstance(fields, list):
            info["form_fields"] = [{k: v for k, v in f.items() if k not in {"default", "value"}}
                                   for f in fields if isinstance(f, dict)]
        out = {"app": safe_app, "info": info, "metadata": row.get("metadata") or {}}
        if entry:
            out["wizard"] = entry
        return self.clean(out)

    def image(self, urn: str) -> tuple[bytes, str]:
        urn = urn_value(urn)
        req = urllib.request.Request(self.rt.base + f"/marketplace/apps/{urn}/image",
                                     headers={"Authorization": "Bearer " + cli_token(self.rt.secret)})
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            kind = response.headers.get("Content-Type", "").split(";", 1)[0]
            if kind not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise BridgeError(502, "unsupported_image")
            data = response.read(4194305)
            if len(data) > 4194304:
                raise BridgeError(502, "image_too_large")
            return data, kind

    def _journal(self) -> dict:
        if not self.operations.exists():
            return {}
        try:
            journal = json.loads(self.operations.read_text())
            if not isinstance(journal, dict):
                raise ValueError
            for key, item in journal.items():
                if (not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(item, dict)
                        or not re.fullmatch(r"[0-9a-f]{64}", item.get("fingerprint", ""))
                        or item.get("state") not in {"reserved", "uncertain", "done"}
                        or not isinstance(item.get("urn"), str)):
                    raise ValueError
                urn_value(item["urn"])
                if item["state"] == "done" and (not isinstance(item.get("code"), int)
                                                     or not isinstance(item.get("result"), dict)):
                    raise ValueError
        except (ValueError, TypeError, OSError, BridgeError):
            raise BridgeError(503, "operation_journal_invalid") from None
        return journal

    def _save(self, journal: dict):
        registry.atomic_write(self.operations, json.dumps(journal).encode(), 0o600)

    def _form(self, urn: str, action: str, body: dict, rows: list, *, noop=False) -> dict:
        if action in {"start", "stop", "restart"}:
            if body:
                raise BridgeError(400, "unexpected_fields")
            return {}
        if action in {"uninstall", "update"}:
            key = "removeBackups" if action == "uninstall" else "performBackup"
            if set(body) - {key} or (key in body and not isinstance(body[key], bool)):
                raise BridgeError(400, "invalid_form")
            return {key: body.get(key, action == "update")}
        app_id = urn.split(":")[0]
        if not any(s.get("slug") == cli.STORE and s.get("enabled") for s in self.rt.stores()):
            raise BridgeError(503, "store_unavailable")
        info = self.rt.app(urn).get("info") or {}
        if not info:
            raise BridgeError(404, "app_not_found")
        meta = registry.app_meta(self.cfg.runtipi_root, cli.STORE, app_id)
        uses = (meta or {}).get("w", {}).get("uses") or []
        have = {(r.get("info") or {}).get("urn", "").split(":")[0] for r in rows}
        external = alongside.external_slots(self.cfg.home)
        external_apps = {v.get("app") for k, v in slots.load_defaults().items() if k in external}
        if set(uses) - have - external_apps:
            raise BridgeError(409, "dependencies_required")
        allowed = {f.get("env_variable") for f in info.get("form_fields") or []} - {
            "port", "exposed", "exposedLocal", "openPort", "domain", "enableAuth", "skipEnv", "skipPull",
            "skipRun", "forcePull", "localSubdomain", "isVisibleOnGuestDashboard", "maxBackups"}
        if (set(body) - allowed or any(not isinstance(v, (str, int, float, bool)) for v in body.values())
                or any(isinstance(v, float) and not math.isfinite(v) for v in body.values())):
            raise BridgeError(400, "invalid_form")
        if noop:
            return {}
        form = self._install_form(app_id, info, body, uses)
        state = alongside.load(self.cfg.home)
        if state and alongside.is_bridge_app(app_id):
            alongside.write_shell_override(self.cfg.runtipi_root, cli.STORE, app_id, self.cfg.home)
            form["port"] = int(state["shell"]["port"])
        return form

    def _install_form(self, app_id: str, info: dict, given: dict, uses: list) -> dict:
        """Stock WAM defaults/shared credentials, with durable generated-password reuse on retry."""
        form = {}
        path = self.cfg.home / "secrets" / f"{app_id}.json"
        saved = registry._json_file(path)
        missing = False
        for field in info.get("form_fields") or []:
            var = field.get("env_variable")
            if not var or field.get("type") == "random":
                continue
            if var in given:
                form[var] = given[var]
            elif field.get("required") and (shared := cli.shared_value(self.cfg, var, uses)):
                form[var] = shared
            elif "default" in field:
                continue
            elif field.get("required") and field.get("type") == "password":
                saved.setdefault(var, secrets.token_urlsafe(max(24, int(field.get("min") or 16))))
                form[var] = saved[var]
            elif field.get("required"):
                missing = True
        if missing:
            raise BridgeError(400, "required_fields_missing")
        if saved:
            path.parent.mkdir(mode=0o700, exist_ok=True)
            registry.atomic_write(path, json.dumps(saved).encode(), 0o600)
        form.update(openPort=False, exposed=False, exposedLocal=False)
        return form

    def _status(self, urn: str) -> str:
        # A failed authenticated lookup is never interpreted as an uninstalled app.
        rows = self.rt.installed()
        return next(((r.get("app") or {}).get("status", "unknown") for r in rows
                     if (r.get("info") or {}).get("urn") == urn), "missing")

    def _verify(self, urn: str, action: str, *, failure=False) -> str:
        want = {"missing", "running", "stopped"} if failure else TERMINAL[action]
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            status = self._status(urn)
            if status in want:
                return status
            time.sleep(self.poll)
        raise BridgeError(504, "terminal_status_unconfirmed")

    def _execute(self, urn: str, action: str, body: dict, on_submit) -> dict:
        reg, rows = self.refresh()
        status = self._status(urn)
        if status not in {"missing", "running", "stopped"}:
            raise BridgeError(409, "app_busy")
        noop = ((action == "install" and status != "missing") or
                (action == "uninstall" and status == "missing") or
                (action == "start" and status == "running") or (action == "stop" and status == "stopped"))
        if action != "install" and action != "uninstall" and status == "missing":
            raise BridgeError(404, "app_not_installed")
        if action == "uninstall" and slots.blocks_uninstall(reg, urn.split(":")[0]):
            raise BridgeError(409, "required_slot")
        # Validate even no-op requests; no exposure flags/host ports or arbitrary lifecycle options.
        form = self._form(urn, action, body, rows, noop=noop)
        if noop:
            return {"success": True, "noop": True, "status": status}
        with self.completion(self.rt, urn, action, self.timeout) as completion:
            on_submit()
            result = self.rt.req(ACTIONS[action], f"/app-lifecycle/{urn}/{action}", form, timeout=120)
            request_id = result.get("requestId")
            if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", request_id):
                raise BridgeError(502, "untrackable_operation")
            if not completion.wait():
                self._verify(urn, action, failure=True)
                raise BridgeError(422, "lifecycle_failed")
            status = self._verify(urn, action)
        return {"success": True, "noop": False, "status": status, "requestId": request_id}

    def operate(self, method: str, urn: str, action: str, body: dict, key: str):
        urn = urn_value(urn)
        if ACTIONS.get(action) != method:
            raise BridgeError(405, "method_not_allowed")
        if not KEY.fullmatch(key):
            raise BridgeError(400, "idempotency_key_required")
        fingerprint = hmac.new(self.token.encode(), json.dumps([method, urn, action, body], sort_keys=True,
                                                              separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
        key = hashlib.sha256(key.encode()).hexdigest()
        with self.serialized():
            replay = False
            try:
                journal = self._journal()
                prior = journal.get(key)
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise BridgeError(409, "idempotency_conflict")
                    replay = True
                    if prior["state"] != "done":
                        raise BridgeError(409, "operation_unconfirmed")
                    code, result = prior["code"], prior["result"]
                else:
                    if any(v["urn"] == urn and v["state"] != "done" for v in journal.values()):
                        raise BridgeError(409, "operation_unconfirmed")
                    journal[key] = {"urn": urn, "action": action, "fingerprint": fingerprint,
                                    "state": "reserved", "created_at": int(time.time())}
                    self._save(journal)  # durable reservation BEFORE any upstream call
                    submitted = False

                    def on_submit():
                        nonlocal submitted
                        # A durable phase marker distinguishes preflight from possible upstream effects.
                        journal[key]["phase"] = "submitted"
                        self._save(journal)
                        submitted = True

                    try:
                        result = self._execute(urn, action, body, on_submit)
                        code = 200
                    except BridgeError as e:
                        code, result = e.code, {"success": False, "error": e.reason}
                    except UpstreamFailure as e:
                        code, result = 502, {"success": False, "error": str(e)}
                    except (Exception, SystemExit):
                        code, result = 502, {"success": False, "error": "upstream_unavailable"}
                    uncertain = submitted and result.get("error") not in {None, "lifecycle_failed", "upstream_rejected"}
                    journal[key].update(state="uncertain" if uncertain else "done", code=code, result=result)
                    self._save(journal)
            finally:
                # Refresh for success, failure, conflicting retries and duplicate deliveries alike.
                try:
                    reg, _ = self.refresh()
                except Exception:
                    raise BridgeError(502, "projection_refresh_failed") from None
            return code, {**result, "urn": urn, "action": action, "replayed": replay, "registry": reg}


class Handler(BaseHTTPRequestHandler):
    server_version = "WAMLifecycle/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass  # No URLs, request bodies, auth headers or raw upstream errors in logs.

    def send_data(self, code: int, data: bytes, kind="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self):
        bridge = cast(Server, self.server).bridge
        try:
            if self.headers.get("Origin") or not hmac.compare_digest(
                    self.headers.get("Authorization", "").encode(), ("Bearer " + bridge.token).encode()):
                raise BridgeError(401, "unauthorized")
            url = urlsplit(self.path)
            if url.scheme or url.netloc or "%" in url.path or "\\" in url.path:
                raise BridgeError(400, "invalid_path")
            if self.command == "GET":
                if url.path == "/healthz":
                    result = {"ok": True}
                else:
                    image = re.fullmatch(r"/api/marketplace/apps/([^/]+)/image", url.path)
                    if image:
                        data, kind = bridge.image(image[1])
                        return self.send_data(200, data, kind)
                    result = bridge.read(url.path, url.query)
                code = 200
            else:
                match = re.fullmatch(r"/api/app-lifecycle/([^/]+)/([a-z]+)", url.path)
                if not match or url.query:
                    raise BridgeError(404, "not_found")
                if self.headers.get("Transfer-Encoding"):
                    raise BridgeError(400, "invalid_body")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise BridgeError(400, "invalid_body") from None
                if not 0 <= size <= 65536:
                    raise BridgeError(413, "body_too_large")
                try:
                    body = json.loads(self.rfile.read(size) or b"{}")
                except ValueError:
                    raise BridgeError(400, "invalid_json") from None
                if not isinstance(body, dict):
                    raise BridgeError(400, "invalid_body")
                code, result = bridge.operate(self.command, match[1], match[2], body,
                                              self.headers.get("Idempotency-Key", ""))
            self.send_data(code, json.dumps(bridge.clean(result)).encode())
        except BridgeError as e:
            self.send_data(e.code, json.dumps({"success": False, "error": e.reason}).encode())
        except UpstreamFailure as e:
            self.send_data(502, json.dumps({"success": False, "error": str(e)}).encode())
        except Exception:
            self.send_data(502, b'{"success":false,"error":"upstream_unavailable"}')

    do_GET = do_POST = do_PATCH = do_DELETE = dispatch


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, bridge: Bridge):
        if not ipaddress.ip_address(address[0]).is_loopback or ":" in address[0]:
            raise ValueError("lifecycle bridge must bind an IPv4 loopback address")
        self.bridge = bridge
        super().__init__(address, Handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["serve"])
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7791)
    ap.add_argument("--token-file", type=Path)
    args = ap.parse_args(argv)
    cfg = cli.Config()
    token = (token_file(args.token_file) if args.token_file else
             os.environ.get("WIZARD_APP_MANAGER_TOKEN") or token_file(cfg.home / "lifecycle-token"))
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", token):
        raise ValueError("dedicated lifecycle token must contain 32-256 header-safe characters")
    bridge = Bridge(cfg, token)
    with Server((args.bind, args.port), bridge) as server:
        print(f"WAM lifecycle bridge on {args.bind}:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
