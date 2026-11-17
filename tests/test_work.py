"""The running-work inventory: every source becomes named items; unreadable sources never read as idle."""
import fcntl
import json
import os
import sqlite3

import pytest

from wam import updates, work


@pytest.fixture
def box(tmp_path, monkeypatch):
    home, hh = tmp_path / "wizard-os", tmp_path / "runtipi/app-data/wizardapps/hermes-agent/data"
    home.mkdir()
    (hh / "cron").mkdir(parents=True)
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.setenv("RUNTIPI_ROOT", str(tmp_path / "runtipi"))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(work.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(updates, "service_origin", lambda home=None: None)
    monkeypatch.setattr(updates, "install_state", lambda home=None: None)
    work._cache.clear()
    with sqlite3.connect(hh / "state.db") as db:
        db.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT);
            CREATE TABLE session_turn_leases (conversation_id TEXT PRIMARY KEY, holder TEXT, acquired_at REAL,
                                              expires_at REAL);
            CREATE TABLE async_delegations (delegation_id TEXT PRIMARY KEY, state TEXT, dispatched_at REAL);""")
    with sqlite3.connect(hh / "cron/executions.db") as db:
        db.execute("CREATE TABLE executions (id TEXT, job_id TEXT, status TEXT, started_at TEXT, finished_at TEXT)")
    with sqlite3.connect(hh / "kanban.db") as db:
        db.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT)")
    return home, hh


def names(items):
    return sorted((i["kind"], i["name"], i["stops_at"]) for i in items)


def test_finds_hermes_data_of_a_fresh_box_in_runtipi(box):
    home, hh = box
    assert work.hermes_home(home) == hh


def test_an_idle_box_has_nothing_running(box):
    home, _ = box
    assert work.inventory(home, now=1000) == []


def test_every_source_is_named(box):
    home, hh = box
    with sqlite3.connect(hh / "state.db") as db:
        db.execute("INSERT INTO sessions VALUES ('c1', 'Build landing page')")
        db.execute("INSERT INTO session_turn_leases VALUES ('c1', 'gw', 900, 2000)")
        db.execute("INSERT INTO session_turn_leases VALUES ('c2', 'gw', 100, 500)")         # expired: not running
        db.execute("INSERT INTO async_delegations VALUES ('d1', 'running', 950)")
        db.execute("INSERT INTO async_delegations VALUES ('d2', 'completed', 950)")
    with sqlite3.connect(hh / "cron/executions.db") as db:
        db.execute("INSERT INTO executions VALUES ('e1', 'j1', 'running', '2026-11-10T01:00', NULL)")
        db.execute("INSERT INTO executions VALUES ('e2', 'j1', 'completed', '2026-11-09T01:00', '2026-11-09T01:05')")
    (hh / "cron/jobs.json").write_text(json.dumps({"jobs": [{"id": "j1", "name": "Weekly report"}]}))
    with sqlite3.connect(hh / "kanban.db") as db:
        db.execute("INSERT INTO tasks VALUES ('t1', 'Fix login', 'running')")
        db.execute("INSERT INTO tasks VALUES ('t2', 'Later', 'todo')")
    (hh / "processes.json").write_text(json.dumps([
        {"session_id": "p1", "pid": 42, "command": "npm run build", "cwd": "/work/site", "started_at": 990},
        {"session_id": "p2", "pid": 43, "command": "sleep 1", "exited": True}]))
    assert names(work.inventory(home, now=1000)) == [
        ("agent", "A helper agent", "hermes"),
        ("agent", "Replying in “Build landing page”", "hermes"),
        ("agent", "Scheduled job “Weekly report”", "hermes"),
        ("agent", "Task “Fix login”", "hermes"),
        ("task", "npm run build (in site)", "hermes"),
    ]


def test_an_unreadable_source_is_never_idle(box):
    home, hh = box
    (hh / "processes.json").write_text("{not json")
    (hh / "gateway_state.json").write_text("{broken")
    kinds = {(i["kind"], i["stops_at"]) for i in work.inventory(home, now=1000)}
    assert kinds == {("unknown", "hermes")}


def test_chat_replies_come_from_the_agent_service_and_only_services_updates_stop_them(box, monkeypatch):
    home, _ = box
    monkeypatch.setattr(updates, "service_origin", lambda home=None: "http://127.0.0.1:1")
    monkeypatch.setattr(updates, "busy", lambda service, timeout=5: True)
    items = work.inventory(home, now=1000)
    assert names(items) == [("agent", "A chat reply", "service")]
    assert work.blocking(items, "ui") == []
    assert work.blocking(items, "service") == items
    monkeypatch.setattr(updates, "busy", lambda service, timeout=5: None)
    work._cache.clear()
    assert [i["kind"] for i in work.inventory(home, now=2000)] == ["unknown"]


def test_app_manager_jobs_are_tasks_while_their_lock_is_held(box):
    home, _ = box
    lock = home / "app-updates.lock"
    lock.touch()
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert names(work.inventory(home, now=1000)) == [("task", "App updates", "service")]
    finally:
        os.close(fd)
    work._cache.clear()
    assert work.inventory(home, now=1000) == []


def test_blocking_follows_what_the_update_stops():
    items = [{"kind": "agent", "name": "reply", "stops_at": "service"},
             {"kind": "task", "name": "build", "stops_at": "hermes"},
             {"kind": "unknown", "name": "?", "stops_at": "hermes"}]
    assert work.blocking(items, "none") == []
    assert work.blocking(items, "ui") == []
    assert [i["name"] for i in work.blocking(items, "service")] == ["reply"]
    assert [i["name"] for i in work.blocking(items, "hermes")] == ["reply", "build", "?"]
    assert len(work.blocking(items, "os")) == 3
