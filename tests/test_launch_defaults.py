"""Launch intent is separate from published release pins and app capabilities."""
import json
import shutil
from pathlib import Path

import pytest
import yaml

from wam import builder, release_pins, slots, wizardmd

ROOT = Path(__file__).resolve().parents[1]
APPS = {
    "voice": ("wizard-voice-service", ["voice"]),
    "connection-router": ("wizard-connection-router", ["connection-router"]),
    "accounts": ("wizard-connectors", ["connector-status", "simulated-private-summary"]),
    "troubleshooter": ("wizard-troubleshooter", None),
    "agent-history": (None, None),
}


@pytest.fixture(autouse=True)
def developer_defaults(monkeypatch):
    monkeypatch.setattr(release_pins, "load", lambda: None)


@pytest.mark.parametrize("slot", APPS)
def test_launch_default_metadata_and_bootstrap(slot):
    app_id, provides = APPS[slot]
    default = slots.load_defaults()[slot]
    assert default["app"] == app_id
    assert default["required"] is False
    assert default["repo"] == "humanitylabs-org/" + (app_id or "wizard-agent-history")
    assert default["note"] and "\n" not in default["note"]
    if provides is not None:
        assert default["provides"] == provides
    if app_id:
        assert app_id in slots.to_install({"apps": []})
        reg = {"apps": [{"id": app_id, "provides": provides or [slot]}]}
        assert app_id not in slots.to_install(reg)
        assert not slots.blocks_uninstall(reg, app_id)
    else:
        row = next(r for r in slots.table({"apps": []}) if r["slot"] == slot)
        assert row["state"] == "no default yet"
        assert None not in slots.to_install({"apps": []})
        assert "wizard-agent-history" not in slots.to_install({"apps": []})


def test_voice_keeps_transcriber_as_its_engine():
    d = slots.load_defaults()
    assert d["transcription"]["app"] == "transcriber"
    assert d["transcription"]["provides"] == ["transcription", "speech-to-text-engine"]
    assert "transcriber" in slots.to_install({"apps": []})
    assert "speech-to-text-engine" not in d  # capability, not a second engine install
    assert slots.to_install({"apps": []}).index("transcriber") < slots.to_install({"apps": []}).index("wizard-voice-service")


def test_release_pins_still_control_the_default_set(monkeypatch):
    monkeypatch.setattr(release_pins, "load", lambda: {"default_apps": {"files": {"app": "files"}}})
    assert slots.to_install({"apps": []}) == ["files"]


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "app"
    shutil.copytree(ROOT / "templates" / "hello-wizard", root)
    return root


def set_needs(app, needs, provides=None):
    path = app / "wizard.md"
    text = path.read_text()
    doc = wizardmd.front_matter(text)
    doc["needs"] = needs
    if provides is not None:
        doc["provides"] = provides
    path.write_text("---\n" + yaml.safe_dump(doc, sort_keys=False) + "---\n" + wizardmd.body(text) + "\n")


@pytest.mark.parametrize("need", [
    "speech-to-text-engine", "transcription", "transcriber", "wizard-voice-service",
    "connection-router", "connector-status", "simulated-private-summary",
    "mic", "system-audio", "camera", "notifications", "files", "credentials:example",
])
def test_needs_accept_known_app_ids_capabilities_and_legacy_permissions(app, need):
    set_needs(app, [need])
    result = builder.check_app(app)
    assert result["ok"], [r for r in result["rules"] if not r["ok"]]


@pytest.mark.parametrize("needs", [
    ["unknown-engine"], ["speech-to-text-engine", "unknown-engine"],
    ["wizard-agent-history"], ["accounts"],
])
def test_needs_reject_unknown_names_and_unpublished_apps(app, needs):
    set_needs(app, needs, provides=["unknown-engine"])
    result = wizardmd.check(app)
    assert any(name == "needs known" and not ok for name, ok, _ in result)


def test_capabilities_come_from_known_app_metadata_not_a_new_allowlist(app, monkeypatch, tmp_path):
    defaults = tmp_path / "defaults.json"
    defaults.write_text(json.dumps({"slots": {
        "example": {"app": "example-engine", "provides": ["example-capability"]},
        "pending": {"app": None, "provides": ["not-published"]},
    }}))
    monkeypatch.setattr(wizardmd, "DEFAULTS_FILE", defaults)
    for need, expected in [("example-engine", True), ("example-capability", True),
                           ("not-published", False), ("speech-to-text-engine", False)]:
        set_needs(app, [need])
        row = next(r for r in wizardmd.check(app) if r[0] == "needs known")
        assert row[1] is expected


def test_checker_does_not_depend_on_selected_release(app, monkeypatch):
    monkeypatch.setattr(release_pins, "load", lambda: {"default_apps": {"files": {"app": "files"}}})
    set_needs(app, ["speech-to-text-engine"])
    assert builder.check_app(app)["ok"]
