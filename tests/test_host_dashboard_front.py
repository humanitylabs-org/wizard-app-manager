"""Browser boundary: the Wizard front for the alongside host Hermes dashboard (wizard-os issue 386, default-off).

App Manager side (ensure): Hermes moves to a host-only address, the front takes the tailnet address, consumer apps
get their OWN key instead of the dashboard password, the password is rotated when the front takes over, and the
owner gets an own-tab listener. Front side (real HTTP): a stand-in for Hermes' login + per-IP limiter (10 attempts a
minute, successes included, like hermes_cli/dashboard_auth/routes.py) behind the real front module.
"""
import asyncio
import hashlib
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from wam import cli, host_dashboard as hd, host_dashboard_front as front

from test_host_dashboard import box

OWNER = {"hosts": ["box.tail0.ts.net:9120"], "logins": ["owner@example.com"]}


def _ok(*a):
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def run(tmp_path, monkeypatch, boundary, owner=None, installed=("wizard-chatgpt",)):
    rt = tmp_path / "runtipi" if (tmp_path / "runtipi").is_dir() else box(tmp_path)
    calls = []
    monkeypatch.setattr(hd, "tailnet_ip", lambda: "100.64.0.7")
    monkeypatch.setattr(hd, "_systemctl", lambda *a: calls.append(a) or _ok())
    out = hd.ensure(tmp_path / "wos", rt, "wizardapps", set(installed), "/usr/bin/hermes", "/home/o/.hermes",
                    cli.write_user_env, unit_dir=tmp_path / "units", boundary=boundary, owner=owner)
    return rt, out, calls


def app_env(rt, app="wizard-chatgpt"):
    return hd.read_env(rt / "user-config" / "wizardapps" / app / "app.env")


def test_boundary_moves_hermes_off_the_tailnet_and_apps_get_their_own_key(tmp_path, monkeypatch):
    wh = tmp_path / "wos"
    rt, before, _ = run(tmp_path, monkeypatch, False)
    old_password = hd.read_env(hd.env_path(wh))[hd.PASSWORD_VAR]
    assert app_env(rt)[hd.PASSWORD_VAR] == old_password          # today: the app holds the dashboard password
    rt, out, calls = run(tmp_path, monkeypatch, True, OWNER)
    assert out["state"] == "ready" and out["front"] == "on" and out["url"] == "http://100.64.0.7:9119"
    unit = (tmp_path / "units" / hd.UNIT).read_text()
    assert "--host 127.0.0.2 --port 19119" in unit and "100.64.0.7" not in unit and "127.0.0.1" not in unit
    funit = (tmp_path / "units" / hd.FRONT_UNIT).read_text()
    assert "-m wam.host_dashboard_front" in funit and str(hd.front_config_path(wh)) in funit
    creds = hd.read_env(hd.env_path(wh))
    assert creds[hd.PASSWORD_VAR] != old_password                 # rotated when the front took over
    key = (hd.keys_dir(wh) / "wizard-chatgpt.key").read_text().strip()
    assert len(key) == 64 and (hd.keys_dir(wh) / "wizard-chatgpt.key").stat().st_mode & 0o777 == 0o600
    env = app_env(rt)
    assert env[hd.PASSWORD_VAR] == key and creds[hd.PASSWORD_VAR] not in env.values()
    assert env["WIZARD_CHATGPT_HERMES_URL"] == "http://100.64.0.7:9119"   # same address: the front's
    assert out["changed_apps"] == ["wizard-chatgpt"]
    cfg_path = hd.front_config_path(wh)
    assert cfg_path.stat().st_mode & 0o777 == 0o600
    cfg = json.loads(cfg_path.read_text())
    assert cfg["listen"] == {"host": "100.64.0.7", "port": 9119}
    assert cfg["upstream"] == {"host": "127.0.0.2", "port": 19119}
    assert cfg["login"]["password"] == creds[hd.PASSWORD_VAR]     # only the front holds it
    assert key not in cfg_path.read_text()                         # key hashes only
    row = cfg["consumers"][hashlib.sha256(key.encode()).hexdigest()]
    assert row["app"] == "wizard-chatgpt" and ["GET", "/api/env"] not in row["allow"]
    assert cfg["owner"] == {"listen": {"host": "127.0.0.1", "port": 9120}, **{k: sorted(v) for k, v in OWNER.items()}}
    assert ("restart", hd.UNIT) in calls and ("enable", hd.FRONT_UNIT) in calls
    # A rerun keeps password, key and config; nothing restarts or changes.
    rt, again, calls = run(tmp_path, monkeypatch, True, OWNER)
    assert again["changed_apps"] == [] and hd.read_env(hd.env_path(wh)) == creds
    assert ("restart", hd.UNIT) not in calls and ("restart", hd.FRONT_UNIT) not in calls
    assert (hd.keys_dir(wh) / "wizard-chatgpt.key").read_text().strip() == key


def test_turning_the_boundary_off_removes_the_front_and_its_keys(tmp_path, monkeypatch):
    wh = tmp_path / "wos"
    run(tmp_path, monkeypatch, True)
    rt, out, calls = run(tmp_path, monkeypatch, False)
    assert out["front"] == "off" and ("disable", "--now", hd.FRONT_UNIT) in calls
    assert not (tmp_path / "units" / hd.FRONT_UNIT).exists() and not hd.front_config_path(wh).exists()
    assert not list(hd.keys_dir(wh).glob("*.key")) and not hd.mode_path(wh).exists()
    assert "--host 100.64.0.7 --port 9119" in (tmp_path / "units" / hd.UNIT).read_text()
    assert app_env(rt)[hd.PASSWORD_VAR] == hd.read_env(hd.env_path(wh))[hd.PASSWORD_VAR]


def test_uninstalled_consumer_loses_its_key(tmp_path, monkeypatch):
    wh = tmp_path / "wos"
    rt = box(tmp_path)
    d = rt / "apps" / "wizardapps" / "wizard-claude"
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text("services:\n  e:\n    environment:\n      WIZARD_CLAUDE_HERMES_URL: "
                                          "${WIZARD_CLAUDE_HERMES_URL:-http://hermes-agent:9119}\n")
    (d / "config.json").write_text('{"form_fields":[{"env_variable":"HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"}]}')
    run(tmp_path, monkeypatch, True, installed=("wizard-chatgpt", "wizard-claude"))
    cfg = json.loads(hd.front_config_path(wh).read_text())
    assert sorted(r["app"] for r in cfg["consumers"].values()) == ["wizard-chatgpt", "wizard-claude"]
    claude = next(r for r in cfg["consumers"].values() if r["app"] == "wizard-claude")
    assert all("/provider-accounts/claude" in p for _, p in claude["allow"])
    run(tmp_path, monkeypatch, True, installed=("wizard-chatgpt",))
    cfg = json.loads(hd.front_config_path(wh).read_text())
    assert [r["app"] for r in cfg["consumers"].values()] == ["wizard-chatgpt"]
    assert not (hd.keys_dir(wh) / "wizard-claude.key").exists()


def test_install_form_gets_the_apps_key_not_the_password(tmp_path, monkeypatch):
    from wam import alongside
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "wos"))
    monkeypatch.setenv("RUNTIPI_ROOT", str(tmp_path / "runtipi"))
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    c = cli.Config()
    monkeypatch.setattr(alongside, "load", lambda home: {"agent": {}})
    hd.env_path(c.home).parent.mkdir(parents=True)
    hd.env_path(c.home).write_text(f"{hd.PASSWORD_VAR}=the-real-dashboard-password\n")
    info = {"form_fields": [{"env_variable": hd.PASSWORD_VAR, "type": "password", "required": True}]}
    monkeypatch.delenv("WIZARD_BROWSER_BOUNDARY", raising=False)
    form, _ = cli.build_form(c, "wizard-chatgpt", info, {})
    assert form[hd.PASSWORD_VAR] == "the-real-dashboard-password"       # default-off: unchanged
    monkeypatch.setenv("WIZARD_BROWSER_BOUNDARY", "1")
    form, _ = cli.build_form(c, "wizard-chatgpt", info, {})
    assert form[hd.PASSWORD_VAR] == hd.app_key(c.home, "wizard-chatgpt") != "the-real-dashboard-password"


def test_owner_tab_is_published_only_under_the_boundary(tmp_path, monkeypatch):
    from wam import alongside
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "wos"))
    monkeypatch.setenv("RUNTIPI_ROOT", str(tmp_path / "runtipi"))
    c = cli.Config()
    monkeypatch.setattr(alongside, "load", lambda home: {"agent": {"hermes_home": "/h"}})
    monkeypatch.setattr(alongside, "hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"wizard-chatgpt"})
    seen = {}
    monkeypatch.setattr(hd, "ensure", lambda *a, **k: seen.update(k) or {"state": "ready", "owner_tab": bool(k["owner"])})
    monkeypatch.setattr(hd, "publish_owner_tab", lambda: "published")
    removed = []
    monkeypatch.setattr(hd, "unpublish_owner_tab", lambda: removed.append(1) or "absent")
    monkeypatch.setattr(cli, "host_dashboard_owner", lambda c: dict(OWNER))
    monkeypatch.setenv("WIZARD_BROWSER_BOUNDARY", "1")
    out = cli.host_dashboard_cmd(c, rt=object())
    assert seen["boundary"] is True and out["owner_tab"] == {"state": "published", "url": "https://box.tail0.ts.net:9120"}
    monkeypatch.delenv("WIZARD_BROWSER_BOUNDARY")
    out = cli.host_dashboard_cmd(c, rt=object())
    assert seen["boundary"] is False and seen["owner"] is None and removed == [1]


def test_owner_tab_serve_entry_never_takes_someone_elses_port(monkeypatch):
    from wam import links
    ran = []
    monkeypatch.setattr(links, "_run", lambda args, timeout=30: ran.append(args) or _ok())
    monkeypatch.setattr(links, "serve_status", lambda: {"Web": {"box:9120": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5000"}}}}})
    assert hd.publish_owner_tab() == "taken" and hd.unpublish_owner_tab() == "not ours" and ran == []
    monkeypatch.setattr(links, "serve_status", lambda: {"Web": {}})
    assert hd.publish_owner_tab() == "published"
    assert ran == [["tailscale", "serve", "--bg", "--https=9120", "http://127.0.0.1:9120"]]


# ---------------------------------------------------------------- the front over real HTTP

def test_paths_and_peers():
    allow = [["GET", "/api/model/info"], ["POST", "/api/plugins/wizard-os/provider-accounts/chatgpt/*"]]
    assert front.path_allowed(allow, "GET", "/api/model/info")
    assert not front.path_allowed(allow, "POST", "/api/model/info")
    assert front.path_allowed(allow, "POST", "/api/plugins/wizard-os/provider-accounts/chatgpt/operations")
    for bad in ("/api/plugins/wizard-os/provider-accounts/chatgpt/", "/api/plugins/wizard-os/provider-accounts/chatgpt/../../env",
                "/api/plugins/wizard-os/provider-accounts/chatgpt/%2e%2e/x", "/api/model/info/", "/api//model/info",
                "/api/plugins/wizard-os/provider-accounts/chatgptx/a"):
        assert not front.path_allowed(allow, "POST" if "accounts" in bad else "GET", bad), bad
    import ipaddress as ip
    peers = front.Peers(reader=lambda: ({ip.ip_address("100.64.0.7"), ip.ip_address("192.168.1.5")},
                                        [ip.ip_network("172.18.0.0/16")]))
    assert peers.allowed("127.0.0.1") and peers.allowed("100.64.0.7") and peers.allowed("172.18.0.5")
    assert not peers.allowed("100.101.1.2")       # another tailnet device
    assert not peers.allowed("192.168.1.20")      # a LAN neighbour (eth0 is not a container bridge)
    assert not peers.allowed("garbage")


class Hermes(BaseHTTPRequestHandler):
    """Stand-in for stock Hermes' dashboard auth: POST /auth/password-login with a per-client-IP budget of 10 attempts
    a minute (successes included), session cookie required on /api/*. Records what reached it."""
    password = "rotated-dashboard-password"
    attempts: dict = {}
    seen: list = []
    sessions: set = set()

    def log_message(self, *a):
        pass

    def _send(self, code, obj, cookies=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        ip = self.client_address[0]
        Hermes.seen.append({"ip": ip, "method": self.command, "path": self.path, "headers": dict(self.headers)})
        if self.path == "/auth/password-login":
            bucket = [t for t in Hermes.attempts.get(ip, []) if t > time.monotonic() - 60]
            if len(bucket) >= 10:
                return self._send(429, {"detail": "Too many login attempts. Try again shortly."})
            Hermes.attempts[ip] = bucket + [time.monotonic()]
            if json.loads(body or b"{}").get("password") != Hermes.password:
                return self._send(401, {"detail": "Invalid credentials"})
            tok = f"at-{len(Hermes.sessions)}"
            Hermes.sessions.add(tok)
            return self._send(200, {"ok": True}, [f"hermes_session_at={tok}; HttpOnly; Path=/; SameSite=lax",
                                                  "hermes_session_rt=rt; HttpOnly; Path=/; SameSite=lax"])
        cookie = self.headers.get("Cookie") or ""
        if not any(f"hermes_session_at={t}" in cookie for t in Hermes.sessions):
            return self._send(401, {"detail": "Not authenticated"})
        self._send(200, {"path": self.path, "body": body.decode()}, ["hermes_session_at=" + cookie.split("hermes_session_at=")[1].split(";")[0] + "; Path=/"])

    do_GET = do_POST = do_DELETE = _go


@pytest.fixture
def stack(tmp_path):
    Hermes.attempts, Hermes.seen, Hermes.sessions = {}, [], set()
    up = ThreadingHTTPServer(("127.0.0.2", 0), Hermes)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    keys = {"wizard-chatgpt": "c" * 64, "wizard-claude": "d" * 64}
    cfg = hd.front_config("127.0.0.1", {hd.PASSWORD_VAR: Hermes.password}, keys, None)
    cfg["listen"]["port"] = 0
    cfg["upstream"]["port"] = up.server_address[1]
    cfg["owner"] = {"listen": {"host": "127.0.0.1", "port": 0}, "hosts": ["box.tail0.ts.net:9120"],
                    "logins": ["owner@example.com"]}
    path = tmp_path / "front.json"
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    got = {}
    loop = asyncio.new_event_loop()

    def ready(servers, consumers, upstream):
        got.update(port=servers[0].sockets[0].getsockname()[1], owner=servers[1].sockets[0].getsockname()[1],
                   upstream=upstream)
        done.set()
    done = threading.Event()
    task = {}

    def main():
        task["t"] = loop.create_task(front.serve(str(path), None, ready))
        try:
            loop.run_until_complete(task["t"])
        except asyncio.CancelledError:
            pass
    t = threading.Thread(target=main, daemon=True)
    t.start()
    assert done.wait(10)
    yield {"port": got["port"], "owner": got["owner"], "keys": keys, "path": path, "cfg": cfg, "up": got["upstream"]}
    up.shutdown()
    loop.call_soon_threadsafe(task["t"].cancel)
    t.join(5)


def call(port, method, path, headers=None, body=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    hd_ = {k.lower(): v for k, v in r.getheaders()}
    if r.msg.get_all("Set-Cookie"):
        hd_["set-cookie"] = ", ".join(r.msg.get_all("Set-Cookie"))
    out = (r.status, hd_, r.read())
    c.close()
    return out


def login(port, key, host=None):
    s, h, _ = call(port, "POST", "/auth/password-login", body={"provider": "basic", "username": "hermes", "password": key},
                   host=host)
    return s, (h.get("set-cookie") or "").split(";")[0]


def test_consumer_login_is_answered_by_the_front_with_the_apps_own_key(stack):
    p, key = stack["port"], stack["keys"]["wizard-chatgpt"]
    st, cookie = login(p, key)
    assert st == 200 and cookie.startswith(front.SESSION_COOKIE + "=")
    assert Hermes.seen == []                                  # the app's login never reaches Hermes
    s, h, b = call(p, "GET", "/api/model/info", {"Cookie": cookie, "Authorization": "Bearer x", "X-Other": "y"})
    assert s == 200 and json.loads(b)["path"] == "/api/model/info" and "set-cookie" not in h
    login_row, api_row = Hermes.seen
    assert login_row["path"] == "/auth/password-login" and login_row["ip"] == front_ip("consumers")
    assert api_row["ip"] == front_ip("consumers") and api_row["headers"]["Host"] == f"127.0.0.2:{stack['cfg']['upstream']['port']}"
    assert "hermes_session_at=at-0" in api_row["headers"]["Cookie"] and front.SESSION_COOKIE not in api_row["headers"]["Cookie"]
    assert "Authorization" not in api_row["headers"] and "X-Other" not in api_row["headers"]
    # A wrong key is refused by the front itself; Hermes sees nothing.
    assert login(p, "e" * 64)[0] == 401 and login(p, "the-dashboard-password")[0] == 401 and len(Hermes.seen) == 2


def front_ip(kind):
    return hd.SOURCES[kind]


def test_allowlist_markers_and_host(stack):
    p = stack["port"]
    gpt, claude = ({"X-Wizard-Grant": stack["keys"][a]} for a in ("wizard-chatgpt", "wizard-claude"))
    assert call(p, "GET", "/api/model/options", gpt)[0] == 200                   # header form works too
    assert call(p, "GET", "/api/env", gpt)[0] == 403                             # admin routes are on nobody's list
    assert call(p, "GET", "/api/config", gpt)[0] == 403
    assert call(p, "GET", "/api/model/info", claude)[0] == 403                   # each app only its own paths
    assert call(p, "POST", "/api/plugins/wizard-os/provider-accounts/chatgpt/operations", claude, {})[0] == 403
    assert call(p, "POST", "/api/plugins/wizard-os/provider-accounts/claude/operations", claude, {"a": 1})[0] == 200
    assert call(p, "GET", "/api/status")[0] == 401                               # no key: nothing, not even status
    assert call(p, "GET", "/api/model/info", {**gpt, "Origin": "https://evil.example"})[0] == 403
    assert call(p, "GET", "/api/model/info", {**gpt, "Origin": "null"})[0] == 403
    assert call(p, "GET", "/api/model/info", {**gpt, "Sec-Fetch-Site": "same-site"})[0] == 403
    assert call(p, "GET", "/api/model/info", {**gpt, "Tailscale-User-Login": "owner@example.com"})[0] == 403
    assert call(p, "GET", "/api/model/info", {**gpt, "X-Forwarded-For": "100.101.1.2"})[0] == 403
    assert call(p, "GET", "/api/model/info", gpt, host="attacker.example")[0] == 400
    assert call(p, "GET", "/api/plugins/wizard-os/provider-accounts/claude/%2e%2e/%2e%2e/env", claude)[0] == 403
    assert all(r["path"] not in ("/api/env", "/api/config", "/api/status") for r in Hermes.seen)


def test_consumers_can_never_rate_limit_the_dashboard_login(stack):
    """Today two app containers share Hermes' per-IP budget (10 a minute, successes included): a burst of wrong
    passwords, or just retries, gives the right password 429. Through the front Hermes sees one login."""
    p, key = stack["port"], stack["keys"]["wizard-chatgpt"]
    for _ in range(30):
        assert login(p, "f" * 64)[0] == 401
    for _ in range(15):
        st, cookie = login(p, key)
        assert st == 200
        assert call(p, "GET", "/api/model/info", {"Cookie": cookie})[0] == 200
    logins = [r for r in Hermes.seen if r["path"] == "/auth/password-login"]
    assert len(logins) == 1 and stack["up"].logins == 1


def test_front_renews_its_own_session_once_after_a_401(stack):
    p, gpt = stack["port"], {"X-Wizard-Grant": stack["keys"]["wizard-chatgpt"]}
    assert call(p, "GET", "/api/model/info", gpt)[0] == 200
    Hermes.sessions.clear()                                        # Hermes restarted: the front's session is gone
    assert call(p, "POST", "/api/model/set", gpt, {"model": "m"})[0] == 200
    assert [r["path"] for r in Hermes.seen].count("/auth/password-login") == 2


def test_revoked_key_stops_on_the_next_request(stack):
    p, key = stack["port"], stack["keys"]["wizard-chatgpt"]
    st, cookie = login(p, key)
    cfg = dict(stack["cfg"])
    cfg["consumers"] = {k: v for k, v in cfg["consumers"].items() if v["app"] != "wizard-chatgpt"}
    time.sleep(0.01)
    stack["path"].write_text(json.dumps(cfg))
    assert call(p, "GET", "/api/model/info", {"Cookie": cookie})[0] == 401
    assert call(p, "GET", "/api/model/info", {"X-Wizard-Grant": key})[0] == 401


def own(stack, method, path, headers=None, body=None, host="box.tail0.ts.net:9120"):
    return call(stack["owner"], method, path, {"Tailscale-User-Login": "owner@example.com", **(headers or {})}, body, host)


def test_owner_tab_is_own_origin_own_login_never_framed(stack):
    nav = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    s, h, _ = own(stack, "GET", "/api/status", nav)
    assert s == 401 and h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]
    assert Hermes.seen[-1]["ip"] == front_ip("owner") and "hermes_session" not in (Hermes.seen[-1]["headers"].get("Cookie") or "")
    origin = {"Origin": "https://box.tail0.ts.net:9120", "Sec-Fetch-Site": "same-origin"}
    s, h, _ = own(stack, "POST", "/auth/password-login", origin, {"provider": "basic", "username": "hermes",
                                                                   "password": Hermes.password})
    assert s == 200 and "hermes_session_at" in h["set-cookie"]               # the owner's own session, own origin
    row = Hermes.seen[-1]
    assert "Origin" not in row["headers"] and "Tailscale-User-Login" not in row["headers"]
    refused = [own(stack, "GET", "/", {**nav, "Sec-Fetch-Dest": "iframe"}),
               own(stack, "GET", "/api/config", {"Origin": "null"}),
               own(stack, "GET", "/api/config", {"Origin": "https://box.tail0.ts.net:8444", "Sec-Fetch-Site": "same-site"}),
               own(stack, "GET", "/api/config", {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}),
               own(stack, "GET", "/", nav, host="127.0.0.1:9120"),
               call(stack["owner"], "GET", "/", nav, host="box.tail0.ts.net:9120"),
               own(stack, "GET", "/", {**nav, "Tailscale-User-Login": "guest@example.com"})]
    assert [r[0] for r in refused] == [403] * 7
    assert all(r[1]["x-frame-options"] == "DENY" for r in refused)


def test_owner_typos_and_the_apps_use_different_login_buckets(stack):
    origin = {"Origin": "https://box.tail0.ts.net:9120", "Sec-Fetch-Site": "same-origin"}
    for _ in range(12):
        own(stack, "POST", "/auth/password-login", origin, {"provider": "basic", "username": "hermes", "password": "typo"})
    assert own(stack, "POST", "/auth/password-login", origin, {"provider": "basic", "username": "hermes",
                                                                "password": Hermes.password})[0] == 429
    # The owner locked only himself out for a minute; the apps still get in.
    assert call(stack["port"], "GET", "/api/model/info", {"X-Wizard-Grant": stack["keys"]["wizard-chatgpt"]})[0] == 200
