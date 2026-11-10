"""What a release touches (impact) and what applying it on this box stops (interrupts)."""
import copy

from wam import update_impact as ui


def release():
    return {
        "version": "0.3.125", "name": "n", "date": "2026-11-01", "about": "a", "notes": [],
        "install": {"command": "curl … | bash"},
        "components": {n: {"repo": f"o/{n}", "commit": "a" * 40} for n in
                       ("wizard-app-manager", "wizard-troubleshooter", "wizard-agent-service", "wizard-ux")},
        "images": {n: {"ref": f"ghcr.io/o/{n}:1", "digest": "sha256:" + "1" * 64} for n in
                   ("wizard-app-manager", "wizard-ux", "wizard-agent-service")},
        "default_apps": {"shell": {"app": "wizard-ux", "commit": "a"}, "agent": {"app": "hermes-agent", "commit": "a"},
                         "agent-service": {"app": "wizard-agent-service", "commit": "a"},
                         "files": {"app": "files", "commit": "a"}, "agent-history": None},
        "hermes": {"tag": "v1", "image": {"digest": "sha256:" + "2" * 64}},
    }


BASE = release()


def test_words_only_is_none_and_never_reloads():
    want = copy.deepcopy(BASE)
    want.update(version="0.3.126", name="new name", about="b", notes=["x"], date="2026-11-02")
    assert ui.summary(BASE, want, "installer")["impact"] == "none"
    assert ui.summary(BASE, want, "pin") == {"impact": "none", "interrupts": "none", "parts": [], "reload": False}


def test_interface_only_is_ui_and_reloads():
    want = copy.deepcopy(BASE)
    want["images"]["wizard-ux"]["digest"] = "sha256:" + "9" * 64
    want["components"]["wizard-ux"]["commit"] = "b" * 40
    want["default_apps"]["shell"]["commit"] = "b"
    s = ui.summary(BASE, want, "quick")
    assert (s["impact"], s["interrupts"], s["parts"], s["reload"]) == ("ui", "ui", ["wizard-ux"], True)


def test_app_manager_and_agent_service_are_service_and_dont_reload():
    for name in ("wizard-app-manager", "wizard-agent-service", "wizard-troubleshooter"):
        want = copy.deepcopy(BASE)
        want["components"][name]["commit"] = "c" * 40
        s = ui.summary(BASE, want, "quick")
        assert (s["impact"], s["reload"]) == ("service", False), name


def test_hermes_and_the_agent_slot_are_hermes():
    want = copy.deepcopy(BASE)
    want["hermes"]["tag"] = "v2"
    assert ui.impact(BASE, want) == "hermes"
    want = copy.deepcopy(BASE)
    want["default_apps"]["agent"]["commit"] = "b"
    assert ui.impact(BASE, want) == "hermes"


def test_anything_unknown_is_hermes_never_less():
    want = copy.deepcopy(BASE)
    want["guest_tools"] = {"x": 1}                       # a new top-level part this App Manager doesn't know
    assert ui.impact(BASE, want) == "hermes"
    want = copy.deepcopy(BASE)
    want["images"]["new-thing"] = {"ref": "x"}
    assert ui.impact(BASE, want) == "hermes"
    assert ui.impact(BASE, None) == "hermes"
    want = copy.deepcopy(BASE)
    want["default_apps"]["agent-history"] = {"app": "something-new"}
    assert ui.impact(BASE, want) == "hermes"


def test_strongest_part_wins():
    want = copy.deepcopy(BASE)
    want["components"]["wizard-ux"]["commit"] = "b" * 40
    want["components"]["wizard-agent-service"]["commit"] = "b" * 40
    assert ui.impact(BASE, want) == "service"
    want["hermes"]["tag"] = "v3"
    assert ui.impact(BASE, want) == "hermes"


def test_installer_path_stops_at_least_services_and_hermes_until_the_link_is_idempotent():
    want = copy.deepcopy(BASE)
    want["components"]["wizard-ux"]["commit"] = "b" * 40
    assert ui.interrupts("ui", "installer", keeps_hermes=False) == "hermes"
    assert ui.interrupts("ui", "installer", keeps_hermes=True) == "service"
    assert ui.interrupts("none", "installer", keeps_hermes=True) == "service"
    assert ui.interrupts("hermes", "installer", keeps_hermes=True) == "hermes"
    assert ui.interrupts("ui", "quick") == "ui"
    assert ui.interrupts("hermes", "pin") == "none"


def test_status_reports_impact_and_reads_unreadable_releases_as_hermes(monkeypatch):
    from wam import quick_update, updates
    docs = {"0.3.125": (BASE, b""), "0.3.126": (copy.deepcopy(BASE), b"")}
    docs["0.3.126"][0]["version"] = "0.3.126"
    docs["0.3.126"][0]["components"]["wizard-ux"]["commit"] = "b" * 40
    monkeypatch.setattr(quick_update, "release_doc", lambda v, base=None, timeout=10: docs.get(v))
    s = updates.impact_of("0.3.125", "0.3.126", None, quick=True)
    assert (s["impact"], s["interrupts"], s["reload"]) == ("ui", "ui", True)
    assert updates.impact_of("0.3.125", "0.3.127", None)["interrupts"] == "hermes"
