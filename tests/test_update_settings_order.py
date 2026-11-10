"""#582: new required settings are filled only once an app update is admitted.

App updates (nightly and release) used to PATCH the saved settings and write generated passwords BEFORE
cli.app_update ran its owner gate, so an update refused for new host access still changed the app's settings.
Synthetic data only: no Docker, Runtipi or owner data.
"""
import json

import pytest

from wam import app_updates as au
from wam import network as net
from test_network import update_env

PLAIN = "services:\n  x:\n    image: a\n"
SNEAKY = PLAIN + "    networks: [sneak]\nnetworks:\n  sneak:\n    external: true\n    name: wizard-link-a--b\n"


def recorder(log, missing=()):
    def settings(rt, urn):
        log.append(("settings", urn))
        return list(missing)
    return settings


def test_a_refused_update_never_touches_settings(tmp_path, monkeypatch):
    cli, updates, steps = update_env(tmp_path, monkeypatch, PLAIN, SNEAKY)
    log = []
    with pytest.raises(SystemExit, match="REFUSED: the update of x asks for access"):
        cli.app_update(cli.Config(), "x", settings=recorder(log))
    assert log == [] and updates == [] and steps == []


def test_settings_are_filled_after_admission_and_before_the_update(tmp_path, monkeypatch):
    cli, updates, steps = update_env(tmp_path, monkeypatch, PLAIN, PLAIN)
    log = []
    real_step = cli.network_step
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: (log.append("network"), real_step(*a, **k))[1])
    assert cli.app_update(cli.Config(), "x", settings=recorder(log))["status"] == "running"
    assert log[:2] == [("settings", "x:wizardapps"), "network"] and updates == ["x:wizardapps"]


def test_a_setting_only_the_owner_can_give_stops_before_anything_changes(tmp_path, monkeypatch):
    cli, updates, steps = update_env(tmp_path, monkeypatch, PLAIN, SNEAKY)
    log = []
    with pytest.raises(SystemExit, match="the new version needs API_KEY"):
        cli.app_update(cli.Config(), "x", allow_host_access=True, settings=recorder(log, ["API_KEY (API key: )"]))
    assert log == [("settings", "x:wizardapps")] and updates == [] and steps == []
    assert net.host_access_allowed(tmp_path / "home") == set()   # the owner flag was not recorded either


def test_an_incomplete_form_leaves_no_generated_password_on_disk(tmp_path):
    from wam import cli

    class C:
        secrets = tmp_path / "secrets"
        home = tmp_path
    info = {"form_fields": [{"env_variable": "PW", "required": True, "type": "password"},
                            {"env_variable": "API_KEY", "required": True, "type": "text"}]}
    _, missing = cli.build_form(C(), "x", info, {})
    assert missing and not (tmp_path / "secrets").exists()
    form, missing = cli.build_form(C(), "x", {"form_fields": info["form_fields"][:1]}, {})
    assert not missing and json.loads((tmp_path / "secrets/x.json").read_text())["PW"] == form["PW"]


def test_nightly_updates_reconcile_inside_the_admitted_update(tmp_path, monkeypatch):
    from wam import cli, updates
    monkeypatch.setattr(updates, "installing", lambda home: False)
    monkeypatch.setattr(au, "candidates", lambda *a, **k: [{"id": "wizard-claude", "name": "Claude", "from": 3,
                                                            "to": 9, "to_version": "0.9", "status": "running",
                                                            "skip": None}])
    monkeypatch.setattr(au, "_containers", lambda urn: ["running"])
    monkeypatch.setattr(au, "baseline", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(au, "reconcile_settings", lambda c, rt, app_id: calls.append(("reconcile", app_id)) or
                        ["API_KEY (API key: )"])

    def app_update(c, app_id, settings=None):
        calls.append(("update", app_id))   # the owner gate would run here, before settings
        missing = settings("rt", f"{app_id}:wizardapps")
        if missing:
            raise SystemExit("the new version needs " + "; ".join(missing) + "\nNothing changed")
        raise AssertionError("not reached")
    monkeypatch.setattr(cli, "app_update", app_update)
    monkeypatch.setattr(cli, "write_registry", lambda c, rt: None)
    monkeypatch.setattr(au, "_notify", lambda *a, **k: True)

    class C:
        home = tmp_path
        runtipi_root = tmp_path

        def runtipi(self):
            return type("Rt", (), {"installed": lambda self: []})()
    out = au.apply(C(), renames=lambda c, rt: [], reporter=lambda *a, **k: {})
    assert calls == [("update", "wizard-claude"), ("reconcile", "wizard-claude")]
    assert out["needs_owner"] == [{"id": "wizard-claude", "reason": "the new version needs API_KEY (API key: )"}]
