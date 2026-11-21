"""#895: a big pre-update backup outlasted a Runtipi update job's 5-minute limit. The job went on and the new
Messenger version came up, but Runtipi's record still said the old version, so `wam app-updates` reported
"still on version 131 (expected 135); the update was rolled back" and marked Messenger failed.

Now a record that lags behind the installed recipe is corrected by one more update WITHOUT a backup (no pile of
full backups), and when it can't be corrected it is reported as a stale record, never as a rollback, and the
backup is not restored over the running new version."""
import json
from types import SimpleNamespace

import pytest

from wam import app_updates as au, cli

STORE = "wizardapps"


def _recipe(folder, tipi, version):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text(json.dumps({"name": "Messenger", "tipi_version": tipi, "version": version}))


class RT:
    """Runtipi after the timed-out job: the app runs again, the record still says 131."""
    def __init__(self, version=131, status="running"):
        self.row = {"status": status, "version": version}

    def app(self, urn):
        return {"app": dict(self.row)}

    def installed(self):
        return []


@pytest.fixture
def box(tmp_path, monkeypatch):
    rt = RT()
    c = SimpleNamespace(home=tmp_path / "home", runtipi_root=tmp_path / "rt", runtipi=lambda: rt)
    c.home.mkdir()
    _recipe(c.runtipi_root / "apps" / STORE / "messenger", 135, "0.2.0-unified.16.90")     # the job copied it in
    _recipe(c.runtipi_root / "repos" / STORE / "apps" / "messenger", 135, "0.2.0-unified.16.90")
    monkeypatch.setattr(cli, "STORE", STORE)
    return SimpleNamespace(c=c, rt=rt)


BEFORE = {"id": "messenger", "name": "Messenger", "from": 131, "to": 135, "status": "running", "running": 1}
STALE = {"id": "messenger", "status": "running", "version": 131, "from_version": 131, "restarted": True}


def test_a_stale_record_is_corrected_by_one_update_without_a_backup(box, monkeypatch):
    calls = []

    def app_update(c, app_id, **k):
        calls.append((app_id, k))
        box.rt.row["version"] = 135
        return {"id": app_id, "status": "running", "version": 135, "from_version": 131}
    monkeypatch.setattr(cli, "app_update", app_update)
    res = au.settle_record(box.c, box.rt, f"messenger:{STORE}", "messenger", dict(STALE), BEFORE)
    assert calls == [("messenger", {"backup": False})]
    assert res["version"] == 135 and res["stale_record"] is True
    problem = au.verify(box.c, box.rt, f"messenger:{STORE}", "messenger", res, BEFORE, sleep=lambda s: None,
                        containers=lambda urn: ["running"], probe=lambda *a: True, endpoint=lambda *a: None)
    assert problem is None


def test_a_record_that_does_not_move_is_never_called_a_rollback(box):
    res = au.settle_record(box.c, box.rt, f"messenger:{STORE}", "messenger", dict(STALE), BEFORE,
                           retry=lambda: dict(STALE))
    problem = au.verify(box.c, box.rt, f"messenger:{STORE}", "messenger", res, BEFORE, sleep=lambda s: None,
                        containers=lambda urn: ["running"], probe=lambda *a: True, endpoint=lambda *a: None)
    assert "rolled back" not in problem
    assert problem.startswith("version 135 is installed, but Runtipi still records version 131")


def test_a_real_rollback_is_left_alone(box):
    _recipe(box.c.runtipi_root / "apps" / STORE / "messenger", 131, "0.2.0-unified.16.80")   # old recipe back
    res = au.settle_record(box.c, box.rt, f"messenger:{STORE}", "messenger", dict(STALE), BEFORE,
                           retry=lambda: pytest.fail("no retry when the old version is what is installed"))
    assert res == STALE
    assert "rolled back" in au.verify(box.c, box.rt, f"messenger:{STORE}", "messenger", res, BEFORE,
                                      sleep=lambda s: None, containers=lambda urn: ["running"])


def test_a_record_that_caught_up_meanwhile_is_read_again(box):
    box.rt.row["version"] = 135   # Runtipi wrote the target after app_update's last read (Codex P2)
    res = au.settle_record(box.c, box.rt, f"messenger:{STORE}", "messenger", dict(STALE), BEFORE,
                           retry=lambda: pytest.fail("the record is already right"))
    assert res["version"] == 135 and not res.get("stale_record")


def test_no_retry_while_runtipi_still_says_updating(box):
    box.rt.row["status"] = "updating"   # a job still running: never start a second update on top of it
    res = {"error": "the update did not finish in time", "status": None}
    assert au.settle_record(box.c, box.rt, f"messenger:{STORE}", "messenger", res, BEFORE,
                            retry=lambda: pytest.fail("never on top of a running update")) is res


def test_a_store_update_with_a_stale_record_is_corrected_not_rolled_back(box, monkeypatch):
    monkeypatch.setattr(cli, "app_update", lambda c, app_id, **k: (
        box.rt.row.update(version=135), {"id": app_id, "status": "running", "version": 135})[1])
    res = au.store_update(box.c, "messenger", lambda: dict(STALE), settle=0, probe_seconds=0, sleep=lambda s: None,
                          containers=lambda urn: ["running"], probe=lambda *a: True, endpoint=lambda *a: None,
                          roll=lambda *a: pytest.fail("the new version runs: nothing to restore"))
    assert res["version"] == 135 and not res.get("error")


def test_a_store_update_whose_record_stays_stale_says_so_and_keeps_the_new_version(box):
    res = au.store_update(box.c, "messenger", lambda: dict(STALE), settle=0, probe_seconds=0, sleep=lambda s: None,
                          containers=lambda urn: ["running"], probe=lambda *a: True, endpoint=lambda *a: None,
                          roll=lambda *a: pytest.fail("the new version runs: nothing to restore"),
                          retry=lambda: dict(STALE))
    assert res["rolled_back"] is False and "Runtipi still records version 131" in res["error"]
    assert "rolled back" not in res["error"]


def test_the_nightly_run_corrects_the_record_instead_of_marking_the_app_failed(box, monkeypatch):
    from wam import updates
    monkeypatch.setattr(updates, "installing", lambda home: False)
    monkeypatch.setattr(au, "candidates", lambda *a, **k: [
        {"id": "messenger", "name": "Messenger", "from": 131, "to": 135, "to_version": "0.2.0-unified.16.90",
         "status": "running", "skip": None}])
    monkeypatch.setattr(au, "_containers", lambda urn: ["running"])
    monkeypatch.setattr(au, "baseline", lambda *a, **k: None)
    monkeypatch.setattr(au, "reconcile_settings", lambda c, rt, app_id: [])
    monkeypatch.setattr(au, "_endpoint", lambda *a, **k: None)
    monkeypatch.setattr(au, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(cli, "write_registry", lambda c, rt: None)
    monkeypatch.setattr(au, "_notify", lambda c, *msg: True)
    monkeypatch.setattr(au, "rollback", lambda *a, **k: pytest.fail("the new version runs: nothing to restore"))
    calls = []

    def app_update(c, app_id, settings=None, backup=True, **k):
        calls.append(backup)
        if settings:
            settings(box.rt, f"{app_id}:{STORE}")
        if not backup:   # the second pass only corrects the record
            box.rt.row["version"] = 135
            return {"id": app_id, "status": "running", "version": 135, "from_version": 131}
        return dict(STALE)   # the first pass: Runtipi's job timed out, the record kept 131
    monkeypatch.setattr(cli, "app_update", app_update)
    real_verify = au.verify
    monkeypatch.setattr(au, "verify", lambda *a, **k: real_verify(
        *a, settle=0, sleep=lambda s: None, containers=lambda urn: ["running"], endpoint=lambda *x: None, **k))
    out = au.apply(box.c, renames=lambda c, rt: [], moves=lambda *a, **k: [], reporter=lambda *a, **k: {})
    assert calls == [True, False]   # one backup only
    assert [x["id"] for x in out["updated"]] == ["messenger"] and out["failed"] == [] and out["rolled_back"] == []
    assert "messenger" not in (json.loads((box.c.home / au.STATE).read_text()).get("failed") or {})
