"""#885: a box whose main agent is the host Hermes but that has no alongside.json (it predates alongside mode) used
to get an `ok` backup that silently left the main agent out. The backup still runs, but its result, the manifest,
backup-status.json (what `wam backup status` and the Backup app read) and `wam doctor` now say so, with the step
that brings it in."""
import contextlib
import io
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backup_fakes import FakeBox, _install_now, _package, _w, hermes_home, make_source, write_tools  # noqa: E402
from test_backup_flow import _contents  # noqa: E402
from wam import cli  # noqa: E402
from wam.backup import cli as bcli  # noqa: E402
from wam.backup import crypto, keys as keylib, schedule, snapshot  # noqa: E402
from wam.backup.box import MAIN_AGENT_LEFT_OUT  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k, v in {"HOME": tmp_path / "proc-home", "WIZARD_OS_HOME": tmp_path / "proc-home/.wizard-os",
                 "RUNTIPI_ROOT": tmp_path / "proc-home/runtipi", "HERMES_HOME": tmp_path / "proc-home/.hermes",
                 "TMPDIR": tmp_path}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    for k in ("WIZARD_BACKUP_KEY", "WIZARD_BACKUP_PASSPHRASE"):
        monkeypatch.delenv(k, raising=False)


def _host_main_box(tmp_path, tools) -> FakeBox:
    """James's box: the Wizard runs on the host Hermes (~/.hermes), no hermes-agent app, no alongside.json."""
    b = FakeBox(tmp_path, tools, "host")
    _w(b.home / "settings.json", json.dumps({"name": "hostbox"}))
    _w(b.home / "apps.json", json.dumps({"apps": [
        {"id": "notes-app", "name": "Notes", "type": "view", "version": "1.0", "urn": "notes-app:wizardapps"}]}))
    hermes_home(b.homedir / ".hermes", wiring=True, messaging=True, marker="hostmain", jobs=[])
    _package(b.runtipi, "wizardapps", "notes-app", 2, "1.0")
    _install_now(b.runtipi, b.rt, "notes-app:wizardapps")
    _w(b.runtipi / "app-data/wizardapps/notes-app/data/db", "notes")
    _w(b.runtipi / "media/plan.txt", "plan")
    return b


def _export(fb, tmp_path):
    key = crypto.new_recovery_key()
    keylib.store_key(fb.home, key)
    out = tmp_path / f"{fb.base.name}.wizardbackup"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = bcli.main(["export", "-o", str(out), "--json"], box=fb.box())
    return code, json.loads(buf.getvalue().strip().splitlines()[-1]), out, key


def test_export_names_the_host_hermes_it_leaves_out(tmp_path):
    fb = _host_main_box(tmp_path, write_tools(tmp_path / "tools"))
    code, res, out, key = _export(fb, tmp_path)
    assert code == 0 and res["ok"], res
    hh = str(fb.homedir / ".hermes")
    assert res["agents"] == []                                  # still not captured: nothing changes on the box
    assert len(res["warnings"]) == 1 and res["warnings"][0].startswith(MAIN_AGENT_LEFT_OUT + hh), res["warnings"]
    assert "`wam alongside`" in res["warnings"][0]
    # the file itself says so too, so a restore never mistakes it for a backup with the main agent in it
    manifest = json.loads(_contents(out, key)["manifest.json"])
    assert manifest["warnings"] == res["warnings"] and manifest["agents"] == []


def test_a_box_with_its_main_agent_in_the_backup_has_no_warning(tmp_path):
    fb = make_source(tmp_path, write_tools(tmp_path / "tools"))
    # a host Hermes home next to a full box (e.g. the owner's own Hermes) is not this box's main agent
    hermes_home(fb.homedir / ".hermes", wiring=False, messaging=False, marker="other", jobs=[])
    code, res, _out, _key = _export(fb, tmp_path)
    assert code == 0 and "hermes" in res["agents"] and res["warnings"] == [], res


def test_alongside_box_captures_the_host_hermes_without_a_warning(tmp_path):
    fb = _host_main_box(tmp_path, write_tools(tmp_path / "tools"))
    _w(fb.home / "alongside.json", json.dumps({"mode": "alongside", "agent": {
        "external": True, "id": "host-hermes", "hermes_home": str(fb.homedir / ".hermes")}}))
    code, res, _out, _key = _export(fb, tmp_path)
    assert code == 0 and res["agents"] == ["hermes"] and res["warnings"] == [], res


def test_no_host_hermes_no_warning(tmp_path):
    fb = _host_main_box(tmp_path, write_tools(tmp_path / "tools"))
    shutil.rmtree(fb.homedir / ".hermes")
    code, res, _out, _key = _export(fb, tmp_path)
    assert code == 0 and res["warnings"] == [], res


class _Repo:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def open_or_init(self): return {"initialized": False}
    def forget(self, *a, **k): return {}
    def check(self, **k): return {"ok": True}
    def stats_raw(self): return {"total_size": 10, "snapshots_count": 1}


def test_scheduled_run_is_ok_but_its_status_carries_the_warning(tmp_path, monkeypatch):
    fb = _host_main_box(tmp_path, write_tools(tmp_path / "tools"))
    keylib.store_key(fb.home, crypto.new_recovery_key())
    _w(fb.home / "backup.json", json.dumps({"schema": 2, "enabled": True, "every": "daily", "box_id": "hostbox0001",
                                            "target": {"kind": "path", "path": str(tmp_path / "repo")}}))
    monkeypatch.setattr(schedule, "open_repo", lambda *a, **k: _Repo())

    def backup(box, repo, host, min_free_bytes=None):
        got = {}

        class Sink(io.RawIOBase):
            def writable(self): return True
            def write(self, b): return len(b)
        from wam.backup import capture
        got.update(capture.export(box, plain_stream=Sink()))
        return {"snapshot": "s" * 64, "short_id": "ssssssss", "data_added": 1, "data_added_packed": 1,
                "capture_bytes": got["content_bytes"], "entries": got["entries"], "agents": got["agents"],
                "apps": got["apps"], "apps_without_data": got["apps_without_data"],
                "skipped_app_copies": got["skipped_app_copies"], "warnings": got["warnings"],
                "peak_extra_disk_bytes": 0, "restic_cache_bytes": 0, "seconds": 0.1}
    monkeypatch.setattr(snapshot, "backup", backup)
    st = schedule.run(fb.box(), scheduled=True)
    assert st["ok"] is True, st
    hh = str(fb.homedir / ".hermes")
    status = json.loads((fb.home / "backup-status.json").read_text())
    assert status["ok"] is True and status["warnings"][0].startswith(MAIN_AGENT_LEFT_OUT + hh), status
    # `wam backup status` (what the Backup app's helper reads) passes the last run on as-is
    assert schedule.show(fb.box())["last"]["warnings"] == status["warnings"]


def test_snapshot_backup_passes_the_warnings_on():
    import inspect
    assert '"warnings": got.get("warnings")' in inspect.getsource(snapshot.backup)


def test_doctor_row(tmp_path, monkeypatch):
    home, rt = tmp_path / "h/.wizard-os", tmp_path / "h/runtipi"
    hh = tmp_path / "h/.hermes"
    monkeypatch.setenv("HERMES_HOME", str(hh))
    installed = []
    c = SimpleNamespace(home=home, runtipi_root=rt,
                        runtipi=lambda: SimpleNamespace(installed=lambda: [{"info": {"urn": u}} for u in installed]))
    home.mkdir(parents=True)
    _w(hh / "config.yaml", "model: x\n")
    assert cli.doctor_backup_main_agent(c) is None                           # no backups set up: no row
    _w(home / "backup.json", json.dumps({"enabled": True, "target": {"kind": "path", "path": "/x"}}))
    name, ok, detail = cli.doctor_backup_main_agent(c)
    assert ok is None and detail.startswith(MAIN_AGENT_LEFT_OUT + str(hh)), detail   # a warning, not a failure
    _w(home / "alongside.json", json.dumps({"mode": "alongside", "agent": {"hermes_home": str(hh)}}))
    assert cli.doctor_backup_main_agent(c)[1] is True
    (home / "alongside.json").unlink()
    # a folder a failed install left behind is not an installed main agent: the backup leaves it out too (Codex P2)
    (rt / "app-data/wizardapps/hermes-agent/data").mkdir(parents=True)
    assert cli.doctor_backup_main_agent(c)[1] is None
    installed.append("hermes-agent:wizardapps")
    assert cli.doctor_backup_main_agent(c)[1] is True


def test_verify_and_import_plan_say_what_the_backup_left_out(tmp_path):
    fb = _host_main_box(tmp_path, write_tools(tmp_path / "tools"))
    code, res, out, key = _export(fb, tmp_path)
    assert code == 0, res
    kf = tmp_path / "k"
    kf.write_text(crypto.encode_recovery_key(key))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = bcli.main(["verify", str(out), "--key-file", str(kf), "--json"], box=fb.box())
    ver = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert code == 0 and ver["manifest"]["warnings"] == res["warnings"], ver
    from wam.backup import restore
    manifest = json.loads(_contents(out, key)["manifest.json"])
    p = restore.plan(fb.box(), manifest, probe=False)
    assert p["warnings"][0] == "this backup: " + res["warnings"][0]
