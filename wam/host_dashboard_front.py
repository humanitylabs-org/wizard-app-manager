"""The Wizard front for the alongside host Hermes dashboard (browser boundary, wizard-os issue 386; default-off).

Stock `hermes dashboard` is a full agent-admin surface behind one password (its /api/env, /api/config, sessions...).
On alongside boxes it used to listen on the box's Tailscale IPv4, so every tailnet device, container and process could
try that password, and the ChatGPT and Claude app containers held it. With WIZARD_BROWSER_BOUNDARY=1 the App Manager
instead binds Hermes to a host-only address (127.0.0.2, which Hermes still treats as a public bind: its password and
Host pinning stay on) and runs this front. No Hermes code changes. Python stdlib only.

Consumer listener (the address the apps already use, <Tailscale IPv4>:9119):
  - answers box peers only: an address of this box, or a container bridge network on it. Other tailnet devices and
    anything else get 403;
  - refuses browser markers (Origin, Referer, Sec-Fetch-Site/Dest/User) and network markers (Tailscale-*,
    X-Forwarded-*, Forwarded, X-Real-IP, CF-*), and any Host other than its own address (DNS rebinding);
  - knows each consumer app by its OWN per-app key, minted by the App Manager. The key arrives as X-Wizard-Grant, or
    as the "password" of the app's existing dashboard login (POST /auth/password-login), which the front answers
    itself with a front session: today's app images keep working unchanged and never see the real password;
  - lets each app reach only the dashboard paths on its reviewed allowlist (method + path);
  - holds the dashboard password itself: it signs in to Hermes once, adds its own session to allowed requests, keeps
    Hermes' cookies to itself, and never forwards a consumer's login. Hermes' per-IP login limit (10 a minute,
    successful logins included) therefore only ever sees the front, from its own source address.

Owner listener (optional, decision H2 = own tab like H1): 127.0.0.1:<port>, published by Tailscale Serve on its own
https port, so the dashboard has its own origin, keeps its own login (the owner types the password) and opens only as
its own tab. Only the owner's tailnet logins (Tailscale-User-Login, set by Serve), only its own Host; frames, foreign
or opaque Origins and cross-/same-site requests other than a top-level navigation are refused; every answer says DENY
to framing. The owner's sign-ins reach Hermes from a different source address than the front's own, so a mistyped
password can never rate-limit the apps (and the apps can never lock the owner out).

  python3 -m wam.host_dashboard_front <config.json>   (0600; re-read when it changes, so a new or revoked key needs
                                                       no restart)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse

MAX_HEAD = 65536
MAX_BODY = 1 << 20
SESSION_COOKIE = "wizard_front_session"
SESSION_TTL = 12 * 3600
BROWSER_MARKERS = ("origin", "referer", "sec-fetch-site", "sec-fetch-dest", "sec-fetch-user")
NETWORK_PREFIXES = ("tailscale-", "x-forwarded-", "cf-")
NETWORK_EXACT = ("forwarded", "x-real-ip")
# Request headers a consumer may send upstream; everything else (its cookies, Authorization, grants, identity hints)
# stays here.
CONSUMER_FORWARD = ("content-type", "content-length", "accept", "user-agent", "x-wizard-account-action")
FRAME_DESTS = {"iframe", "frame", "embed", "object", "fencedframe"}
OWN_TAB_HEADERS = ("X-Frame-Options: DENY\r\nContent-Security-Policy: frame-ancestors 'none'\r\n"
                   "Cross-Origin-Resource-Policy: same-origin\r\nCross-Origin-Opener-Policy: same-origin\r\n")
BRIDGE_IFACES = ("docker", "br-", "podman", "cni", "veth")
REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
           411: "Length Required", 413: "Payload Too Large", 502: "Bad Gateway", 503: "Service Unavailable"}


def log(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}, sort_keys=True), flush=True)


# ---------------------------------------------------------------- configuration

class Config:
    """The front's 0600 file, re-read when its stamp changes. A broken file keeps nothing open: no consumers."""

    def __init__(self, path: str):
        self.path = path
        self.stamp = None
        self.doc: dict = {}

    def get(self) -> dict:
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
            if stamp != self.stamp:
                if st.st_mode & 0o077:
                    raise ValueError("config must be 0600")
                with open(self.path, "rb") as f:
                    doc = json.loads(f.read(1 << 20))
                if not isinstance(doc, dict) or not isinstance(doc.get("consumers", {}), dict):
                    raise ValueError("malformed config")
                self.doc, self.stamp = doc, stamp
        except (OSError, ValueError) as e:
            if self.stamp is not None or not self.doc:
                log("config-unavailable", error=type(e).__name__)
            self.doc, self.stamp = {**self.doc, "consumers": {}}, None
        return self.doc


def path_allowed(allow: list, method: str, path: str) -> bool:
    """`allow` rows are [method, pattern]; a pattern is an exact path or a prefix ending in `/*` (one or more
    segments below it). Encoded dots/slashes, backslashes and empty or dot segments never match."""
    if not path.startswith("/") or "\\" in path or re.search(r"%(2e|2f|5c)", path, re.I):
        return False
    segs = path.split("/")[1:]
    if any(s in ("", ".", "..") for s in segs):
        return False
    for row in allow or []:
        if not (isinstance(row, (list, tuple)) and len(row) == 2):
            continue
        m, pat = str(row[0]).upper(), str(row[1])
        if m != method:
            continue
        if pat.endswith("/*"):
            if path.startswith(pat[:-1]) and len(path) > len(pat) - 1:
                return True
        elif path == pat:
            return True
    return False


# ---------------------------------------------------------------- box peers

class Peers:
    """Which source addresses are this box: its own interface addresses (loopback, LAN, its Tailscale IPv4) and the
    networks of its container bridges (docker0, br-*, podman, cni). Another tailnet device is never one of them:
    tailscale0 carries a /32. Re-read every 30 s (containers come and go)."""

    def __init__(self, reader=None):
        self.reader = reader or self._read
        self.at = 0.0
        self.addrs: set = set()
        self.nets: list = []

    @staticmethod
    def _read() -> tuple[set, list]:
        addrs, nets = {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}, []
        try:
            rows = json.loads(subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=5).stdout
                              or "[]")
        except (OSError, ValueError, subprocess.SubprocessError):
            rows = []
        for row in rows:
            name = str(row.get("ifname") or "")
            for a in row.get("addr_info") or []:
                try:
                    iface = ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}")
                except (KeyError, ValueError):
                    continue
                addrs.add(iface.ip)
                if name.startswith(BRIDGE_IFACES):
                    nets.append(iface.network)
        return addrs, nets

    def allowed(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip.split("%", 1)[0])
        except ValueError:
            return False
        if getattr(a, "ipv4_mapped", None):
            a = a.ipv4_mapped
        if a.is_loopback:
            return True
        if time.monotonic() - self.at > 30:
            self.addrs, self.nets = self.reader()
            self.at = time.monotonic()
        return a in self.addrs or any(a in n for n in self.nets)


# ---------------------------------------------------------------- HTTP plumbing

def parse_head(head: bytes) -> tuple[str, str, str, list[tuple[str, str]]]:
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise ValueError("bad request line")
    pairs = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            pairs.append((k.strip().lower(), v.strip()))
    return parts[0].upper(), parts[1], parts[2], pairs


def networked(pairs) -> bool:
    return any(k.startswith(NETWORK_PREFIXES) or k in NETWORK_EXACT for k, _ in pairs)


async def respond(writer, code: int, obj=None, extra: str = "", cookie: str = "") -> None:
    body = json.dumps(obj if obj is not None else {}).encode()
    head = (f"HTTP/1.1 {code} {REASONS.get(code, 'Error')}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n{extra}"
            + (f"Set-Cookie: {cookie}\r\n" if cookie else "") + "Connection: close\r\n\r\n")
    try:
        writer.write(head.encode("latin-1") + body)
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def pipe(reader, writer) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def cookies_of(pairs) -> dict[str, str]:
    out = {}
    for k, v in pairs:
        if k == "cookie":
            for part in v.split(";"):
                n, _, val = part.strip().partition("=")
                if n:
                    out[n] = val
    return out


def set_cookie_pairs(head_lines: list[str]) -> dict[str, str]:
    """name -> value for every Set-Cookie in an upstream response head (a cleared cookie maps to '')."""
    out = {}
    for line in head_lines:
        k, _, v = line.partition(":")
        if k.strip().lower() == "set-cookie":
            first = v.strip().split(";", 1)[0]
            n, _, val = first.partition("=")
            gone = re.search(r"max-age=0|expires=thu, 01 jan 1970", v, re.I)
            out[n.strip()] = "" if gone else val.strip()
    return out


async def open_upstream(cfg: dict, source: str | None):
    up = cfg.get("upstream") or {}
    return await asyncio.wait_for(asyncio.open_connection(
        up.get("host", "127.0.0.2"), int(up.get("port", 0)),
        local_addr=(source, 0) if source else None), timeout=10)


# ---------------------------------------------------------------- the front's own Hermes session

class Upstream:
    """The front's own signed-in session on the dashboard. One sign-in, reused; renewed only after a 401."""

    def __init__(self, conf: Config):
        self.conf = conf
        self.jar: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.logins = 0

    def cookie(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.jar.items() if v)

    def absorb(self, head_lines: list[str]) -> None:
        for k, v in set_cookie_pairs(head_lines).items():
            if k.startswith("hermes_session"):
                if v:
                    self.jar[k] = v
                else:
                    self.jar.pop(k, None)

    async def login(self, stale: str | None = None) -> bool:
        async with self.lock:
            if stale is not None and self.cookie() != stale and self.jar:
                return True   # another request already renewed it
            cfg = self.conf.get()
            cred = cfg.get("login") or {}
            body = json.dumps({"provider": "basic", "username": cred.get("username", "hermes"),
                               "password": cred.get("password", "")}).encode()
            up = cfg.get("upstream") or {}
            try:
                r, w = await open_upstream(cfg, (cfg.get("source") or {}).get("consumers"))
            except (OSError, asyncio.TimeoutError):
                return False
            w.write((f"POST /auth/password-login HTTP/1.1\r\nHost: {up.get('host')}:{up.get('port')}\r\n"
                     f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                     ).encode() + body)
            await w.drain()
            try:
                head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=30)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
                w.close()
                return False
            w.close()
            lines = head.decode("latin-1").split("\r\n")
            status = int(lines[0].split(" ")[1]) if len(lines[0].split(" ")) > 1 else 0
            self.logins += 1
            log("upstream-login", status=status, count=self.logins)
            if status != 200:
                self.jar = {}
                return False
            self.jar = {}
            self.absorb(lines[1:])
            return bool(self.jar)


# ---------------------------------------------------------------- consumer listener

class Consumers:
    def __init__(self, conf: Config, upstream: Upstream, peers: Peers):
        self.conf, self.up, self.peers = conf, upstream, peers
        self.sessions: dict[str, tuple[str, float]] = {}   # front session -> (key sha256, expiry)
        self.port = 0                                        # the port actually bound (Host pinning)

    def identify(self, cfg: dict, pairs) -> tuple[str, dict] | None:
        """(key sha256, consumer row) for a known key in X-Wizard-Grant or a live front session, else None."""
        h = dict(pairs)
        rows = cfg.get("consumers") or {}
        key = h.get("x-wizard-grant", "")
        if key:
            digest = hashlib.sha256(key.encode()).hexdigest()
            for sha, row in rows.items():
                if hmac.compare_digest(sha.encode(), digest.encode()):
                    return sha, row
            return None
        tok = cookies_of(pairs).get(SESSION_COOKIE, "")
        s = self.sessions.get(tok) if tok else None
        if s and s[1] > time.time() and s[0] in rows:
            return s[0], rows[s[0]]
        return None

    async def handle(self, reader, writer) -> None:
        peer = (writer.get_extra_info("peername") or ("", 0))[0]
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
            if len(head) > MAX_HEAD:
                return await respond(writer, 400, {"error": "request head too large"})
            method, target, _ver, pairs = parse_head(head[:-4])
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError,
                ConnectionError):
            writer.close()
            return
        cfg = self.conf.get()
        lst = cfg.get("listen") or {}
        h = dict(pairs)
        path = urllib.parse.urlsplit(target).path if target.startswith("/") else ""
        if not self.peers.allowed(peer):
            log("refused", why="not a box peer", peer=peer)
            return await respond(writer, 403, {"error": "the agent dashboard answers only apps on this box"})
        marked = [k for k, _ in pairs if k in BROWSER_MARKERS] or (["network"] if networked(pairs) else [])
        if marked:
            log("refused", why="marker", marker=marked[0], path=path[:80])
            return await respond(writer, 403, {"error": "the agent dashboard is box-only: refused a request carrying "
                                                        + marked[0]})
        host = h.get("host", "").lower()
        if host not in {str(lst.get("host", "")).lower(), f"{lst.get('host', '')}:{self.port}".lower()}:
            return await respond(writer, 400, {"error": "Invalid Host header"})
        if "transfer-encoding" in h or "upgrade" in h:
            return await respond(writer, 411, {"error": "send a Content-Length body; no upgrades"})
        try:
            length = int(h.get("content-length", "0") or 0)
        except ValueError:
            return await respond(writer, 400, {"error": "bad Content-Length"})
        if length < 0 or length > MAX_BODY:
            return await respond(writer, 413, {"error": "body too large"})
        try:
            body = await asyncio.wait_for(reader.readexactly(length), timeout=60) if length else b""
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            writer.close()
            return
        # The app's existing dashboard login: its "password" is its own key. Answered here, never forwarded.
        if path == "/auth/password-login" and method == "POST":
            try:
                given = str((json.loads(body or b"{}") or {}).get("password") or "")
            except (ValueError, AttributeError):
                given = ""
            who = self.identify(cfg, [("x-wizard-grant", given)]) if given else None
            if not who:
                log("login-refused", peer=peer)
                return await respond(writer, 401, {"detail": "Invalid credentials"})
            tok = secrets.token_urlsafe(32)
            now = time.time()
            self.sessions = {t: s for t, s in self.sessions.items() if s[1] > now}
            self.sessions[tok] = (who[0], now + SESSION_TTL)
            log("login", app=who[1].get("app"))
            return await respond(writer, 200, {"ok": True, "next": "/"},
                                 cookie=f"{SESSION_COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict")
        if path == "/auth/logout" and method == "POST":
            self.sessions.pop(cookies_of(pairs).get(SESSION_COOKIE, ""), None)
            return await respond(writer, 200, {"ok": True})
        who = self.identify(cfg, pairs)
        if not who:
            return await respond(writer, 401, {"detail": "Not authenticated"})
        app, allow = who[1].get("app", "?"), who[1].get("allow") or []
        if not path_allowed(allow, method, path):
            log("refused", why="path not on the app's allowlist", app=app, method=method, path=path[:120])
            return await respond(writer, 403, {"error": f"{app} may not call {method} {path[:120]} on the agent "
                                                        "dashboard"})
        await self.forward(writer, cfg, method, target, pairs, body, app)

    async def forward(self, writer, cfg, method, target, pairs, body, app) -> None:
        up = cfg.get("upstream") or {}
        source = (cfg.get("source") or {}).get("consumers")
        keep = [(k, v) for k, v in pairs if k in CONSUMER_FORWARD and k != "content-length"]
        if not self.up.jar:
            await self.up.login()
        for attempt in (0, 1):
            sent_cookie = self.up.cookie()
            lines = [f"{method} {target} HTTP/1.1", f"Host: {up.get('host')}:{up.get('port')}"]
            lines += [f"{k}: {v}" for k, v in keep]
            lines += [f"Content-Length: {len(body)}"] if body or method in ("POST", "PUT", "PATCH") else []
            if sent_cookie:
                lines.append(f"Cookie: {sent_cookie}")
            lines.append("Connection: close")
            try:
                ur, uw = await open_upstream(cfg, source)
            except (OSError, asyncio.TimeoutError):
                return await respond(writer, 502, {"error": "the agent dashboard is not reachable"})
            uw.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
            await uw.drain()
            try:
                uhead = await asyncio.wait_for(ur.readuntil(b"\r\n\r\n"), timeout=180)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
                uw.close()
                return await respond(writer, 502, {"error": "the agent dashboard did not answer"})
            hl = uhead[:-4].decode("latin-1").split("\r\n")
            status = int(hl[0].split(" ")[1]) if len(hl[0].split(" ")) > 1 else 502
            self.up.absorb(hl[1:])
            if status == 401 and attempt == 0:
                uw.close()   # the front's own session lapsed: sign in again once (Hermes never ran the handler)
                if not await self.up.login(stale=sent_cookie):
                    return await respond(writer, 503, {"error": "the agent dashboard refused the front's sign-in"})
                continue
            out = [hl[0]] + [l for l in hl[1:] if l.split(":", 1)[0].strip().lower()
                             not in ("set-cookie", "connection", "keep-alive")]
            log("forward", app=app, method=method, path=target.split("?", 1)[0][:120], status=status)
            try:
                writer.write(("\r\n".join(out) + "\r\nConnection: close\r\n\r\n").encode("latin-1"))
                await writer.drain()
            except ConnectionError:
                uw.close()
                return
            await pipe(ur, writer)
            uw.close()
            return


# ---------------------------------------------------------------- owner listener (own tab, H2)

class Owner:
    def __init__(self, conf: Config):
        self.conf = conf

    def refusal(self, cfg: dict, method: str, pairs) -> str | None:
        own = cfg.get("owner") or {}
        h = dict(pairs)
        login = h.get("tailscale-user-login", "").lower()
        logins = {str(x).lower() for x in own.get("logins") or [] if x}
        if not login or login not in logins:
            return "The agent dashboard opens only for this Wizard's owner."
        host = h.get("host", "").lower()
        if host not in {str(x).lower() for x in own.get("hosts") or []}:
            return "The agent dashboard opens in its own tab, not at this address."
        dest, site, mode = h.get("sec-fetch-dest", ""), h.get("sec-fetch-site", ""), h.get("sec-fetch-mode", "")
        if dest in FRAME_DESTS:
            return "The agent dashboard never opens inside another page."
        origin = h.get("origin")
        if origin is not None and (origin == "null" or urllib.parse.urlsplit(origin).netloc.lower() != host):
            return "The agent dashboard answers only its own page."
        if site in ("cross-site", "same-site") and not (mode == "navigate" and dest == "document"
                                                         and method in ("GET", "HEAD")):
            return "The agent dashboard answers only its own page."
        return None

    async def handle(self, reader, writer) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
            if len(head) > MAX_HEAD:
                return await respond(writer, 400, {"error": "request head too large"})
            method, target, ver, pairs = parse_head(head[:-4])
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError,
                ConnectionError):
            writer.close()
            return
        cfg = self.conf.get()
        why = self.refusal(cfg, method, pairs)
        if why:
            log("owner-refused", why=why, path=target.split("?", 1)[0][:80])
            return await respond(writer, 403, {"error": why}, OWN_TAB_HEADERS)
        up = cfg.get("upstream") or {}
        upgrade = any(k == "upgrade" for k, _ in pairs)
        drop = ("host", "origin", "referer", "connection", "keep-alive", "x-wizard-grant", "authorization")
        fwd = [(k, v) for k, v in pairs if k not in drop and not k.startswith(NETWORK_PREFIXES)
               and k not in NETWORK_EXACT and not k.startswith("x-wizard-")]
        lines = [f"{method} {target} {ver}", f"Host: {up.get('host')}:{up.get('port')}"]
        lines += [f"{k}: {v}" for k, v in fwd]
        lines.append("Connection: Upgrade" if upgrade else "Connection: close")
        try:
            ur, uw = await open_upstream(cfg, (cfg.get("source") or {}).get("owner"))
        except (OSError, asyncio.TimeoutError):
            return await respond(writer, 502, {"error": "the agent dashboard is not reachable"}, OWN_TAB_HEADERS)
        uw.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await uw.drain()
        upload = asyncio.ensure_future(pipe(reader, uw))
        try:
            uhead = await ur.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            upload.cancel()
            writer.close()
            return
        keep = [l for l in uhead[:-4].decode("latin-1").split("\r\n") if l and not l.lower().startswith(
            ("x-frame-options:", "cross-origin-resource-policy:", "cross-origin-opener-policy:"))]
        writer.write(("\r\n".join(keep) + "\r\n" + OWN_TAB_HEADERS + "\r\n").encode("latin-1"))
        await asyncio.gather(upload, pipe(ur, writer))


# ---------------------------------------------------------------- main

async def serve(path: str, peers: Peers | None = None, ready=None):
    conf = Config(path)
    cfg = conf.get()
    if not cfg.get("listen"):
        raise SystemExit("host dashboard front: config has no listen address")
    upstream = Upstream(conf)
    consumers = Consumers(conf, upstream, peers or Peers())
    servers = [await asyncio.start_server(consumers.handle, cfg["listen"]["host"], int(cfg["listen"]["port"]),
                                          limit=MAX_HEAD + 4)]
    consumers.port = servers[0].sockets[0].getsockname()[1]
    own = cfg.get("owner") or {}
    if own.get("listen"):
        servers.append(await asyncio.start_server(Owner(conf).handle, own["listen"]["host"],
                                                  int(own["listen"]["port"]), limit=MAX_HEAD + 4))
    log("listening", consumers=f"{cfg['listen']['host']}:{cfg['listen']['port']}",
        owner=f"{own['listen']['host']}:{own['listen']['port']}" if own.get("listen") else "")
    if ready:
        ready(servers, consumers, upstream)
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit("usage: python3 -m wam.host_dashboard_front <config.json>")
    asyncio.run(serve(argv[0]))


if __name__ == "__main__":
    main()
