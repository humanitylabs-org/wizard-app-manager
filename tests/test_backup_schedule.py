"""Scheduled backups into a restic repository (docs/BACKUP.md, "Scheduled backups").

Uses a real restic binary against a local-folder repository in the case's temp folder: WAM_TEST_RESTIC, else
`restic` on PATH (any 0.17+); the cases that need one skip cleanly without it. Boxes are the fake boxes of
backup_fakes (temp HOME / WIZARD_OS_HOME / RUNTIPI_ROOT / HERMES_HOME / TMPDIR)."""
import bz2
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backup_fakes import make_fresh, make_source, mode, write_tools  # noqa: E402
from test_backup_flow import check_imported  # noqa: E402
from wam.backup import cli as bcli  # noqa: E402
from wam.backup import crypto, keys as keylib, restic, schedule, snapshot  # noqa: E402
from wam.backup.box import box_digest  # noqa: E402


def _restic_bin() -> str | None:
    p = os.environ.get("WAM_TEST_RESTIC") or shutil.which("restic")
    if p and (restic.version_of(p) or (0,)) >= restic.MIN_VERSION:
        return p
    return None


RESTIC = _restic_bin()
needs_restic = pytest.mark.skipif(not RESTIC, reason="no restic binary (set WAM_TEST_RESTIC)")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k, v in {"HOME": tmp_path / "proc-home", "WIZARD_OS_HOME": tmp_path / "proc-home/.wizard-os",
                 "RUNTIPI_ROOT": tmp_path / "proc-home/runtipi", "HERMES_HOME": tmp_path / "proc-home/.hermes",
                 "TMPDIR": tmp_path}.items():
        monkeypatch.setenv(k, str(v))
    for k in ("WIZARD_BACKUP_KEY", "WIZARD_BACKUP_PASSPHRASE", "WIZARD_S3_ACCESS_KEY_ID",
              "WIZARD_S3_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)


class Recorder:
    """Wraps subprocess.run/Popen: every argv and env any restic call got."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple[list[str], dict]] = []
        real_run, real_popen = subprocess.run, subprocess.Popen
        rec = self

        def run(argv, *a, **kw):
            rec.calls.append((list(map(str, argv)), dict(kw.get("env") or {})))
            return real_run(argv, *a, **kw)

        class Popen(real_popen):
            def __init__(self, argv, *a, **kw):
                rec.calls.append((list(map(str, argv)), dict(kw.get("env") or {})))
                super().__init__(argv, *a, **kw)
        monkeypatch.setattr(restic.subprocess, "run", run)
        monkeypatch.setattr(restic.subprocess, "Popen", Popen)


@pytest.fixture
def world(tmp_path, monkeypatch):
    tools = write_tools(tmp_path / "tools")
    old = make_source(tmp_path, tools)
    new = make_fresh(tmp_path, tools)
    key = crypto.new_recovery_key()
    keylib.store_key(old.home, key)
    keyfile = tmp_path / "recovery.key"
    keyfile.write_text(crypto.encode_recovery_key(key) + "\n")
    keyfile.chmod(0o600)
    for b in (old, new):
        if RESTIC:
            b.env["WAM_RESTIC_BIN"] = RESTIC
        b.env["WAM_RESTIC_NO_INSTALL"] = "1"
        b.env["WAM_RESTIC_CACHE"] = str(b.base / "restic-cache")
    linger = []
    return {"tmp": tmp_path, "old": old, "new": new, "key": key, "keyfile": keyfile, "repo": tmp_path / "repo",
            "rec": Recorder(monkeypatch), "linger": linger}


def _box(fb, world=None):
    b = fb.box()
    if world is not None:
        b.linger = lambda: world["linger"].append(1) or True
    return b


def _cli(bx, *argv):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = bcli.main([*argv, "--json"], box=bx)
    finally:
        sys.stdout = old
    return code, json.loads(buf.getvalue().strip().splitlines()[-1])


def _set(world, *extra):
    return _cli(_box(world["old"], world), "schedule", "set", "--path", str(world["repo"]), *extra)


def _secrets(world) -> list[str]:
    return [crypto.encode_recovery_key(world["key"]), world["key"].hex()]


def _assert_no_secret_leaked(world, extra_files=()):
    secrets = _secrets(world)
    for argv, env in world["rec"].calls:
        line = " ".join(argv)
        assert not any(s in line for s in secrets), argv
        assert not any(s in v for s in secrets for v in env.values()), "secret in restic's environment"
        if env.get("RESTIC_PASSWORD_FILE"):
            assert not Path(env["RESTIC_PASSWORD_FILE"]).exists(), "password file left behind"
    old = world["old"]
    files = [old.home / "backup.json", old.home / "backup-status.json", *extra_files]
    for f in files:
        if f.is_file():
            text = f.read_text()
            assert not any(s in text for s in secrets), f
    # no password file anywhere in the box's temp/work folders
    leftovers = [p for p in old.base.rglob("password") if "restic-" in str(p.parent.name)]
    assert not leftovers


# ---------------------------------------------------------------- unit level (no restic needed)

def test_retention_defaults_and_old_keep_mapping():
    assert schedule.retention({}) == {"daily": 7, "weekly": 4, "monthly": 6}
    assert schedule.retention({"keep": 3}) == {"daily": 3, "weekly": 4, "monthly": 6}
    assert schedule.retention({"retention": {"daily": 2, "weekly": 0, "monthly": 12}}) == \
        {"daily": 2, "weekly": 0, "monthly": 12}


def test_repo_urls_and_options():
    t = schedule.target_from_flags(s3="https://s3.example.com/bkt/some/prefix", region="us-east-1", path_style=True)
    assert restic.repo_url(t) == "s3:https://s3.example.com/bkt/some/prefix"
    assert restic.options(t) == ["-o", "s3.region=us-east-1", "-o", "s3.bucket-lookup=path"]
    assert restic.repo_url(schedule.target_from_flags(sftp="u@host:/srv/b")) == "sftp:u@host:/srv/b"
    with pytest.raises(schedule.ScheduleError):
        schedule.target_from_flags(s3="https://x/b", path="/tmp/x")
    with pytest.raises(schedule.ScheduleError):
        schedule.parse_sftp("not a target")


def test_unit_texts():
    u = schedule.unit_texts({"every": "daily", "at": "04:10"}, "%h/.local/bin/wam")
    assert "OnCalendar=*-*-* 04:10:00" in u["wizard-backup.timer"] and "Persistent=true" in u["wizard-backup.timer"]
    assert "ExecStart=%h/.local/bin/wam backup run --scheduled" in u["wizard-backup.service"]
    assert "restic" in u["wizard-backup.service"]


def _fake_release(tmp_path, version="0.19.1"):
    script = f"#!/bin/sh\necho 'restic {version} compiled with go on linux/amd64'\n".encode()
    return bz2.compress(script)


def test_install_checks_the_pinned_checksum(tmp_path, monkeypatch):
    data = _fake_release(tmp_path)
    fb_env = {"HOME": str(tmp_path / "h"), "PATH": "/usr/bin:/bin", "WAM_BACKUP_TEST": "1"}
    from wam.backup.box import Box
    bx = Box(home=tmp_path / "h/.wizard-os", runtipi_root=tmp_path / "rt", env=fb_env)
    # mismatch: refused, nothing installed
    with pytest.raises(restic.ResticError, match="pinned checksum"):
        restic.install(bx, fetch=lambda url: data, machine="x86_64")
    assert not (tmp_path / "h/.local/bin/restic").exists()
    # matching pin: installed 0755 and found afterwards
    monkeypatch.setitem(restic.PINS, "amd64", hashlib.sha256(data).hexdigest())
    seen = []
    res = restic.install(bx, fetch=lambda url: seen.append(url) or data, machine="x86_64")
    assert res["path"] == str(tmp_path / "h/.local/bin/restic") and mode(Path(res["path"])) == 0o755
    assert seen == [f"https://github.com/restic/restic/releases/download/v{restic.VERSION}/"
                    f"restic_{restic.VERSION}_linux_amd64.bz2"]
    assert restic.find(bx) == res["path"]
    # an owner-installed restic of another version is never replaced
    other = _fake_release(tmp_path, "0.16.0")
    (tmp_path / "h/.local/bin/restic").write_bytes(bz2.decompress(other))
    res2 = restic.install(bx, fetch=lambda url: data, machine="x86_64")
    assert res2["path"].endswith(f"wizard-backup/bin/restic-{restic.VERSION}")
    assert b"0.16.0" in (tmp_path / "h/.local/bin/restic").read_bytes()
    assert restic.find(bx) == res2["path"]


def test_pins_cover_both_linux_arches():
    assert set(restic.PINS) == {"amd64", "arm64"}
    assert all(len(v) == 64 for v in restic.PINS.values())
    assert restic.arch("aarch64") == "arm64" and restic.arch("x86_64") == "amd64"


def test_scheduled_run_without_schedule_is_a_noop(world):
    world["old"].home.joinpath("backup.json").unlink()
    code, res = _cli(_box(world["old"]), "run", "--scheduled")
    assert code == 0 and res["skipped"]


# ---------------------------------------------------------------- with a real restic

@needs_restic
def test_schedule_set_creates_the_repo_units_and_linger(world):
    code, res = _set(world)
    assert code == 0, res
    assert res["repository"]["initialized"] is True and (world["repo"] / "config").is_file()
    assert res["retention"] == {"daily": 7, "weekly": 4, "monthly": 6}
    d = Path(world["old"].env["HOME"]) / ".config/systemd/user"
    assert (d / "wizard-backup.timer").is_file() and (d / "wizard-backup.service").is_file()
    assert ["enable", "--now", "wizard-backup.timer"] in world["old"].systemctl_calls
    assert world["linger"] == [1]
    cfg = json.loads((world["old"].home / "backup.json").read_text())
    assert cfg["schema"] == 2 and cfg["target"] == {"kind": "path", "path": str(world["repo"])}
    # setting it again opens the existing repository
    code, res = _set(world, "--keep", "3")
    assert code == 0 and res["repository"]["initialized"] is False and res["retention"]["daily"] == 3
    code, res = _cli(_box(world["old"]), "schedule", "off")
    assert code == 0 and res["enabled"] is False
    _assert_no_secret_leaked(world)


@needs_restic
def test_two_runs_second_adds_little_then_check_and_status(world):
    rt = world["old"].runtipi
    # big enough to span many of restic's content-defined chunks (512 KiB..8 MiB, ~1 MiB average): every changed
    # spot in the stream costs about one chunk, so the capture must be much larger than a few chunks
    for i in range(4):
        (rt / f"media/Photos/big-{i}.raw").write_bytes(os.urandom(6 << 20))
    (rt / "app-data/wizardapps/notes-app/data/attachments.bin").write_bytes(os.urandom(3 << 20))
    (rt / "app-data/wizardapps/hermes-agent/data/memories/big-notes.bin").write_bytes(os.urandom(2 << 20))
    assert _set(world)[0] == 0
    code, r1 = _cli(_box(world["old"]), "run")
    assert code == 0, r1
    assert r1["ok"] and r1["verified"] and r1["check"]["ok"] and r1["read_data"]["ok"]
    assert r1["data_added"] > 25 << 20 and r1["capture_bytes"] > 25 << 20 and r1["snapshots"] == 1
    assert r1["peak_extra_disk_bytes"] >= 0 and "repository_bytes" in r1
    # a small change: a new owner file and a new agent session marker
    (rt / "media/Documents/new-note.txt").write_text("a new note\n")
    sess = rt / "app-data/wizardapps/hermes-agent/data/sessions"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "session-new.json").write_text(json.dumps({"marker": "new session"}))
    code, r2 = _cli(_box(world["old"]), "run")
    assert code == 0, r2
    assert r2["snapshots"] == 2
    assert r2["data_added"] < r1["data_added"] / 3, (r1["data_added"], r2["data_added"])
    assert "read_data" in r2 and r2["read_data"]["at"] == r1["read_data"]["at"]   # weekly: not again today
    st = json.loads((world["old"].home / "backup-status.json").read_text())
    assert st["ok"] and st["snapshot"] == r2["snapshot"] and st["last_success_at"]
    code, sn = _cli(_box(world["old"]), "snapshots")
    assert code == 0 and [s["id"] for s in sn["snapshots"]] == [r1["snapshot"], r2["snapshot"]]
    # the snapshot holds the plain, dedupe-friendly capture
    names = _snapshot_names(world, r2["snapshot"])
    assert names[0] == "manifest.json" and names[-1] == "checksums.json"
    assert "apps/notes-app/data.tar" in names and not any(n.endswith(".tar.gz") for n in names)
    _assert_no_secret_leaked(world)


def _snapshot_names(world, snap_id):
    bx = _box(world["old"])
    with schedule.open_repo(bx, {"kind": "path", "path": str(world["repo"])}, world["key"]) as repo:
        with repo.dump(snap_id, snapshot.PATH) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as t:
                names = [m.name for m in t]
    return names


@needs_restic
def test_retention_forgets_and_prunes(world):
    assert _set(world, "--keep-daily", "1", "--keep-weekly", "0", "--keep-monthly", "0")[0] == 0
    for _ in range(3):
        code, r = _cli(_box(world["old"]), "run")
        assert code == 0, r
    # three runs today: keep-daily 1 keeps only the newest
    assert r["snapshots"] == 1 and r["retention"]["removed"] >= 1
    assert r["retention"]["policy"] == {"daily": 1, "weekly": 0, "monthly": 0}


@needs_restic
def test_object_lock_target_tolerates_refused_deletes(world, monkeypatch):
    assert _set(world)[0] == 0
    cfg = json.loads((world["old"].home / "backup.json").read_text())
    cfg["target"]["object_lock"] = True
    (world["old"].home / "backup.json").write_text(json.dumps(cfg))
    seen = []

    def refused(self, policy, *, host, prune=True, max_unused=None):
        seen.append(max_unused)
        raise restic.Refused("restic forget: the storage refused it (Access Denied.)")
    monkeypatch.setattr(restic.Repo, "forget", refused)
    code, r = _cli(_box(world["old"]), "run")
    assert code == 0 and r["ok"] and r["retention"]["deferred"] is True, r
    assert seen == ["unlimited"]
    # without object_lock a refused delete is a failed run
    cfg["target"]["object_lock"] = False
    (world["old"].home / "backup.json").write_text(json.dumps(cfg))
    code, r = _cli(_box(world["old"]), "run")
    assert code == 1 and "refused" in r["error"]
    # prune-by owner: the box does not delete at all
    cfg["prune_by"] = "owner"
    (world["old"].home / "backup.json").write_text(json.dumps(cfg))
    seen.clear()
    code, r = _cli(_box(world["old"]), "run")
    assert code == 0 and r["retention"]["by"] == "owner" and seen == []


@needs_restic
def test_wrong_key_is_refused(world, tmp_path):
    assert _set(world)[0] == 0
    assert _cli(_box(world["old"]), "run")[0] == 0
    other = tmp_path / "other.key"
    other.write_text(crypto.encode_recovery_key(crypto.new_recovery_key()) + "\n")
    new = world["new"]
    code, res = _cli(_box(new), "snapshots", "--path", str(world["repo"]), "--key-file", str(other))
    assert code == 1 and "does not open" in res["error"]
    code, res = _cli(_box(new), "import", "--from-repo", "latest", "--path", str(world["repo"]), "--yes",
                     "--key-file", str(other))
    assert code == 1 and "does not open" in res["error"]
    # the box's own key changed: the scheduled run reports it
    keylib.store_key(world["old"].home, crypto.new_recovery_key())
    code, r = _cli(_box(world["old"]), "run")
    assert code == 1 and r["kind"] == "WrongKey"
    # schedule set onto a repository locked with another key is refused
    code, r = _set(world)
    assert code == 1 and "another key" in r["error"]


@needs_restic
def test_tampered_pack_is_detected(world):
    assert _set(world)[0] == 0
    code, r = _cli(_box(world["old"]), "run")
    assert code == 0
    packs = sorted((world["repo"] / "data").rglob("*"), key=lambda p: p.stat().st_size if p.is_file() else 0)
    big = packs[-1]
    big.chmod(0o600)
    b = bytearray(big.read_bytes())
    b[len(b) // 2] ^= 0xFF
    big.write_bytes(bytes(b))
    bx = _box(world["old"])
    with schedule.open_repo(bx, {"kind": "path", "path": str(world["repo"])}, world["key"]) as repo:
        with pytest.raises(restic.ResticError):
            repo.check(read_data=True)
    # restoring from it fails before anything on the target changes
    new = world["new"]
    before = box_digest(new.box())
    code, res = _cli(_box(new), "import", "--from-repo", "latest", "--path", str(world["repo"]), "--yes",
                     "--key-file", str(world["keyfile"]))
    assert code == 1, res
    assert box_digest(new.box()) == before
    assert not list((new.home / "restores").glob("*/stage")) if (new.home / "restores").exists() else True


@needs_restic
def test_from_repo_import_onto_a_second_box_then_undo(world):
    assert _set(world)[0] == 0
    code, r = _cli(_box(world["old"]), "run")
    assert code == 0, r
    new = world["new"]
    before = box_digest(new.box())
    # a new box: nothing configured, the repository named by flags, key from a file
    code, rec = _cli(_box(new), "import", "--from-repo", "latest", "--path", str(world["repo"]), "--yes",
                     "--key-file", str(world["keyfile"]))
    assert code == 0, rec
    assert rec["snapshot"]["id"] == r["snapshot"]
    check_imported(new, rec)
    sched = json.loads((new.home / "backup.json").read_text())
    assert sched["target"] == {"kind": "path", "path": str(world["repo"])} and sched["enabled"] is False
    code, res = _cli(_box(new), "undo")
    assert code == 0 and res["digest_match"] is True, res
    assert box_digest(new.box()) == before
    # by snapshot id prefix too
    code, rec = _cli(_box(new), "import", "--from-repo", r["snapshot"][:8], "--path", str(world["repo"]), "--yes",
                     "--key-file", str(world["keyfile"]))
    assert code == 0, rec
    _assert_no_secret_leaked(world)


@needs_restic
def test_password_never_on_argv_env_or_files(world, capfd):
    assert _set(world)[0] == 0
    assert _cli(_box(world["old"]), "run")[0] == 0
    assert _cli(_box(world["old"]), "snapshots")[0] == 0
    calls = [(argv, env) for argv, env in world["rec"].calls if argv and argv[0] == RESTIC]
    assert calls and all(env.get("RESTIC_PASSWORD_FILE") for argv, env in calls if "version" not in argv)
    assert not any("RESTIC_PASSWORD" == k for _, env in calls for k in env)
    out = capfd.readouterr()
    assert not any(s in out.out + out.err for s in _secrets(world))
    _assert_no_secret_leaked(world)


@needs_restic
def test_s3_credentials_reach_restic_only_by_env(world, tmp_path):
    creds = tmp_path / "s3.env"
    creds.write_text("AWS_ACCESS_KEY_ID=AKTEST\nAWS_SECRET_ACCESS_KEY=SKTEST-very-secret\n")
    bx = _box(world["old"])
    t = schedule.target_from_flags(s3="http://127.0.0.1:9/bucket/p", region="us-east-1", path_style=True)
    repo = schedule.open_repo(bx, t, world["key"], schedule.read_s3_secret(bx, str(creds)))
    with repo:
        env = repo.env()
        assert env["AWS_SECRET_ACCESS_KEY"] == "SKTEST-very-secret"
        assert "SKTEST-very-secret" not in " ".join(repo._argv(["snapshots"]))
        assert repo.scrub("x SKTEST-very-secret y") == "x *** y"
    assert not Path(env["RESTIC_PASSWORD_FILE"]).exists()


@needs_restic
def test_second_concurrent_run_exits_cleanly(world):
    assert _set(world)[0] == 0
    with schedule.lock(_box(world["old"])):
        code, res = _cli(_box(world["old"]), "run", "--scheduled")
    assert code == 0 and "another backup" in res["skipped"]


@needs_restic
def test_read_data_check_is_weekly(world):
    assert _set(world)[0] == 0
    bx = _box(world["old"])
    r1 = schedule.run(bx)
    assert r1["read_data"]["subset"] == "10%"
    r2 = schedule.run(bx, now=time.time() + 3 * 86400)
    assert r2["read_data"]["at"] == r1["read_data"]["at"]
    r3 = schedule.run(bx, now=time.time() + 8 * 86400)
    assert r3["read_data"]["at"] >= r1["read_data"]["at"] and r3["read_data"] is not r1["read_data"]
    st = json.loads((world["old"].home / "backup-status.json").read_text())
    assert st["read_data"]["ok"] is True


@needs_restic
def test_streamed_run_keeps_at_most_one_app_archive_on_disk(world):
    assert _set(world)[0] == 0
    fb = world["old"]
    seen = []
    from wam.backup.appdata import AppData
    real = AppData.archive

    def archive(self, store, app_id, out, **kw):             # app archives are made in the work folder
        seen.append(len(list(Path(out).parent.glob("app-*.tar.gz"))))
        return real(self, store, app_id, out, **kw)
    AppData.archive = archive
    try:
        code, r = _cli(_box(fb), "run")
    finally:
        AppData.archive = real
    assert code == 0, r
    assert len(seen) >= 2 and max(seen) == 0, seen          # every earlier archive was gone before the next
    assert not list((fb.runtipi / "backups").rglob("*.tar.gz"))
    assert r["peak_extra_disk_bytes"] >= 0


def test_storage_refusals_become_refused_with_a_short_reason(tmp_path):
    from wam.backup.box import Box
    bx = Box(home=tmp_path / "h", runtipi_root=tmp_path / "rt", env={"HOME": str(tmp_path), "WAM_BACKUP_TEST": "1"})
    repo = restic.Repo(bx, {"kind": "path", "path": str(tmp_path / "r")}, "pw", binary="/bin/false")
    err = repo._fail("forget", 3, 'Remove(<snapshot/4c6e>) failed: client.RemoveObject: Access Denied.\n'
                                   '{"message_type":"exit_error","code":3,"message":"failed to remove\\nmain.init"}')
    assert isinstance(err, restic.Refused) and "Access Denied" in str(err) and "main.init" not in str(err)
    assert isinstance(repo._fail("open", 12, "Fatal: wrong password or no key found"), restic.WrongKey)
    assert isinstance(repo._fail("open", 10, "Fatal: repository does not exist"), restic.NoRepo)


# ---------------------------------------------------------------- stale repository locks (#870)

class LockRepo:
    """Stands in for restic.Repo: a list of locks; `unlock` drops the ones restic would call stale."""

    def __init__(self, locks, restic_stale=None, started=()):
        self._locks, self.unlocked, self.started = list(locks), 0, set(started)
        self.restic_stale = restic_stale or (lambda lk: True)

    def started_here(self, pid, at):
        return pid in self.started

    def locks(self):
        return [dict(lk) for lk in self._locks]

    def unlock(self):
        self.unlocked += 1
        self._locks = [lk for lk in self._locks if not self.restic_stale(lk)]


def _lk(i, host="box", pid=100, age=60.0, now=1_800_000_000.0):
    t = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - age)) + ".123456789Z"
    return {"id": f"{i:064x}", "time": t, "exclusive": False, "hostname": host, "username": "o", "pid": pid}


NOW = 1_800_000_000.0


def test_a_crashed_runs_lock_on_this_box_is_removed():
    repo = LockRepo([_lk(1, pid=111, age=20 * 3600)])
    res = schedule.clear_stale_locks(repo, hostname="box", alive=lambda pid: False, now=NOW)
    assert repo.unlocked == 1 and [r["pid"] for r in res["removed"]] == [111] and res["left"] == []


def test_a_live_lock_and_another_machines_fresh_lock_are_left_alone():
    live, theirs, dead = _lk(1, pid=222), _lk(2, host="laptop", pid=333), _lk(3, pid=444, age=3 * 3600)
    # restic itself would remove only the dead one (the others are fresh and, here, alive)
    repo = LockRepo([live, theirs, dead], restic_stale=lambda lk: lk["pid"] == 444)
    res = schedule.clear_stale_locks(repo, hostname="box", alive=lambda pid: pid == 222, now=NOW)
    assert [r["pid"] for r in res["removed"]] == [444]
    left = {r["pid"]: r["why"] for r in res["left"]}
    assert set(left) == {222, 333} and "still running" in left[222] and "another machine" in left[333]


def test_nothing_to_clear_runs_no_unlock():
    repo = LockRepo([_lk(1, pid=222)])
    res = schedule.clear_stale_locks(repo, hostname="box", alive=lambda pid: True, now=NOW)
    assert repo.unlocked == 0 and res["removed"] == [] and len(res["left"]) == 1
    repo = LockRepo([])
    assert schedule.clear_stale_locks(repo, hostname="box", now=NOW) == {"removed": [], "left": []}
    assert repo.unlocked == 0


def test_a_reused_pid_does_not_keep_an_old_lock():
    # the PID now belongs to another process, but restic refreshes a live lock every 5 minutes: 3 h old = stale
    repo = LockRepo([_lk(1, pid=555, age=3 * 3600)])
    res = schedule.clear_stale_locks(repo, hostname="box", alive=lambda pid: True, now=NOW)
    assert repo.unlocked == 1 and [r["pid"] for r in res["removed"]] == [555]


def test_another_machines_abandoned_lock_stops_the_unlock():
    # `restic unlock` would also drop the other machine's old lock: that box decides, so nothing is unlocked
    repo = LockRepo([_lk(1, pid=111, age=3 * 3600), _lk(2, host="laptop", pid=9, age=2 * 3600)])
    res = schedule.clear_stale_locks(repo, hostname="box", alive=lambda pid: False, now=NOW)
    assert repo.unlocked == 0 and res["removed"] == [] and len(res["left"]) == 2
    assert any("restic unlock" in r["why"] for r in res["left"])


def test_a_young_lock_with_a_gone_pid_waits_for_restics_stale_limit():
    # it may be a box with the same hostname sharing the repository (restic can't tell): `restic unlock` would
    # remove it, so nothing is unlocked, also not an old one of ours next to it, until it is 30 minutes old
    repo = LockRepo([_lk(1, pid=111, age=5 * 60), _lk(2, pid=112, age=3 * 3600)])
    res = schedule.clear_stale_locks(repo, hostname="box", alive=lambda pid: False, now=NOW)
    assert repo.unlocked == 0 and res["removed"] == [] and len(res["left"]) == 2
    assert "less than 30 minutes" in {r["pid"]: r["why"] for r in res["left"]}[111]
    repo = LockRepo([_lk(1, pid=111, age=31 * 60)])
    res = schedule.clear_stale_locks(repo, hostname="box", alive=lambda pid: False, now=NOW)
    assert repo.unlocked == 1 and [r["pid"] for r in res["removed"]] == [111]


def test_a_young_lock_of_a_restic_this_box_started_is_removed_at_once():
    repo = LockRepo([_lk(1, pid=111, age=60)], started={111})
    res = schedule.clear_stale_locks(repo, hostname="box", alive=lambda pid: False, now=NOW)
    assert repo.unlocked == 1 and [r["pid"] for r in res["removed"]] == [111]


def test_started_here_reads_the_boxs_own_restic_starts(tmp_path):
    r = restic.Repo.__new__(restic.Repo)
    r.box = type("B", (), {"env": {"WAM_RESTIC_CACHE": str(tmp_path / "c" / "restic")}})()
    for pid in range(1000, 1000 + restic.STARTED_KEEP + 5):
        r._note_started(pid)
    at = time.time()
    assert r.started_here(1000 + restic.STARTED_KEEP + 4, at) and not r.started_here(1000, at)   # oldest dropped
    assert not r.started_here(1000 + restic.STARTED_KEEP + 4, at - 3600)    # a lock older than the start: not it
    assert not r.started_here(7, at) and (tmp_path / "c" / "restic-started.jsonl").is_file()


def test_a_lock_listing_failure_never_fails_the_run():
    class Broken(LockRepo):
        def locks(self):
            raise restic.ResticError("restic list locks failed (exit 1): boom")
    res = schedule.clear_stale_locks(Broken([]), hostname="box", now=NOW)
    assert res["removed"] == [] and "boom" in res["error"]


def test_already_locked_reads_in_plain_words():
    r = restic.Repo.__new__(restic.Repo)
    r._password, r._creds, r.url = "pw", {}, "/r"
    e = r._fail("forget", 11, "unable to create lock in backend: repository is already locked by PID 1353369 on "
                              "box by o (UID 1000, GID 1000)\nthe `unlock` command can be used to remove stale locks")
    assert isinstance(e, restic.Locked) and "in use by another restic run (PID 1353369 on box)" in str(e)
    assert "left alone" in str(e)


def _repo_env(world):
    bx = _box(world["old"])
    return schedule.open_repo(bx, {"kind": "path", "path": str(world["repo"])}, world["key"])


def _hold_lock(world, noted=False):
    """A real `restic backup --stdin` that holds its repository lock until the caller ends it (`noted`: as if this
    box's Repo had started it)."""
    with _repo_env(world) as repo:
        env = repo.env()
        env["TMPDIR"] = str(world["tmp"])
        pw = world["tmp"] / "pw-copy"
        pw.write_text(repo._password)
        env["RESTIC_PASSWORD_FILE"] = str(pw)
        p = subprocess.Popen([RESTIC, "backup", "--stdin", "--stdin-filename", "x", "--host", "elsewhere"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        if noted:
            repo._note_started(p.pid)
        for _ in range(100):
            if repo.locks():
                return p
            time.sleep(0.1)
    p.kill()
    raise AssertionError("restic never took its lock")


@needs_restic
def test_a_run_after_a_crashed_run_clears_its_lock_and_passes(world, monkeypatch):
    assert _set(world)[0] == 0
    p = _hold_lock(world)
    p.kill()            # the crash: restic dies holding its lock
    p.wait()
    with _repo_env(world) as repo:
        assert [lk["pid"] for lk in repo.locks()] == [p.pid]
    # right after the crash it is left (it could be a same-named box's); restic's own stale limit is 30 minutes
    code, r = _cli(_box(world["old"]), "run", "--scheduled")
    assert not r["ok"] and r["kind"] == "Locked" and [x["pid"] for x in r["locks"]["left"]] == [p.pid], r
    # the next day's run (the crash in #870 was a day earlier) removes it and passes
    sweep = schedule.clear_stale_locks
    monkeypatch.setattr(schedule, "clear_stale_locks", lambda repo, **k: sweep(repo, now=time.time() + 20 * 3600))
    code, r = _cli(_box(world["old"]), "run", "--scheduled")
    assert code == 0 and r["ok"], r
    assert [x["pid"] for x in r["locks"]["removed"]] == [p.pid] and r["retention"]["ok"] and r["check"]["ok"]
    with _repo_env(world) as repo:
        assert repo.locks() == []


@needs_restic
def test_the_next_run_clears_the_lock_of_a_crashed_run_of_this_box(world):
    assert _set(world)[0] == 0
    p = _hold_lock(world, noted=True)
    p.kill()
    p.wait()
    code, r = _cli(_box(world["old"]), "run", "--scheduled")
    assert code == 0 and r["ok"] and [x["pid"] for x in r["locks"]["removed"]] == [p.pid], r
    assert r["retention"]["ok"] and r["check"]["ok"]
    with _repo_env(world) as repo:
        assert repo.locks() == []


@needs_restic
def test_a_live_restic_lock_is_left_alone_and_reported(world):
    assert _set(world)[0] == 0
    p = _hold_lock(world)
    try:
        code, r = _cli(_box(world["old"]), "run", "--scheduled")
        assert not r["ok"] and r["kind"] == "Locked" and "in use by another restic run" in r["error"], r
        assert [x["pid"] for x in r["locks"]["left"]] == [p.pid] and r["locks"]["removed"] == []
        with _repo_env(world) as repo:
            assert [lk["pid"] for lk in repo.locks()] == [p.pid]
    finally:
        p.kill()
        p.wait()
