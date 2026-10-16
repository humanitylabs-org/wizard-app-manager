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
