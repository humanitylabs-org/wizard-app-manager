"""Switch-off differential tests against frozen main 58eb02d, synthetic data only.

The baseline functions are verbatim main source, not a second production path.
No Docker, Runtipi, installed owner data or tailnet is accessed.
"""
import contextlib
import io
import json
import shutil
from pathlib import Path

import pytest

from wam import cli, host_access_gate as gate, network, owner_proof, owner_edge, links, registry
from test_network import gate_env, update_env
from test_owner_proof import fake_tailscale, short_home, _reg_with_messenger

BASELINE = json.loads((Path(__file__).parent / "fixtures/owner-proof-main.json").read_text())
ROUTER = "services:\n  x:\n    image: a\n    volumes: ['${ROOT_FOLDER_HOST}/repos:/repos', '/app-data/wizard-os:/wizard']\n"
RECIPES = {
    "plain": "services:\n  x: {image: a}\n",
    "router": ROUTER,
    "privileged": "services:\n  x: {image: a, privileged: true}\n",
    "network": "services:\n  x: {image: a, network_mode: host}\n",
    "socket": "services:\n  x: {image: a, volumes: ['/var/run/docker.sock:/s']}\n",
    "form": "services:\n  x: {image: a, volumes: ['${MEDIA:-${APP_DATA_DIR}/m}:/m:ro']}\n",
    "capability": "services:\n  x: {image: a, cap_add: [SYS_ADMIN]}\n",
}


def baseline(name):
    scope = dict(vars(cli))
    exec(BASELINE["cli.host_access_risks"], scope)
    exec(BASELINE["cli." + name], scope)
    return scope[name]


def observe(call, path, events, steps):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            result = ("return", call())
        except SystemExit as exc:
            result = ("exit", str(exc))
    files = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    return result, out.getvalue(), err.getvalue(), list(events), list(steps), files


@pytest.mark.parametrize("setting", [None, False])
@pytest.mark.parametrize("recipe", sorted(RECIPES))
@pytest.mark.parametrize("flag,allowed", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize("operation", ["install", "app_update"])
def test_off_is_byte_identical_to_main(tmp_path, monkeypatch, setting, recipe, flag, allowed, operation):
    path = tmp_path / "lane"
    observations = []
    for old in (True, False):
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir()
        with monkeypatch.context() as mp:
            if operation == "install":
                _, events, steps = gate_env(path, mp, RECIPES[recipe])
                home = path / "home"
                home.mkdir()
            else:
                _, events, steps = update_env(path, mp, RECIPES["plain"], RECIPES[recipe])
                home = path / "home"
            settings = {} if setting is None else {"owner_host_access_grants": setting, "owner_proof_enabled": setting}
            if allowed:
                settings["host_access_allowed"] = ["x"]
            (home / "app-manager.json").write_text(json.dumps(settings))
            assert not gate.enabled(home) and not owner_proof.enabled(home)
            # Any accidental call into the enabled-only path is a regression, even if it returns a refusal.
            for method in ("valid", "consume", "issue"):
                mp.setattr(gate, method, lambda *a, **k: pytest.fail("grant path reached with switch off"))
            mp.setattr(network, "owner_host_access", lambda *a, **k: pytest.fail("enhanced detection reached while off"))
            fn = baseline(operation) if old else getattr(cli, operation)
            observations.append(observe(lambda: fn(cli.Config(), "x", allow_host_access=flag), path, events, steps))
    assert observations[0] == observations[1]


def test_legacy_detector_is_verbatim_main():
    import inspect
    assert inspect.getsource(network.host_access).strip() == BASELINE["network.host_access"].strip()


@pytest.mark.parametrize("allowed", [False, True])
def test_network_allow_off_keeps_cli_and_policy_bytes(tmp_path, monkeypatch, allowed):
    from test_network import Cfg
    c = Cfg(tmp_path)
    mp = monkeypatch
    mp.setattr(cli, "Config", lambda: c)
    mp.setattr(c, "runtipi", lambda: object(), raising=False)
    mp.setattr(cli, "network_step", lambda *a, **k: {})
    mp.setattr(network, "reconcile", lambda *a, **k: {})
    mp.setattr(cli, "write_registry", lambda *a, **k: {})
    mp.setattr(gate, "consume", lambda *a, **k: pytest.fail("off CLI consumed a grant"))
    if allowed:
        network.set_host_access_allowed(c.home, "x", True)
    assert cli.main(["network", "allow", "x"]) == 0
    assert network.host_access_allowed(c.home) == {"x"}
    network.write_policy(c, True)
    assert network.policy_path(c.runtipi_root).read_bytes() == b'{\n  "schema": 1,\n  "enabled": true,\n  "allow_host_access": [\n    "x"\n  ]\n}\n'


def test_off_cannot_provision_or_start_edge(tmp_path):
    c = type("C", (), {"home": tmp_path})()
    with pytest.raises(ValueError, match="owner proof is off"):
        owner_proof.provision(c, "x")
    with pytest.raises(ValueError, match="owner proof is off"):
        owner_edge.run(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_off_routes_declared_app_like_main_even_with_stale_edge_socket(short_home, fake_tailscale):
    import socket
    calls, _ = fake_tailscale
    p = owner_proof.socket_path(short_home, "messenger")
    p.parent.mkdir(parents=True)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(p))
    try:
        res = links.publish(_reg_with_messenger(), short_home, {"wizard-shell"})
    finally:
        sock.close()
    row = next(x for x in res["links"] if x["id"] == "messenger")
    assert "owner_proof" not in row and "kept" not in row
    assert "unix:" not in calls.read_text()
    assert "http://127.0.0.1:9245" in calls.read_text()


def test_off_registry_ignores_owner_proof_declaration(tmp_path):
    d = tmp_path / "apps/wizardapps/x"
    d.mkdir(parents=True)
    manifest = d / "wizard.md"
    manifest.write_text("---\nname: X\nowner_proof: hmac-v1\n---\n")
    args = ([{"id": "x:wizardapps", "info": {"id": "x", "port": 9999}}], "wizardapps", "127.0.0.1", tmp_path)
    declared = registry.build(*args)
    manifest.write_text("---\nname: X\n---\n")
    assert declared == registry.build(*args)
