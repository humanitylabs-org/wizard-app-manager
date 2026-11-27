"""Thin client for the stock Runtipi API (v4). The App Manager never edits Runtipi's files or database."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def cli_token(secret: str, ttl: int = 600) -> str:
    """The same HS256 token runtipi-cli mints (sub=cli), so we use Runtipi's own supported auth path."""
    now = int(time.time())
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"sub": "cli", "iat": now, "nbf": now, "exp": now + ttl}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


class RuntipiError(RuntimeError):
    pass


class Runtipi:
    def __init__(self, base: str, secret: str, fallback: str | None = None, root: Path | None = None):
        self.base = base.rstrip("/")
        # The Runtipi root this client belongs to: with the stock-Runtipi switch on, wam answers the fork-only
        # grants and connections routes itself from the files there (wam/stock_grants.py, wizard-os#822).
        self.root = Path(root) if root is not None else None
        self.secret = secret
        # JWT_SECRET when `secret` is Runtipi 4.12's CLI key (WAM#839): a 401 with the
        # key is retried once with it, so a key rotated since it was read never locks wam out while the fork accepts both.
        self.fallback = fallback if fallback and fallback != secret else None

    @classmethod
    def from_install(cls, root: Path) -> "Runtipi":
        env = read_env(root / ".env")
        host = env.get("INTERNAL_IP") or "localhost"
        port = env.get("NGINX_PORT") or "80"
        if not env.get("JWT_SECRET"):
            raise RuntipiError(f"JWT_SECRET missing in {root / '.env'}")
        from .runtipi_key import secret
        key = secret(root, env["JWT_SECRET"])
        return cls(f"http://{host}:{port}/api", key, env["JWT_SECRET"], root=root)

    def ensure_operator(self, cred_file: Path) -> bool:
        """A fresh Runtipi has no operator, so CLI tokens map to nobody (401). Create one, once.

        The password is random and saved 0600 next to apps.json so the owner can sign in to Runtipi's own
        dashboard if they ever need it. Returns True if an operator was created now.
        """
        # A Runtipi that is still starting (right after the installer recreates it, wam#937) answers 502/404 while
        # its proxy route comes up, and 401 to the CLI token while its database is not ready: the auth middleware
        # looks up the operator, swallows the error and serves the request unauthenticated. So wait out the first,
        # and only call a 401 "no operator" once it repeats.
        waited, unauth, t0 = 0, 0, time.monotonic()

        def left(budget: int) -> float:
            # Counted naps (tests nap instantly) and the clock both bound the wait: a Runtipi that accepts the
            # connection but stalls must not stretch 2 minutes into 40 request timeouts (Codex P2).
            return max(0.0, min(budget - waited, budget - (time.monotonic() - t0)))

        while True:
            code, ans = self._probe(max(1.0, min(self.PROBE_TIMEOUT, left(self.READY_WAIT))))
            if code == 200:
                return False
            if code == 401:
                unauth += 1
                if unauth >= self.AUTH_TRIES:
                    break
            elif self._starting(code):
                unauth = 0
                if not left(self.READY_WAIT):
                    raise RuntipiError(f"Runtipi did not answer within {self.READY_WAIT} s: {ans}")
            else:
                raise RuntipiError(ans)
            self.sleep(self.STEP)
            waited += self.STEP
        import secrets
        user = "owner@wizard.local"
        pw = secrets.token_urlsafe(24)
        while True:
            r = urllib.request.Request(f"{self.base}/auth/register", json.dumps({"username": user, "password": pw}).encode(),
                                       {"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(r, timeout=max(1.0, min(30.0, left(self.READY_WAIT)))).read()
                break
            except urllib.error.HTTPError as e:
                code, reg = e.code, f"{e.code} {e.read()[:300]!r}"
            except OSError as e:  # URLError, refused, timeout: still starting
                code, reg = None, f"{type(e).__name__}: {e}"
            if code in (400, 403, 409) and "ALREADY_EXISTS" in reg:
                return self._operator_exists(ans, reg)
            if not self._starting(code) or not left(self.READY_WAIT):
                raise RuntipiError(f"Runtipi has no usable operator and register failed: {reg}") from None
            self.sleep(self.STEP)
            waited += self.STEP
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(json.dumps({"username": user, "password": pw}) + "\n")
        cred_file.chmod(0o600)
        self.req("GET", "/marketplace/all")
        return True

    # ensure_operator's waits (wam#937): 2 min for a starting Runtipi, a 401 must repeat 5 times (3 s apart), and
    # 60 s for an operator that register says exists. Tests set `sleep` to a no-op.
    STEP = 3
    READY_WAIT = 120
    AUTH_TRIES = 5
    EXISTS_WAIT = 60
    PROBE_TIMEOUT = 10
    sleep = staticmethod(time.sleep)

    @staticmethod
    def _starting(code: int | None) -> bool:
        """No answer, a missing proxy route (404) or a server error: Runtipi is (re)starting."""
        return code is None or code == 404 or code >= 500

    def _probe(self, timeout: float = 10) -> tuple[int | None, str]:
        """(HTTP status or None when nothing answered, what it said) for GET /marketplace/all with the CLI token."""
        try:
            self.req("GET", "/marketplace/all", timeout=timeout)
            return 200, "200"
        except RuntipiError as e:
            m = re.search(r"-> (\d{3})", str(e))
            return (int(m.group(1)) if m else None), str(e)
        except OSError as e:  # URLError (refused, reset), timeout
            return None, f"GET /marketplace/all: {type(e).__name__}: {e}"

    def _operator_exists(self, first: str, reg: str) -> bool:
        """Register says the operator exists, so the 401s came from a Runtipi still starting: wait for it."""
        waited, t0 = 0, time.monotonic()
        while True:
            left = max(0.0, min(self.EXISTS_WAIT - waited, self.EXISTS_WAIT - (time.monotonic() - t0)))
            code, ans = self._probe(max(1.0, min(self.PROBE_TIMEOUT, left)))
            if code == 200:
                return False
            if not left:
                raise RuntipiError(f"Runtipi refuses the CLI token ({first}; after {self.EXISTS_WAIT} s: {ans}) "
                                   f"although register says an operator exists ({reg})")
            self.sleep(self.STEP)
            waited += self.STEP

    def req(self, method: str, path: str, body: dict | None = None, timeout: int = 60):
        if self.root is not None:
            from . import stock_grants
            if stock_grants.handles(path):
                g = stock_grants.for_runtipi(self)
                if g is not None:
                    try:
                        return stock_grants.handle(g, method, path, body)
                    except stock_grants.GrantError as e:
                        raise RuntipiError(f"{method} {path} -> 400: {str(e)[:400]!r}") from None
        data = json.dumps(body).encode() if body is not None else None
        for secret in (self.secret, self.fallback):
            if secret is None:
                continue
            r = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
            r.add_header("Authorization", f"Bearer {cli_token(secret)}")
            if data is not None:
                r.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(r, timeout=timeout) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 401 and secret is self.secret and self.fallback:
                    continue
                raise RuntipiError(f"{method} {path} -> {e.code}: {e.read()[:400]!r}") from None
        return json.loads(raw) if raw else {}

    # stores
    def stores(self) -> list[dict]:
        return self.req("GET", "/marketplace/all").get("appStores", [])

    def ensure_store(self, name: str, url: str) -> dict:
        for s in self.stores():
            if s.get("slug") == name or s.get("name") == name:
                if s.get("url") != url:
                    raise RuntipiError(f"store '{name}' already points at {s.get('url')}; remove it in Runtipi settings or pick another store_name")
                if not s.get("enabled"):
                    self.req("PATCH", f"/marketplace/{s['slug']}", {"name": s["name"], "enabled": True})
                return s
        return self.req("POST", "/marketplace/create", {"name": name, "url": url})

    def pull(self) -> dict:
        return self.req("POST", "/marketplace/pull", timeout=300)

    # apps
    def installed(self) -> list[dict]:
        return self.req("GET", "/apps/installed").get("installed", [])

    def app(self, urn: str) -> dict:
        return self.req("GET", f"/apps/{urn}")

    def install(self, urn: str, form: dict | None = None) -> dict:
        return self.req("POST", f"/app-lifecycle/{urn}/install", form or {}, timeout=120)

    def update(self, urn: str, perform_backup: bool = True) -> dict:
        body = {"performBackup": perform_backup}
        if urn.split(':', 1)[0] == 'hermes-agent':
            from .hermes_lock import authorize
            body['hermesPromotion'] = authorize()
        return self.req("PATCH", f"/app-lifecycle/{urn}/update", body, timeout=120)

    def start(self, urn: str) -> dict:
        return self.req("POST", f"/app-lifecycle/{urn}/start", {}, timeout=120)

    def stop(self, urn: str) -> dict:
        return self.req("POST", f"/app-lifecycle/{urn}/stop", {}, timeout=120)

    def restart(self, urn: str) -> dict:
        return self.req("POST", f"/app-lifecycle/{urn}/restart", {}, timeout=120)

    def uninstall(self, urn: str, remove_backups: bool = False) -> dict:
        return self.req("DELETE", f"/app-lifecycle/{urn}/uninstall", {"removeBackups": remove_backups}, timeout=120)

    # per-app backups (Runtipi's own: a tar.gz of app-data/, app/ and user-config/, made and restored as root by the
    # Runtipi container with the app stopped; see packages/backend/src/modules/backups/backup.manager.ts)
    def backup_app(self, urn: str) -> dict:
        return self.req("POST", f"/backups/{urn}/backup", {}, timeout=120)

    def list_backups(self, urn: str) -> list[dict]:
        """Newest first: [{id (file name), size, date (ms)}]."""
        return self.req("GET", f"/backups/{urn}?page=1&pageSize=1000").get("data") or []

    def restore_backup(self, urn: str, filename: str) -> dict:
        return self.req("POST", f"/backups/{urn}/restore", {"filename": filename}, timeout=120)

    def delete_backup(self, urn: str, filename: str) -> dict:
        return self.req("DELETE", f"/backups/{urn}", {"filename": filename})

    def download_backup(self, urn: str, filename: str, dst: Path, timeout: int = 3600) -> int:
        """Stream one backup archive to `dst`; returns its size."""
        r = urllib.request.Request(f"{self.base}/backups/{urn}/{urllib.parse.quote(filename)}/download")
        r.add_header("Authorization", f"Bearer {cli_token(self.secret)}")
        n = 0
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp, open(dst, "wb") as out:
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
                    n += len(chunk)
        except urllib.error.HTTPError as e:
            raise RuntipiError(f"GET backup {urn}/{filename} -> {e.code}: {e.read()[:300]!r}") from None
        return n

    def upload_backup(self, urn: str, src: Path, filename: str, timeout: int = 3600) -> dict:
        """Upload an archive as `filename` (multipart, streamed from disk; Runtipi itself buffers it in memory)."""
        boundary = "wizard-" + hashlib.sha256(f"{filename}{time.time()}".encode()).hexdigest()[:24]
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                f"Content-Type: application/gzip\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        size = Path(src).stat().st_size

        def body():
            yield head
            with open(src, "rb") as f:
                while chunk := f.read(1 << 20):
                    yield chunk
            yield tail
        r = urllib.request.Request(f"{self.base}/backups/{urn}/upload", data=body(), method="POST")
        r.add_header("Authorization", f"Bearer {cli_token(self.secret)}")
        r.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        r.add_header("Content-Length", str(len(head) + size + len(tail)))
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise RuntipiError(f"upload backup {urn} -> {e.code}: {e.read()[:300]!r}") from None
        return json.loads(raw) if raw else {}

    def wait_status(self, urn: str, want: set[str], timeout: int = 900) -> str:
        deadline = time.time() + timeout
        status = "unknown"
        while time.time() < deadline:
            try:
                info = self.app(urn)
                status = ((info.get("app") or {}).get("status")) or "missing"
            except RuntipiError:
                status = "missing"
            if status in want:
                return status
            time.sleep(3)
        return status
