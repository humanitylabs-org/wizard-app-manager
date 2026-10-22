"""Restore never downgrades an app (wizard-os#91): a backup from a newer release than this box offers is refused for
that app, with a message, its data left as it is here, and every other app restored."""
import io
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backup_fakes import make_fresh, make_source, write_tools  # noqa: E402
from wam.backup import capture, crypto, keys as keylib  # noqa: E402
from wam.backup import cli as bcli  # noqa: E402
from wam.backup.box import box_digest  # noqa: E402

FILES_MSG = ("Files not restored: your backup is from Files 2.0.0, newer than this box's 1.9.0. "
             "Update this box, then restore again.")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k, v in {"HOME": tmp_path / "proc-home", "WIZARD_OS_HOME": tmp_path / "proc-home/.wizard-os",
                 "RUNTIPI_ROOT": tmp_path / "proc-home/runtipi", "HERMES_HOME": tmp_path / "proc-home/.hermes",
                 "TMPDIR": tmp_path}.items():
        monkeypatch.setenv(k, str(v))
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
    return {"tmp": tmp_path, "old": old, "new": new, "keyfile": keyfile}


def _set_version(box, app_id: str, version: str, *, installed=True, store=True, row=True) -> None:
    """Make `box` run / offer `app_id` at `version` (wizard.md of its installed and store packages, apps.json)."""
    rt = box.runtipi
    for on, d in ((installed, rt / "apps/wizardapps" / app_id), (store, rt / "repos/wizardapps/apps" / app_id)):
        if on and (d / "wizard.md").is_file():
            t = (d / "wizard.md").read_text()
            (d / "wizard.md").write_text(re.sub(r"(?m)^version: .*$", f"version: {version}", t))
    if row:
        reg = json.loads((box.home / "apps.json").read_text())
        for a in reg["apps"]:
            if a["id"] == app_id:
                a["version"] = version
        (box.home / "apps.json").write_text(json.dumps(reg))


def _export(world):
    out = world["tmp"] / "out" / "move.wizardbackup"
    out.parent.mkdir(exist_ok=True)
    capture.export(world["old"].box(), out)
    return out


def _cli(box, *argv, json_out=True):
    import contextlib
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bcli.main([*argv, *(["--json"] if json_out else [])], box=box)
    if not json_out:
        return code, out.getvalue() + err.getvalue()
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def _import(world, *extra):
    return _cli(world["new"].box(), "import", str(_export(world)), "--yes", "--key-file", str(world["keyfile"]),
                *extra)


def _files_data(box) -> str:
    return (box.runtipi / "app-data/wizardapps/files-app/data/index.json").read_text()


# ---------------------------------------------------------------- comparison

@pytest.mark.parametrize("a,b,want", [
    ("2.63.23-wizard1", "2.63.23-wizard2", -1),
    ("2.63.23-wizard10", "2.63.23-wizard9", 1),       # numeric, not string order
    ("2.64.0", "2.63.23", 1),
    ("2.63.23", "2.63.23-wizard1", -1),               # plain upstream is wizard0
    ("2.64.0-wizard1", "2.63.99-wizard7", 1),         # upstream first, then the wizard patch
    ("v1.10.0", "1.9.9", 1),
    ("1.0", "1.0.0", 0),
    ("1.0.0-rc1", "1.0.0", -1),
    ("1.0.0-rc.2", "1.0.0-rc.10", -1),
    ("2.0.0", "2.0.0", 0),
    ("nightly", "2.0.0", None),
    (None, "2.0.0", None),
    ("", "1", None),
])
def test_compare(a, b, want):
    from wam.backup import versions
    assert versions.compare(a, b) == want


def test_unknown_is_never_a_refusal_and_same_commit_is_equal():
    from wam.backup import versions
    rec = {"id": "x", "name": "X"}
    assert versions.check(rec, {"version": "garbage", "commit": "a" * 40},
                          {"version": "1.0", "commit": "a" * 40})["verdict"] == "ok"
    w = versions.check(rec, {"version": "garbage", "commit": "a" * 40}, {"version": "1.0", "commit": "b" * 40})
    assert w["verdict"] == "warn" and "restoring it anyway" in w["message"]
    assert versions.check(rec, {"version": None, "commit": None}, None)["verdict"] == "warn"


# ---------------------------------------------------------------- restore

def test_newer_backup_is_refused_data_untouched_others_restored(world):
    new = world["new"]
    _set_version(new, "files-app", "1.9.0")
    secret_before = (new.home / "secrets/files-app.json").read_text()
    uc_before = (new.runtipi / "user-config/wizardapps/files-app/app.env").read_text()
    code, rec = _import(world)
    assert code == bcli.PARTIAL == 3, rec
    assert rec["ok"] is False and rec["partial"] is True
    assert rec["not_restored"] == [FILES_MSG]
    assert FILES_MSG in rec["error"]
    assert [x["id"] for x in rec["refused"]] == ["files-app"]
    assert rec["refused"][0]["backup"]["version"] == "2.0.0" and rec["refused"][0]["here"]["version"] == "1.9.0"
    assert "files-app" not in rec["plan"]["replace"] + rec["plan"]["install"]
    # this box's Files is exactly as it was: data, wiring, secret, release
    assert _files_data(new) == "fresh empty index"
    assert (new.runtipi / "user-config/wizardapps/files-app/app.env").read_text() == uc_before
    assert (new.home / "secrets/files-app.json").read_text() == secret_before
    assert "version: 1.9.0" in (new.runtipi / "apps/wizardapps/files-app/wizard.md").read_text()
    assert not any(c[0] in ("install", "uninstall", "restore") and c[1] == "files-app:wizardapps" for c in new.rt.calls)
    # other apps and agents went on
    assert (new.runtipi / "app-data/wizardapps/notes-app/data/notes.db").read_text() == "old notes database"
    assert set(rec["agents"]) == {"hermes", "research"}
    # the receipt and journal say so too
    receipt = json.loads((new.home / "restores" / rec["stamp"] / "receipt.json").read_text())
    assert receipt["partial"] is True and receipt["not_restored"] == [FILES_MSG]


def test_newer_backup_refusal_cli_text_and_retry_after_update(world):
    new = world["new"]
    _set_version(new, "files-app", "1.9.0")
    before = box_digest(new.box())
    out = _export(world)
    code, text = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(world["keyfile"]), json_out=False)
    assert code == 3
    assert "partly restored" in text and FILES_MSG in text
    # undo still puts the box back exactly
    code, res = _cli(new.box(), "undo")
    assert code == 0 and res["digest_match"] is True, res
    assert box_digest(new.box()) == before
    # the box updates to the backup's release: the same backup now restores Files
    _set_version(new, "files-app", "2.0.0")
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(world["keyfile"]))
    assert code == 0 and rec["ok"] is True and not rec["refused"], rec
    assert _files_data(new) == "old files index"


def test_verify_shows_the_refusal_before_anything_changes(world):
    new = world["new"]
    _set_version(new, "files-app", "1.9.0")
    out = _export(world)
    code, text = _cli(new.box(), "verify", str(out), "--key-file", str(world["keyfile"]), json_out=False)
    assert code == 0 and "NOT restore: " + FILES_MSG in text


def test_equal_version_is_restored(world):
    code, rec = _import(world)
    assert code == 0 and rec["ok"] is True and rec["partial"] is False and rec["refused"] == [], rec
    assert _files_data(world["new"]) == "old files index"


def test_older_backup_restores_onto_newer_release(world):
    """The #91 rig case: a Files -wizard1 backup onto a box that offers -wizard2 keeps the data and the newer build."""
    old, new = world["old"], world["new"]
    _set_version(old, "files-app", "2.63.23-wizard1")
    _set_version(new, "files-app", "2.63.23-wizard2")
    code, rec = _import(world)
    assert code == 0 and rec["ok"] is True and rec["refused"] == [], rec
    assert _files_data(new) == "old files index"
    assert "version: 2.63.23-wizard2" in (new.runtipi / "apps/wizardapps/files-app/wizard.md").read_text()


def test_unparseable_version_proceeds_with_a_warning(world):
    old, new = world["old"], world["new"]
    _set_version(old, "files-app", "2.0.0")
    _set_version(new, "files-app", "nightly")
    (new.runtipi / "apps/wizardapps/files-app/.wizard-commit").write_text("e" * 40 + "\n")
    code, rec = _import(world)
    assert code == 0 and rec["ok"] is True and rec["refused"] == [], rec
    assert _files_data(new) == "old files index"
    assert any("Files: cannot tell" in w and "restoring it anyway" in w for w in rec["plan"]["warnings"])


def test_newer_agent_app_skips_its_agent_and_keeps_this_boxs(world):
    old, new = world["old"], world["new"]
    _set_version(old, "hermes-agent", "1.0")         # this box offers 0.9
    home = new.runtipi / "app-data/wizardapps/hermes-agent/data"
    cfg_before = (home / "config.yaml").read_text()
    code, rec = _import(world)
    assert code == 3, rec
    assert rec["not_restored"] == ["Hermes not restored: your backup is from Hermes 1.0, newer than this box's 0.9. "
                                   "Update this box, then restore again."]
    assert rec["plan"]["agents_skipped"] == ["hermes"] and "hermes" not in rec["agents"]
    assert (home / "config.yaml").read_text() == cfg_before
    assert (home / "memories/MEMORY.md").read_text() == "memory of newmain"
    assert "research" in rec["agents"] and _files_data(new) == "old files index"


def test_release_installed_by_runtipi_is_checked_again(world, monkeypatch):
    """A public app is fetched from the App Index when Runtipi installs it: if that release is older than the
    backup's, the app is taken off again and none of the backup's data goes into it."""
    new = world["new"]
    real = new.rt.install

    def install(urn, form=None):
        if urn == "notes-app:wizardapps":
            _set_version(new, "notes-app", "1.3.0", installed=False, row=False)
        return real(urn, form)
    monkeypatch.setattr(new.rt, "install", install)
    code, rec = _import(world)
    assert code == 3, rec
    assert rec["not_restored"] == ["Notes not restored: your backup is from Notes 1.4.0, newer than this box's "
                                   "1.3.0. Update this box, then restore again."]
    assert ("uninstall", "notes-app:wizardapps") in new.rt.calls
    assert "notes-app:wizardapps" not in new.rt.apps
    assert not (new.runtipi / "app-data/wizardapps/notes-app/data/notes.db").exists()
    assert _files_data(new) == "old files index"
    assert ("start", "notes-app:wizardapps") not in new.rt.calls
    code, res = _cli(new.box(), "undo")
    assert code == 0 and res["digest_match"] is True, res
