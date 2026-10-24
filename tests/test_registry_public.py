"""Public registry boundaries must not expose internal skill-copy paths."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from test_wam import _fake_store_app
from wam import cli, registry


@pytest.fixture
def config(tmp_path, monkeypatch):
    root = tmp_path / "runtipi"
    source = _fake_store_app(root)
    installed = [{"app": {"status": "running", "port": 8799},
                  "info": {"urn": "hello-wizard:wizardapps"}}]
    c = SimpleNamespace(home=tmp_path / "home", runtipi_root=root,
                        host_ip=lambda: "127.0.0.1",
                        runtipi=lambda: SimpleNamespace(installed=lambda: installed))
    monkeypatch.setattr(cli, "Config", lambda: c)
    return c, source


def assert_public_and_skills(c, source, result):
    assert all(not key.startswith("_") for app in result["apps"] for key in app)
    assert result["apps"][0]["skill"] == "skills/hello-wizard/SKILL.md"
    assert "parking" in result
    for dest in (c.home, c.runtipi_root / "app-data" / "wizard-os"):
        assert json.loads((dest / "apps.json").read_text()) == result
        assert (dest / "skills" / "hello-wizard" / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()


def test_public_projection_does_not_mutate_copy_metadata():
    reg = {"apps": [{"id": "test", "_skill_src": "/synthetic/path", "_internal": True}], "slots": {}}
    assert registry.public_projection(reg)["apps"] == [{"id": "test"}]
    assert reg["apps"][0]["_skill_src"] == "/synthetic/path"


def test_write_registry_returns_public_data(config):
    c, source = config
    assert_public_and_skills(c, source, cli.write_registry(c))
    assert_public_and_skills(c, source, cli.write_registry(c))


def test_owner_route_projection_matches_return_and_shared_registry(config):
    c, source = config
    c.home.mkdir(parents=True)
    (c.home / "routes.json").write_text(json.dumps({
        "hello-wizard": {"view": "/hello-wizard-next/", "staged_as": "hello-wizard-next"}
    }))
    result = cli.write_registry(c)
    assert_public_and_skills(c, source, result)
    assert result["apps"][0]["id"] == "hello-wizard-next"
    assert result["apps"][0]["view"] == {"url": "/hello-wizard-next/"}
    assert result["apps"][0]["app_id"] == "hello-wizard"


def test_private_inference_survives_registry_rewrite(config):
    c, source = config
    manifest = source / "wizard.md"
    manifest.write_text(manifest.read_text().replace("---\n", "---\nprivate_inference: {kind: local, client: passthrough, relay_port: 8090, egress: []}\n", 1))
    expected = {"kind": "local", "client": "passthrough", "relay_port": 8090, "egress": []}
    for _ in range(2):
        result = cli.write_registry(c)
        assert result["apps"][0]["private_inference"] == expected
        for dest in (c.home, c.runtipi_root / "app-data" / "wizard-os"):
            assert json.loads((dest / "apps.json").read_text())["apps"][0]["private_inference"] == expected


def test_apps_cli_serializes_public_data(config, capsys):
    c, source = config
    assert cli.main(["apps", "--json"]) == 0
    assert_public_and_skills(c, source, json.loads(capsys.readouterr().out))


def test_registry_api_serializes_public_data(config):
    c, source = config
    class Handler(cli.Handler):
        cfg = c
        token = "synthetic-test-token"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/registry", b"{}",
                                     {"Authorization": "Bearer synthetic-test-token"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:
            assert_public_and_skills(c, source, json.load(response))
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_registry_rewrite_keeps_routing_skills(config):
    """`wam apps` (and every other registry rewrite) must not delete the wizard-job-* routing skills
    that the Agent Service / wizard-os Hermes plugin keep in the same skills/ folder."""
    c, source = config
    sk = c.home / "skills"
    (sk / "wizard-job-files").mkdir(parents=True)
    (sk / "wizard-job-files" / "SKILL.md").write_text("routing\n")
    (sk / ".wizard-jobs").write_text("1\n")
    (sk / "removed-app").mkdir()
    (sk / "removed-app" / "SKILL.md").write_text("stale\n")
    for _ in range(2):   # repeated rewrites, as timers and `wam apps --json` do
        result = cli.write_registry(c)
        assert_public_and_skills(c, source, result)
        assert (sk / "wizard-job-files" / "SKILL.md").read_text() == "routing\n"
        # Managed Mac refreshes the pointer stamp after every registry write.
        assert (sk / ".wizard-jobs").read_text().strip().isdigit()
        # Unknown skills are owner data, not ours to prune without an ownership hash.
        assert (sk / "removed-app" / "SKILL.md").read_text() == "stale\n"
        shared = c.runtipi_root / "app-data" / "wizard-os" / "skills"
        assert (shared / "wizard-job-files" / "SKILL.md").read_text() == "routing\n"
