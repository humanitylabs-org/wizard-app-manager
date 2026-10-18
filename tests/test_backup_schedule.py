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
