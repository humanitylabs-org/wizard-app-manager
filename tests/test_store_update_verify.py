"""#857: a Store (or Tracker) Update and a Store Start are checked like the nightly updater. Runtipi reports
`running` while a container restart-loops, so on main a version the nightly run had rolled back was installed again
by a Store tap and reported `installed`, and Start of the crashing app said Started."""
import functools
import json
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

from wam import app_updates as au, cli, private, store_install as si, store_selection as sel, store_start as sst

PIN = "a" * 40
OLD = "b" * 40
STORE = "wizardapps"


class RT:
    """Runtipi as it answers for a crash-looping app: `running` after the update and after the rollback."""
    def __init__(self, version=4, status="running"):
        self.row = {"status": status, "version": version}
        self.restored = []

    def app(self, urn):
        return {"app": dict(self.row)}


def _recipe(folder, tipi, version, commit):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text(json.dumps({"name": "Photos", "tipi_version": tipi, "version": version}))
    (folder / ".wizard-source").write_text(json.dumps({"repo": "org/photos", "source": "standard"}))
    (folder / ".wizard-commit").write_text(commit)


@pytest.fixture
def box(tmp_path, monkeypatch):
    rt = RT()
    c = SimpleNamespace(home=tmp_path / "home", runtipi_root=tmp_path / "rt", index="https://index.test",
                        runtipi=lambda: rt)
    c.home.mkdir()
    _recipe(c.runtipi_root / "apps" / STORE / "photos", 4, "0.3.1", OLD)        # installed: Photos 0.3.1
    _recipe(c.runtipi_root / "repos" / STORE / "apps" / "photos", 5, "0.4.0", PIN)   # the Store's new release
    monkeypatch.setattr(cli, "STORE", STORE)
    return SimpleNamespace(c=c, rt=rt)


def _crashing_update(box):
    """app_update of a version whose container exits 1 on start: Runtipi still says running, version moved."""
    def update():
        shutil.copytree(box.c.runtipi_root / "repos" / STORE / "apps" / "photos",
                        box.c.runtipi_root / "apps" / STORE / "photos", dirs_exist_ok=True)
        box.rt.row.update(version=5, status="running")
        return {"id": "photos", "status": "running", "version": 5, "from_version": 4}
    return update


def _roll(box):
    def roll(c, rt, urn, started_ms, before):
        rt.restored.append(urn)
        _recipe(c.runtipi_root / "apps" / STORE / "photos", 4, "0.3.1", OLD)
        rt.row.update(version=4, status="running")
        return {"rolled_back": True, "status": "running", "version": 4}
    return roll


def _checks(states):
    return dict(settle=0, probe_seconds=0, sleep=lambda s: None, containers=lambda urn: list(states.pop(0)),
                probe=lambda *a: True, endpoint=lambda *a: None)


def test_a_store_update_that_restart_loops_is_rolled_back_and_failed(box):
    states = [["running"], ["restarting"]]   # before the update; after it settled
    res = au.store_update(box.c, "photos", _crashing_update(box), roll=_roll(box), **_checks(states))
    assert res["rolled_back"] is True and box.rt.restored == [f"photos:{STORE}"]
    assert res["error"] == "Photos 0.4.0 keeps crashing (a container keeps restarting); kept 0.3.1"
    # recorded like the nightly run's failure: the nightly updater skips it, and so does the next Store tap
    rec = json.loads((box.c.home / au.STATE).read_text())["failed"]["photos"]
    assert rec["to"] == 5 and "restarting" in rec["reason"]
    assert au.candidates([{"app": {"appName": "photos", "version": 4, "status": "running", "appStoreSlug": STORE,
                                   "ignoredVersion": None}, "metadata": {"latestVersion": 5}}],
                         store=STORE, runtipi_root=box.c.runtipi_root, home=box.c.home,
                         release_managed=set())[0]["skip"] == "failed"


def test_a_store_update_that_stays_up_passes_and_clears_an_old_failure(box):
    au._write(box.c.home, failed={"photos": {"to": 3, "at": int(time.time())}})
    states = [["running"], ["running"]]
    res = au.store_update(box.c, "photos", _crashing_update(box), roll=_roll(box), **_checks(states))
    assert not res.get("error") and res["status"] == "running" and box.rt.restored == []
    assert "photos" not in json.loads((box.c.home / au.STATE).read_text())["failed"]


def test_a_rollback_that_fails_says_so(box):
    states = [["running"], ["restarting"]]
    res = au.store_update(box.c, "photos", _crashing_update(box),
                          roll=lambda *a: {"rolled_back": False, "error": "no backup from this update to roll back to"},
                          **_checks(states))
    assert res["rolled_back"] is False
    assert res["error"].startswith("Photos 0.4.0 keeps crashing") and "no backup" in res["error"]


def test_a_failed_version_is_skipped_by_the_store_unless_forced(box):
    au._write(box.c.home, failed={"photos": {"to": 5, "at": int(time.time()), "reason": "a container keeps restarting"}})
    why = au.store_refusal(box.c, "photos")
    assert why and why.startswith("Photos 0.4.0 failed its check") and "keeps restarting" in why
    assert au.store_refusal(box.c, "photos", force=True) is None
    au._write(box.c.home, failed={"photos": {"to": 6, "at": int(time.time())}})   # a newer release is not held
    assert au.store_refusal(box.c, "photos") is None
    au._write(box.c.home, failed={"photos": {"to": 5, "at": int(time.time()) - (au.RETRY_DAYS + 1) * 86400}})
    assert au.store_refusal(box.c, "photos") is None


def test_force_is_the_only_optional_selection_field(box, monkeypatch):
    monkeypatch.setattr(sel.test_catalog, "resolve", lambda app: None)
    monkeypatch.setattr(sel.index, "resolve", lambda index, ref: {"id": "photos", "repo": "org/photos",
                                                                    "install": {"commit": PIN}})
    body = {"source": "standard", "repo": "org/photos", "commit": PIN, "operation": "update"}
    assert "force" not in sel.resolve(box.c, "photos", body)
    assert sel.resolve(box.c, "photos", {**body, "force": True})["force"] is True
    with pytest.raises(ValueError):
        sel.resolve(box.c, "photos", {**body, "force": "yes"})


def _wait(job, busy):
    for _ in range(300):
        if job["state"] != busy:
            return job
        threading.Event().wait(0.02)
    return job


def _store_path(box, monkeypatch, states):
    """POST /<app> as the Store sends it, down to app_update: the real selection and job code."""
    monkeypatch.setattr(sel.test_catalog, "resolve", lambda app: None)
    monkeypatch.setattr(sel.index, "resolve", lambda index, ref: {"id": "photos", "repo": "org/photos",
                                                                    "install": {"commit": PIN}})
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"photos"})
    monkeypatch.setattr(private, "prepare", lambda *a, **k: {"id": "photos"})
    monkeypatch.setattr(cli, "app_update", lambda c, a: _crashing_update(box)())
    monkeypatch.setattr(cli, "write_registry", lambda c: None)
    monkeypatch.setattr(si, "_links", lambda c: None)
    from wam import store_remove
    monkeypatch.setattr(store_remove, "removing", lambda *a: False)
    real = au.store_update
    monkeypatch.setattr(au, "store_update", functools.partial(real, roll=_roll(box), **_checks(states)))
    si._jobs.clear()


def test_store_update_post_of_a_crashing_version_ends_failed_and_rolled_back(box, monkeypatch):
    _store_path(box, monkeypatch, [["running"], ["restarting"]])
    selection = sel.resolve(box.c, "photos", {"source": "standard", "repo": "org/photos", "commit": PIN,
                                              "operation": "update"})
    code, job = si.start(box.c, "photos", lambda c_, a: sel.execute(c_, a, selection), lambda: {"photos"},
                         selection=selection)
    assert code == 202
    job = _wait(job, "installing")
    # on main: state "installed" while the container restart-looped
    assert job["state"] == "failed" and job["error"] == "Photos 0.4.0 keeps crashing (a container keeps restarting); kept 0.3.1"
    assert box.rt.restored == [f"photos:{STORE}"] and box.rt.row["version"] == 4
    # the failed commit's pin is gone, so a later fixed release reaches the app again
    assert not (box.c.runtipi_root / "repos" / STORE / "apps" / "photos" / ".wizard-selection").exists()

    # the next Store tap of that same version is refused before anything changes, unless forced
    si._jobs.clear()
    calls = []
    monkeypatch.setattr(cli, "app_update", lambda c, a: calls.append(a) or _crashing_update(box)())
    code, job = si.start(box.c, "photos", lambda c_, a: sel.execute(c_, a, selection), lambda: {"photos"},
                         selection=selection)
    job = _wait(job, "installing")
    assert job["state"] == "failed" and "failed its check" in job["error"] and calls == []


def test_store_start_of_a_crash_looping_app_is_failed_not_started(box):
    sst._jobs.clear(); si._jobs.clear()
    box.rt.row.update(status="stopped")
    check = functools.partial(au.check_started, settle=0, sleep=lambda s: None, containers=lambda urn: ["restarting"])
    code, job = sst.start(box.c, "photos", {}, lambda c, v, a: {"status": "running"}, check)
    assert code == 202
    job = _wait(job, "starting")
    assert job["state"] == "failed" and job["error"] == "Photos keeps crashing after it starts (a container keeps restarting)"

    sst._jobs.clear()
    check = functools.partial(au.check_started, settle=0, sleep=lambda s: None, containers=lambda urn: ["running"])
    code, job = sst.start(box.c, "photos", {}, lambda c, v, a: {"status": "running"}, check)
    assert _wait(job, "starting")["state"] == "running"


def test_store_start_uses_the_check_by_default(box, monkeypatch):
    sst._jobs.clear(); si._jobs.clear()
    box.rt.row.update(status="stopped")
    monkeypatch.setattr(cli, "app_lifecycle", lambda c, v, a: {"status": "running"})
    seen = []
    monkeypatch.setattr(au, "check_started", lambda c, a: seen.append(a) or "Photos keeps crashing after it starts")
    code, job = sst.start(box.c, "photos", {})
    assert _wait(job, "starting")["state"] == "failed" and seen == ["photos"]
