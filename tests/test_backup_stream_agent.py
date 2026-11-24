"""#771: a scheduled run streams each agent's archive into restic. Its SQLite databases are snapshotted just before
the agent streams and read straight into a stored zip built on the fly, so the work folder never holds a second
uncompressed copy: peak extra disk is about one agent's databases, not ~2x state.db."""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from backup_fakes import HERMES_MODS, make_source, write_tools  # noqa: E402
from wam.backup import bundle, capture  # noqa: E402
from wam.backup.box import DEFER_DB_MARK, HERMES_BACKUP_PATCH, HERMES_MAIN  # noqa: E402


def _entries(data: dict) -> list:
    out = []
    for name, d in data.items():
        attr = (0o40700 << 16) | 0x10 if d is None else 0o100600 << 16
        out.append(bundle.ZipEntry(name, (2026, 11, 18, 12, 0, 0), attr, 3, len(d or b""),
                                   (lambda d=d: io.BytesIO(d))))
    return out


@pytest.mark.parametrize("zip64_at", [None, 1000])
def test_stored_zip_streams_exactly_the_planned_size(monkeypatch, zip64_at):
    if zip64_at:      # zip64 records (a big state.db) change the layout: both passes must agree on it
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", zip64_at)
    data = {"config.yaml": b"model: x\n", "state.db": os.urandom(3 << 20), "memories/": None,
            "memories/MEMORY.md": b"m" * 5000, "empty": b""}
    ents = _entries(data)
    size = bundle.stored_zip_size(ents)
    out = b"".join(bundle.stored_zip_chunks(ents))
    assert len(out) == size
    z = zipfile.ZipFile(io.BytesIO(out))
    assert z.testzip() is None
    assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
    for n, d in data.items():
        if d is not None:
            assert z.read(n) == d


def test_bundle_refuses_a_member_that_shrank():
    ents = _entries({"a": b"12345"})
    ents[0].open = lambda: io.BytesIO(b"12")
    bw = bundle.BundleWriter(io.BytesIO())
    with pytest.raises(ValueError):
        bw.add_stored_zip("agents/x/hermes-backup.zip", ents)


def _mods(tmp: Path) -> Path:
    mods = tmp / "mods"
    for rel, text in HERMES_MODS.items():
        p = mods / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    return mods


def _db(path: Path, rows: int, blob: int = 1000) -> None:
    c = sqlite3.connect(path)
    c.execute("pragma journal_mode=wal")
    c.execute("create table sessions(text)")
    c.executemany("insert into sessions values (?)", [(os.urandom(blob).hex(),) for _ in range(rows)])
    c.commit()
    c.close()


def test_patch_defers_databases_and_snapshot_script_copies_them(tmp_path):
    mods = _mods(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("model: x\n")
    _db(home / "state.db", 50)
    env = {**os.environ, "PYTHONPATH": str(mods), "HERMES_HOME": str(home), "WAM_BACKUP_DEFER_DBS": "1"}
    out = tmp_path / "work" / "a.zip"
    out.parent.mkdir()
    code = "import sys\n" + HERMES_BACKUP_PATCH + HERMES_MAIN
    r = subprocess.run([sys.executable, "-c", code, "backup", "-o", str(out)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    z = zipfile.ZipFile(out)
    info = z.getinfo("state.db")
    assert info.file_size == 0 and info.comment == DEFER_DB_MARK + str(home / "state.db").encode()
    assert z.read("config.yaml") == b"model: x\n"
    assert list(out.parent.iterdir()) == [out]                    # no database copy beside the zip
    # without the switch the patch leaves the database in the zip, as before
    del env["WAM_BACKUP_DEFER_DBS"]
    out2 = tmp_path / "work" / "b.zip"
    assert subprocess.run([sys.executable, "-c", code, "backup", "-o", str(out2)], env=env).returncode == 0
    assert zipfile.ZipFile(out2).getinfo("state.db").file_size > 0
    # the snapshot run (it exits before Hermes' main): a consistent copy, a gone database reported, not an error
    req = {"dir": str(out.parent), "dbs": [[str(home / "state.db"), "s0.db"], [str(home / "gone.db"), "s1.db"]]}
    r = subprocess.run([sys.executable, "-c", code, "backup"], env={**env, "WAM_BACKUP_SNAPSHOT_DBS": "1"},
                       input=json.dumps(req), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {"ok": True, "dbs": {"s0.db": "ok", "s1.db": "gone"}}
    assert sqlite3.connect(out.parent / "s0.db").execute("select count(*) from sessions").fetchone()[0] == 50


@pytest.fixture
def old(tmp_path):
    return make_source(tmp_path, write_tools(tmp_path / "tools"))


class _Watch(io.BytesIO):
    """The restic stream. Also notes how much the work folder holds: on every write into the stream, and every
    millisecond from a thread while it is open (so the agent step before the stream is seen too)."""

    def __init__(self, box) -> None:
        super().__init__()
        self.box, self.peak, self.done = box, 0, threading.Event()
        self.t = threading.Thread(target=self._poll, daemon=True)
        self.t.start()

    def _sample(self) -> None:
        root = Path(self.box.work_dir())
        try:
            self.peak = max(self.peak, sum(p.stat().st_size for p in root.rglob("*") if p.is_file()))
        except OSError:
            pass

    def _poll(self) -> None:
        while not self.done.wait(0.001):
            self._sample()

    def write(self, b):
        self._sample()
        return super().write(b)

    def stop(self) -> None:
        self.done.set()
        self.t.join()


def test_scheduled_stream_snapshots_each_agents_databases_just_in_time(old, monkeypatch):
    home = old.home / "agents/research/home"
    (home / "state.db").unlink()
    _db(home / "state.db", 3000, 1500)                   # ~9 MB of history
    db_size = (home / "state.db").stat().st_size
    (home / "logs").mkdir(exist_ok=True)
    (home / "logs/agent.log").write_text("per machine")
    monkeypatch.setattr(capture, "need_space", lambda *a, **k: None)
    snaps = []
    box = old.box()
    real = box.runner.snapshot_dbs

    def snapshot(a, dbs, work):
        res = real(a, dbs, work)
        snaps.append((a.id, sum((Path(work) / n).stat().st_size for _s, n in dbs if res[n] == "ok"),
                      sum(p.stat().st_size for p in Path(work).rglob("*") if p.is_file())))
        return res
    monkeypatch.setattr(box.runner, "snapshot_dbs", snapshot)
    stream = _Watch(box)
    try:
        res = capture.export(box, plain_stream=stream)
    finally:
        stream.stop()
    assert res["ok"]
    with tarfile.open(fileobj=io.BytesIO(stream.getvalue()), mode="r:") as t:
        c = {m.name: t.extractfile(m).read() for m in t if m.isreg()}
    z = zipfile.ZipFile(io.BytesIO(c["agents/research/hermes-backup.zip"]))
    assert z.testzip() is None
    assert all(i.compress_type == zipfile.ZIP_STORED and not i.comment for i in z.infolist())
    assert not [n for n in z.namelist() if n.startswith("logs/")]
    got = Path(old.base) / "restored.db"
    got.write_bytes(z.read("state.db"))
    assert sqlite3.connect(got).execute("select count(*) from sessions").fetchone()[0] == 3000
    # both agents' databases were snapshotted only when their turn came, and the work folder never held more
    # than the agent's database snapshot plus the small database-free zips
    assert sorted(s[0] for s in snaps) == ["hermes", "research"]
    research = [s for s in snaps if s[0] == "research"][0]
    assert research[2] <= research[1] + (1 << 20)
    assert stream.peak <= 1.1 * db_size, (stream.peak, db_size)
    # the container agents ran the patched backup with the databases deferred
    runs = [x["argv"] for x in old.tool_calls() if x["tool"] == "docker" and x["argv"][0] == "run"]
    assert any("WAM_BACKUP_DEFER_DBS=1" in a for a in runs)
    assert not Path(box.work_dir()).exists() or not list(Path(box.work_dir()).rglob("*.db"))


def test_stream_falls_back_when_hermes_keeps_the_databases(old, monkeypatch):
    """A Hermes the patch does not fit writes the databases into its zip: the stream still carries them, stored."""
    monkeypatch.setattr(capture, "need_space", lambda *a, **k: None)
    box = old.box()
    real = box.runner.backup
    monkeypatch.setattr(box.runner, "backup", lambda a, z, defer_dbs=False, defer_files=None: real(a, z))
    monkeypatch.setattr(box.runner, "snapshot_dbs", lambda *a: pytest.fail("nothing was deferred"))
    stream = io.BytesIO()
    assert capture.export(box, plain_stream=stream)["ok"]
    with tarfile.open(fileobj=io.BytesIO(stream.getvalue()), mode="r:") as t:
        c = {m.name: t.extractfile(m).read() for m in t if m.isreg()}
    z = zipfile.ZipFile(io.BytesIO(c["agents/research/hermes-backup.zip"]))
    assert z.getinfo("state.db").file_size > 0 and z.getinfo("state.db").compress_type == zipfile.ZIP_STORED
