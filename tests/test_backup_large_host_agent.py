"""#960: turning on the main agent's backup (`wam alongside`) made the nightly backup fail on a box with a big host
Hermes. The scheduled run still staged every non-database file of the agent in the work folder before streaming it,
so the space check asked for ~106 GiB on a box with 72 GiB free and backed up nothing, apps included. Now big files
are read straight from the agent's home into the restic stream, a host Hermes that still does not fit is left out
with a warning (everything else is backed up), the size walk never enters folders `hermes backup` skips, and
`wam alongside` says what the next backup needs."""
import io
import json
import os
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backup_fakes import HERMES_MODS, FakeBox, _install_now, _package, _w, hermes_home, write_tools  # noqa: E402
from wam import cli  # noqa: E402
from wam.backup import capture  # noqa: E402
from wam.backup.box import DEFER_FILE_MARK, DEFER_FILES_MIN, HERMES_BACKUP_PATCH, HERMES_MAIN, SpaceError  # noqa: E402

GiB = 1 << 30


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k, v in {"HOME": tmp_path / "proc-home", "WIZARD_OS_HOME": tmp_path / "proc-home/.wizard-os",
                 "RUNTIPI_ROOT": tmp_path / "proc-home/runtipi", "HERMES_HOME": tmp_path / "proc-home/.hermes",
                 "TMPDIR": tmp_path}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


def _host_box(tmp_path) -> FakeBox:
    """An alongside box: the main agent is the host Hermes, with one big file, small files and a state.db."""
    b = FakeBox(tmp_path, write_tools(tmp_path / "tools"), "box")
    hh = b.homedir / ".hermes"
    _w(b.home / "alongside.json", json.dumps({"mode": "alongside", "agent": {"external": True, "id": "host-hermes",
                                                                            "hermes_home": str(hh)}}))
    _w(b.home / "settings.json", json.dumps({"name": "hostbox"}))
    _w(b.home / "backup.json", json.dumps({"schema": 1, "enabled": True}))
    hermes_home(hh, wiring=True, messaging=False, marker="host", jobs=[])
    (hh / "media").mkdir()
    (hh / "media" / "talk.mp4").write_bytes(os.urandom(3 * DEFER_FILES_MIN))
    _w(hh / "sessions" / "s1.json", '{"small": true}')
    _package(b.runtipi, "wizardapps", "notes-app", 2, "1.0")
    _install_now(b.runtipi, b.rt, "notes-app:wizardapps")
    _w(b.runtipi / "app-data/wizardapps/notes-app/data/db", "notes")
    _w(b.runtipi / "media/plan.txt", "plan")
    # a host `hermes` launcher of the console-script shape, so wam runs it with its backup patch (as on a box)
    launcher = b.base / "hermes-launcher"
    launcher.write_text(f"#!{sys.executable}\nimport sys\nfrom hermes_cli.main import main\nsys.exit(main())\n")
    launcher.chmod(0o755)
    b.env["WIZARD_HERMES_BIN"] = str(launcher)
    return b


def _stream(box) -> tuple[dict, dict]:
    out = io.BytesIO()
    res = capture.export(box, plain_stream=out)
    with tarfile.open(fileobj=io.BytesIO(out.getvalue()), mode="r:") as t:
        return res, {m.name: t.extractfile(m).read() for m in t if m.isreg()}


def test_patch_defers_big_files_and_keeps_small_ones(tmp_path):
    mods = tmp_path / "mods"
    for rel, text in HERMES_MODS.items():
        (mods / rel).parent.mkdir(parents=True, exist_ok=True)
        (mods / rel).write_text(textwrap.dedent(text))
    home = tmp_path / "home"
    _w(home / "config.yaml", "model: x\n")
    (home / "big.bin").write_bytes(b"b" * 5000)
    _w(home / "small.txt", "s")
    env = {**os.environ, "PYTHONPATH": str(mods), "HERMES_HOME": str(home), "WAM_BACKUP_DEFER_DBS": "1",
           "WAM_BACKUP_DEFER_FILES": "1000"}
    out = tmp_path / "a.zip"
    code = "import sys\n" + HERMES_BACKUP_PATCH + HERMES_MAIN
    r = subprocess.run([sys.executable, "-c", code, "backup", "-o", str(out)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    z = zipfile.ZipFile(out)
    big = z.getinfo("big.bin")
    assert big.file_size == 0 and big.comment == DEFER_FILE_MARK + str(home / "big.bin").encode()
    assert z.read("small.txt") == b"s" and not z.getinfo("small.txt").comment
    # without the database switch nothing is deferred (only a streamed run asks for it)
    del env["WAM_BACKUP_DEFER_DBS"]
    out2 = tmp_path / "b.zip"
    assert subprocess.run([sys.executable, "-c", code, "backup", "-o", str(out2)], env=env).returncode == 0
    assert zipfile.ZipFile(out2).read("big.bin") == b"b" * 5000


def test_streamed_run_reads_big_files_from_the_home_not_the_work_folder(tmp_path, monkeypatch):
    b = _host_box(tmp_path)
    hh = b.homedir / ".hermes"
    big = (hh / "media" / "talk.mp4").read_bytes()
    box = b.box()
    staged = []
    real = box.runner.backup

    def backup(a, z, **kw):
        real(a, z, **kw)
        staged.append(Path(z).stat().st_size)
    monkeypatch.setattr(box.runner, "backup", backup)
    res, c = _stream(box)
    assert res["ok"] and res["agents"] == ["hermes"] and res["warnings"] == []
    z = zipfile.ZipFile(io.BytesIO(c["agents/hermes/hermes-backup.zip"]))
    assert z.testzip() is None and not any(i.comment for i in z.infolist())
    assert z.read("media/talk.mp4") == big and z.read("sessions/s1.json") == b'{"small": true}'
    assert staged and staged[0] < DEFER_FILES_MIN          # the zip in the work folder never held the big file
    assert c["apps/notes-app/app.json"]


def test_jamess_numbers_fit_now(tmp_path, monkeypatch):
    """105.7 GiB that `hermes backup` reads, 39.4 GiB of it databases, 71.9 GiB free: the run used to ask for ~106
    GiB. Now it needs the small files plus one database snapshot."""
    b = _host_box(tmp_path)
    b.free = int(71.9 * GiB)
    monkeypatch.setattr(capture, "_agent_sizes", lambda home: {"raw": int(105.7 * GiB), "kept": int(105.7 * GiB),
                                                              "dbs": int(39.4 * GiB), "small": 2 * GiB})
    box = b.box()
    agents = box.agents(box.installed())
    sizes = {a.id: capture._agent_sizes(a.home) for a in agents}
    room = capture._room(box, agents, sizes, [1 << 20])
    assert room["fits"] and room["need"] < 42 * GiB, room


def test_host_hermes_too_big_is_left_out_and_everything_else_is_backed_up(tmp_path, monkeypatch):
    b = _host_box(tmp_path)
    b.free = 400 << 20
    real = capture._agent_sizes
    monkeypatch.setattr(capture, "_agent_sizes", lambda home: {**real(home), "dbs": 500 << 20}
                        if Path(home).name == ".hermes" else real(home))
    res, c = _stream(b.box())
    assert res["ok"] and res["agents"] == [] and res["apps"] == ["notes-app"]
    assert len(res["warnings"]) == 1 and res["warnings"][0].startswith(capture.MAIN_AGENT_NO_ROOM)
    assert "Free up disk space" in res["warnings"][0] and "keeps everything else" in res["warnings"][0]
    assert "apps/notes-app/app.json" in c and "files/plan.txt" in c
    assert not [n for n in c if n.startswith("agents/hermes/")]
    m = json.loads(c["manifest.json"])
    assert m["warnings"] == res["warnings"]


def test_apps_alone_not_fitting_still_fails_with_the_numbers(tmp_path):
    b = _host_box(tmp_path)
    b.free = 1 << 20
    with pytest.raises(SpaceError, match="not enough free space for the work folder"):
        capture.export(b.box(), plain_stream=io.BytesIO())


@pytest.mark.skipif(os.geteuid() == 0, reason="root can look inside any folder")
def test_unsearchable_pycache_does_not_stop_the_estimate(tmp_path):
    b = _host_box(tmp_path)
    pc = b.homedir / ".hermes" / "plugins" / "x" / "__pycache__"
    pc.mkdir(parents=True)
    (pc / "m.cpython-312.pyc").write_bytes(b"pyc")
    deep = b.homedir / ".hermes" / "notes" / "locked"     # a folder hermes does read, but cannot be looked into
    deep.mkdir(parents=True)
    (deep / "x.txt").write_text("x")
    pc.chmod(0o600)
    deep.chmod(0o600)
    try:
        sizes = capture._agent_sizes(b.homedir / ".hermes")
        assert sizes["dbs"] > 0 and sizes["raw"] >= 3 * DEFER_FILES_MIN
    finally:
        pc.chmod(0o700)
        deep.chmod(0o700)


def test_alongside_says_what_the_next_backup_needs(tmp_path, monkeypatch, capsys):
    from wam.backup import capture as cap
    c = SimpleNamespace(home=tmp_path / "h", runtipi_root=tmp_path / "rt")
    monkeypatch.setattr(cap, "schedule_on", lambda box: True)
    monkeypatch.setattr(cap, "stream_room", lambda box: {"need": 106 * GiB, "free": 72 * GiB, "fits": False,
                                                          "host_left_out": True, "path": "/"})
    r = cli.alongside_backup_room(c)
    assert not r["fits"] and r["main_agent_left_out"]
    assert "106.0 GB" in r["note"] and "72.0 GB" in r["note"] and "leaves the main agent out" in r["note"]
    monkeypatch.setattr(cap, "stream_room", lambda box: {"need": 40 * GiB, "free": 72 * GiB, "fits": True,
                                                          "host_left_out": False, "path": "/"})
    assert cli.alongside_backup_room(c)["fits"]
    monkeypatch.setattr(cap, "schedule_on", lambda box: False)
    assert cli.alongside_backup_room(c) is None


def test_alongside_estimate_is_the_one_the_backup_makes(tmp_path, monkeypatch):
    from wam.backup import box as boxmod
    b = _host_box(tmp_path)
    monkeypatch.setattr(boxmod, "Box", lambda *a, **k: b.box())
    c = SimpleNamespace(home=b.home, runtipi_root=b.runtipi)
    b.free = 2 * GiB
    r = cli.alongside_backup_room(c)
    assert r["fits"] and "includes the main agent" in r["note"]
    assert r["need_bytes"] < capture.MARGIN + DEFER_FILES_MIN            # the big file is not staged
    b.free = 100 << 20
    r = cli.alongside_backup_room(c)
    assert not r["fits"] and not r["main_agent_left_out"] and "free up disk space" in r["note"]
