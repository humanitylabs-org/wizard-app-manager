"""Store Get: owner-only, same-origin, catalog-listed apps only, one install per app at a time."""
import threading
from wam import store_install as si

ORIG = {"https://box.tail1.ts.net:8796"}
GOOD = {"Tailscale-User-Login": "Owner@x.com", "Origin": "https://box.tail1.ts.net:8796",
        "X-Wizard-Store": "1", "Content-Type": "application/json"}


def test_request_checks():
    assert si.check_request(GOOD, "owner@x.com", ORIG) is None
    assert "Tailscale" in si.check_request(GOOD, "", ORIG)
    assert "owner" in si.check_request({**GOOD, "Tailscale-User-Login": "guest@x.com"}, "owner@x.com", ORIG)
    assert si.check_request({**GOOD, "Origin": "https://evil.example"}, "owner@x.com", ORIG) == "wrong origin"
    assert si.check_request({k: v for k, v in GOOD.items() if k != "X-Wizard-Store"}, "owner@x.com", ORIG) == "bad request"


def test_only_listed_apps_and_one_job_per_app(monkeypatch):
    si._jobs.clear()
    monkeypatch.setattr(si, "allowed", lambda c, a: a == "wisdom")
    monkeypatch.setattr(si, "_links", lambda c: None)
    gate, calls = threading.Event(), []

    def install(c, a):
        calls.append(a); gate.wait(5); return {"status": "running"}
    assert si.start(None, "../etc", install, set)[0] == 400
    assert si.start(None, "evil-app", install, set)[0] == 404
    code, job = si.start(None, "wisdom", install, set)
    assert code == 202 and job["state"] == "installing"
    assert si.start(None, "wisdom", install, set)[1] is job      # second tap joins
    gate.set()
    for _ in range(100):
        if job["state"] != "installing":
            break
        threading.Event().wait(0.02)
    assert job["state"] == "installed" and calls == ["wisdom"]
    assert si.start(None, "wisdom", install, lambda: {"wisdom"})[1]["state"] == "installed"


def test_failed_install_reports_reason(monkeypatch):
    si._jobs.clear()
    monkeypatch.setattr(si, "allowed", lambda c, a: True)

    def boom(c, a):
        raise SystemExit("x: install failed: pull access denied")
    code, job = si.start(None, "x", boom, set)
    for _ in range(100):
        if job["state"] != "installing":
            break
        threading.Event().wait(0.02)
    assert job["state"] == "failed" and "pull access denied" in job["error"]


def test_peer_uid_reads_proc(tmp_path, monkeypatch):
    rows = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:D431 0100007F:1E6F 01 00000000:00000000 00:00000000 00000000     0        0 1\n"
            "   1: 0100007F:D432 0100007F:1E6F 01 00000000:00000000 00:00000000 00000000  1000        0 2\n")
    import builtins
    real = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: __import__("io").StringIO(rows) if f == "/proc/net/tcp" else real(f, *a, **k))
    assert si.peer_uid(0xD431) == 0 and si.peer_uid(0xD432) == 1000 and si.peer_uid(1) is None


def test_route_targets_the_store_port():
    assert si.route(8796)[-1] == "http://127.0.0.1:7791" and "--set-path=/wizard-os-home/store-install" in si.route(8796)


def _serve_store(monkeypatch, cfg):
    import http.client, json as _j
    from http.server import ThreadingHTTPServer
    from wam import cli
    monkeypatch.setattr(si, "peer_uid", lambda p, *args: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "o@x")
    monkeypatch.setattr(si, "served_origins", lambda h: {"https://b"})
    monkeypatch.setattr(si, "check_request", lambda *a: None)
    monkeypatch.setattr(cli.StoreHandler, "cfg", cfg, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.StoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    body = _j.dumps({"source": "standard", "repo": "org/app", "commit": "a" * 40, "operation": "install"})
    h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    h.request("POST", "/app", body, {"Content-Type": "application/json", "Content-Length": str(len(body)), "Tailscale-User-Login": "o@x"})
    r = h.getresponse(); out = (r.status, _j.loads(r.read() or b"{}")); srv.shutdown(); return out


def test_store_get_without_docker_access_still_resolves(monkeypatch, tmp_path):
    """A manager service that can't run `docker info` (fresh box, docker group not active yet) used to crash the
    request, which the phone showed as "Install failed (502)"."""
    import subprocess, platform
    from wam import index, test_catalog
    real = subprocess.run
    def no_docker(argv, **kw):
        if "docker" in argv[:3]:
            raise subprocess.CalledProcessError(1, argv, "", "permission denied")
        return real(argv, **kw)
    monkeypatch.setattr(subprocess, "run", no_docker)
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert test_catalog.host_architecture() == "amd64"
    monkeypatch.setattr(index, "resolve", lambda i, r: {"id": "app", "repo": "org/app", "commit": "a" * 40,
                                                       "architectures": ["amd64"]})
    monkeypatch.setattr(si, "start", lambda *a, **k: (202, {"app": "app", "state": "installing"}))
    class C: index = "x"; home = tmp_path
    code, body = _serve_store(monkeypatch, C())
    assert code == 202 and body["state"] == "installing"


def test_store_get_unexpected_error_answers_json(monkeypatch, tmp_path):
    from wam import store_selection
    def boom(*a):
        raise KeyError("surprise")
    monkeypatch.setattr(store_selection, "resolve", boom)
    class C: index = "x"; home = tmp_path
    code, body = _serve_store(monkeypatch, C())
    assert code == 500 and "Update Wizard" in body["error"]


def test_installed_is_reported_only_after_links_published(monkeypatch):
    """Fresh boxes: the Store re-reads the shell's apps as soon as the job says `installed`, so the same-origin
    route and registry rewrite (`_links`) must already be done by then, or the Store shows '✓ Installed', not Open."""
    si._jobs.clear()
    monkeypatch.setattr(si, "allowed", lambda c, a: True)
    seen = []
    monkeypatch.setattr(si, "_links", lambda c: seen.append(dict(si._jobs["mr"])))
    code, job = si.start(None, "mr", lambda c, a: {"status": "running"}, set)
    for _ in range(100):
        if job["state"] != "installing":
            break
        threading.Event().wait(0.02)
    assert job["state"] == "installed"
    assert seen and seen[0]["state"] == "installing"
