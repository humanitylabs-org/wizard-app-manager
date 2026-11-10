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
        (tmp_path / si.GATEWAY_PROOF_FILE).parent.mkdir(mode=0o700, exist_ok=True)
        (tmp_path / si.GATEWAY_PROOF_FILE).write_text(proof + "\n")
        (tmp_path / si.GATEWAY_PROOF_FILE).chmod(0o600)
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
        for path in ("/_remove/wizard-mail", "/_host-access/wizard-mail", "/_views/x", "/_slots/chats", "/_recommended",
                     "/a/b", "/Bad_ID", "/_connectionsX"):   # removal, host access and the rest stay on :7791
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


def _get(srv, path, headers):
    import http.client, json as _j
    h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    h.request("GET", path, headers=headers)
    r = h.getresponse()
    raw = r.read()
    try:
        return r.status, _j.loads(raw or b"{}")
    except ValueError:
        return r.status, raw


def test_gateway_listener_serves_the_settings_routes_to_the_owner_only(monkeypatch, tmp_path):
    srv = _gateway(monkeypatch, tmp_path)
    from wam import main_compare, updates, network
    monkeypatch.setattr(updates, "status", lambda: {"mode": "ask", "update_available": False})
    monkeypatch.setattr(main_compare, "compare", lambda commit: {"commit": commit, "ahead_by": 0})
    monkeypatch.setattr(network, "report", lambda cfg: {"apps": []})
    owner = {"Tailscale-User-Login": "owner@x.com", "X-Wizard-Store-Proof": PROOF}
    try:
        assert _get(srv, "/_updates", owner) == (200, {"mode": "ask", "update_available": False})
        assert _get(srv, "/_main-compare?commit=" + "a" * 40, owner)[1]["ahead_by"] == 0
        assert _get(srv, "/_connections/api/connections", owner) == (200, {"apps": []})
        for path in ("/_updates", "/_main-compare", "/_connections/api/connections"):
            assert _get(srv, path, {k: v for k, v in owner.items() if k != "X-Wizard-Store-Proof"})[0] == 403, path
            assert _get(srv, path, {**owner, "Tailscale-User-Login": "guest@x.com"})[0] == 403, path
        # a write keeps every POST check (other device, shell origin, JSON + X-Wizard-Store) and the proof
        monkeypatch.setattr(updates, "set_mode", lambda m: {"mode": m})
        assert _post(srv, "/_updates", GW, {"mode": "auto"})[0] == 200
        assert _post(srv, "/_updates", {**GW, "Origin": "https://evil.example"}, {"mode": "auto"})[1]["error"] == "wrong origin"
        assert "this Wizard itself" in _post(srv, "/_updates", {**GW, "X-Forwarded-For": "100.64.0.1"}, {"mode": "auto"})[1]["error"]
    finally:
        srv.shutdown()


def test_gateway_feature_switches_reuses_owner_gate_and_exact_route(monkeypatch, tmp_path):
    """The Tracker's existing endpoint must work on the private edge without widening that edge."""
    import http.client
    from wam import feature_switches as fs

    srv = _gateway(monkeypatch, tmp_path)
    calls = []
    listing = {"switches": [], "locked": False}
    monkeypatch.setattr(fs, "listing", lambda: listing)
    monkeypatch.setattr(fs, "set_switch", lambda tid, on, **kw:
                        calls.append((tid, on, kw)) or {"ok": True})
    body = {"id": "miguel/send-and-go", "on": True}
    try:
        assert si.gateway_path_ok("/_feature-switches")
        assert _get(srv, "/_feature-switches", GW) == (200, listing)
        assert _post(srv, "/_feature-switches", GW, body) == (200, {"ok": True})
        assert calls == [(body["id"], True, {"by": "owner", "origin": "https://box.tail1.ts.net"})]
        calls.clear()
        for overrides in ({"X-Wizard-Store-Proof": None}, {"X-Wizard-Store-Proof": "forged"},
                          {"Tailscale-User-Login": None}, {"Tailscale-User-Login": "guest@x.com"}):
            headers = {k: v for k, v in {**GW, **overrides}.items() if v is not None}
            assert _get(srv, "/_feature-switches", headers)[0] == 403
            assert _post(srv, "/_feature-switches", headers, body)[0] == 403
        # A bearer or app grant cannot replace gateway proof or owner identity.
        for headers in ({}, {"Authorization": "Bearer forged"}, {"X-Wizard-Grant": "f" * 64}):
            assert _get(srv, "/_feature-switches", headers)[0] == 403
            assert _post(srv, "/_feature-switches", headers, body)[0] == 403
        for overrides in ({"Origin": "https://evil.example"}, {"X-Wizard-Store": None},
                          {"Content-Type": "text/plain"}, {"X-Forwarded-For": next(iter(si.own_ips()))}):
            headers = {k: v for k, v in {**GW, **overrides}.items() if v is not None}
            assert _post(srv, "/_feature-switches", headers, body)[0] == 403
        for path in ("/_feature-switchesX", "/_feature-switches/child", "/_feature-switch", "/_feature-switches%2fchild",
                     "/_remove/wizard-mail", "/_host-access/wizard-mail", "/_views/x"):
            assert not si.gateway_path_ok(path)
            assert _get(srv, path, GW)[0] == 404
            assert _post(srv, path, GW, body)[0] == 404
        for method in ("PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            try:
                conn.request(method, "/_feature-switches", headers=GW)
                response = conn.getresponse()
                assert response.status == 501
                response.read()
            finally:
                conn.close()
        assert calls == [], "no rejected request may reach the existing feature writer"
    finally:
        srv.shutdown()
        srv.server_close()


def test_gateway_proof_is_off_without_a_long_secret_and_origins_come_from_settings(tmp_path):
    assert si.gateway_proof(tmp_path) == ""
    (tmp_path / si.GATEWAY_PROOF_FILE).parent.mkdir(mode=0o700, exist_ok=True)
    (tmp_path / si.GATEWAY_PROOF_FILE).write_text("short\n")
    assert si.gateway_proof(tmp_path) == "" and not si.gateway_proof_ok({"X-Wizard-Store-Proof": "short"}, tmp_path)
    (tmp_path / si.GATEWAY_PROOF_FILE).write_text(PROOF + "\n")
    (tmp_path / si.GATEWAY_PROOF_FILE).chmod(0o600)
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


def _private_proof(home):
    proof = home / si.GATEWAY_PROOF_FILE
    proof.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    proof.write_text(PROOF + "\n")
    proof.chmod(0o600)
    return proof


def test_gateway_proof_rechecks_modes_and_ownership(monkeypatch, tmp_path):
    proof = _private_proof(tmp_path)
    assert si.gateway_proof(tmp_path) == PROOF
    for mode in (0o644, 0o640, 0o666, 0o700):
        proof.chmod(mode)
        assert si.gateway_proof(tmp_path) == "", oct(mode)
        assert not si.gateway_proof_ok({"X-Wizard-Store-Proof": PROOF}, tmp_path)
    proof.chmod(0o600)
    assert si.gateway_proof(tmp_path) == PROOF
    monkeypatch.setattr(si.os, "geteuid", lambda: proof.stat().st_uid + 1)
    assert si.gateway_proof(tmp_path) == ""


def test_gateway_proof_rejects_linked_paths_and_hardlinks(tmp_path):
    import os
    proof = _private_proof(tmp_path)
    target = tmp_path / "target"
    proof.rename(target)
    proof.symlink_to(target)
    assert si.gateway_proof(tmp_path) == ""
    proof.unlink()
    os.link(target, proof)
    assert si.gateway_proof(tmp_path) == ""
    proof.unlink()
    target.rename(proof)
    actual = tmp_path / "actual"
    proof.parent.rename(actual)
    proof.parent.symlink_to(actual, target_is_directory=True)
    assert si.gateway_proof(tmp_path) == ""
    alias = tmp_path.parent / (tmp_path.name + "-alias")
    try:
        alias.symlink_to(tmp_path, target_is_directory=True)
        assert si.gateway_proof(alias) == ""
    finally:
        alias.unlink()


def test_gateway_proof_rejects_nonfiles_and_bad_bytes(tmp_path):
    proof = _private_proof(tmp_path)
    proof.write_bytes(b"\xff" * 48)
    assert si.gateway_proof(tmp_path) == ""
    proof.write_text("p" * 4097)
    assert si.gateway_proof(tmp_path) == ""
    proof.unlink()
    proof.mkdir()
    assert si.gateway_proof(tmp_path) == ""
    # Opening a FIFO must not wait for a writer.
    proof.rmdir()
    si.os.mkfifo(proof, 0o600)
    assert si.gateway_proof(tmp_path) == ""


def test_gateway_proof_rejects_writable_parent(tmp_path):
    proof = _private_proof(tmp_path)
    for mode in (0o777, 0o770):
        proof.parent.chmod(mode)
        assert si.gateway_proof(tmp_path) == ""
    proof.parent.chmod(0o700)
    assert si.gateway_proof(tmp_path) == PROOF


def test_gateway_proof_exact_mode_size_and_home_boundary(tmp_path):
    home = tmp_path / "home"
    proof = _private_proof(home)
    home.chmod(0o755)
    proof.parent.chmod(0o755)
    proof.write_text("p" * 4096)
    assert si.gateway_proof(home) == "p" * 4096
    for mode in (0o400, 0o4600):
        proof.chmod(mode)
        assert si.gateway_proof(home) == ""
    proof.chmod(0o600)
    home.chmod(0o770)
    assert si.gateway_proof(home) == ""
    home.chmod(0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    assert si.gateway_proof(str(alias) + "/") == ""


def test_gateway_proof_checks_file_owner_and_closes_handles(monkeypatch, tmp_path):
    import errno
    proof = _private_proof(tmp_path)
    inode = proof.stat().st_ino
    real_fstat, real_open = si.os.fstat, si.os.open
    opened = []

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def wrong_file_owner(fd):
        result = real_fstat(fd)
        if result.st_ino == inode:
            fields = list(result)
            fields[4] += 1
            return si.os.stat_result(fields)
        return result

    monkeypatch.setattr(si.os, "open", track_open)
    monkeypatch.setattr(si.os, "fstat", wrong_file_owner)
    assert si.gateway_proof(tmp_path) == ""
    assert len(opened) == 3
    monkeypatch.setattr(si.os, "fstat", real_fstat)
    for fd in opened:
        try:
            real_fstat(fd)
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise AssertionError("proof reader leaked a descriptor")
    opened.clear()
    monkeypatch.setattr(si.os, "read", lambda *a: (_ for _ in ()).throw(OSError("injected read failure")))
    assert si.gateway_proof(tmp_path) == ""
    assert len(opened) == 3
    for fd in opened:
        try:
            real_fstat(fd)
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise AssertionError("proof read failure leaked a descriptor")


def test_gateway_listener_rejects_proof_after_permission_drift(monkeypatch, tmp_path):
    srv = _gateway(monkeypatch, tmp_path)
    monkeypatch.setattr(si, "start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not install")))
    try:
        (tmp_path / si.GATEWAY_PROOF_FILE).chmod(0o644)
        assert _post(srv, "/wizard-mail", GW)[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()
