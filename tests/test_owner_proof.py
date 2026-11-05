"""Owner proof (wizard-os spec/OWNER_PROOF.md): manifest, signing, keys, the override block and links routing."""
import json
import os
import socket
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import links, network, owner_proof as op  # noqa: E402
from wam.registry import _owner_proof  # noqa: E402

from test_links import REG, fake_tailscale  # noqa: E402,F401  (fixture)


# ---------------------------------------------------------------- the wizard.md declaration

def test_shorthand_and_long_form_parse():
    assert op.parse({"owner_proof": "hmac-v1"}) == {"scheme": "hmac-v1", "wrap_key": False, "activity": [], "strip": []}
    assert op.parse({"owner_proof": {"scheme": "hmac-v1", "wrap_key": True, "activity": ["/api/drafts"],
                                     "strip": ["X-Messenger-Owner-Proof"]}}) == {
        "scheme": "hmac-v1", "wrap_key": True, "activity": ["/api/drafts"], "strip": ["x-messenger-owner-proof"]}
    assert op.parse({}) is None and op.parse({"owner_proof": False}) is None


@pytest.mark.parametrize("bad", ["hmac-v2", {"scheme": "hmac-v1", "sign_agents": True},
                                 {"scheme": "hmac-v1", "strip": ["Cookie"]}, {"scheme": "hmac-v1", "strip": ["Host"]},
                                 {"scheme": "hmac-v1", "activity": ["api/x"]}, {"scheme": "hmac-v1", "wrap_key": "yes"}, 7])
def test_malformed_declarations_are_refused_and_dropped_from_the_registry(bad):
    with pytest.raises(ValueError):
        op.parse({"owner_proof": bad})
    assert _owner_proof({"owner_proof": bad}) is None     # the app keeps its own sign-in


# ---------------------------------------------------------------- signing

KEY = bytes(range(32))


def test_signature_round_trips_and_binds_app_method_and_target():
    v = op.sign(KEY, "messenger", "POST", "/api/x?y=1", now=1000)
    assert op.verify(v, KEY, "messenger", "POST", "/api/x?y=1", now=1010) == v.split(".")[1]
    for app, method, target in (("contacts", "POST", "/api/x?y=1"), ("messenger", "GET", "/api/x?y=1"),
                                ("messenger", "POST", "/api/x?y=2")):
        with pytest.raises(ValueError):
            op.verify(v, KEY, app, method, target, now=1010)
    with pytest.raises(ValueError):
        op.verify(v, bytes(32), "messenger", "POST", "/api/x?y=1", now=1010)
    with pytest.raises(ValueError):
        op.verify(v, KEY, "messenger", "POST", "/api/x?y=1", now=1031)      # outside the 30 s window
    assert op.sign(KEY, "a", "GET", "/") != op.sign(KEY, "a", "GET", "/")   # fresh nonce every time


def test_known_vector_for_other_language_verifiers():
    """Pinned so app-side verifiers (Messenger's Python, a Node app) can test against the same bytes."""
    v = op.sign(KEY, "messenger", "GET", "/api/status", now=1700000000, nonce="ab" * 24)
    assert v == ("1700000000." + "ab" * 24 + "."
                 "ecf2e5886a06c458496949d2f61e88e8893ea19846121298b2d3374159e28da3")
    assert op.message("messenger", "1", "n", "GET", "/") == b"wizard-app-owner-v1\nmessenger\n1\nn\nGET\n/"


# ---------------------------------------------------------------- keys

def test_edge_key_is_private_created_once_and_never_replaced(tmp_path):
    home = tmp_path / "h"
    assert op.ensure_edge_key(home, "messenger") is True
    p = op.edge_key_path(home, "messenger")
    first = p.read_bytes()
    assert len(first) == 32 and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert op.ensure_edge_key(home, "messenger") is False and p.read_bytes() == first


def test_adopting_the_hand_built_ingress_key_keeps_it(tmp_path):
    old = tmp_path / "proof"
    old.write_bytes(os.urandom(32)); old.chmod(0o600)
    home = tmp_path / "h"
    assert op.ensure_edge_key(home, "messenger", adopt=old)
    assert op.edge_key_path(home, "messenger").read_bytes() == old.read_bytes()
    other = tmp_path / "other"
    other.write_bytes(os.urandom(32)); other.chmod(0o600)
    with pytest.raises(ValueError):
        op.ensure_edge_key(home, "messenger", adopt=other)     # never silently swaps the owner's key


def test_a_world_readable_key_is_refused(tmp_path):
    loose = tmp_path / "k"
    loose.write_bytes(os.urandom(32)); loose.chmod(0o644)
    with pytest.raises(ValueError):
        op.ensure_edge_key(tmp_path / "h", "messenger", adopt=loose)


def test_app_copy_is_made_in_a_networkless_throwaway_container_and_keeps_the_wrap_key(tmp_path):
    home = tmp_path / "h"
    op.ensure_edge_key(home, "messenger")
    seen = []

    class R:
        returncode = 0
        stderr = ""
    op.provision_app_copy(home, "messenger", {"wrap_key": True}, image="ghcr.io/x/m:sha-1@sha256:ab", user="1000:1000",
                          adopt_wrap=tmp_path / "old-wrap", run=lambda cmd, **kw: seen.append(cmd) or R())
    cmd = seen[0]
    assert cmd[:3] == ["docker", "run", "--rm"] and ["--network", "none"] == cmd[5:7]
    script = cmd[-1]
    assert "cp /src/edge.key /dst/proof.key" in script
    assert "if [ ! -s /dst/wrap.key ]; then cp /old/wrap.key /dst/wrap.key; fi" in script   # existing wrap kept
    assert "head -c 32 /dev/urandom > /dst/wrap.key" in script and "! cmp -s /dst/proof.key /dst/wrap.key" in script
    assert "chown -R 1000:1000 /dst" in script
    with pytest.raises(ValueError):
        op.provision_app_copy(home, "messenger", {}, image="x; rm -rf /", run=lambda *a, **k: R())


# ---------------------------------------------------------------- the override block

OVERRIDE = """# App networks: the block marked x-wizard-networks is written by wam (`wam network reconcile`).
services:
  messenger:
    environment:
      MESSENGER_AGENT_FILES_DIR: /attachments
    volumes:
    - /media/Messenger/Attachments:/attachments:ro
    networks: !override
      wizard-app-messenger:
        gw_priority: 1
networks:
  wizard-app-messenger:
    name: wizard-app-messenger
    external: true
x-wizard-networks:
  services:
  - messenger
  networks:
  - wizard-app-messenger
"""


def test_override_block_is_added_and_removed_exactly(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text(OVERRIDE)
    block = op.overlay(tmp_path / "h", "messenger", "messenger", {"wrap_key": True})
    assert op.write_overlay(path, block)
    doc = network.load_yaml(path.read_text())
    env = doc["services"]["messenger"]["environment"]
    assert env["WIZARD_OWNER_PROOF_KEY_FILE"] == "/run/wizard-owner/proof.key"
    assert env["WIZARD_OWNER_WRAP_KEY_FILE"] == "/run/wizard-owner/wrap.key"
    assert env["MESSENGER_AGENT_FILES_DIR"] == "/attachments"                       # owner's line kept
    assert f"{op.app_secret_dir(tmp_path / 'h', 'messenger')}:/run/wizard-owner:ro" in doc["services"]["messenger"]["volumes"]
    assert path.read_text().startswith("# App networks")
    assert not op.write_overlay(path, block)                                         # idempotent
    # wam's network writer rewrites only `networks`; the owner-proof lines survive it
    network.write_overlay(path, {"services": {"messenger": {"wizard-app-messenger": {"gw_priority": 1}}},
                                 "networks": {"wizard-app-messenger": {"name": "wizard-app-messenger", "external": True}}})
    assert "WIZARD_OWNER_PROOF_KEY_FILE" in path.read_text()
    assert op.write_overlay(path, None)
    after = network.load_yaml(path.read_text())
    assert "WIZARD_OWNER_PROOF_KEY_FILE" not in json.dumps(after, default=str)
    assert after["services"]["messenger"]["environment"] == {"MESSENGER_AGENT_FILES_DIR": "/attachments"}
    assert after["services"]["messenger"]["volumes"] == ["/media/Messenger/Attachments:/attachments:ro"]


def test_a_hand_built_trusted_owner_block_must_be_migrated_first(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text("services:\n  messenger:\n    environment:\n      MESSENGER_TRUST_WIZARD_OWNER: '1'\n")
    with pytest.raises(ValueError, match="migrate"):
        op.write_overlay(path, op.overlay(tmp_path / "h", "messenger", "messenger", {}))
    assert path.read_text() == "services:\n  messenger:\n    environment:\n      MESSENGER_TRUST_WIZARD_OWNER: '1'\n"


def test_main_service_reads_the_default_image():
    doc = {"services": {"init": {"image": "busybox"}, "messenger": {
        "image": "${MESSENGER_IMAGE:-ghcr.io/x/m:sha-1@sha256:ab}", "user": "1000:1000", "x-runtipi": {"is_main": True}}}}
    assert op.main_service(doc) == ("messenger", "ghcr.io/x/m:sha-1@sha256:ab", "1000:1000")


# ---------------------------------------------------------------- links: through the edge, never downgraded

def _reg_with_messenger():
    return {"apps": [dict(a) for a in REG["apps"]] + [
        {"id": "messenger", "name": "Messenger", "category": "app", "status": "running",
         "view": {"url": "http://127.0.0.1:9245/"}, "owner_proof": op.parse({"owner_proof": "hmac-v1"})}]}


@pytest.fixture
def short_home():
    """AF_UNIX paths are capped at 108 bytes; pytest's tmp_path is often longer than the real ~/.wizard-os."""
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="oe"))
    yield d / "h"
    shutil.rmtree(d, ignore_errors=True)


def test_owner_proof_app_is_published_through_its_edge_socket(short_home, fake_tailscale, monkeypatch):
    monkeypatch.setattr(op, "enabled", lambda home: True)
    calls, _ = fake_tailscale
    home = short_home
    sock_path = op.socket_path(home, "messenger")
    sock_path.parent.mkdir(parents=True, mode=0o700)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock_path))
    try:
        res = links.publish(_reg_with_messenger(), home, {"wizard-shell"})
    finally:
        s.close()
    m = next(l for l in res["links"] if l["id"] == "messenger")
    assert m["open"] == "/messenger/" and m["owner_proof"] == "edge"
    assert f"--set-path=/messenger/ unix:{sock_path}" in calls.read_text()
    assert "messenger" not in res["errors"]


def test_without_the_edge_an_owner_proof_app_keeps_its_previous_wizard_path(tmp_path, fake_tailscale, monkeypatch):
    monkeypatch.setattr(op, "enabled", lambda home: True)
    calls, _ = fake_tailscale
    home = tmp_path / "home"
    home.mkdir()
    prev = {"id": "messenger", "name": "Messenger", "category": "app", "port": 9245, "path": "/",
            "url": "https://box.tailnet.example.test:8796/messenger/", "open": "/messenger/", "gateway_strip": True,
            "gateway_implicit": True}
    (home / "links.json").write_text(json.dumps({"links": [prev]}))
    res = links.publish(_reg_with_messenger(), home, {"wizard-shell"})
    m = next(l for l in res["links"] if l["id"] == "messenger")
    assert m["open"] == "/messenger/" and m["kept"] == "owner edge unavailable"
    assert "owner edge not running" in res["errors"]["messenger"]
    assert "--set-path=/messenger/ http://127.0.0.1:9245" not in calls.read_text()   # never the raw port


def test_checker_reports_a_malformed_owner_proof(tmp_path):
    from wam import wizardmd
    (tmp_path / "wizard.md").write_text("---\nwizard: 0.2\nid: demo\nowner_proof: hmac-v9\n---\n")
    rows = {name: ok for name, ok, _ in wizardmd.check(tmp_path)}
    assert rows["owner_proof valid"] is False
    (tmp_path / "wizard.md").write_text("---\nwizard: 0.2\nid: demo\nowner_proof: hmac-v1\n---\n")
    assert {name: ok for name, ok, _ in wizardmd.check(tmp_path)}["owner_proof valid"] is True
