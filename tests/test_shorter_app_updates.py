"""#1074 (box switch wizard/shorter-app-updates, off by default): with it on, the update request of a running app with
a backup asks Runtipi for the short path (one stop, data copied ahead, new version started from the backup's stop).
Off, the request body is exactly what it was. The Runtipi side is tested in packages/backend
(update-app-command.test.ts, backup-archive.linux.test.ts)."""
import json

from wam import cli, feature_switches, short_updates
from wam.runtipi import Runtipi

URN = "wizard-calendar:wizardapps"


class _RT:
    def __init__(self, status="running"):
        self.status, self.calls = status, []

    def app(self, urn):
        return {"app": {"status": self.status, "version": 45}}

    def update(self, urn, perform_backup=True, **kw):
        self.calls.append(("update", perform_backup, kw))

    def wait_status(self, urn, want, timeout=900):
        return self.status

    def start(self, urn):
        self.status = "running"


def _update(tmp_path, monkeypatch, rt, *, on, backup=True):
    c = cli.Config.__new__(cli.Config)
    c.runtipi_root, c.home = tmp_path / "rt", tmp_path / "wos"
    c.runtipi_root.mkdir()
    c.home.mkdir()
    asked = []
    monkeypatch.setattr(short_updates, "on", lambda home=None: asked.append(home) or on)
    monkeypatch.setattr(c, "runtipi", lambda: rt, raising=False)
    monkeypatch.setattr(cli.alongside, "load", lambda h: None)
    monkeypatch.setattr(cli, "host_access_risks", lambda *a, **k: [])
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: {})
    monkeypatch.setattr(cli, "backfill_owner_login", lambda *a, **k: False)
    monkeypatch.setattr(cli, "keep_trusted_hosts", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_app_container_states", lambda urn: [])
    monkeypatch.setattr(cli.update_backups, "prune_app", lambda *a, **k: {"deleted": [], "errors": []})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    out = cli._app_update(c, "wizard-calendar", backup=backup)
    return out, asked


def test_switch_on_asks_runtipi_for_one_stop(tmp_path, monkeypatch):
    rt = _RT("running")
    out, asked = _update(tmp_path, monkeypatch, rt, on=True)
    assert rt.calls == [("update", True, {"short_downtime": True})]
    assert asked == [tmp_path / "wos"] and out["status"] == "running" and not out.get("error")


def test_switch_off_sends_the_same_request_as_before(tmp_path, monkeypatch):
    rt = _RT("running")
    _update(tmp_path, monkeypatch, rt, on=False)
    assert rt.calls == [("update", True, {})]


def test_no_backup_or_a_stopped_app_never_asks(tmp_path, monkeypatch):
    rt = _RT("running")
    _, asked = _update(tmp_path, monkeypatch, rt, on=True, backup=False)
    assert rt.calls == [("update", False, {})] and asked == []
    rt = _RT("stopped")
    (tmp_path / "rt").rename(tmp_path / "rt0")
    (tmp_path / "wos").rename(tmp_path / "wos0")
    _, asked = _update(tmp_path, monkeypatch, rt, on=True)
    assert rt.calls == [("update", True, {})] and asked == []


def test_request_body(monkeypatch):
    sent = []
    rt = Runtipi.__new__(Runtipi)
    monkeypatch.setattr(rt, "req", lambda m, p, body=None, timeout=None: sent.append((m, p, json.dumps(body))) or {},
                        raising=False)
    rt.update(URN)
    rt.update(URN, perform_backup=True, short_downtime=True)
    assert sent == [("PATCH", f"/app-lifecycle/{URN}/update", '{"performBackup": true}'),
                    ("PATCH", f"/app-lifecycle/{URN}/update", '{"performBackup": true, "wizardShortDowntime": true}')]


def test_switch_reads_the_box_switch(monkeypatch):
    seen = []
    monkeypatch.setattr(feature_switches, "is_on", lambda tid, home=None, timeout=10: seen.append((tid, timeout)) or True)
    assert short_updates.on() is True and seen == [("wizard/shorter-app-updates", 4)]
