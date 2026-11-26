"""#1063: Runtipi records an app with one container up and another crash-looping as `stopped` (it says `running`
only when every container runs). An update of such an app used to keep it stopped (both containers gone) and report
a plain success. Now it is started again and checked; an app the owner stopped stays stopped and the notice says so;
an app that should run but does not after its update is never reported as updated."""
from wam import app_updates as au
from wam import cli, updates

URN = "wizard-calendar:wizardapps"


class _RT:
    def __init__(self, status):
        self.status, self.calls = status, []

    def app(self, urn):
        return {"app": {"status": self.status, "version": 45}}

    def update(self, urn, perform_backup=True):
        self.calls.append("update")

    def wait_status(self, urn, want, timeout=900):
        return self.status

    def start(self, urn):
        self.calls.append("start")
        self.status = "running"


def _update(tmp_path, monkeypatch, rt, states):
    c = cli.Config.__new__(cli.Config)
    c.runtipi_root, c.home = tmp_path / "rt", tmp_path / "wos"
    c.runtipi_root.mkdir()
    c.home.mkdir()
    monkeypatch.setattr(c, "runtipi", lambda: rt, raising=False)
    monkeypatch.setattr(cli.alongside, "load", lambda h: None)
    monkeypatch.setattr(cli, "host_access_risks", lambda *a, **k: [])
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: {})
    monkeypatch.setattr(cli, "backfill_owner_login", lambda *a, **k: False)
    monkeypatch.setattr(cli, "keep_trusted_hosts", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_app_container_states", lambda urn: list(states))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return cli._app_update(c, "wizard-calendar", backup=False)


def test_a_half_running_app_is_started_again_after_its_update(tmp_path, monkeypatch):
    rt = _RT("stopped")   # Runtipi: "mixed container states: 1/2 running" -> stopped
    out = _update(tmp_path, monkeypatch, rt, ["running", "restarting"])
    assert rt.calls == ["update", "start"]
    assert out["status"] == "running" and out["partly_running"] and not out.get("left_stopped")
    assert not out.get("error")


def test_a_half_running_app_that_does_not_come_back_is_an_error(tmp_path, monkeypatch):
    rt = _RT("stopped")
    rt.start = lambda urn: rt.calls.append("start")   # still crash-looping: Runtipi keeps it `stopped`
    out = _update(tmp_path, monkeypatch, rt, ["running", "exited"])
    assert rt.calls == ["update", "start"] and out["status"] == "stopped"
    assert out.get("error") and not out.get("left_stopped")


def test_an_app_the_owner_stopped_stays_stopped_and_says_so(tmp_path, monkeypatch):
    rt = _RT("stopped")
    out = _update(tmp_path, monkeypatch, rt, ["exited", "exited"])
    assert rt.calls == ["update"] and out["status"] == "stopped"
    assert out["left_stopped"] and not out.get("partly_running") and not out.get("error")


def test_was_up_counts_any_live_container():
    assert au.was_up("stopped", ["running", "exited"]) == "running"
    assert au.was_up("stopped", ["restarting", "exited"]) == "running"
    assert au.was_up("stopped", ["exited", "exited"]) == "stopped"
    assert au.was_up("stopped", []) == "stopped"
    assert au.was_up("running", ["running"]) == "running"


def _apply(tmp_path, monkeypatch, *, status, before_states, after_states, res):
    monkeypatch.setattr(updates, "installing", lambda home: False)
    monkeypatch.setattr(au, "candidates", lambda *a, **k: [{
        "id": "wizard-calendar", "name": "Calendar", "from": 45, "to": 48, "to_version": "0.3.30",
        "status": status, "skip": None}])
    calls = []

    def containers(urn):
        calls.append(urn)
        return list(before_states if len(calls) == 1 else after_states)
    monkeypatch.setattr(au, "_containers", containers)
    real_verify = au.verify
    monkeypatch.setattr(au, "verify", lambda *a, **k: real_verify(*a, **{**k, "sleep": lambda s: None,
                                                                         "containers": containers,
                                                                         "endpoint": lambda *x: None}))
    monkeypatch.setattr(au, "baseline", lambda *a, **k: None)
    monkeypatch.setattr(au.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "app_update", lambda c, app_id, **k: dict(res))
    monkeypatch.setattr(cli, "write_registry", lambda c, rt: None)
    rolled = []
    monkeypatch.setattr(au, "rollback", lambda c, rt, urn, ms, before: rolled.append(before) or {
        "rolled_back": True, "status": before["recorded_status"], "version": 45})
    sent = []
    monkeypatch.setattr(au, "_notify", lambda c, *msg: sent.append(msg) or True)

    class C:
        home = tmp_path
        runtipi_root = tmp_path

        def runtipi(self):
            return type("Rt", (), {"installed": lambda self: []})()
    out = au.apply(C(), only=["wizard-calendar"], renames=lambda c, rt: [], moves=lambda *a, **k: [])
    return out, sent, rolled


def test_apply_a_half_running_app_ends_running_and_healthy(tmp_path, monkeypatch):
    out, sent, rolled = _apply(tmp_path, monkeypatch, status="stopped", before_states=["running", "restarting"],
                               after_states=["running", "running"],
                               res={"status": "running", "version": 48, "partly_running": True})
    assert [d["id"] for d in out["updated"]] == ["wizard-calendar"] and not out["failed"] and not rolled
    assert out["updated"][0]["status"] == "running" and out["updated"][0]["started_again"]
    assert "left stopped" not in out["message"]


def test_apply_a_half_running_app_left_stopped_is_never_a_success(tmp_path, monkeypatch):
    # what James's box got: the update kept it stopped and both containers were gone
    out, sent, rolled = _apply(tmp_path, monkeypatch, status="stopped", before_states=["running", "restarting"],
                               after_states=[], res={"status": "stopped", "version": 48})
    assert not out["updated"]
    assert [r["id"] for r in out["rolled_back"] + out["failed"]] == ["wizard-calendar"]
    assert "came back stopped (was running)" in (out["rolled_back"] + out["failed"])[0]["reason"]
    assert out["message"] and not out["message"].startswith("Updated")


def test_apply_a_half_running_app_still_crashing_after_its_update_fails(tmp_path, monkeypatch):
    out, sent, rolled = _apply(tmp_path, monkeypatch, status="stopped", before_states=["running", "restarting"],
                               after_states=["running", "restarting"], res={"status": "running", "version": 48})
    assert not out["updated"] and rolled and "keeps restarting" in out["rolled_back"][0]["reason"]


def test_apply_an_owner_stopped_app_stays_stopped_and_the_notice_says_so(tmp_path, monkeypatch):
    out, sent, rolled = _apply(tmp_path, monkeypatch, status="stopped", before_states=["exited", "exited"],
                               after_states=[], res={"status": "stopped", "version": 48, "left_stopped": True})
    assert [d["id"] for d in out["updated"]] == ["wizard-calendar"] and not rolled
    assert out["updated"][0]["left_stopped"] and out["updated"][0]["status"] == "stopped"
    assert "Calendar (0.3.30), left stopped as you had it" in out["message"]
    assert "left stopped as you had it" in sent[0][1]


def test_rollback_of_a_half_running_app_may_end_as_runtipi_recorded_it(monkeypatch):
    class Rt:
        def list_backups(self, urn):
            return [{"id": "b1", "date": 10**13}]

        def restore_backup(self, urn, bid):
            pass

        def wait_status(self, urn, want, timeout):
            return "stopped"   # the old version's crash-looping container: Runtipi says stopped again

        def start(self, urn):
            pass

        def app(self, urn):
            return {"app": {"version": 45, "status": "stopped"}}
    monkeypatch.setattr(au.time, "sleep", lambda s: None)
    monkeypatch.setattr(au, "open_restored_folders", lambda c, urn: [])
    monkeypatch.setattr(cli, "network_step", lambda c, rt: {})
    rb = au.rollback(None, Rt(), URN, 0, {"status": "running", "recorded_status": "stopped", "from": 45})
    assert rb["rolled_back"]
