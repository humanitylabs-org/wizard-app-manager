"""#733: `GET store-install/_app-updates` says which app updates are running now (`running`), so the Tracker, Settings
and the Store can show "Updating…" after a reload instead of offering the update again.

A `wam app-updates --apply` run (timer or Update now) writes the app it is on and its step next to its lock; the
reader trusts that file only while a run holds the lock. Store Get/Update jobs come from the API's own memory."""
import fcntl
import json
import os
import subprocess

import pytest

from wam import app_updates as au
from wam import store_install as si
from test_app_updates_settings import _cfg, _route


def _hold_lock(home):
    """Another process's run: a second open file holding app-updates.lock (flock is per open file)."""
    home.mkdir(parents=True, exist_ok=True)
    f = open(home / au.LOCK, "w")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def _file(home, pid=None, **over):
    doc = {"pid": os.getpid() if pid is None else pid, "app": "money", "from": 1, "to": 2, "to_version": "0.2",
           "step": "updating", "started": 1_800_000_000, **over}
    (home / au.RUNNING).write_text(json.dumps(doc))


ENTRY = {"app": "money", "from": 1, "to": 2, "to_version": "0.2", "step": "updating", "started": 1_800_000_000,
         "by": "app-updates"}


def test_owner_status_says_nothing_runs_when_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "_release_managed", lambda: set())
    s = au.owner_status(_cfg(tmp_path))
    assert s["running"] == []


def test_owner_status_lists_the_app_a_run_is_updating(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "_release_managed", lambda: set())
    c = _cfg(tmp_path)
    lock = _hold_lock(c.home)
    try:
        _file(c.home)
        assert au.owner_status(c)["running"] == [ENTRY]
        assert (c.home / au.RUNNING).exists()        # the reader never takes or breaks a live run's lock
    finally:
        lock.close()


def test_a_file_a_killed_run_left_is_ignored_and_removed(tmp_path):
    home = tmp_path / "wos"
    _hold_lock(home).close()                         # the lock file exists, nothing holds it
    _file(home)
    assert au.running(home) == []
    assert not (home / au.RUNNING).exists()
    _file(home)
    (home / au.LOCK).unlink()                        # no lock file at all: no run
    assert au.running(home) == []


def test_lock_held_by_something_else_over_a_dead_runs_file(tmp_path):
    home = tmp_path / "wos"
    dead = subprocess.Popen(["true"])
    dead.wait()
    lock = _hold_lock(home)                          # e.g. the maintenance lease, which writes no running file
    try:
        _file(home, pid=dead.pid)
        assert au.running(home) == []
        _file(home, step="dancing")
        assert au.running(home) == []
        (home / au.RUNNING).write_text("{not json")
        assert au.running(home) == []
    finally:
        lock.close()


def test_store_jobs_installing_are_listed_and_an_app_update_run_wins(tmp_path, monkeypatch):
    home = tmp_path / "wos"
    monkeypatch.setattr(si, "_jobs", {
        "movies": {"app": "movies", "state": "installing", "started": 1_800_000_100},
        "notes": {"app": "notes", "state": "installed", "started": 1_800_000_000, "finished": 1_800_000_050},
        "money": {"app": "money", "state": "installing", "started": 1_800_000_200},
        "files": {"app": "files", "state": "failed", "started": 1_800_000_000}})
    assert au.running_all(home) == [
        {"app": "money", "from": None, "to": None, "to_version": None, "step": "installing", "started": 1_800_000_200,
         "by": "store"},
        {"app": "movies", "from": None, "to": None, "to_version": None, "step": "installing", "started": 1_800_000_100,
         "by": "store"}]
    lock = _hold_lock(home)
    try:
        _file(home)
        got = au.running_all(home)
    finally:
        lock.close()
    assert got == [ENTRY, {"app": "movies", "from": None, "to": None, "to_version": None, "step": "installing",
                           "started": 1_800_000_100, "by": "store"}]


def test_route_returns_running(tmp_path, monkeypatch):
    call, calls, c = _route(tmp_path, monkeypatch, [True])
    code, b = call("GET")
    assert code == 200 and b["running"] == []
    lock = _hold_lock(c.home)
    try:
        _file(c.home)
        code, b = call("GET")
    finally:
        lock.close()
    assert code == 200 and b["running"] == [ENTRY]


def _apply_box(tmp_path, monkeypatch, seen, *, verify=None):
    from wam import cli, updates
    monkeypatch.setattr(updates, "installing", lambda home: False)
    monkeypatch.setattr(au, "candidates", lambda *a, **k: [
        {"id": "money", "name": "Money", "from": 1, "to": 2, "to_version": "0.2", "status": "running", "skip": None},
        {"id": "notes", "name": "Notes", "from": 3, "to": 4, "to_version": None, "status": "running", "skip": None},
        {"id": "plans", "name": "Plans", "from": 5, "to": 6, "to_version": "6.0", "status": "running", "skip": None}])
    monkeypatch.setattr(au, "_containers", lambda urn: ["running"])
    monkeypatch.setattr(au, "baseline", lambda c, rt, app_id, st: seen.append(("baseline", au.running(home))))
    monkeypatch.setattr(au, "reconcile_settings", lambda c, rt, app_id: ["a password"] if app_id == "plans" else [])
    monkeypatch.setattr(cli, "write_registry", lambda c, rt: seen.append(("registry", au.running(home))))
    monkeypatch.setattr(au, "_notify", lambda c, *msg: True)
    home = tmp_path / "wos"

    def update(c, app_id, settings=None, **k):
        seen.append(("call", au.running(home)))
        missing = settings(None, f"{app_id}:wizardapps")
        seen.append(("admitted", au.running(home)))
        if missing:
            raise SystemExit("the new version needs " + "; ".join(missing))
        if app_id == "notes":
            raise RuntimeError("Runtipi went away")
        return {"status": "running", "version": 2}
    monkeypatch.setattr(cli, "app_update", update)
    monkeypatch.setattr(au, "verify", verify or (lambda c, rt, urn, app_id, res, before: seen.append(
        ("verify", au.running(home))) or (None if app_id == "money" else "it did not come back")))
    monkeypatch.setattr(au, "rollback", lambda c, rt, urn, started_ms, before: seen.append(
        ("rollback", au.running(home))) or {"rolled_back": True})

    class C:
        runtipi_root = tmp_path

        def runtipi(self):
            return type("Rt", (), {"installed": lambda self: []})()
    C.home = home
    return C(), home


def test_apply_says_each_step_and_clears_when_done_even_after_a_failed_update(tmp_path, monkeypatch):
    seen = []
    c, home = _apply_box(tmp_path, monkeypatch, seen)
    out = au.apply(c, renames=lambda c, rt: [], moves=lambda *a, **k: [], reporter=lambda *a, **k: {})
    assert [x["id"] for x in out["updated"]] == ["money"] and [x["id"] for x in out["rolled_back"]] == ["notes"]
    assert [x["id"] for x in out["needs_owner"]] == ["plans"]
    steps = [(what, [(e["app"], e["step"]) for e in got]) for what, got in seen]
    assert steps == [
        ("baseline", [("money", "preparing")]), ("call", [("money", "preparing")]),
        ("admitted", [("money", "updating")]), ("verify", [("money", "checking")]),
        ("baseline", [("notes", "preparing")]), ("call", [("notes", "preparing")]),
        ("admitted", [("notes", "updating")]), ("verify", [("notes", "checking")]),
        ("rollback", [("notes", "rolling back")]),
        ("baseline", [("plans", "preparing")]), ("call", [("plans", "preparing")]),
        ("admitted", [("plans", "preparing")]),     # a setting only the owner can give: never updating
        ("registry", [])]
    first = seen[2][1][0]
    assert first == {"app": "money", "from": 1, "to": 2, "to_version": "0.2", "step": "updating",
                     "started": first["started"], "by": "app-updates"} and isinstance(first["started"], int)
    assert seen[6][1][0]["to_version"] is None
    assert not (home / au.RUNNING).exists() and au.running(home) == []


def test_apply_clears_the_file_when_the_run_itself_breaks(tmp_path, monkeypatch):
    seen = []

    class Boom(Exception):
        pass

    def verify(*a, **k):
        raise Boom()
    c, home = _apply_box(tmp_path, monkeypatch, seen, verify=verify)
    with pytest.raises(Boom):
        au.apply(c, renames=lambda c, rt: [], moves=lambda *a, **k: [], reporter=lambda *a, **k: {})
    assert seen[-1][0] == "admitted" and seen[-1][1][0]["step"] == "updating"
    assert not (home / au.RUNNING).exists() and au.running(home) == []
