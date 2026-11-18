"""#764: asking for an app update while an app-updates run is going is queued for that run, not refused.

Settings' Update now / Retry, the Tracker and `wam app-updates --apply <id>` used to get "app updates are already
running" (or a detached run that quietly skipped), so a request made during a run was lost. Now the request waits in
<home>/app-updates-queue.json and the running update takes it after its own apps, under the same lock, still one app
at a time with the same check and rollback.
"""
import fcntl
import json
import os
import time

from wam import app_updates as au
from test_app_updates_running import _apply_box
from test_app_updates_settings import _route


def _hold_run(home, pid=None):
    """Another process's app-updates run: holds app-updates.lock and wrote its pid into it."""
    home.mkdir(parents=True, exist_ok=True)
    f = open(home / au.LOCK, "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid() if pid is None else pid}\n")
    f.flush()
    return f


def _hold_other(home):
    """Maintenance or an OS update: holds the same lock but is no app-updates run (no pid in it)."""
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / au.LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_no_run_means_no_queue(tmp_path):
    home = tmp_path / "wos"
    assert au.request(["movies"], home) is None
    assert not (home / au.QUEUE).exists() and au.queued(home) == []


def test_a_request_during_a_run_is_queued_once(tmp_path):
    home = tmp_path / "wos"
    run = _hold_run(home)
    try:
        assert au.run_active(home)
        assert au.request(["movies"], home) == {"started": False, "queued": ["movies"], "note": au.QUEUE_NOTE}
        au.request(["movies", "notes"], home)
        assert au.request(None, home)["queued"] == ["*"]
        assert [e["app"] for e in au.queued(home)] == ["movies", "notes", None]
        assert au.queued(home)[-1]["name"] == "All apps"
    finally:
        run.close()


def test_maintenance_holding_the_lock_is_not_a_run(tmp_path):
    home = tmp_path / "wos"
    fd = _hold_other(home)
    try:
        assert not au.run_active(home) and au.request(["movies"], home) is None
        out = au.apply(type("C", (), {"home": home})(), only=["movies"])
        assert out == {"started": False, "skipped": "running", "note": "app updates are already running"}
    finally:
        os.close(fd)
    assert not (home / au.QUEUE).exists()


def test_a_dead_runs_pid_is_not_a_run(tmp_path):
    home = tmp_path / "wos"
    run = _hold_run(home, pid=2 ** 22 + 12345)
    try:
        assert not au.run_active(home)
    finally:
        run.close()


def test_old_requests_are_dropped(tmp_path):
    home = tmp_path / "wos"
    home.mkdir(parents=True)
    now = int(time.time())
    (home / au.QUEUE).write_text(json.dumps([{"app": "movies", "at": now - au.QUEUE_MAX_AGE - 5},
                                             {"app": "notes", "at": now}, {"app": "../x", "at": now}, "junk"]))
    assert [e["app"] for e in au.queued(home)] == ["notes"]
    assert [e["app"] for e in json.loads((home / au.QUEUE).read_text())] == ["notes"]


def test_the_timer_does_not_queue(tmp_path):
    home = tmp_path / "wos"
    run = _hold_run(home)
    try:
        out = au.apply(type("C", (), {"home": home})(), queue=False)
    finally:
        run.close()
    assert out["skipped"] == "running" and not (home / au.QUEUE).exists()


def test_the_running_update_takes_a_request_made_during_it(tmp_path, monkeypatch):
    """End to end: Update now on Notes while Money updates. The second apply() (another process in real life; a
    second open file here, so flock refuses it the same way) queues and returns at once; the run then updates Notes
    after Money, with the same check and rollback, and lets its lock go only with the queue empty."""
    from wam import cli
    seen = []
    c, home = _apply_box(tmp_path, monkeypatch, seen)
    real = cli.app_update
    asked = []

    def update(c_, app_id, settings=None, **k):
        if app_id == "money":
            asked.append(au.apply(c, only=["notes"]))
        return real(c_, app_id, settings=settings, **k)
    monkeypatch.setattr(cli, "app_update", update)
    out = au.apply(c, only=["money"], renames=lambda c, rt: [], moves=lambda *a, **k: [], reporter=lambda *a, **k: {})
    assert asked == [{"started": False, "queued": ["notes"], "note": au.QUEUE_NOTE}]
    assert [x["id"] for x in out["updated"]] == ["money"] and [x["id"] for x in out["rolled_back"]] == ["notes"]
    assert out["queued_ran"] == [["notes"]]
    calls = [got[0]["app"] for what, got in seen if what == "call"]
    assert calls == ["money", "notes"]
    assert not (home / au.QUEUE).exists() and (home / au.LOCK).read_text() == ""
    assert not au.run_active(home) and au.running(home) == []


def test_route_queues_during_a_run_and_lists_it(tmp_path, monkeypatch):
    call, calls, c = _route(tmp_path, monkeypatch, [True])
    assert call("POST", {"action": "apply", "app": "movies"})[0] == 200 and calls[-1] == ("apply", ["movies"])
    run = _hold_run(c.home)
    try:
        n = len(calls)
        code, b = call("POST", {"action": "apply", "app": "messenger"})
        assert code == 200 and b == {"started": False, "queued": ["messenger"], "note": au.QUEUE_NOTE}
        code, b = call("POST", {"action": "apply", "apps": ["notes", "movies", "notes"]})
        assert code == 200 and b["queued"] == ["movies", "notes"]
        assert len(calls) == n   # nothing new started: the running update takes them
        code, b = call("GET")
        assert code == 200 and [(q["app"], q["name"]) for q in b["queued"]] == [
            ("messenger", "messenger"), ("movies", "movies"), ("notes", "notes")]
        assert call("POST", {"action": "apply", "apps": []})[0] == 400
        assert call("POST", {"action": "apply", "apps": ["../x"]})[0] == 400
        assert call("POST", {"action": "apply", "apps": ["notes"], "app": "movies"})[0] == 400
    finally:
        run.close()
    assert call("GET")[1]["queued"] == [{"app": a, "at": q, "name": a} for a, q in
                                         [(e["app"], e["at"]) for e in au._queue_read(c.home)]]
