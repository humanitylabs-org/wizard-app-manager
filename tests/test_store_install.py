"""Store Get: owner-only, same-origin, catalog-listed apps only, one install per app at a time."""
import threading
from wam import store_install as si

ORIG = {"https://box.tail1.ts.net:8796"}
GOOD = {"X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": "Owner@x.com", "Origin": "https://box.tail1.ts.net:8796",
        "X-Wizard-Store": "1", "Content-Type": "application/json"}


def test_request_checks():
    assert si.check_request(GOOD, "owner@x.com", ORIG) is None
    assert "Tailscale" in si.check_request(GOOD, "", ORIG)
    assert "owner" in si.check_request({**GOOD, "Tailscale-User-Login": "guest@x.com"}, "owner@x.com", ORIG)
    assert si.check_request({**GOOD, "Origin": "https://evil.example"}, "owner@x.com", ORIG) == "wrong origin"
    assert si.check_request({k: v for k, v in GOOD.items() if k != "X-Wizard-Store"}, "owner@x.com", ORIG) == "bad request"


def test_changes_from_the_box_itself_are_refused():
    """Serve gives a connection from the node's own address the owner's login, and every app container reaches
    Serve from that address: a POST from there carries every header a phone sends, and must not act as the owner."""
    box = {"100.64.0.1"}   # this node's tailnet address (conftest)
    assert si.check_request(GOOD, "owner@x.com", ORIG, box) is None                     # the owner's phone
    for xff in ("100.64.0.1", "100.64.0.9, 100.64.0.1", "127.0.0.1", "::1", ""):
        assert si.check_request({**GOOD, "X-Forwarded-For": xff}, "owner@x.com", ORIG, box) == si.FROM_BOX, xff
    assert si.check_request({k: v for k, v in GOOD.items() if k != "X-Forwarded-For"}, "owner@x.com", ORIG,
                            box) == si.FROM_BOX
    # Tailscale didn't say which addresses are this node's: nothing can be told apart, so refuse
    assert si.check_request(GOOD, "owner@x.com", ORIG, set()) == si.FROM_BOX
    # the address comes from Tailscale itself (the conftest stub) when not given
    assert si.check_request({**GOOD, "X-Forwarded-For": "fd7a:115c:a1e0::1"}, "owner@x.com", ORIG) == si.FROM_BOX


def test_listener_refuses_a_post_from_the_box_over_http(monkeypatch, tmp_path):
    import http.client, json as _j
    from http.server import ThreadingHTTPServer
    from wam import cli
    monkeypatch.setattr(si, "peer_uid", lambda p: 0)            # it did come through tailscaled
    monkeypatch.setattr(si, "owner_login", lambda: "owner@x.com")
    monkeypatch.setattr(si, "served_origins", lambda h: ORIG)
    monkeypatch.setattr(si, "start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not install")))
    (tmp_path / "links.json").write_text(_j.dumps({"host": "box.tail1.ts.net"}))
    class C: index = "x"; home = tmp_path
    monkeypatch.setattr(cli.StoreHandler, "cfg", C(), raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.StoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = _j.dumps({"source": "standard", "repo": "org/app", "commit": "a" * 40, "operation": "install"})
        h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        h.request("POST", "/app", body, {**GOOD, "X-Forwarded-For": "100.64.0.1", "Content-Length": str(len(body))})
        r = h.getresponse()
        assert r.status == 403 and "this Wizard itself" in _j.loads(r.read())["error"]
    finally:
        srv.shutdown()


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
    monkeypatch.setattr(si, "peer_uid", lambda p: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "o@x")
    monkeypatch.setattr(si, "served_origins", lambda h: {"https://b"})
    monkeypatch.setattr(si, "check_request", lambda *a: None)
    monkeypatch.setattr(cli.StoreHandler, "cfg", cfg, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.StoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    body = _j.dumps({"source": "standard", "repo": "org/app", "commit": "a" * 40, "operation": "install"})
    h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    h.request("POST", "/app", body, {"Content-Type": "application/json", "Content-Length": str(len(body))})
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


# ---- gateway listener (:7792): the private gateway's userspace Caddy/tailscaled cannot satisfy the :7791 root-peer gate

PROOF = "p" * 48


def _gateway(monkeypatch, tmp_path, proof=PROOF):
    import json as _j
    from http.server import ThreadingHTTPServer
    from wam import cli
    if proof:
        (tmp_path / si.GATEWAY_PROOF_FILE).write_text(proof + "\n")
    (tmp_path / "settings.json").write_text(_j.dumps({"network": {"base_url": "https://box.tail1.ts.net"}}))
    monkeypatch.setattr(si, "owner_login", lambda: "owner@x.com")
    monkeypatch.setattr(si, "peer_uid", lambda p: (_ for _ in ()).throw(AssertionError("gateway must not use the peer gate")))
    class C: index = "x"; home = tmp_path
    monkeypatch.setattr(cli.GatewayStoreHandler, "cfg", C(), raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.GatewayStoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _post(srv, path, headers, body=None):
    import http.client, json as _j
    body = _j.dumps(body or {"source": "standard", "repo": "org/app", "commit": "a" * 40, "operation": "install"})
    h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    h.request("POST", path, body, {**headers, "Content-Length": str(len(body))})
    r = h.getresponse()
    return r.status, _j.loads(r.read() or b"{}")


GW = {**GOOD, "Origin": "https://box.tail1.ts.net", "X-Forwarded-For": "100.64.0.9", "X-Wizard-Store-Proof": PROOF}


def test_gateway_listener_needs_the_proof_and_a_plain_app_id(monkeypatch, tmp_path):
    srv = _gateway(monkeypatch, tmp_path)
    monkeypatch.setattr(si, "start", lambda c, a, *x, **k: (_ for _ in ()).throw(AssertionError("must not install")))
    try:
        for hdrs in ({k: v for k, v in GW.items() if k != "X-Wizard-Store-Proof"}, {**GW, "X-Wizard-Store-Proof": "x" * 48}):
            st, j = _post(srv, "/wizard-mail", hdrs)
            assert st == 403 and "owner gateway" in j["error"]
        for path in ("/_updates", "/_remove/wizard-mail", "/_connections/", "/a/b", "/Bad_ID"):   # control paths stay on :7791
            assert _post(srv, path, GW)[0] == 404, path
    finally:
        srv.shutdown()


def test_gateway_listener_keeps_every_owner_check(monkeypatch, tmp_path):
    srv = _gateway(monkeypatch, tmp_path)
    monkeypatch.setattr(si, "start", lambda c, a, *x, **k: (_ for _ in ()).throw(AssertionError("must not install")))
    try:
        assert "this Wizard itself" in _post(srv, "/wizard-mail", {**GW, "X-Forwarded-For": "100.64.0.1"})[1]["error"]
        assert _post(srv, "/wizard-mail", {**GW, "Origin": "https://evil.example"})[1]["error"] == "wrong origin"
        assert "owner" in _post(srv, "/wizard-mail", {**GW, "Tailscale-User-Login": "guest@x.com"})[1]["error"]
        assert _post(srv, "/wizard-mail", {k: v for k, v in GW.items() if k != "X-Wizard-Store"})[1]["error"] == "bad request"
    finally:
        srv.shutdown()


def test_gateway_listener_starts_an_install_for_the_owner_on_another_device(monkeypatch, tmp_path):
    srv = _gateway(monkeypatch, tmp_path)
    from wam import store_selection
    monkeypatch.setattr(store_selection, "resolve", lambda c, a, sel: sel)
    monkeypatch.setattr(si, "start", lambda c, a, *x, selection=None, **k: (202, {"app": a, "state": "installing"}))
    try:
        assert _post(srv, "/wizard-mail", GW) == (202, {"app": "wizard-mail", "state": "installing"})
    finally:
        srv.shutdown()


def test_gateway_proof_is_off_without_a_long_secret_and_origins_come_from_settings(tmp_path):
    assert si.gateway_proof(tmp_path) == ""
    (tmp_path / si.GATEWAY_PROOF_FILE).write_text("short\n")
    assert si.gateway_proof(tmp_path) == "" and not si.gateway_proof_ok({"X-Wizard-Store-Proof": "short"}, tmp_path)
    (tmp_path / si.GATEWAY_PROOF_FILE).write_text(PROOF + "\n")
    assert si.gateway_proof_ok({"X-Wizard-Store-Proof": PROOF}, tmp_path)
    assert not si.gateway_proof_ok({}, tmp_path)
    assert si.gateway_origins(tmp_path) == set()
    (tmp_path / "settings.json").write_text('{"network": {"base_url": "https://box.ts.net/", "aliases": ["http://plain.example", 5]}}')
    assert si.gateway_origins(tmp_path) == {"https://box.ts.net"}


def test_tailscale_cli_can_target_the_gateway_daemon(monkeypatch):
    monkeypatch.setenv("WAM_TAILSCALE_BIN", "/x/tailscale")
    monkeypatch.setenv("WAM_TAILSCALE_SOCKET", "/run/ts.sock")
    assert si.tailscale_cmd("status", "--json") == ["/x/tailscale", "--socket", "/run/ts.sock", "status", "--json"]
    monkeypatch.delenv("WAM_TAILSCALE_SOCKET")
    assert si.tailscale_cmd("status") == ["/x/tailscale", "status"]
