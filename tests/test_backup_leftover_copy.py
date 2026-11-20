"""#826: a leftover copy of an installed app under a second store (runtipi/apps/<other>/<id>, app-data/<other>/<id>)
never fails the whole backup on `duplicate bundle entry apps/<id>`: the installed copy (the apps.json row's urn) is
backed up, the leftover is left out and named in the result, and the backup restores the installed app."""
import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backup_fakes import make_fresh, make_source, write_tools  # noqa: E402
from wam.backup import capture, crypto, keys as keylib  # noqa: E402
from wam.backup import cli as bcli  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k, v in {"HOME": tmp_path / "proc-home", "WIZARD_OS_HOME": tmp_path / "proc-home/.wizard-os",
                 "RUNTIPI_ROOT": tmp_path / "proc-home/runtipi", "HERMES_HOME": tmp_path / "proc-home/.hermes",
                 "TMPDIR": tmp_path}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    for k in ("WIZARD_BACKUP_KEY", "WIZARD_BACKUP_PASSPHRASE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def world(tmp_path):
    tools = write_tools(tmp_path / "tools")
    old = make_source(tmp_path, tools)
    new = make_fresh(tmp_path, tools)
    key = crypto.new_recovery_key()
    keylib.store_key(old.home, key)
    keyfile = tmp_path / "recovery.key"
    keyfile.write_text(crypto.encode_recovery_key(key) + "\n")
    keyfile.chmod(0o600)
    # the leftover: an old copy of notes-app under a second store, still listed by Runtipi (stopped)
    rt = old.runtipi
    for part in ("apps", "app-data"):
        shutil.copytree(rt / part / "wizardapps" / "notes-app", rt / part / "wizard" / "notes-app")
    (rt / "app-data" / "wizard" / "notes-app" / "data" / "notes.db").write_text("stale leftover notes")
    old.rt.add("notes-app:wizard", "stopped", 1)
    return {"tmp": tmp_path, "old": old, "new": new, "key": key, "keyfile": keyfile}


def _members(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as t:
        return {m.name: t.extractfile(m).read() for m in t if m.isreg()}


def test_one_copy_per_app_prefers_the_registry_then_running():
    inst = {"v:wizard": {"id": "v", "store": "wizard", "status": "running"},
            "v:wizardapps": {"id": "v", "store": "wizardapps", "status": "stopped"},
            "w:wizardapps": {"id": "w", "store": "wizardapps", "status": "running"}}
    keep, skipped = capture.one_copy_per_app(inst, {"v": {"id": "v", "urn": "v:wizardapps"}})
    assert sorted(keep) == ["v:wizardapps", "w:wizardapps"] and len(skipped) == 1 and "v:wizard)" in skipped[0]
    keep, skipped = capture.one_copy_per_app(inst, {})
    assert sorted(keep) == ["v:wizard", "w:wizardapps"] and "v:wizardapps" in skipped[0]
    # an agent's runtime urn wins over the registry and the running copy
    keep, _ = capture.one_copy_per_app(inst, {"v": {"id": "v", "urn": "v:wizard"}}, prefer={"v:wizardapps"})
    assert "v:wizardapps" in keep and "v:wizard" not in keep


def test_a_leftover_copy_of_an_agent_app_keeps_the_agents_runtime(world):
    old = world["old"]
    rt = old.runtipi
    for part in ("apps", "app-data"):
        src = rt / part / "wizardapps" / "agent-research"
        if src.is_dir():
            shutil.copytree(src, rt / part / "wizard" / "agent-research")
    old.rt.add("agent-research:wizard", "running", 1)
    stream = io.BytesIO()
    res = capture.export(old.box(), plain_stream=stream)
    assert res["ok"] and any("agent-research:wizard)" in m for m in res["skipped_app_copies"])
    m = json.loads(_members(stream.getvalue())["manifest.json"])
    assert [a["urn"] for a in m["apps"] if a["id"] == "agent-research"] == ["agent-research:wizardapps"]


def test_scheduled_stream_survives_a_leftover_copy(world):
    stream = io.BytesIO()
    res = capture.export(world["old"].box(), plain_stream=stream)
    assert res["ok"]
    assert len(res["skipped_app_copies"]) == 1 and "notes-app:wizard)" in res["skipped_app_copies"][0]
    c = _members(stream.getvalue())
    app = json.loads(c["apps/notes-app/app.json"])
    assert app["urn"] == "notes-app:wizardapps" and app["store"] == "wizardapps"
    with tarfile.open(fileobj=io.BytesIO(c["apps/notes-app/data.tar"])) as t:
        db = t.extractfile("app-data/data/notes.db").read()
    assert b"stale leftover" not in db
    m = json.loads(c["manifest.json"])
    assert [a["urn"] for a in m["apps"] if a["id"] == "notes-app"] == ["notes-app:wizardapps"]
    # the leftover was never stopped or started for the backup
    assert not [x for x in world["old"].rt.calls if x[1:] == ("notes-app:wizard",)]


def test_backup_file_with_a_leftover_copy_restores_the_installed_app(world):
    out = world["tmp"] / "out" / "move.wizardbackup"
    out.parent.mkdir()
    res = capture.export(world["old"].box(), out)
    assert res["ok"] and res["apps"].count("notes-app") == 1
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = bcli.main(["import", str(out), "--yes", "--key-file", str(world["keyfile"]), "--json"],
                         box=world["new"].box())
    assert code == 0, buf.getvalue()
    new = world["new"]
    assert new.rt.apps["notes-app:wizardapps"]
    assert "notes-app:wizard" not in new.rt.apps
    db = new.runtipi / "app-data" / "wizardapps" / "notes-app" / "data" / "notes.db"
    assert db.is_file() and "stale leftover" not in db.read_text()
