"""System apps = slot defaults: apps.json says so per app."""
from wam import release_pins, slots


def test_registry_marks_slot_defaults_as_system(monkeypatch):
    monkeypatch.setattr(release_pins, "load", lambda: None)
    reg = {"apps": [{"id": "files", "provides": ["files"]}, {"id": "wizard-cloudflare", "provides": []},
                    {"id": "wizard-tinfoil", "provides": ["ai-connection"]}, {"id": "cinequeue", "provides": []}]}
    slots.merge_into_registry(reg)
    flags = {a["id"]: a["system"] for a in reg["apps"]}
    assert flags == {"files": True, "wizard-cloudflare": True, "wizard-tinfoil": False, "cinequeue": False}


def test_release_decides_which_apps_are_system(monkeypatch):
    doc = {"default_apps": {"files": {"app": "files", "repo": "oscarhenrycollins/files", "commit": "a" * 40,
                                      "index_version": "1"}}}
    monkeypatch.setattr(release_pins, "load", lambda: doc)
    reg = {"apps": [{"id": "files", "provides": ["files"]}, {"id": "wizard-search", "provides": ["web-search"]}]}
    slots.merge_into_registry(reg)
    assert {a["id"]: a["system"] for a in reg["apps"]} == {"files": True, "wizard-search": False}



def test_search_is_the_web_search_default(monkeypatch):
    monkeypatch.setattr(release_pins, "load", lambda: None)
    row = slots.load_defaults()["web-search"]
    assert row["app"] == "wizard-search" and row["required"] is False
    assert "wizard-search" in slots.to_install({"apps": []})
    assert "wizard-search" not in slots.to_install({"apps": [{"id": "wizard-search", "provides": ["web-search"]}]})
    assert slots.system_apps() >= {"wizard-search"}
