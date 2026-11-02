"""Synthetic only: settings reconciliation belongs inside single-app mutation admission."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wam import app_updates as au, cli, network


@pytest.fixture
def box(tmp_path, monkeypatch):
    recipe = tmp_path / "repos/wizardapps/apps/conn"
    recipe.mkdir(parents=True)
    (recipe / "config.json").write_text(json.dumps({"form_fields": [
        {"env_variable": "NEW_PASSWORD", "type": "password", "required": True}]}))
    (recipe / "wizard.md").write_text("---\nid: conn\n---\n")
    calls = []

    class RT:
        response = {"app": {"status": "stopped", "version": 1,
                            "config": {"KEEP": "owner", "openPort": True}}}

        def app(self, urn):
            calls.append(("read", urn))
            return self.response

        def req(self, method, path, body, **kwargs):
            calls.append((method, path, body))

        def update(self, urn, **kwargs):
            calls.append(("queue", urn))

        def wait_status(self, *args):
            return "stopped"

        def installed(self):
            return [{"app": {"appName": "conn", "appStoreSlug": "wizardapps", "status": "stopped", "version": 1},
                     "metadata": {"latestVersion": 2}}]

    rt = RT()
    c = SimpleNamespace(home=tmp_path / "home", runtipi_root=tmp_path,
                        secrets=tmp_path / "secrets", runtipi=lambda: rt)
    monkeypatch.setattr(cli, "shared_value", lambda *a: None)
    monkeypatch.setattr(cli, "host_access_risks", lambda *a: [])
    monkeypatch.setattr(network, "host_access_allowed", lambda *a: [])
    monkeypatch.setattr(network, "enabled", lambda *a: False)
    monkeypatch.setattr(cli, "network_step", lambda *a, **kw: calls.append(("network",)) or {})
    monkeypatch.setattr(cli.time, "sleep", lambda *a: None)
    monkeypatch.setattr(cli, "write_registry", lambda *a: {})
    monkeypatch.setattr(au, "_containers", lambda *a: [])
    monkeypatch.setattr(au, "baseline", lambda *a: None)
    monkeypatch.setattr(au, "verify", lambda *a: None)
    monkeypatch.setattr(au, "_release_managed", lambda: set())

    return c, rt, calls, recipe


def mutations(calls):
    return [x for x in calls if x[0] != "read"]


def test_direct_update_reconciles_inside_queue_admission(box):
    c, rt, calls, recipe = box
    cli.app_update(c, "conn", backup=False)
    patch, = [x for x in calls if x[0] == "PATCH"]
    assert patch[1] == "/app-lifecycle/conn:wizardapps/update-config"
    assert patch[2]["KEEP"] == "owner" and patch[2]["openPort"] is True
    assert len(patch[2]["NEW_PASSWORD"]) >= 24
    assert calls.index(patch) < calls.index(("queue", "conn:wizardapps"))
    assert (c.secrets / "conn.json").is_file()


def test_host_refusal_precedes_batch_reconciliation(box, monkeypatch):
    c, rt, calls, recipe = box
    monkeypatch.setattr(cli, "host_access_risks", lambda c, a, where: ["host"] if where == "store" else [])
    result = au.apply(c, only=["conn"], notify=False)
    assert result["needs_owner"]
    assert mutations(calls) == []
    assert not c.secrets.exists()


@pytest.mark.parametrize("response", [None, [], {}, {"app": None}, {"app": []},
    {"app": {"status": "stopped"}}, {"app": {"status": "stopped", "config": None}},
    {"app": {"status": "stopped", "config": []}}, {"app": {"status": "stopped", "config": "bad"}},
    {"app": {"status": "updating", "config": {}}},
    {"app": {"status": "stopped", "config": {}, "appStoreSlug": "other"}},
    {"app": {"status": "stopped", "config": {}, "appName": "someone-else"}}])
def test_bad_readback_refuses_without_mutation(box, response):
    c, rt, calls, recipe = box
    rt.response = response
    with pytest.raises(SystemExit):
        cli.app_update(c, "conn", backup=False)
    assert mutations(calls) == []
    assert not c.secrets.exists()


def test_manual_policy_blocks_all_automatic_mutation(box):
    c, rt, calls, recipe = box
    c.home.mkdir()
    (c.home / au.STATE).write_text('{"mode":"ask"}')
    result = cli.app_update(c, "conn", automatic=True, backup=False)
    assert result["skipped"] == "manual"
    assert mutations(calls) == [] and not c.secrets.exists()
    # Explicit one-shot still works without changing the saved choice.
    cli.app_update(c, "conn", backup=False)
    assert any(x[0] == "PATCH" for x in calls)
    assert json.loads((c.home / au.STATE).read_text())["mode"] == "ask"


@pytest.mark.parametrize("policy", ['null', '[]', '{', '{"mode":null}', '{"mode":"bad"}'])
def test_malformed_policy_is_not_automatic_permission(box, policy):
    c, rt, calls, recipe = box
    c.home.mkdir()
    (c.home / au.STATE).write_text(policy)
    result = cli.app_update(c, "conn", automatic=True, backup=False)
    assert result["skipped"] == "manual"
    assert mutations(calls) == [] and not c.secrets.exists()


def test_missing_owner_setting_leaves_generated_secrets_unwritten(box):
    c, rt, calls, recipe = box
    data = json.loads((recipe / "config.json").read_text())
    data["form_fields"].append({"env_variable": "API_URL", "type": "url", "required": True})
    (recipe / "config.json").write_text(json.dumps(data))
    with pytest.raises(SystemExit, match="required settings"):
        cli.app_update(c, "conn", backup=False)
    assert mutations(calls) == [] and not c.secrets.exists()


def test_foreign_store_identity_never_uses_wizard_recipe_or_secrets(box):
    c, rt, calls, recipe = box
    cli.app_update(c, "conn:other", backup=False)
    assert mutations(calls) == [("queue", "conn:other")]
    assert not c.secrets.exists()


@pytest.mark.parametrize("modified", [False, True])
def test_unverified_or_dirty_source_blocks_settings(box, modified):
    c, rt, calls, recipe = box
    live = c.runtipi_root / "apps/wizardapps/conn"
    live.mkdir(parents=True)
    (live / "wizard.md").write_text("---\nid: conn\nruntime:\n  package: source\n---\n")
    commit = "a" * 40
    (live / ".wizard-commit").write_text(commit)
    if modified:
        from wam import source_integrity as si, packaging
        source = packaging.source_dir(packaging.app_data_root(c.runtipi_root), "wizardapps", "conn", commit)
        source.mkdir(parents=True)
        (source / "app.py").write_text("original")
        si.record(source)
        (source / "app.py").write_text("owner edit")
    with pytest.raises(SystemExit, match="source"):
        cli.app_update(c, "conn", backup=False)
    assert mutations(calls) == [] and not c.secrets.exists()


def test_clean_recorded_source_can_reconcile(box):
    c, rt, calls, recipe = box
    from wam import source_integrity as si, packaging
    live = c.runtipi_root / "apps/wizardapps/conn"
    live.mkdir(parents=True)
    (live / "wizard.md").write_text("---\nid: conn\nruntime:\n  package: source\n---\n")
    commit = "a" * 40
    (live / ".wizard-commit").write_text(commit)
    source = packaging.source_dir(packaging.app_data_root(c.runtipi_root), "wizardapps", "conn", commit)
    source.mkdir(parents=True)
    (source / "app.py").write_text("original")
    si.record(source)
    cli.app_update(c, "conn", backup=False)
    assert any(x[0] == "PATCH" for x in calls)
