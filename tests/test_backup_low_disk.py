"""A running backup must stop before filling a live Wizard's disk; no live services are used."""
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backup_fakes import make_source, write_tools  # noqa: E402
from wam.backup import capture, cli, crypto, keys, restic, schedule, snapshot  # noqa: E402
from wam.backup.box import box_digest, human  # noqa: E402


@pytest.fixture
def source(tmp_path, monkeypatch):
    fb = make_source(tmp_path, write_tools(tmp_path / "tools"))
    for name in ("HOME", "WIZARD_OS_HOME", "HERMES_HOME", "RUNTIPI_ROOT", "TMPDIR"):
        monkeypatch.setenv(name, fb.env[name])
    monkeypatch.setattr(snapshot, "FREE_POLL_SECONDS", 0.01, raising=False)
    keys.store_key(fb.home, crypto.new_recovery_key())
    cfg = schedule.load(fb.box())
    cfg.update({"schema": 2, "min_free_bytes": 1000,
                "target": {"kind": "path", "path": str(tmp_path / "repo")}})
    schedule.save(fb.box(), cfg)
    return fb


class MemoryRepo:
    """Models atomic publication: only a successful writer publishes a snapshot."""
    def __init__(self, tmp_path):
        self.saved = [b"earlier snapshot"]
        self.maintenance = []
        self.root = tmp_path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def open_or_init(self):
        return {"initialized": False}

    def backup_stream(self, write, **kwargs):
        stream = io.BytesIO()
        write(stream)
        self.saved.append(stream.getvalue())
        return {"snapshot_id": "a" * 64, "data_added": 10, "total_bytes_processed": len(stream.getvalue())}

    def cache_dir(self):
        return self.root / "cache"

    def forget(self, *args, **kwargs):
        self.maintenance.append("forget")
        return {}

    def check(self, **kwargs):
        self.maintenance.append("check")
        return {"ok": True, "at": int(time.time())}

    def stats_raw(self):
        return {"snapshots_count": len(self.saved)}


def attach_repo(source, tmp_path, monkeypatch):
    repo = MemoryRepo(tmp_path)
    monkeypatch.setattr(schedule, "open_repo", lambda *args, **kwargs: repo)
    return repo


def test_mid_capture_stop_cleans_only_this_run_and_records_status(source, tmp_path, monkeypatch, capsys):
    box = source.box()
    repo = attach_repo(source, tmp_path, monkeypatch)
    old_work = box.work_dir() / "export-earlier"
    old_work.mkdir()
    (old_work / "keep.zip").write_bytes(b"previous work")
    old_archive = source.runtipi / "backups/wizardapps/notes-app/earlier.tar.gz"
    old_archive.parent.mkdir(parents=True)
    old_archive.write_bytes(b"previous app backup")
    owner_before = box_digest(box)
    schedule.write_status(box, {"ok": True, "finished_at": 123})
    low = {"now": False}
    box.disk_free = lambda path: 999 if low["now"] else 10**15
    real = box.runner.backup
    created = []

    def growing_archive(agent, out):
        real(agent, out)
        created.append(Path(out))
        low["now"] = True
        # No writes to restic here: a stream-only guard would miss this phase.
        time.sleep(0.08)
    monkeypatch.setattr(box.runner, "backup", growing_archive)
    code = cli.main(["run", "--scheduled", "--json"], box=box)
    result = json.loads(capsys.readouterr().out)
    assert code == 75, result
    assert result["error"] == "stopped: free disk below floor"
    assert result["stopped_low_disk"] is True
    assert result["free_bytes"] == 999 and result["min_free_bytes"] == 1000
    assert result["message"] == (f"Backup stopped to protect your Wizard: only {human(999)} free, "
                                 f"needs {human(1000)}. Free some space; nothing was changed.")
    assert not result["ok"] and not result["verified"] and "snapshot" not in result
    assert schedule.read_status(box) == result
    assert result["last_success_at"] == 123
    assert created and not any(p.exists() for p in created)
    assert sorted(p.name for p in box.work_dir().iterdir()) == ["export-earlier"]
    assert (old_work / "keep.zip").read_bytes() == b"previous work"
    assert old_archive.read_bytes() == b"previous app backup"
    assert repo.saved == [b"earlier snapshot"] and repo.maintenance == []
    assert box_digest(box) == owner_before
    assert not signal.getitimer(signal.ITIMER_REAL)[0]


def test_space_fine_completes_capture(source, tmp_path, monkeypatch):
    box = source.box()
    box.disk_free = lambda path: 10**15
    repo = attach_repo(source, tmp_path, monkeypatch)
    result = schedule.run(box, scheduled=True)
    assert result["ok"], result
    assert len(repo.saved) == 2 and repo.saved[0] == b"earlier snapshot"
    assert result["snapshot"] == "a" * 64
    assert not result.get("stopped_low_disk")
    assert not list(box.work_dir().glob("export-*"))


@pytest.mark.parametrize("low_path", ["work", "backups"])
def test_each_filesystem_is_checked_before_capture(source, tmp_path, monkeypatch, capsys, low_path):
    box = source.box()
    repo = attach_repo(source, tmp_path, monkeypatch)
    work = box.work_dir()
    backups = source.runtipi / "backups"
    backups.mkdir()
    box.disk_free = lambda path: 100 if Path(path) == (work if low_path == "work" else backups) else 10**15
    code = cli.main(["run"], box=box)
    assert code == 75
    assert "Backup stopped to protect your Wizard: only" in capsys.readouterr().err
    assert repo.saved == [b"earlier snapshot"] and repo.maintenance == []
    assert not source.tool_calls()


@pytest.mark.parametrize("total,floor", [(20 << 30, 2 << 30), (100 << 30, 5 << 30)])
def test_default_floor_and_missing_backups_parent(source, tmp_path, monkeypatch, total, floor):
    box = source.box()
    cfg = schedule.load(box)
    cfg.pop("min_free_bytes")
    schedule.save(box, cfg)
    repo = attach_repo(source, tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(total, 0, total))
    paths = []
    box.disk_free = lambda path: paths.append(Path(path)) or (floor - 1 if Path(path) == source.runtipi else total)
    result = schedule.run(box)
    assert result.get("stopped_low_disk"), result
    assert result["min_free_bytes"] == floor
    assert all(p.exists() for p in paths)
    assert source.runtipi in paths and not (source.runtipi / "backups").exists()
    assert repo.saved == [b"earlier snapshot"]


@pytest.mark.parametrize("bad", [-1, 0, True, "1000", 1.5])
def test_invalid_floor_cannot_disable_guard(source, tmp_path, monkeypatch, bad):
    box = source.box()
    cfg = schedule.load(box)
    cfg["min_free_bytes"] = bad
    schedule.save(box, cfg)
    repo = attach_repo(source, tmp_path, monkeypatch)
    result = schedule.run(box)
    assert not result["ok"] and "min_free_bytes" in result["error"], result
    assert repo.saved == [b"earlier snapshot"]


@pytest.mark.parametrize("phase", ["stop", "archive"])
def test_low_disk_during_app_capture_restarts_app_and_cleans_partial(source, tmp_path, monkeypatch, phase):
    box = source.box()
    repo = attach_repo(source, tmp_path, monkeypatch)
    low = {"now": False}
    box.disk_free = lambda path: 1 if low["now"] else 10**15
    before = box_digest(box)
    stopped, start = box.rt.stop, box.rt.start
    real_archive = box.appdata.archive
    partial = []

    def stop(urn):
        stopped(urn)
        if phase == "stop" and urn == "notes-app:wizardapps":
            low["now"] = True
            time.sleep(0.08)

    def archive(store, app_id, out, **kwargs):
        if app_id != "notes-app":
            return real_archive(store, app_id, out, **kwargs)
        assert box.rt.apps[f"{app_id}:{store}"]["status"] == "stopped"
        p = Path(str(out) + ".part")
        p.write_bytes(b"this run only")
        partial.append(p)
        low["now"] = True
        time.sleep(0.08)
        pytest.fail("low-disk guard did not interrupt app archive")

    def restart(urn):
        # Cleanup must not be interrupted again even if space stays low during a slow restart.
        time.sleep(0.04)
        return start(urn)

    monkeypatch.setattr(box.rt, "stop", stop)
    monkeypatch.setattr(box.rt, "start", restart)
    monkeypatch.setattr(box.appdata, "archive", archive)
    monkeypatch.setattr(capture, "_missing_images", lambda *args: [])
    result = schedule.run(box)
    assert result.get("stopped_low_disk"), result
    assert box.rt.apps["notes-app:wizardapps"]["status"] == "running"
    assert ("start", "notes-app:wizardapps") in box.rt.calls
    assert not any(p.exists() for p in partial)
    assert not list(box.work_dir().glob("export-*"))
    assert repo.saved == [b"earlier snapshot"] and repo.maintenance == []
    assert box_digest(box) == before


def test_final_check_prevents_publication_between_polls(source, tmp_path, monkeypatch):
    box = source.box()
    repo = attach_repo(source, tmp_path, monkeypatch)
    low = {"now": False}
    box.disk_free = lambda path: 1 if low["now"] else 10**15
    monkeypatch.setattr(snapshot, "FREE_POLL_SECONDS", 60)

    def capture_finishes(box, plain_stream):
        plain_stream.write(b"complete stream but not yet admitted")
        low["now"] = True
        return {}
    monkeypatch.setattr(snapshot.capture, "export", capture_finishes)
    old_handler = signal.getsignal(signal.SIGALRM)
    result = schedule.run(box)
    assert result.get("stopped_low_disk"), result
    assert repo.saved == [b"earlier snapshot"]
    assert signal.getsignal(signal.SIGALRM) == old_handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)


def test_floor_equality_is_allowed_and_alarm_restored(source, tmp_path, monkeypatch):
    box = source.box()
    repo = attach_repo(source, tmp_path, monkeypatch)
    box.disk_free = lambda path: 1000
    monkeypatch.setattr(snapshot.capture, "export", lambda *args, **kwargs: {})
    handler = signal.getsignal(signal.SIGALRM)
    result = schedule.run(box)
    assert result["ok"], result
    assert len(repo.saved) == 2
    assert signal.getsignal(signal.SIGALRM) == handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)


def test_schedule_set_preserves_configured_floor(source, tmp_path, monkeypatch):
    box = source.box()
    attach_repo(source, tmp_path, monkeypatch)
    monkeypatch.setattr(restic, "ensure", lambda box: "not-run")
    box.linger = lambda: True
    result = schedule.set_schedule(box, every="weekly")
    assert result["min_free_bytes"] == 1000
    assert schedule.show(box)["min_free_bytes"] == 1000
    assert schedule.load(box)["min_free_bytes"] == 1000


def test_real_restic_does_not_publish_partial_snapshot(source, tmp_path, monkeypatch):
    binary = os.environ.get("WAM_TEST_RESTIC") or shutil.which("restic")
    if not binary:
        pytest.skip("no restic binary (set WAM_TEST_RESTIC)")
    box = source.box()
    box.disk_free = lambda path: 10**15
    target = {"kind": "path", "path": str(tmp_path / "real-repo")}
    box.env["WAM_RESTIC_CACHE"] = str(tmp_path / "restic-cache")
    with restic.Repo(box, target, "test-only", binary=binary) as repo:
        repo.init()
        earlier = repo.backup_stream(lambda stream: stream.write(b"earlier content"), filename="wizard.tar",
                                     host="test", tags=[])["snapshot_id"]
        before = repo.snapshots()
        low = {"now": False}
        box.disk_free = lambda path: 1 if low["now"] else 10**15

        def stalled_capture(box, plain_stream):
            plain_stream.write(b"partial capture" * 2000)
            plain_stream.flush()
            low["now"] = True
            time.sleep(0.1)
            return {}
        monkeypatch.setattr(snapshot.capture, "export", stalled_capture)
        with pytest.raises(KeyboardInterrupt):
            snapshot.backup(box, repo, "test", min_free_bytes=1000)
        assert repo.snapshots() == before
        with repo.dump(earlier, "/wizard.tar") as stream:
            assert stream.read() == b"earlier content"
        assert not repo.run(["list", "locks"]).strip()
        repo.check(read_data=True)
        box.disk_free = lambda path: 10**15
        resumed = repo.backup_stream(lambda stream: stream.write(b"retry content"), filename="wizard.tar",
                                     host="test", tags=[])
        assert resumed["snapshot_id"] != earlier and len(repo.snapshots()) == 2


def test_restic_cancellation_escalates_and_reaps_child(source, tmp_path, monkeypatch):
    ready, signals = tmp_path / "ready", tmp_path / "signals"
    script = ("import os,signal,time; from pathlib import Path; "
              f"log=Path({str(signals)!r}); "
              "handler=lambda sig,frame: log.open('a').write(str(sig)+'\\n'); "
              "signal.signal(signal.SIGINT,handler); signal.signal(signal.SIGTERM,handler); "
              f"Path({str(ready)!r}).touch(); time.sleep(10)")
    waits, processes = [], []
    real_popen = subprocess.Popen

    class Popen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            processes.append(self)

        def wait(self, timeout=None):
            waits.append(timeout)
            return super().wait(timeout=0.05 if timeout in (10, 5) else timeout)

    monkeypatch.setattr(restic.subprocess, "Popen", Popen)
    box = source.box()
    with restic.Repo(box, {"kind": "path", "path": str(tmp_path / "repo")}, "test-only", binary="unused") as repo:
        monkeypatch.setattr(repo, "_argv", lambda args: [sys.executable, "-c", script])

        def stop(stream):
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists()
            raise snapshot.LowDiskStop(tmp_path, 1, 1000)

        with pytest.raises(snapshot.LowDiskStop):
            repo.backup_stream(stop, filename="wizard.tar", host="test", tags=[])
    assert signals.read_text().splitlines() == [str(signal.SIGINT), str(signal.SIGTERM)]
    assert 10 in waits and 5 in waits
    assert processes[0].returncode == -signal.SIGKILL
    assert processes[0].stdin.closed
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


@pytest.mark.parametrize("interrupted_wait", [10, 5])
def test_repeated_interrupt_cannot_leave_restic_running(source, tmp_path, monkeypatch, interrupted_wait):
    ready = tmp_path / "ready"
    script = ("import signal,time; from pathlib import Path; "
              "signal.signal(signal.SIGINT,signal.SIG_IGN); "
              "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
              f"Path({str(ready)!r}).touch(); time.sleep(10)")
    processes, waits, eof_while_alive = [], [], []
    real_popen = subprocess.Popen
    original_handler = signal.getsignal(signal.SIGINT)

    class Popen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            processes.append(self)

        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout == interrupted_wait:
                raise KeyboardInterrupt("second interrupt during grace period")
            if timeout is None:
                # A further real Ctrl-C during forced cleanup must not interrupt reaping.
                os.kill(os.getpid(), signal.SIGINT)
                eof_while_alive.append(self.stdin.closed and self.poll() is None)
            return super().wait(timeout=0.05 if timeout in (10, 5) else timeout)

    monkeypatch.setattr(restic.subprocess, "Popen", Popen)
    box = source.box()
    try:
        with restic.Repo(box, {"kind": "path", "path": str(tmp_path / "repo")}, "test-only", binary="unused") as repo:
            monkeypatch.setattr(repo, "_argv", lambda args: [sys.executable, "-c", script])

            def stop(stream):
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert ready.exists()
                raise snapshot.LowDiskStop(tmp_path, 1, 1000)

            with pytest.raises(KeyboardInterrupt):
                repo.backup_stream(stop, filename="wizard.tar", host="test", tags=[])
        assert interrupted_wait in waits and None in waits
        assert processes[0].returncode == -signal.SIGKILL
        assert processes[0].stdin.closed
        assert not any(eof_while_alive)
        assert signal.getsignal(signal.SIGINT) == original_handler
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
    finally:
        # A red regression must not leak its intentionally stubborn fixture child.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                    real_popen.wait(proc, timeout=2)
                if not proc.stdin.closed:
                    proc.stdin.close()
        finally:
            signal.signal(signal.SIGINT, original_handler)


def test_stop_interrupts_blocked_capture_child(source, tmp_path, monkeypatch):
    box = source.box()
    repo = attach_repo(source, tmp_path, monkeypatch)
    box.disk_free = lambda path: 1 if (tmp_path / "ready").exists() else 10**15
    child_pid = tmp_path / "child.pid"
    script = ("import os,time; from pathlib import Path; "
              f"Path({str(child_pid)!r}).write_text(str(os.getpid())); "
              f"Path({str(tmp_path / 'ready')!r}).touch(); time.sleep(5)")

    def blocked_capture(box, plain_stream):
        subprocess.run([sys.executable, "-c", script], check=True)
        return {}
    monkeypatch.setattr(snapshot.capture, "export", blocked_capture)
    result = schedule.run(box)
    assert result.get("stopped_low_disk"), result
    assert child_pid.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid.read_text()), 0)
    assert repo.saved == [b"earlier snapshot"]
