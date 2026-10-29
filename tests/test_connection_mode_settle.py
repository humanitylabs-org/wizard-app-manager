"""Upgrade safety for app isolation: a box that ran apps before isolation keeps them connected (open) unless its owner
chose; a new box starts with apps kept apart (declared). Decided once per box, at the start of `wam bootstrap`."""
import json
from types import SimpleNamespace

import pytest

from wam import cli, network


class RT:
    def __init__(self, fail=False):
        self.posts, self.fail = [], fail

    def req(self, method, path, body=None, **kw):
        if self.fail:
            raise RuntimeError("404 not found")
        self.posts.append((method, path, body))
        return {"mode": (body or {}).get("mode")}


@pytest.fixture
def box(tmp_path):
    home, root = tmp_path / "home", tmp_path / "runtipi"
    home.mkdir()
    (root / "state").mkdir(parents=True)
    return SimpleNamespace(home=home, runtipi_root=root)


def grants(box, doc):
    (box.runtipi_root / "state" / "wizard-grants.json").write_text(json.dumps(doc))


DEFAULTS = {"wizard-ux", "wizard-agent-service", "files", "notifications", "wizard-search"}


def settle(box, rt, installed, sent=None, ok=True):
    return network.settle_mode(box, rt, set(installed), DEFAULTS,
                               notify=(lambda t, m: sent.append((t, m)) or ok) if sent is not None else None)


def test_existing_box_with_its_own_apps_keeps_them_connected(box):
    """Music → YouTube Music and Search → Tinfoil were never declared by the installed versions: declared mode would
    cut them on upgrade. No mode recorded + owner-installed apps = open, and the owner is told how to turn it on."""
    grants(box, {"grants": [], "denied": []})
    rt, sent = RT(), []
    out = settle(box, rt, {"files", "wizard-music", "wizard-youtube-music", "wizard-search", "wizard-tinfoil"}, sent)
    assert rt.posts == [("POST", "/apps/wizard/connection-mode", {"mode": "open"})]
    assert out["mode"] == "open" and out["owner_apps"] == ["wizard-music", "wizard-tinfoil", "wizard-youtube-music"]
    assert len(sent) == 1 and "Keep apps apart" in sent[0][1]
    assert json.loads((box.home / "app-manager.json").read_text())[network.SETTLED]["mode"] == "open"


def test_missing_grants_file_on_an_existing_box_is_open(box):
    rt = RT()
    assert settle(box, rt, {"files", "money"})["mode"] == "open"
    assert rt.posts[0][2] == {"mode": "open"}


def test_new_box_with_only_wizard_defaults_is_declared_and_quiet(box):
    rt, sent = RT(), []
    out = settle(box, rt, {"wizard-ux", "wizard-agent-service"}, sent)
    assert out["mode"] == "declared" and rt.posts == [("POST", "/apps/wizard/connection-mode", {"mode": "declared"})]
    assert sent == []


@pytest.mark.parametrize("mode", ["open", "declared"])
def test_an_owner_choice_is_never_changed(box, mode):
    grants(box, {"grants": [], "denied": [], "mode": mode})
    rt, sent = RT(), []
    out = settle(box, rt, {"files", "wizard-music"}, sent)
    assert out["mode"] == mode and rt.posts == [] and sent == []


def test_settled_once_then_never_again(box):
    rt = RT()
    settle(box, rt, {"files", "money"})
    grants(box, {"grants": [], "denied": []})   # even if the file later loses its mode
    assert settle(box, rt, {"files", "money"}) is None and len(rt.posts) == 1


def test_an_app_manager_without_modes_is_retried_next_run(box):
    out = settle(box, RT(fail=True), {"files", "money"})
    assert out["mode"] is None
    assert not (box.home / "app-manager.json").exists()


def test_bootstrap_settles_the_mode_before_installing_anything(monkeypatch):
    from wam import release_pins, slots
    order = []
    monkeypatch.setattr(release_pins, "load", lambda: None)
    monkeypatch.setattr(cli, "settle_connection_mode", lambda c, rt: order.append("settle") or {"mode": "open"})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt: {"apps": []})
    monkeypatch.setattr(slots, "to_install", lambda reg: ["files"])
    monkeypatch.setattr(slots, "table", lambda reg: [])
    monkeypatch.setattr(cli, "install", lambda c, a: order.append(a) or {"id": a})
    monkeypatch.setattr(cli, "release_updates", lambda *a, **k: [])
    monkeypatch.setattr(cli, "network_step", lambda c, rt: None)
    out = cli.bootstrap(SimpleNamespace(runtipi=lambda: None))
    assert order == ["settle", "files"] and out["connection_mode"] == {"mode": "open"}


def test_the_notice_is_retried_until_notifications_takes_it(box):
    """Codex: a box without a working Notifications app must not be switched to open silently."""
    rt, sent = RT(), []
    out = settle(box, rt, {"files", "money"}, sent, ok=False)
    assert out["mode"] == "open" and out["notice"] == "pending" and len(sent) == 1
    assert settle(box, rt, {"files", "money"}, sent, ok=False) is None and len(sent) == 2   # next run tries again
    assert settle(box, rt, {"files", "money"}, sent, ok=True) is None and len(sent) == 3
    assert json.loads((box.home / "app-manager.json").read_text())[network.SETTLED]["notice"] == "sent"
    assert settle(box, rt, {"files", "money"}, sent) is None and len(sent) == 3            # never twice
    assert len(rt.posts) == 1                                                               # the mode is set once


def test_bootstrap_retries_a_pending_notice_after_installing_defaults(monkeypatch):
    from wam import release_pins, slots
    calls = []
    monkeypatch.setattr(release_pins, "load", lambda: None)
    monkeypatch.setattr(cli, "settle_connection_mode",
                        lambda c, rt: calls.append(len(calls)) or {"mode": "open", "notice": "pending"})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt: {"apps": []})
    monkeypatch.setattr(slots, "to_install", lambda reg: ["notifications"])
    monkeypatch.setattr(slots, "table", lambda reg: [])
    monkeypatch.setattr(cli, "install", lambda c, a: {"id": a})
    monkeypatch.setattr(cli, "release_updates", lambda *a, **k: [])
    monkeypatch.setattr(cli, "network_step", lambda c, rt: None)
    cli.bootstrap(SimpleNamespace(runtipi=lambda: None))
    assert calls == [0, 1]


def test_notify_reports_whether_notifications_took_it(tmp_path, monkeypatch):
    from wam import app_updates
    home = tmp_path
    c = SimpleNamespace(home=home)
    assert app_updates._notify(c, "t", "m", required=True) is False            # no apps.json / no Notifications
    (home / "apps.json").write_text(json.dumps({"apps": [{"id": "notifications", "mcp": {"url": "http://x/mcp"}}]}))

    class R:
        def __init__(self, b): self.b = b
        def read(self): return self.b
    monkeypatch.setattr(app_updates.urllib.request, "urlopen", lambda *a, **k: R(b'{"result":{"isError":true}}'))
    assert app_updates._notify(c, "t", "m", required=True) is False
    monkeypatch.setattr(app_updates.urllib.request, "urlopen", lambda *a, **k: R(b'{"result":{"content":[]}}'))
    assert app_updates._notify(c, "t", "m", required=True) is True
