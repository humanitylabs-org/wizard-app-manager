"""PR #61 review fixes: import over an unfinished restore, symlink chains in every root, engine secrets kept out of
child processes, export under the engine lock, a bounded scrypt cost, and a skipped scheduled run recorded."""
import io
import json
import os
import sys
import tarfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from wam.backup import appdata, bundle, crypto, fmt, restore, schedule  # noqa: E402
from wam.backup.box import Agent, Box, HermesRunner  # noqa: E402
from test_backup_flow import _cli, _export, isolated, world  # noqa: E402,F401 (fixtures)


# ---------------------------------------------------------------- a second import over an unfinished restore

def _last_journal(box):
    root, j = restore.last_restore(box)
    return root, j


@pytest.mark.parametrize("status", ["failed", "applying", "setting-aside", "undo-failed"])
def test_import_refused_while_last_restore_is_unfinished(world, status):
    out, _ = _export(world)
    box = world["new"].box()
    code, rep = _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 0, rep
    root, j = _last_journal(box)
    j["status"] = status
    (root / "journal.json").write_text(json.dumps(j))
    pointer = (box.home / "restores" / "last.json").read_text()
    entries = sorted(p.name for p in (box.home / "restores").iterdir())
    code, rep = _cli(world["new"].box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 1 and not rep["ok"], rep
    assert rep["kind"] == "RestoreError" and "did not finish" in rep["error"] and "wam backup undo" in rep["error"]
    # nothing moved: the pointer still names the restore that holds the real original, and no new restore exists
    assert (box.home / "restores" / "last.json").read_text() == pointer
    assert sorted(p.name for p in (box.home / "restores").iterdir()) == entries


def test_import_refused_after_an_undo_that_stopped_part_way(world):
    out, _ = _export(world)
    box = world["new"].box()
    assert _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")[0] == 0
    root, j = _last_journal(box)
    j["undo_error"] = "OSError: disk went away"
    (root / "journal.json").write_text(json.dumps(j))
    code, rep = _cli(world["new"].box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 1 and "undo stopped part way" in rep["error"], rep


def test_import_allowed_after_restored_and_after_undone(world):
    out, _ = _export(world)
    box = world["new"].box()
    assert _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")[0] == 0
    code, rep = _cli(world["new"].box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 0, rep        # last restore "restored": a new one may start
    assert _cli(world["new"].box(), "undo")[0] == 0
    code, rep = _cli(world["new"].box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 0, rep        # last restore "undone"


def test_unreadable_last_restore_refuses_import(world):
    out, _ = _export(world)
    box = world["new"].box()
    (box.home / "restores").mkdir(parents=True)
    (box.home / "restores" / "last.json").write_text(json.dumps({"stamp": "20260101-000000"}))
    code, rep = _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 1 and "cannot be read" in rep["error"], rep


# ---------------------------------------------------------------- symlink chains, every root

def _tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, kind, target in members:
            ti = tarfile.TarInfo(name)
            ti.type, ti.linkname, ti.mode = kind, target, 0o755
            t.addfile(ti)
    buf.seek(0)
    return buf


ESCAPE = [("app-data", tarfile.DIRTYPE, ""), ("app-data/x", tarfile.DIRTYPE, ""),
          ("app-data/x/l", tarfile.SYMTYPE, ".."), ("app-data/x/esc", tarfile.SYMTYPE, "l/../..")]


def test_symlink_chain_escape_is_refused_in_a_full_archive():
    assert appdata.link_inside("app-data/x/esc", "l/../..")      # textually inside: the chain is what escapes
    with pytest.raises(bundle.Unsafe, match="chain"):
        bundle.check_inner_archive(_tar(ESCAPE))
    with pytest.raises(appdata.UnsafeMember, match="chain"):
        appdata.check_archive(_tar(ESCAPE))


@pytest.mark.parametrize("root", ["app", "user-config", "volumes/v1"])
def test_symlink_chain_escape_is_refused_in_every_root(root):
    members = [(root, tarfile.DIRTYPE, ""), (f"{root}/x", tarfile.DIRTYPE, ""),
               (f"{root}/x/l", tarfile.SYMTYPE, ".."), (f"{root}/x/esc", tarfile.SYMTYPE, "l/../..")]
    if root.startswith("volumes/"):
        members.insert(0, ("volumes", tarfile.DIRTYPE, ""))
    with pytest.raises(bundle.Unsafe, match="chain"):
        bundle.check_inner_archive(_tar(members))


def test_symlink_chains_that_stay_inside_are_kept():
    ok = [("app-data", tarfile.DIRTYPE, ""), ("app-data/x", tarfile.DIRTYPE, ""), ("app-data/x/y", tarfile.DIRTYPE, ""),
          ("app-data/x/l", tarfile.SYMTYPE, ".."), ("app-data/x/y/in", tarfile.SYMTYPE, "../l/x"),
          ("app-data/blobs", tarfile.DIRTYPE, ""), ("app-data/x/snap", tarfile.SYMTYPE, "../blobs")]
    assert bundle.check_inner_archive(_tar(ok))["entries"] == len(ok)


def test_full_restore_refuses_the_chain_before_any_helper(tmp_path):
    p = tmp_path / "a.tar.gz"
    p.write_bytes(_tar(ESCAPE).getvalue())

    class _B:
        runtipi_root = tmp_path
        env = child_env = {}

        def agents_root(self):
            return tmp_path / "agents"
    ad = appdata.AppData(_B())
    ad.keep_in_place = lambda store, app_id: []
    ad.image = lambda: pytest.fail("a helper was started for an unsafe archive")
    with pytest.raises(appdata.UnsafeMember, match="chain"):
        ad.restore("migrated", "notes-app", p)


# ---------------------------------------------------------------- engine secrets stay out of child processes

SECRETS = {"WIZARD_BACKUP_KEY": "WZRK-secret", "WIZARD_BACKUP_PASSPHRASE": "pass-secret",
           "WIZARD_S3_ACCESS_KEY_ID": "AKSECRET", "WIZARD_S3_SECRET_ACCESS_KEY": "SKSECRET"}


def test_host_hermes_and_docker_do_not_inherit_engine_secrets(tmp_path, monkeypatch):
    box = Box(tmp_path / "home", tmp_path / "rt", env={"PATH": "/usr/bin:/bin", "KEEP_ME": "1", **SECRETS})
    assert box.env["WIZARD_BACKUP_KEY"] == "WZRK-secret"      # the engine still reads them
    seen = []

    def fake_run(argv, **kw):
        seen.append(kw["env"])

        class P:
            returncode, stdout, stderr = 0, "", ""
        return P()
    monkeypatch.setattr("subprocess.run", fake_run)
    r = HermesRunner(box)
    monkeypatch.setattr(r, "host_binary", lambda: "/bin/true")
    a = Agent("hermes", True, tmp_path / "hh", "host")
    r.import_zip(a, tmp_path / "x.zip")
    r.gateway(a, "restart")
    r._run(["docker", "ps"])
    box.appdata._run(["docker", "ps"])
    assert len(seen) == 4
    for env in seen:
        assert env["KEEP_ME"] == "1"
        assert not set(SECRETS) & set(env), env
        assert not any(v in SECRETS.values() for v in env.values())
    assert seen[0]["HERMES_HOME"] == str(a.home)


# ---------------------------------------------------------------- export takes the engine lock

def test_export_is_refused_while_another_engine_run_holds_the_lock(world):
    box = world["old"].box()
    out = world["tmp"] / "out" / "locked.wizardbackup"
    with schedule.lock(box):
        code, rep = _cli(world["old"].box(), "export", "-o", str(out))
    assert code == 1 and "another backup or restore" in rep["error"], rep
    assert not out.exists()
    code, rep = _cli(world["old"].box(), "export", "-o", str(out))
    assert code == 0 and out.is_file(), rep


# ---------------------------------------------------------------- scrypt cost is bounded

def test_a_file_cannot_ask_for_more_scrypt_work_than_the_cap(tmp_path):
    assert crypto.SCRYPT_MAX_N == 1 << 18 and crypto.SCRYPT_N == 1 << 17
    with pytest.raises(crypto.AuthError):
        crypto.scrypt("pw", b"s" * 16, 1 << 19)
    p = tmp_path / "f"
    with open(p, "wb") as f:
        w = fmt.Writer(f, fmt.Keys([], ["pw"]), scrypt_n=1 << 10)
        w.write(b"hello")
        w.close()
    with open(p, "rb") as f:
        header, signed, mac = fmt.read_header(f)
    slot = next(s for s in header["slots"] if s.get("kind") == "passphrase")
    slot["n"] = 1 << 20
    called = []
    real = crypto.scrypt
    try:
        crypto.scrypt = lambda *a, **k: called.append(a) or real(*a, **k)
        with pytest.raises(fmt.Damaged, match="scrypt"):
            fmt.unlock(header, signed, mac, fmt.Keys([], ["pw"]))
    finally:
        crypto.scrypt = real
    assert not called                              # refused before any key-derivation work


def test_default_scrypt_files_still_open(tmp_path):
    p = tmp_path / "f"
    with open(p, "wb") as f:
        w = fmt.Writer(f, fmt.Keys([], ["pw"]))     # SCRYPT_N = 2^17
        w.write(b"hello")
        w.close()
    with open(p, "rb") as f:
        r = fmt.open_reader(f, fmt.Keys([], ["pw"]))
        assert r.read() == b"hello"


# ---------------------------------------------------------------- a skipped scheduled run is recorded

def test_scheduled_run_blocked_by_busy_is_recorded(world):
    box = world["old"].box()
    schedule.save(box, {"enabled": True, "target": {"kind": "path", "path": str(world["tmp"] / "repo")}})
    (box.home / "backup-status.json").write_text(json.dumps({"ok": True, "short_id": "abc", "finished_at": 1}))
    with schedule.lock(box):
        code, res = _cli(world["old"].box(), "run", "--scheduled")
    assert code == 0 and "another backup" in res["skipped"], res
    st = json.loads((box.home / "backup-status.json").read_text())
    assert st["last_skipped"]["reason"].startswith("another backup") and st["last_skipped"]["scheduled"] is True
    assert st["short_id"] == "abc" and st["ok"] is True       # the last real run's record is kept
