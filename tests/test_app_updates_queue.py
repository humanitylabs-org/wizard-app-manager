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
        assert au.request(["notes", "movies", "notes"], home)["queued"] == ["notes", "movies"]
        assert au.request([], home) == {"started": False, "queued": [], "note": au.QUEUE_NOTE}
        assert [e["app"] for e in au.queued(home)] == ["movies", "notes"]   # deduped, first place kept
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
        assert code == 200 and b["queued"] == ["notes", "movies"]   # the order asked
        assert len(calls) == n   # nothing new started: the running update takes them
        code, b = call("GET")
        assert code == 200 and [(q["app"], q["name"]) for q in b["queued"]] == [
            ("messenger", "messenger"), ("notes", "notes"), ("movies", "movies")]
        assert call("POST", {"action": "apply", "apps": []})[0] == 400
        assert call("POST", {"action": "apply", "apps": ["../x"]})[0] == 400
        assert call("POST", {"action": "apply", "apps": ["notes"], "app": "movies"})[0] == 400
    finally:
        run.close()
    assert call("GET")[1]["queued"] == [{"app": a, "at": q, "name": a} for a, q in
                                         [(e["app"], e["at"]) for e in au._queue_read(c.home)]]


def test_queued_requests_run_as_one_named_batch_in_the_order_asked(tmp_path, monkeypatch):
    """Codex P1: requests are named apps (Update all queues the ids due then), so each keeps its override of
    holds/failures; the run's own apps leave the queue when it starts."""
    home = tmp_path / "wos"
    run = _hold_run(home)
    try:
        au.request(["notes", "plans", "money"], home)
    finally:
        run.close()
    batches = []

    def fake(c, h, only, **kw):
        batches.append(only)
        return {"started": True, "updated": [{"id": x} for x in only]}
    monkeypatch.setattr(au, "_apply_locked", fake)
    out = au.apply(type("C", (), {"home": home})(), only=["plans"])
    assert batches == [["plans"], ["notes", "money"]]
    assert [x["id"] for x in out["updated"]] == ["plans", "notes", "money"] and out["queued_ran"] == [["notes", "money"]]
    assert not (home / au.QUEUE).exists()


def test_a_dry_run_is_not_a_run_requests_wait_for(tmp_path, monkeypatch):
    """Codex P2: a dry run never drains, so it must not make real requests queue behind it."""
    home = tmp_path / "wos"
    seen = []

    def fake(c, h, only, **kw):
        seen.append((au.run_active(h), au.request(["notes"], h)))
        return {"dry_run": True}
    monkeypatch.setattr(au, "_apply_locked", fake)
    au.apply(type("C", (), {"home": home})(), dry_run=True)
    assert seen == [(False, None)] and not (home / au.QUEUE).exists()


# ---------------------------------------------------------------- the Tracker contract (#764 follow-up)

def test_update_all_during_a_run_queues_the_apps_due_now(tmp_path, monkeypatch):
    call, calls, c = _route(tmp_path, monkeypatch, [True])
    run = _hold_run(c.home)
    try:
        code, b = call("POST", {"action": "apply"})
        assert (code, b) == (200, {"started": False, "queued": ["messenger", "movies", "notes"], "note": au.QUEUE_NOTE})
        assert all(isinstance(q["app"], str) and set(q) == {"app", "at", "name"} for q in call("GET")[1]["queued"])
        # `wam app-updates --apply` (no ids) during the run does the same
        assert au.apply(c)["queued"] == ["messenger", "movies", "notes"]
        assert calls == []
    finally:
        run.close()


def test_requests_wait_through_a_long_run_and_expire_only_without_one(tmp_path):
    home = tmp_path / "wos"
    home.mkdir(parents=True)
    old = int(time.time()) - au.QUEUE_MAX_AGE - 60
    (home / au.QUEUE).write_text(json.dumps([{"app": "movies", "at": old}]))
    run = _hold_run(home)
    try:
        assert [e["app"] for e in au.queued(home)] == ["movies"]   # the run is still going: it gets there
    finally:
        run.close()
    assert au.queued(home) == [] and not (home / au.QUEUE).exists()


def test_a_full_queue_answers_only_what_it_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "QUEUE_MAX", 3)
    home = tmp_path / "wos"
    run = _hold_run(home)
    try:
        au.request(["a", "b"], home)
        assert au.request(["b", "x", "y"], home)["queued"] == ["b", "x"]
        assert [e["app"] for e in au.queued(home)] == ["a", "b", "x"]
    finally:
        run.close()


def test_route_takes_up_to_100_ids_in_one_body(tmp_path, monkeypatch):
    call, calls, c = _route(tmp_path, monkeypatch, [True])
    full = ["a" * 60 + f"{i:03}" for i in range(au.QUEUE_MAX)]
    assert call("POST", {"action": "apply", "apps": full}) == (200, {"started": True}) and calls[-1] == ("apply", full)
    assert call("POST", {"action": "apply", "apps": full + ["b"]})[0] == 400
    assert call("POST", {"action": "apply", "apps": "movies"})[0] == 400
    monkeypatch.setattr(au, "start_detached", lambda runner=None, only=None: {"started": False, "error": "no systemd"})
    assert call("POST", {"action": "apply", "app": "movies"}) == (409, {"started": False, "error": "no systemd"})


def test_a_batch_that_fails_puts_its_requests_back(tmp_path, monkeypatch):
    home = tmp_path / "wos"
    run = _hold_run(home)
    try:
        au.request(["notes", "money"], home)
    finally:
        run.close()

    def fake(c, h, only, **kw):
        if only != ["plans"]:
            au.request(["movies"], h)   # asked for while the batch ran
            raise ConnectionError("Runtipi went away")
        return {"started": True, "updated": []}
    monkeypatch.setattr(au, "_apply_locked", fake)
    try:
        au.apply(type("C", (), {"home": home})(), only=["plans"])
    except ConnectionError:
        pass
    assert [e["app"] for e in au._queue_read(home)] == ["notes", "money", "movies"]
    assert not au.run_active(home)
