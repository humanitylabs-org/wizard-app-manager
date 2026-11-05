"""The owner edge: the platform's authenticated front door for apps that declare `owner_proof` (owner_proof.py).

Generalizes Messenger's hand-built owner ingress (one per box, maintained by hand) into an App Manager component
that serves every owner-proof app the same way:

    phone/laptop ──tailnet──▶ tailscaled Serve  https://<box>:<shell port>/<app>/
                                │  sets Tailscale-User-Login / X-Forwarded-For / X-Forwarded-Host itself
                                ▼  (Serve handler `unix:<wizard-os>/owner-edge/<app>.sock`, published by `wam links`)
                       owner edge (this file, host user)
                                │  1. kernel peer credentials: the connecting process IS tailscaled (root, its PID)
                                │  2. Serve identity: the owner's tailnet login, from one of the owner's enrolled devices
                │     (owner_devices.py: resolved with tailscale whois, listed in the Devices record), not this box
                │     and not the owner's other servers
                                │  3. Host is this box's tailnet name, same-origin writes, no bearer
                                │  4. strips every identity/proof header a client sent, signs the request
                                ▼
                       the app's loopback port 127.0.0.1:<port>  (X-Wizard-App-Owner-Proof: <signed>)

A request that fails 1-3 is refused before anything is forwarded; nothing is ever signed for an Authorization
(bearer/agent) request. Agents never come through here: they use the Agent Service's MCP gateway, which talks to the
app's port with its own per-app key.

Also, for every owner-proof app alike:
  - while the app restarts, a page load gets a small "<App> is restarting" page that reopens it by itself (API calls
    keep a JSON 502 so the app's own retry logic runs);
  - a brief connection refusal while the app comes back is retried instead of becoming a 502;
  - <wizard-os>/owner-activity/<app> is touched (at most every 15 s) on page loads, writes and the app's declared
    `activity` paths, so deploys and nightly updates can wait until the owner is not using the app.

Run: `wam owner-edge serve` (systemd user unit `wizard-owner-edge.service`). Linux only (SO_PEERCRED).
"""
from __future__ import annotations

import html
import http.client
import json
import os
import re
import shutil
import socket
import socketserver
import stat
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from . import owner_devices, owner_proof as op

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade"}
# Headers only the platform may set. A browser copy is dropped before forwarding (the HMAC is the real protection;
# this keeps apps from ever seeing a forged look-alike).
STRIP_PREFIXES = ("tailscale-", "x-forwarded-", "x-wizard-", "cf-access-")
STRIP_NAMES = {"forwarded", "x-real-ip", "remote-user", "proxy", "cf-connecting-ip"}
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CTL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
MAX_BODY = 110 * 1024 * 1024        # the shell's own client_max_body_size
ACTIVITY_EVERY_S = 15
BACKEND_RETRY_S = 5.0
WAITING = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title><style>html,body{{margin:0;height:100%;background:transparent;font:15px/1.4 system-ui,sans-serif;color:#9a9aa2}}
@media (prefers-color-scheme:light){{body{{color:#5a5a62}}}}main{{height:100%;display:grid;place-items:center;text-align:center;padding:24px}}
.d{{width:28px;height:28px;margin:0 auto 14px;border-radius:50%;border:3px solid currentColor;border-right-color:transparent;animation:s 1s linear infinite}}
@keyframes s{{to{{transform:rotate(360deg)}}}}</style></head><body><main><div><div class="d"></div><p>{name} is restarting. It will open by itself in a moment.</p></div></main>
<script>(function(){{var t=1500;function k(){{fetch(location.href,{{cache:'no-store',credentials:'same-origin'}}).then(function(r){{if(r.ok&&!r.headers.get('x-wizard-waiting'))location.reload();else s()}}).catch(s)}}function s(){{t=Math.min(t*1.3,3000);setTimeout(k,t)}}setTimeout(k,t)}})();</script></body></html>"""


# ---------------------------------------------------------------- who is calling

def tailscaled_peer(conn: socket.socket) -> bool:
    """The connecting process is tailscaled itself: uid/gid 0 and the PID systemd reports for tailscaled.service.
    The owner's own programs (and rootless containers, whose root maps to the owner) fail this."""
    pid, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != 0 or gid != 0 or pid <= 1:
        return False
    expected = subprocess.check_output(["systemctl", "show", "tailscaled.service", "--property=MainPID", "--value"],
                                       timeout=3, text=True).strip()
    proc = Path("/proc") / str(pid)
    return str(pid) == expected and proc.stat().st_uid == 0 and (proc / "comm").read_text().strip() == "tailscaled"


class Identity:
    """The owner's tailnet login(s), this node's own addresses and its tailnet name, refreshed at most every 60 s.
    Last known good is kept when Tailscale doesn't answer, so a slow `tailscale status` never locks the owner out;
    with nothing known yet every request is refused (fail closed)."""

    def __init__(self, home: Path, status=None, ttl: float = 60.0):
        self.home, self.ttl, self._status = Path(home), ttl, status or self._tailscale_status
        self._at, self._val, self._lock = 0.0, {"logins": set(), "ips": set(), "host": ""}, threading.Lock()

    @staticmethod
    def _tailscale_status() -> dict:
        if not shutil.which("tailscale"):
            return {}
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=15)
        return json.loads(r.stdout or "{}")

    def _config(self) -> dict:
        try:
            d = json.loads((self.home / "owner-edge.json").read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def current(self) -> dict:
        with self._lock:
            if time.monotonic() - self._at < self.ttl and self._val["host"]:
                return self._val
            try:
                d = self._status() or {}
                me = d.get("Self") or {}
                login = str(((d.get("User") or {}).get(str(me.get("UserID") or "")) or {}).get("LoginName") or "")
                cfg = self._config()
                pinned = [str(x).strip().lower() for x in cfg.get("owner_logins") or [] if str(x).strip()]
                # A tagged node has no owner user: then only an explicitly configured login can be the owner.
                logins = set(pinned) or ({login.lower()} if login and not me.get("Tags") else set())
                host = str(me.get("DNSName") or "").rstrip(".").lower()
                ips = {str(ip) for ip in me.get("TailscaleIPs") or []}
                if host and ips:
                    self._val = {"logins": logins, "ips": ips, "host": host,
                                 "allow_box": bool(cfg.get("allow_requests_from_this_box"))}
                    self._at = time.monotonic()
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            return self._val


# ---------------------------------------------------------------- serving

class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 256      # the opening burst of an app (lists, avatars) must queue, not EAGAIN
    home: Path
    app: dict
    identity: "Identity"
    peer_check = staticmethod(tailscaled_peer)
    key: "_KeyCache"
    active_at = 0.0

    def verify_request(self, request, address):
        try:
            ok = self.peer_check(request)
        except (OSError, ValueError, subprocess.SubprocessError):
            ok = False
        if not ok:
            try:
                request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        return ok


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _UnixHTTPServer

    def log_message(self, *_):   # never log requests: paths and headers can carry private data
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def _one(self, name: str):
        values = self.headers.get_all(name, [])
        return values[0] if len(values) == 1 else None

    def deny(self, code: int, reason: str) -> None:
        self.close_connection = True
        raw = json.dumps({"ok": False, "error": {"code": reason}}).encode()
        self.send_response(code)
        for k, v in (("Content-Type", "application/json"), ("Cache-Control", "no-store"),
                     ("Content-Length", str(len(raw))), ("Connection", "close")):
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass

    def _check(self, ident: dict) -> str | None:
        """None when this is the owner on one of their own devices, else the refusal code."""
        # Serve proxies to a Unix socket with Host: localhost and puts the TLS authority in X-Forwarded-Host
        # (tailscale ipn/ipnlocal/serve.go addProxyForwardedHeaders: Set, never append).
        host = self._one("X-Forwarded-Host") if self._one("Host") == "localhost" else None
        name, _, port = (host or "").rpartition(":")
        if not host or name.lower() != ident["host"] or not port.isdigit():
            return "wizard_host_required"
        login = self._one("Tailscale-User-Login")
        if not login or login.lower() not in ident["logins"]:
            return "authenticated_owner_required"
        hop = (self._one("X-Forwarded-For") or "").strip()
        if not hop or hop in ident["ips"] or hop in ("127.0.0.1", "::1"):
            # Serve labels this node's own address with the node owner's login, so every program on the box would
            # otherwise pass as the owner (wam store_install.from_box, same rule).
            if not ident.get("allow_box"):
                return "owner_device_required"
        else:
            # The owner's login is on every untagged node they are signed in to, including their other servers where
            # agents run. Only a device in the Wizard's own device record is "the owner's device" (owner_devices.py).
            # A caller Tailscale cannot resolve, or a record that cannot be read, is refused.
            why = self.server.devices.check(hop, login)
            if why:
                return why
        if self.headers.get_all("Authorization") is not None:
            return "bearer_owner_handoff_denied"
        origin = self._one("Origin")
        if self.headers.get_all("Origin") and origin != f"https://{host}":
            return "origin_forbidden"
        if self.command not in ("GET", "HEAD") and origin != f"https://{host}":
            return "origin_required"
        site = self.headers.get_all("Sec-Fetch-Site", [])
        if len(site) > 1 or (site and site[0] not in ("same-origin", "none")):
            return "cross_site_forbidden"
        self.authority = host
        return None

    def _activity(self, path: str) -> None:
        app = self.server.app
        bare = path.split("?", 1)[0]
        if not (self._one("Sec-Fetch-Mode") == "navigate" or self.command not in ("GET", "HEAD")
                or bare in app["activity"]):
            return
        now = time.time()
        if now - getattr(self.server, "active_at", 0) < ACTIVITY_EVERY_S:
            return
        self.server.active_at = now
        try:
            d = self.server.home / "owner-activity"
            d.mkdir(mode=0o700, exist_ok=True)
            target = d / app["id"]
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            os.utime(target, (now, now))
        except OSError:
            pass

    def _connect(self) -> http.client.HTTPConnection:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.app["port"], timeout=120)
        deadline = time.monotonic() + BACKEND_RETRY_S
        while True:
            try:
                conn.connect()
                return conn
            except ConnectionRefusedError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def _waiting_or_502(self) -> None:
        if self.command == "GET" and (self._one("Sec-Fetch-Mode") == "navigate" or "text/html" in (self._one("Accept") or "")):
            page = WAITING.format(name=html.escape(self.server.app["name"])).encode()
            self.close_connection = True
            self.send_response(503)
            for k, v in (("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
                         ("Retry-After", "2"), ("X-Wizard-Waiting", "1"), ("Content-Length", str(len(page))),
                         ("Connection", "close")):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(page)
        else:
            self.deny(502, "app_unavailable")

    def handle_one(self) -> None:
        app = self.server.app
        reason = self._check(self.server.identity.current())
        if reason:
            return self.deny(403, reason)
        raw_target = self.raw_requestline.split(b" ", 2)[1] if self.raw_requestline.count(b" ") >= 2 else b""
        if (not raw_target.startswith(b"/") or raw_target.startswith(b"//") or not self.path.startswith("/")
                or self.path.startswith("//") or "#" in self.path or "\\" in self.path):
            return self.deny(400, "invalid_target")
        if self.headers.get_all("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
            return self.deny(400, "ambiguous_body")
        try:
            length = int(self._one("Content-Length") or "0")
        except ValueError:
            return self.deny(400, "invalid_length")
        if not 0 <= length <= MAX_BODY:
            return self.deny(413, "body_too_large")
        try:
            key = self.server.key()
        except (OSError, ValueError):
            return self.deny(503, "owner_proof_unavailable")
        tokens = {x.strip().lower() for x in (self._one("Connection") or "").split(",")}
        headers = {}
        for k, v in self.headers.items():
            low = k.lower()
            if (low in HOP or low in tokens or low.startswith(STRIP_PREFIXES) or low in STRIP_NAMES
                    or low in app["strip"] or low == "host"):
                continue
            headers[k] = v
        headers["Host"] = self.authority
        headers["Connection"] = "close"
        headers[op.HEADER] = op.sign(key, app["id"], self.command, self.path)
        self._activity(self.path)
        try:
            backend = self._connect()
        except OSError:
            if length:
                self.close_connection = True
            return self._waiting_or_502()
        started = False
        try:
            backend.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for k, v in headers.items():
                backend.putheader(k, v)
            backend.endheaders()
            left = length
            while left:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    raise ValueError("truncated_body")
                backend.send(chunk)
                left -= len(chunk)
            resp = backend.getresponse()
            started = True
            self.send_response(resp.status)      # the standard phrase, never the app's own text
            for k, v in resp.getheaders():
                # Relay only well-formed headers: a CR/LF (or a non-token name) from the app never reaches the
                # browser as a second header or a second response.
                if k.lower() in HOP or k.lower() in ("server", "date") or not _TOKEN.fullmatch(k) or _CTL.search(v):
                    continue
                self.send_header(k.replace("\r", "").replace("\n", "").replace(":", ""),
                                 v.replace("\r", "").replace("\n", ""))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                # Stream (long polls, media ranges, event streams): no whole-response buffering.
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except ValueError:
            if not started:
                self.deny(400, "truncated_body")
            self.close_connection = True
        except (OSError, http.client.HTTPException):
            if not started:
                self._waiting_or_502()
            self.close_connection = True
        finally:
            backend.close()

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_one


class _KeyCache:
    """Re-reads the edge key when its file changes (rotation), never on every request. The permission policy is
    checked on every use, and a permission change (which moves ctime) also invalidates the cached key: a key that
    became group/other readable stops signing at once."""

    def __init__(self, path: Path):
        self.path, self._sig, self._key = path, None, b""

    def __call__(self) -> bytes:
        st = os.stat(self.path, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            self._key, self._sig = b"", None
            raise ValueError(f"{self.path}: not a private key file")
        sig = (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_mode)
        if sig != self._sig:
            self._key, self._sig = b"", None
            self._key, self._sig = op.read_private_key(self.path), sig
        return self._key


def serve_app(home: Path, app: dict, identity: Identity, peer_check=tailscaled_peer, devices=None) -> _UnixHTTPServer:
    """Bind <home>/owner-edge/<app>.sock (0600 in a 0700 folder) for one app; returns the server (not started).
    `devices.check(hop, login)` says whether the caller is one of the owner's enrolled devices (default: the real one)."""
    d = op.socket_dir(home)
    d.mkdir(mode=0o700, exist_ok=True)
    info = d.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"{d} must be a 0700 folder owned by this user")
    path = op.socket_path(home, app["id"])
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise ValueError(f"refusing to replace {path}: not a socket")
        path.unlink()
    old = os.umask(0o077)
    try:
        server = _UnixHTTPServer(str(path), Handler)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    server.home, server.app, server.identity, server.peer_check = Path(home), app, identity, peer_check
    server.devices = devices or owner_devices.Enrollment(home)
    server.key = _KeyCache(op.edge_key_path(home, app["id"]))
    return server


def apps_from_registry(home: Path) -> dict[str, dict]:
    """Owner-proof apps with a page on a loopback port, from apps.json (view.local, else view.url)."""
    try:
        reg = json.loads((Path(home) / "apps.json").read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for a in reg.get("apps") or []:
        opts = a.get("owner_proof") if isinstance(a, dict) else None
        if not isinstance(opts, dict):
            continue
        url = str((a.get("view") or {}).get("local") or (a.get("view") or {}).get("url") or "")
        hostport = url[len("http://"):].split("/", 1)[0] if url.startswith("http://") else ""
        host, _, port = hostport.rpartition(":")
        if host in ("127.0.0.1", "localhost") and port.isdigit() and op.edge_key_path(home, a["id"]).is_file():
            out[a["id"]] = {"id": a["id"], "name": a.get("name") or a["id"], "port": int(port),
                            "activity": list(opts.get("activity") or []), "strip": list(opts.get("strip") or [])}
    return out


def run(home: Path, stop: threading.Event | None = None, every: float = 10.0, peer_check=tailscaled_peer,
        identity: Identity | None = None) -> None:
    """Serve every owner-proof app; follow installs, removals and port changes from apps.json."""
    if not op.enabled(home):
        raise ValueError("owner proof is off; complete follow-up 2 before enabling owner_proof_enabled")
    stop = stop or threading.Event()
    identity = identity or Identity(home)
    devices = owner_devices.Enrollment(home)
    live: dict[str, tuple[dict, _UnixHTTPServer]] = {}

    def close(app_id: str) -> None:
        _, srv = live.pop(app_id)
        srv.shutdown()
        srv.server_close()
        op.socket_path(home, app_id).unlink(missing_ok=True)

    try:
        while not stop.is_set():
            want = apps_from_registry(home)
            for app_id in [i for i in live if i not in want or live[i][0] != want[i]]:
                close(app_id)
            for app_id, app in want.items():
                if app_id not in live:
                    try:
                        srv = serve_app(home, app, identity, peer_check, devices)
                    except (OSError, ValueError):
                        continue
                    threading.Thread(target=srv.serve_forever, daemon=True, name=f"owner-edge-{app_id}").start()
                    live[app_id] = (app, srv)
            stop.wait(every)
    finally:
        for app_id in list(live):
            close(app_id)
