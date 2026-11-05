"""Amendment 2: accepting an app with host access is an owner-device action no agent can trigger.

Every path an agent can reach is exercised as an agent would: `wam install --allow-host-access`, `wam app update
--allow-host-access`, `wam network allow`, the in-process functions the Agent Service and Builder call, and the HTTP
listener (bearer / box / unenrolled caller). Synthetic recipes and a synthetic tailnet only."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import host_access_gate as gate, network as net, owner_devices as od  # noqa: E402

from test_network import gate_env, update_env  # noqa: E402

RECIPES = {
    "privileged": "services:\n  x:\n    image: a\n    privileged: true\n",
    "docker socket": "services:\n  x:\n    image: a\n    volumes: ['/var/run/docker.sock:/s']\n",
    "containerd socket": "services:\n  x:\n    image: a\n    volumes: ['/run/containerd/containerd.sock:/s']\n",
    "host /home bind": "services:\n  x:\n    image: a\n    volumes: ['/home/someone:/h']\n",
    "host / bind (long form)": "services:\n  x:\n    image: a\n    volumes:\n      - {type: bind, source: /, target: /h}\n",
    "host network": "services:\n  x:\n    image: a\n    network_mode: host\n",
    "host pid": "services:\n  x:\n    image: a\n    pid: host\n",
    "cap_add SYS_ADMIN": "services:\n  x:\n    image: a\n    cap_add: [SYS_ADMIN]\n",
    "cap_add NET_ADMIN": "services:\n  x:\n    image: a\n    cap_add: [CAP_NET_ADMIN]\n",
    "host devices": "services:\n  x:\n    image: a\n    devices: ['/dev/sda:/dev/sda']\n",
    "relative path climbing out": "services:\n  x:\n    image: a\n    volumes: ['../../../:/h']\n",
    "app data climbing out": "services:\n  x:\n    image: a\n    volumes: ['${APP_DATA_DIR}/../../..:/h']\n",
    "other variable path": "services:\n  x:\n    image: a\n    volumes: ['${HOME}:/h']\n",
    "tilde path": "services:\n  x:\n    image: a\n    volumes: ['~/.ssh:/h']\n",
    "localtime read-write": "services:\n  x:\n    image: a\n    volumes: ['/etc/localtime:/etc/localtime']\n",
    "privileged through a variable": "services:\n  x:\n    image: a\n    privileged: ${ADV:-true}\n",
    "pid through a variable": "services:\n  x:\n    image: a\n    pid: ${P}\n",
    "cap_add through a variable": "services:\n  x:\n    image: a\n    cap_add: ['${C}']\n",
    "volumes_from": "services:\n  x:\n    image: a\n    volumes_from: [other]\n",
    "named volume bound to a host path": "services:\n  x:\n    image: a\n    volumes: ['v:/h']\nvolumes:\n  v: {driver: local, driver_opts: {type: none, o: bind, device: /home/someone}}\n",
    "secret read from a host file": "services:\n  x:\n    image: a\nsecrets:\n  k: {file: /home/someone/.ssh/id_ed25519}\n",
    "cgroup host": "services:\n  x:\n    image: a\n    cgroup: host\n",
    "device_cgroup_rules": "services:\n  x:\n    image: a\n    device_cgroup_rules: ['b *:* rwm']\n    cap_add: [MKNOD]\n",
    "label=disable": "services:\n  x:\n    image: a\n    security_opt: ['label=disable']\n",
    "seccomp profile": "services:\n  x:\n    image: a\n    security_opt: ['seccomp=/tmp/open.json']\n",
    "pid of another container": "services:\n  x:\n    image: a\n    pid: 'container:other'\n",
    "include": "include: [other.yml]\nservices:\n  x:\n    image: a\n",
    "extends": "services:\n  x:\n    extends: {file: other.yml, service: y}\n",
    "gpu reservation": "services:\n  x:\n    image: a\n    deploy: {resources: {reservations: {devices: [{driver: nvidia, count: 1}]}}}\n",
    "foreign network": "services:\n  x:\n    image: a\n    networks: [n]\nnetworks:\n  n: {external: true, name: runtipi_tipi_main_network}\n",
}


@pytest.mark.parametrize("name", sorted(RECIPES))
def test_every_kind_of_host_access_is_detected(name):
    assert net.host_access(RECIPES[name], "x"), name


@pytest.mark.parametrize("compose", [
    "services:\n  x:\n    image: a\n    cap_add: [CHOWN, DAC_OVERRIDE, FOWNER]\n    volumes: ['data:/d', './conf:/c']\n",
    "services:\n  x:\n    image: a\n    volumes: ['/etc/localtime:/etc/localtime:ro']\n",
    "services:\n  x:\n    image: a\n    volumes: ['${APP_DATA_DIR}/data:/d', './conf:/c', 'v:/v']\n    security_opt: ['no-new-privileges:true']\n    pid: 'service:y'\n  y:\n    image: b\n",
    "services:\n  x:\n    image: a\n    volumes:\n      - {type: bind, source: '${APP_DATA_DIR}/q', target: /q}\n      - {type: tmpfs, target: /t}\n",
])
def test_ordinary_recipes_need_no_owner(compose):
    assert net.host_access(compose, "x") == []


ROOT = "/home/u/runtipi"


def _svc(vol, extra=""):
    return f"services:\n  x:\n    image: a\n    volumes: ['{vol}']\n{extra}"


@pytest.mark.parametrize("app,vol", [
    ("notes", "${ROOT_FOLDER_HOST}/repos/wizardapps/apps/notes/releases/${NOTES_RELEASE}:/app:ro"),
    ("wizard-calendar", ROOT + "/repos/wizardapps/apps/wizard-calendar/releases/1bbc58854185:/app:ro"),
    ("agent-private", "${ROOT_FOLDER_HOST}/app-data/wizard-os/agents/private/home:/opt/data"),
    ("notifications", "${INBOX_MEDIA_1:-${APP_DATA_DIR}/no-media-1}:${INBOX_MEDIA_1:-/no-media-1}:ro"),
    ("wizard-files", "${ROOT_FOLDER_HOST}/media:/media"),                  # Runtipi's own variables: the existing store model
    ("movies", "${APP_SOURCE_DIR}:/app:ro"),
])
def test_what_wizards_own_recipes_mount_is_not_host_access(app, vol):
    assert net.host_access(_svc(vol), app, "wizardapps", runtipi_root=ROOT) == []


@pytest.mark.parametrize("app,vol", [
    ("notes", "${ROOT_FOLDER_HOST}/repos/wizardapps/apps/notes/releases/${R}:/app"),                 # writable release
    ("notes", "${ROOT_FOLDER_HOST}/repos/wizardapps/apps/tasks/releases/${R}:/app:ro"),              # another app's folder
    ("notes", "${ROOT_FOLDER_HOST}/repos/wizardapps/apps/notes/../tasks/releases/x:/app:ro"),
    ("agent-private", "${ROOT_FOLDER_HOST}/app-data/wizard-os/agents/private/../philosopher/home:/o"),
    ("notifications", "${INBOX_MEDIA_1:-/home/u}:/m:ro"),
    ("x", "${ROOT_FOLDER_HOST}/../../:/m"),
    ("x", "${APP_SOURCE_DIR}/../..:/m"),                                           # a default outside the app
])
def test_own_folder_waivers_do_not_stretch(app, vol):
    assert net.host_access(_svc(vol), app, "wizardapps", runtipi_root=ROOT)


def test_joining_another_containers_namespaces_is_host_access():
    for k in ("network_mode", "pid", "ipc", "uts", "userns_mode"):
        assert net.host_access(f"services:\n  x:\n    image: a\n    {k}: 'container:victim'\n", "x"), k
    for k in ("pid", "ipc", "uts", "userns_mode"):       # another app's service (not in this compose) is the same thing
        assert net.host_access(f"services:\n  x:\n    image: a\n    {k}: 'service:victim'\n", "x"), k
    assert net.host_access("services:\n  x:\n    image: a\n  y:\n    image: b\n    pid: 'service:x'\n", "x") == []


@pytest.mark.parametrize("name", sorted(RECIPES))
def test_an_agent_cannot_install_with_host_access_whatever_the_flag(tmp_path, monkeypatch, name):
    cli, installs, steps = gate_env(tmp_path, monkeypatch, RECIPES[name])
    for flag in (False, True):
        with pytest.raises(SystemExit, match="REFUSED: x asks for access"):
            cli.install(cli.Config(), "x", allow_host_access=flag)
    with pytest.raises(SystemExit, match="owner action"):
        cli.install_with_retry(cli.Config(), "x", allow_host_access=True)      # the Agent Service / bootstrap path
    assert installs == [] and steps == [] and net.host_access_allowed(tmp_path / "home") == set()


def test_the_refusal_names_the_owner_action(tmp_path, monkeypatch):
    cli, *_ = gate_env(tmp_path, monkeypatch, RECIPES["host network"])
    with pytest.raises(SystemExit) as e:
        cli.install(cli.Config(), "x", allow_host_access=True)
    msg = str(e.value)
    assert "owner's own device" in msg and "Allow host access" in msg and "network_mode: host" in msg


def test_the_cli_flag_from_a_shell_accepts_nothing(tmp_path, monkeypatch):
    cli, installs, _ = gate_env(tmp_path, monkeypatch, RECIPES["privileged"])
    with pytest.raises(SystemExit, match="owner action"):
        cli.main(["install", "x", "--allow-host-access"])
    assert installs == []


def test_an_agent_cannot_update_into_host_access(tmp_path, monkeypatch):
    plain = "services:\n  x:\n    image: a\n"
    cli, updates, steps = update_env(tmp_path, monkeypatch, plain, RECIPES["docker socket"])
    for flag in (False, True):
        with pytest.raises(SystemExit, match="REFUSED: the update of x asks for access"):
            cli.app_update(cli.Config(), "x", allow_host_access=flag)
    with pytest.raises(SystemExit, match="owner action"):
        cli.main(["app", "update", "x", "--allow-host-access"])
    assert updates == [] and net.host_access_allowed(tmp_path / "home") == set()


def test_an_earlier_allowance_does_not_cover_what_an_update_adds(tmp_path, monkeypatch):
    """Review P2: allowed once (say, for a bind mount), an update must not be able to add the Docker socket unasked."""
    plain = "services:\n  x:\n    image: a\n"
    cli, updates, steps = update_env(tmp_path, monkeypatch, plain, RECIPES["docker socket"])
    net.set_host_access_allowed(tmp_path / "home", "x", True)
    for flag in (False, True):
        with pytest.raises(SystemExit, match="REFUSED: the update of x asks for access"):
            cli.app_update(cli.Config(), "x", allow_host_access=flag)
    assert updates == []


def test_an_allowance_already_on_file_never_opens_an_install(tmp_path, monkeypatch):
    """Review P1: app-manager.json is a plain file; a line in it must not stand in for the owner's grant."""
    cli, installs, _ = gate_env(tmp_path, monkeypatch, RECIPES["privileged"])
    net.set_host_access_allowed(cli.Config().home, "x", True)
    for flag in (False, True):
        with pytest.raises(SystemExit, match="owner action"):
            cli.install(cli.Config(), "x", allow_host_access=flag)
    assert installs == []


def test_a_grant_for_another_purpose_or_recipe_does_not_open_the_install(tmp_path, monkeypatch):
    cli, installs, _ = gate_env(tmp_path, monkeypatch, RECIPES["privileged"])
    c = cli.Config()
    risks = cli.host_access_risks(c, "x")
    gate.issue(c.home, "x", risks, "update", cli.recipe_hash(c, "x"))              # an update grant is not an install grant
    with pytest.raises(SystemExit, match="owner action"):
        cli.install(c, "x", allow_host_access=True)
    gate.issue(c.home, "x", risks, "install", "0" * 64)                            # the recipe changed after the owner looked
    with pytest.raises(SystemExit, match="owner action"):
        cli.install(c, "x", allow_host_access=True)
    assert installs == []


def test_an_agent_cannot_allow_an_installed_app_with_network_allow(tmp_path, monkeypatch):
    from wam import cli
    root, home = tmp_path / "runtipi", tmp_path / "home"
    d = root / "apps" / "wizardapps" / "x"
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text(RECIPES["privileged"])
    home.mkdir()
    monkeypatch.setenv("RUNTIPI_ROOT", str(root))
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.setattr(cli.release_pins, "load", lambda *a, **k: None)
    with pytest.raises(SystemExit, match="owner action"):
        cli.main(["network", "allow", "x"])
    assert net.host_access_allowed(home) == set()


# ---------------------------------------------------------------- the grant

RISKS = ["x: privileged (full control of the server)"]


def test_a_grant_is_for_one_app_one_purpose_one_recipe_and_one_use(tmp_path):
    gate.issue(tmp_path, "x", RISKS, "install", "r1")
    assert not gate.valid(tmp_path, "y", RISKS, "install", "r1")                 # another app
    assert not gate.valid(tmp_path, "x", RISKS + ["x: host devices"], "install", "r1")   # more access than shown
    assert not gate.valid(tmp_path, "x", RISKS, "update", "r1")                  # another purpose
    assert not gate.valid(tmp_path, "x", RISKS, "install", "r2")                 # the same words on a changed recipe/image
    assert gate.consume(tmp_path, "x", RISKS, "update", "r1") is False           # a wrong try does not use it up
    assert gate.consume(tmp_path, "x", RISKS, "install", "r1") is True
    assert gate.consume(tmp_path, "x", RISKS, "install", "r1") is False          # used up


def test_a_blank_grant_cannot_be_made(tmp_path):
    with pytest.raises(ValueError):
        gate.issue(tmp_path, "x", [], "allow", "r")
    assert not gate.valid(tmp_path, "x", [], "allow", "r")


def test_a_grant_expires(tmp_path):
    gate.issue(tmp_path, "x", RISKS, now=time.time() - gate.TTL_S - 5)
    assert not gate.valid(tmp_path, "x", RISKS)


def test_a_forged_or_edited_grant_is_refused(tmp_path):
    gate.issue(tmp_path, "x", RISKS)
    path = gate.grants_dir(tmp_path) / "x.json"
    doc = json.loads(path.read_text())
    path.write_text(json.dumps({**doc, "exp": doc["exp"] + 99999}))              # extended
    assert not gate.valid(tmp_path, "x", RISKS)
    path.write_text(json.dumps({"app": "x", "risks": gate._digest(RISKS), "exp": int(time.time()) + 60,
                                "nonce": "n", "mac": "0" * 64}))                 # written by someone without the key
    assert not gate.valid(tmp_path, "x", RISKS)
    path.write_text("not json")
    assert not gate.valid(tmp_path, "x", RISKS)


def test_a_grant_with_a_loosened_key_file_is_refused(tmp_path):
    gate.issue(tmp_path, "x", RISKS)
    os.chmod(gate.key_path(tmp_path), 0o644)
    assert not gate.valid(tmp_path, "x", RISKS)


def test_a_grant_cannot_be_used_twice_concurrently(tmp_path):
    gate.issue(tmp_path, "x", RISKS)
    barrier, won = threading.Barrier(12), []
    def go():
        barrier.wait()
        won.append(gate.consume(tmp_path, "x", RISKS))
    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert won.count(True) == 1


def test_an_app_id_cannot_escape_the_grant_folder(tmp_path):
    with pytest.raises(ValueError):
        gate.issue(tmp_path, "../x", RISKS)
    assert not gate.valid(tmp_path, "../x", RISKS)
    assert not gate.consume(tmp_path, "../x", RISKS)


# ---------------------------------------------------------------- the owner listener (the only writer of a grant)

OWNER, BOX_IP, PHONE, SERVER = "owner@example.com", "100.64.0.1", "100.64.0.10", "100.64.0.20"


def _node(name, os_):
    return {"Node": {"StableID": "n" + name, "Name": f"{name}.t.ts.net.", "Tags": None,
                     "Hostinfo": {"OS": os_, "Hostname": name}}, "UserProfile": {"LoginName": OWNER}}


TAILNET = {PHONE: _node("pixel", "android"), SERVER: _node("agent-box", "linux"), BOX_IP: _node("box", "linux")}


@pytest.fixture
def listener(tmp_path, monkeypatch):
    from wam import cli, store_install as si
    root, home = tmp_path / "runtipi", tmp_path / "home"
    d = root / "repos" / "wizardapps" / "apps" / "x"
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text(RECIPES["privileged"])
    home.mkdir()
    (home / "links.json").write_text(json.dumps({"host": "box.t.ts.net"}))
    monkeypatch.setenv("RUNTIPI_ROOT", str(root))
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.delenv("WIZARD_DEVICES_FILE", raising=False)
    monkeypatch.setattr(si, "peer_uid", lambda port: 0)
    monkeypatch.setattr(si, "owner_login", lambda: OWNER)
    monkeypatch.setattr(si, "own_ips", lambda: {BOX_IP})
    monkeypatch.setattr(si, "served_origins", lambda host: {"https://box.t.ts.net"})
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set())
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: object())
    monkeypatch.setattr(od, "tailscale_whois", lambda ip: TAILNET[ip] if ip in TAILNET else (_ for _ in ()).throw(od.Unverified("x")))
    cli.StoreHandler.cfg = cli.Config()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.StoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def post(hop, extra=None, body=b"{}", login=OWNER, path="/_host-access/x"):
        h = {"Tailscale-User-Login": login, "X-Forwarded-For": hop, "Origin": "https://box.t.ts.net",
             "X-Wizard-Store": "1", "Content-Type": "application/json", **(extra or {})}
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", data=body, headers=h, method="POST")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    post.port = srv.server_address[1]
    yield post, home
    srv.shutdown(); srv.server_close()


def test_the_owners_phone_can_accept(listener):
    post, home = listener
    status, body = post(PHONE)
    assert status == 200 and body["app"] == "x" and body["risks"] == ["x: privileged (full control of the server)"]
    assert (gate.grants_dir(home) / "x.json").is_file()


def test_the_grant_the_listener_writes_matches_what_the_install_checks(listener):
    from wam import cli
    post, home = listener
    status, body = post(PHONE, body=b'{"for": "install"}')
    assert status == 200
    c = cli.Config()
    assert gate.valid(home, "x", cli.host_access_risks(c, "x"), "install", cli.recipe_hash(c, "x"))


@pytest.mark.parametrize("hop,extra,code", [
    (SERVER, None, "owner_device_required"),                  # the owner's other (agent) server
    (BOX_IP, None, None),                                     # the box itself (store_install.from_box)
    ("100.64.0.99", None, "owner_device_unverified"),         # tailscale cannot resolve the caller
    (PHONE, {"Authorization": "Bearer agent-token"}, None),   # an agent's bearer, even from a trusted address
])
def test_nothing_but_an_enrolled_owner_device_can_make_a_grant(listener, hop, extra, code):
    post, home = listener
    status, body = post(hop, extra)
    assert status == 403 and (code is None or body.get("code") == code)
    assert not gate.grants_dir(home).exists() or not list(gate.grants_dir(home).iterdir())


def test_another_login_cannot_make_a_grant(listener):
    post, home = listener
    assert post(PHONE, login="guest@example.com")[0] == 403
    assert not gate.grants_dir(home).exists()


def test_a_peer_that_is_not_tailscaled_is_refused_before_any_header_is_read(listener, monkeypatch):
    """Review P1: forged headers from a direct (non-Serve) connection never reach the grant."""
    from wam import store_install as si
    post, home = listener
    monkeypatch.setattr(si, "peer_uid", lambda port: 1000)
    assert post(PHONE)[0] == 403
    monkeypatch.setattr(si, "peer_uid", lambda port: None)
    assert post(PHONE)[0] == 403
    assert not gate.grants_dir(home).exists()


def test_the_store_listener_never_keeps_a_connection_alive():
    """Review P1: a refused request must not leave its body to be parsed as a second request (HTTP/1.0 closes)."""
    from wam import cli
    assert cli.StoreHandler.protocol_version == "HTTP/1.0"


def test_a_smuggled_second_request_in_a_refused_body_is_never_served(listener):
    import socket
    post, home = listener
    inner = (b"POST /_host-access/x HTTP/1.0\r\nTailscale-User-Login: " + OWNER.encode() + b"\r\nX-Forwarded-For: " + PHONE.encode()
             + b"\r\nOrigin: https://box.t.ts.net\r\nX-Wizard-Store: 1\r\nContent-Length: 2\r\n\r\n{}")
    outer = (b"POST /_host-access/x HTTP/1.1\r\nHost: x\r\nTailscale-User-Login: " + OWNER.encode() + b"\r\nX-Forwarded-For: " + SERVER.encode()
             + b"\r\nOrigin: https://box.t.ts.net\r\nX-Wizard-Store: 1\r\nContent-Length: " + str(len(inner)).encode() + b"\r\n\r\n" + inner)
    s = socket.create_connection(("127.0.0.1", post.port))
    s.sendall(outer)
    data = b""
    while chunk := s.recv(65536):
        data += chunk
    s.close()
    assert data.count(b"HTTP/1.") == 1 and b" 403 " in data.split(b"\r\n")[0]
    assert not gate.grants_dir(home).exists() or not list(gate.grants_dir(home).iterdir())


def test_a_grant_is_only_made_for_an_app_that_asks_for_host_access(listener):
    post, home = listener
    d = home.parent / "runtipi" / "repos" / "wizardapps" / "apps" / "plain"
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text("services:\n  plain:\n    image: a\n")
    assert post(PHONE, path="/_host-access/plain")[0] == 409
    assert post(PHONE, path="/_host-access/Bad_Id")[0] in (400, 403)
