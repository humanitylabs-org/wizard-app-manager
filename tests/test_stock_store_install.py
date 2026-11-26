"""wizard-os#822: on a stock-Runtipi box (switch WIZARD_STOCK_RUNTIPI) wam installs from the generated git store
(wizard-app-store `stock` branch) and never asks the App Index; with the switch off nothing changes."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from wam import cli, index, stock_runtipi

TRIAL = "https://github.com/humanitylabs-org/wizard-app-store/tree/builder/822-trial"


def store_app(root: Path, app_id: str, repo: str, uses=(), provides=(), commit="a" * 40):
    d = root / "repos" / "wizardapps" / "apps" / app_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"id": app_id, "port": 8000, "tipi_version": 1}))
    (d / "docker-compose.yml").write_text("services: {}\n")
    fm = {"id": app_id, "name": app_id.title(), "uses": list(uses), "provides": list(provides)}
    (d / "wizard.md").write_text("---\n" + json.dumps(fm) + "\n---\n# " + app_id + "\n")
    (d / ".wizard-source").write_text(json.dumps({"repo": repo, "source": "stock-store"}))
    (d / ".wizard-commit").write_text(commit + "\n")


class FakeRt:
    def __init__(self, root: Path, stores=None, on_pull=None):
        self.root, self._stores, self.calls, self.on_pull = root, list(stores or []), [], on_pull

    def stores(self):
        return list(self._stores)

    def req(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        if path == "/marketplace/create":
            self._stores.append({"slug": body["name"], "name": body["name"], "url": body["url"], "enabled": False})
        elif method == "PATCH" and path.startswith("/marketplace/"):
            for s in self._stores:
                if s["slug"] == path.rsplit("/", 1)[1]:
                    s.update(enabled=body.get("enabled", s["enabled"]), name=body.get("name", s["name"]))
        return {}

    def pull(self):
        self.calls.append(("POST", "/marketplace/pull", None))
        if self.on_pull:
            self.on_pull()
        return {}


@pytest.fixture
def box(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "runtipi"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.setenv("RUNTIPI_ROOT", str(root))
    monkeypatch.delenv("WIZARD_STOCK_RUNTIPI", raising=False)
    monkeypatch.delenv("WIZARD_STOCK_STORE", raising=False)
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)

    def no_index(*a, **k):
        raise AssertionError("a stock box must never ask the App Index")
    monkeypatch.setattr(index, "resolve", no_index)
    c = cli.Config()
    return c, home, root


def test_marker_records_switch_and_store(box, monkeypatch):
    c, home, _ = box
    assert not stock_runtipi.on(home)
    monkeypatch.setenv("WIZARD_STOCK_STORE", TRIAL)
    stock_runtipi.write_marker(home, store=stock_runtipi.box_store_url(home))
    monkeypatch.delenv("WIZARD_STOCK_STORE")
    assert stock_runtipi.on(home)
    assert stock_runtipi.box_store_url(home) == TRIAL   # a later run (no env) keeps the store it was installed with
    monkeypatch.setenv("WIZARD_STOCK_RUNTIPI", "0")
    assert not stock_runtipi.on(home)


def test_record_reads_the_store_copy(box):
    _, _, root = box
    store_app(root, "wizard-ux", "humanitylabs-org/wizard-ux", uses=["agent-service"], provides=["shell"])
    rec = stock_runtipi.record(root, "wizardapps", "wizard-ux")
    assert rec["repo"] == "humanitylabs-org/wizard-ux" and rec["uses"] == ["agent-service"]
    assert rec["provides"] == ["shell"] and rec["install"]["commit"] == "a" * 40
    with pytest.raises(stock_runtipi.NotInStore):
        stock_runtipi.record(root, "wizardapps", "nope")


def test_stock_install_source_is_the_git_store_not_the_index(box, monkeypatch):
    c, home, root = box
    stock_runtipi.write_marker(home, store=TRIAL)
    rt = FakeRt(root)
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: rt)
    # first resolve: no store yet -> created as `wizardapps` with the stock URL and pulled; the pull brings the app
    rt.on_pull = lambda: store_app(root, "files", "humanitylabs-org/files")
    rec = cli.resolve_any(c, "files")
    assert rec["id"] == "files" and rec["source"] == "stock-store"
    assert ("POST", "/marketplace/create", {"name": "wizardapps", "url": TRIAL}) in rt.calls
    assert rt._stores[0]["enabled"] is True
    # by repo works too; a missing app gets one more pull, then a plain refusal (never the index)
    assert cli.resolve_any(c, "humanitylabs-org/files")["id"] == "files"
    n = len(rt.calls)
    rt.on_pull = None
    with pytest.raises(index.IndexError_, match="Wizard store"):
        cli.resolve_any(c, "not-there")
    assert rt.calls[n:] == [("POST", "/marketplace/pull", None)]


def test_stock_store_refuses_a_wizardapps_store_pointing_elsewhere(box, monkeypatch):
    c, home, root = box
    stock_runtipi.write_marker(home, store=TRIAL)
    rt = FakeRt(root, stores=[{"slug": "wizardapps", "name": "Wizard Apps", "url": "https://apps.aiwizards.com",
                               "enabled": True}])
    with pytest.raises(SystemExit, match="not the stock store"):
        cli.ensure_store(c, rt)
    assert rt.calls == []


def test_release_updates_on_stock_follow_the_store_not_the_release_pins(box, monkeypatch):
    c, home, root = box
    stock_runtipi.write_marker(home, store=TRIAL)
    monkeypatch.setattr(cli.release_pins, "resolve", lambda ref: (_ for _ in ()).throw(AssertionError("no release pins")))
    for app_id, repo in (("wizard-ux", "humanitylabs-org/wizard-ux"), ("files", "humanitylabs-org/files")):
        store_app(root, app_id, repo, commit="b" * 40)
        live = root / "apps" / "wizardapps" / app_id
        live.mkdir(parents=True)
        (live / "config.json").write_text(json.dumps({"tipi_version": 1}))
        (live / ".wizard-commit").write_text("a" * 40)
    # the store's wizard-ux is newer (tipi 2); files is the same
    cfg = root / "repos" / "wizardapps" / "apps" / "wizard-ux" / "config.json"
    cfg.write_text(json.dumps({"id": "wizard-ux", "tipi_version": 2}))
    rt = FakeRt(root, stores=[{"slug": "wizardapps", "name": "Wizard Apps", "url": TRIAL, "enabled": True}])
    rt.installed = lambda: [{"app": {"id": a, "appName": a}, "urn": f"{a}:wizardapps"} for a in ("wizard-ux", "files")]
    monkeypatch.setattr(cli, "installed_ids", lambda rt_: ["wizard-ux", "files"])
    rt.app = lambda urn: {"app": {"status": "running", "config": {}}}
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: rt)
    updated = []

    def fake_update(c_, app_id, **k):
        updated.append(app_id)
        (root / "apps" / "wizardapps" / app_id / ".wizard-commit").write_text("b" * 40)
        return {"status": "running"}
    monkeypatch.setattr(cli, "app_update", fake_update)
    failed = {}
    assert cli.release_updates(c, rt, failed) == ["wizard-ux"] and updated == ["wizard-ux"] and failed == {}
    assert ("POST", "/marketplace/pull", None) in rt.calls


def test_switch_off_keeps_the_index_store(box, monkeypatch):
    c, home, root = box
    rt = FakeRt(root)
    cli.ensure_store(c, rt)   # unchanged path: the store is created with the index URL
    assert ("POST", "/marketplace/create", {"name": "wizardapps", "url": c.index}) in rt.calls
    seen = []
    monkeypatch.setattr(index, "resolve", lambda base, ref: seen.append(ref) or {"id": ref, "repo": "o/r"})
    store_app(root, "files", "humanitylabs-org/files")
    assert cli.resolve_any(c, "files")["repo"] == "o/r" and seen == ["files"]


def test_installer_reads_the_switch_like_wam(box, monkeypatch, tmp_path):
    """install.sh decides with wam's own reader: unset and no marker = 0 (nothing changes), env or marker = 1."""
    _, home, _ = box
    here = Path(__file__).resolve().parents[1]
    line = next(x for x in (here / "install.sh").read_text().splitlines() if x.startswith("STOCK=\"$("))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "WIZARD_OS_HOME": str(home)}

    def stock(**extra):
        out = subprocess.run(["bash", "-c", f"HERE={here}\n{line}\necho $STOCK"], env={**env, **extra},
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    assert stock() == "0"
    assert stock(WIZARD_STOCK_RUNTIPI="1") == "1"
    stock_runtipi.write_marker(home, store=TRIAL)
    assert stock() == "1"
    assert stock(WIZARD_STOCK_RUNTIPI="0") == "0"


@pytest.mark.parametrize("on", [True, False])
def test_recover_starts_an_app_a_reboot_left_restarting_only_on_stock(box, monkeypatch, on):
    """Seen on a stock box rebooted mid-installer: hermes-agent kept status `restarting` with its containers stopped
    (stock Runtipi never settles it), so the rerun left the agent down. A stopping app stays stopped."""
    c, home, _ = box
    if on:
        stock_runtipi.write_marker(home, store=TRIAL)
    rows = [("wizard-ux:wizardapps", "updating"), ("hermes-agent:wizardapps", "restarting"),
            ("browser:wizardapps", "stopping")]
    rt = type("RT", (), {"installed": lambda self: [{"info": {"urn": u}, "app": {"status": s}} for u, s in rows]})()
    monkeypatch.setattr(c, "runtipi", lambda: rt, raising=False)
    started = []
    monkeypatch.setattr(cli.slots, "load_defaults", lambda: {"shell": {"app": "wizard-ux"}})
    monkeypatch.setattr(cli, "_recover_states", lambda urn, settle: ["running"] if "ux" in urn else ["exited"])
    monkeypatch.setattr(cli, "app_lifecycle", lambda c, verb, urn: started.append(urn) or {"status": "running"})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: {})
    res = cli.recover(c)
    assert started == (["hermes-agent:wizardapps"] if on else [])
    assert res["failed"] == {}
