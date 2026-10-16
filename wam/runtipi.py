"""Thin client for the stock Runtipi API (v4). The App Manager never edits Runtipi's files or database."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
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
    def __init__(self, base: str, secret: str):
        self.base = base.rstrip("/")
        self.secret = secret

    @classmethod
    def from_install(cls, root: Path) -> "Runtipi":
        env = read_env(root / ".env")
        host = env.get("INTERNAL_IP") or "localhost"
        port = env.get("NGINX_PORT") or "80"
        if not env.get("JWT_SECRET"):
            raise RuntipiError(f"JWT_SECRET missing in {root / '.env'}")
        return cls(f"http://{host}:{port}/api", env["JWT_SECRET"])

    def ensure_operator(self, cred_file: Path) -> bool:
        """A fresh Runtipi has no operator, so CLI tokens map to nobody (401). Create one, once.

        The password is random and saved 0600 next to apps.json so the owner can sign in to Runtipi's own
        dashboard if they ever need it. Returns True if an operator was created now.
        """
        try:
            self.req("GET", "/marketplace/all")
            return False
        except RuntipiError as e:
            if "-> 401" not in str(e):
                raise
        import secrets
        user = "owner@wizard.local"
        pw = secrets.token_urlsafe(24)
        r = urllib.request.Request(f"{self.base}/auth/register", json.dumps({"username": user, "password": pw}).encode(),
                                   {"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(r, timeout=30).read()
        except urllib.error.HTTPError as e:
            raise RuntipiError(f"Runtipi has no usable operator and register failed: {e.code} {e.read()[:300]!r}") from None
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(json.dumps({"username": user, "password": pw}) + "\n")
        cred_file.chmod(0o600)
        self.req("GET", "/marketplace/all")
        return True

    def req(self, method: str, path: str, body: dict | None = None, timeout: int = 60):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        r.add_header("Authorization", f"Bearer {cli_token(self.secret)}")
        if data is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
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
        return self.req("PATCH", f"/app-lifecycle/{urn}/update", {"performBackup": perform_backup}, timeout=120)

    def start(self, urn: str) -> dict:
        return self.req("POST", f"/app-lifecycle/{urn}/start", {}, timeout=120)

    def stop(self, urn: str) -> dict:
        return self.req("POST", f"/app-lifecycle/{urn}/stop", {}, timeout=120)

    def restart(self, urn: str) -> dict:
        return self.req("POST", f"/app-lifecycle/{urn}/restart", {}, timeout=120)

    def uninstall(self, urn: str, remove_backups: bool = False) -> dict:
        return self.req("DELETE", f"/app-lifecycle/{urn}/uninstall", {"removeBackups": remove_backups}, timeout=120)

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
