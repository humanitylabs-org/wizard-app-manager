"""#730: an app the #711 timeout (or any failed update) left stopped comes back: the next update or `wam recover`
starts it. An app the owner stopped stays stopped."""
import json

from wam import cli, update_strand

URN = "messenger:wizardapps"


def _log(root, *lines):
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "app.log").write_text("".join(f"{ts} - {lvl} > \t{msg}\n" for ts, lvl, msg in lines))


STARTED = ("2026-11-14T10:00:00.000Z", "info", f"App {URN} started")
TIMED_OUT = ("2026-11-15T09:00:00.000Z", "error", f"Failed to update app {URN}: RPC response timed out")
RETRY = ("2026-11-15T20:00:00.000Z", "info", f"Updating app {URN}")
STOPPING = ("2026-11-15T21:00:00.000Z", "info", f"Stopping app {URN}")


def _c(tmp_path):
    c = cli.Config.__new__(cli.Config)
    c.runtipi_root, c.home = tmp_path / "rt", tmp_path / "wos"
    c.runtipi_root.mkdir()
    c.home.mkdir()
    return c


def test_a_timed_out_update_after_a_start_leaves_it_stranded(tmp_path):
    c = _c(tmp_path)
    _log(c.runtipi_root, STARTED, TIMED_OUT, RETRY)
    assert update_strand.stranded(c, URN, "stopped")
    assert not update_strand.stranded(c, URN, "running")
    assert not update_strand.stranded(c, "messenger-two:wizardapps", "stopped")   # another app, same prefix


def test_an_owner_stop_after_the_failure_wins(tmp_path):
    c = _c(tmp_path)
    _log(c.runtipi_root, STARTED, TIMED_OUT, STOPPING)
    assert not update_strand.stranded(c, URN, "stopped")


def test_an_app_stopped_before_its_update_failed_stays_stopped(tmp_path):
    c = _c(tmp_path)
    _log(c.runtipi_root, STARTED, ("2026-11-15T08:00:00.000Z", "info", f"Stopping app {URN}"), TIMED_OUT)
    assert not update_strand.stranded(c, URN, "stopped")


def test_logs_path_from_runtipi_env_and_no_log_means_stopped_on_purpose(tmp_path):
    c = _c(tmp_path)
    assert not update_strand.stranded(c, URN, "stopped")
    other = tmp_path / "elsewhere"
    _log(other, TIMED_OUT)
    (c.runtipi_root / ".env").write_text(f"RUNTIPI_LOGS_PATH={other}\n")
    assert update_strand.stranded(c, URN, "stopped")   # a timeout forces `stopped` whatever it was before


def test_wams_record_counts_until_the_owner_stops_it(tmp_path):
    c = _c(tmp_path)
    update_strand.mark(c.home, URN, "update ended stopped")
    assert update_strand.stranded(c, URN, "stopped")
    _log(c.runtipi_root, ("2999-01-01T00:00:00.000Z", "info", f"Stopping app {URN}"))
    assert not update_strand.stranded(c, URN, "stopped")
    assert URN not in update_strand.marked(c.home)


class _RT:
    def __init__(self, status):
        self.status, self.calls = status, []

    def app(self, urn):
        return {"app": {"status": self.status, "version": "16.31"}}

    def update(self, urn, perform_backup=True):
        self.calls.append("update")

    def wait_status(self, urn, want, timeout=900):
        return self.status

    def start(self, urn):
        self.calls.append("start")
        self.status = "running"


def _update(tmp_path, monkeypatch, rt):
    c = _c(tmp_path) if not (tmp_path / "rt").exists() else None
    if c is None:
        c = cli.Config.__new__(cli.Config)
        c.runtipi_root, c.home = tmp_path / "rt", tmp_path / "wos"
    monkeypatch.setattr(c, "runtipi", lambda: rt, raising=False)
    monkeypatch.setattr(cli.alongside, "load", lambda h: None)
    monkeypatch.setattr(cli, "host_access_risks", lambda *a, **k: [])
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: {})
    monkeypatch.setattr(cli, "backfill_owner_login", lambda *a, **k: False)
    monkeypatch.setattr(cli, "keep_trusted_hosts", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return c, cli._app_update(c, "messenger", backup=False)


def test_updating_an_app_a_failed_update_left_stopped_ends_running(tmp_path, monkeypatch):
    (tmp_path / "rt").mkdir()
    (tmp_path / "wos").mkdir()
    _log(tmp_path / "rt", STARTED, TIMED_OUT)
    rt = _RT("stopped")
    _, out = _update(tmp_path, monkeypatch, rt)
    assert rt.calls == ["update", "start"]
    assert out["status"] == "running" and out["revived"] and not out.get("error")


def test_updating_an_app_the_owner_stopped_keeps_it_stopped(tmp_path, monkeypatch):
    (tmp_path / "rt").mkdir()
    (tmp_path / "wos").mkdir()
    _log(tmp_path / "rt", STARTED, STOPPING)
    rt = _RT("stopped")
    _, out = _update(tmp_path, monkeypatch, rt)
    assert rt.calls == ["update"] and out["status"] == "stopped" and not out.get("revived")


def test_a_running_app_whose_update_leaves_it_stopped_is_recorded(tmp_path, monkeypatch):
    rt = _RT("running")
    rt.wait_status = lambda urn, want, timeout=900: "stopped"
    rt.start = lambda urn: rt.calls.append("start")   # the start fails too: still stopped
    c, out = _update(tmp_path, monkeypatch, rt)
    assert out.get("error")
    assert json.loads((c.home / update_strand.STATE).read_text())[URN]["reason"]
    rt.status = "stopped"
    assert update_strand.stranded(c, URN, "stopped")


def test_recover_starts_an_app_a_failed_update_left_stopped(tmp_path, monkeypatch):
    c = _c(tmp_path)
    _log(c.runtipi_root, STARTED, TIMED_OUT,
         ("2026-11-14T10:00:00.000Z", "info", "Stopping app browser:wizardapps"))
    rows = [("wizard-ux:wizardapps", "running"), (URN, "stopped"), ("browser:wizardapps", "stopped")]
    rt = type("RT", (), {"installed": lambda self: [{"info": {"urn": u}, "app": {"status": s}} for u, s in rows]})()
    c.runtipi = lambda: rt
    started = []
    monkeypatch.setattr(cli.slots, "load_defaults", lambda: {"shell": {"app": "wizard-ux"}})
    monkeypatch.setattr(cli, "_app_container_states", lambda urn: ["running"] if "ux" in urn else [])
    monkeypatch.setattr(cli, "app_lifecycle", lambda c, verb, urn: started.append(urn) or {"status": "running"})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: {})
    res = cli.recover(c)
    assert started == [URN]
    assert res["started"] == ["messenger"] and res["left_stopped_by_update"] == ["messenger"]
