"""End to end on fake boxes: export, verify, import onto a fresh box, undo, activate (docs/BACKUP.md).

Every box is a temp folder (HOME, WIZARD_OS_HOME, RUNTIPI_ROOT, HERMES_HOME, TMPDIR point into it); Runtipi is a
FakeRuntipi acting on that tree; `docker` and `hermes` are fake executables on PATH that emulate stock Hermes."""
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backup_fakes import make_fresh, make_source, mode, write_tools  # noqa: E402
from wam.backup import bundle, capture, crypto, fmt, keys as keylib, restore  # noqa: E402
from wam.backup import cli as bcli  # noqa: E402
from wam.backup.box import SpaceError, box_digest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Belt and braces: even code that reads os.environ or Path.home() sees only the case's temp folder."""
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
    return {"tmp": tmp_path, "old": old, "new": new, "key": key, "keyfile": keyfile}


def _export(world, **kw):
    out = world["tmp"] / "out" / "move.wizardbackup"
    out.parent.mkdir(exist_ok=True)
    res = capture.export(world["old"].box(), out, **kw)
    return out, res


def _contents(path: Path, key: bytes) -> dict[str, bytes]:
    with open(path, "rb") as f:
        r = fmt.open_reader(f, fmt.Keys([key]))
        data = r.read()
        r.drain()
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as t:
        for m in t:
            out[m.name] = t.extractfile(m).read() if m.isreg() else None
    return out


def _cli(box, *argv) -> tuple[int, dict]:
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = bcli.main([*argv, "--json"], box=box)
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 1, buf.getvalue()
    return code, json.loads(lines[0])


# ---------------------------------------------------------------- 3. export

def test_export_against_a_fake_box(world):
    old = world["old"]
    before = box_digest(old.box())
    out, res = _export(world)
    assert res["ok"] and out.is_file() and mode(out) == 0o600
    assert not list(out.parent.glob("*.part"))
    c = _contents(out, world["key"])
    names = list(c)
    assert names[0] == "manifest.json" and names[-1] == "checksums.json"
    m = json.loads(c["manifest.json"])
    # manifest: 2 agents, apps with versions/commits/data, files totals, active record
    assert sorted(a["id"] for a in m["agents"]) == ["hermes", "research"]
    apps = {a["id"]: a for a in m["apps"]}
    assert {"notes-app", "files-app", "hermes-agent", "agent-research"} <= set(apps)
    assert apps["notes-app"]["version"] == "1.4.0" and apps["notes-app"]["commit"] == "n" * 40
    assert apps["notes-app"]["tipi_version"] == 3 and apps["notes-app"]["data"] == "runtipi-backup"
    assert apps["hermes-agent"]["data"] == "agent"
    assert m["files"] == {"included": True, "count": 3, "bytes": len("the owner's plan") + 900 + len("dotfile")}
    act = m["active"]
    assert act["schedule"] is True
    assert act["agents"]["hermes"] == {"messaging": ["slack", "telegram"], "cron": ["j1"]}
    assert act["agents"]["research"] == {"messaging": [], "cron": ["j3"]}
    assert m["source"]["mode"] == "full" and m["source"]["versions"]["runtipi"] == "v4.0.0-test"
    # no secrets in the manifest
    blob = c["manifest.json"].decode()
    for s in ("tg-secret-token", "old-generated-pw", "OLD-API-TOKEN", "WZRK", "SOME_FORM"):
        assert s not in blob
    # excluded per-machine and regenerated items are absent; secrets/ present (inside the encrypted file)
    for gone in ("wizard-os/api-token", "wizard-os/mcp-token", "wizard-os/runtipi-operator.json",
                 "wizard-os/agent-runtime/helper.json", "wizard-os/apps.json", "wizard-os/skills/notes-app/SKILL.md",
                 "wizard-os/cache/junk.bin", "wizard-os/settings.json.bak1", "wizard-os/agents/research/agent.json",
                 "agents/research/folder/upload-staging/partial.bin", "agents/research/folder/home"):
        assert gone not in c, gone
    assert json.loads(c["wizard-os/secrets/notes-app.json"]) == {"DB_PASSWORD": "old-generated-pw"}
    assert c["agents/research/folder/runtime.key"] == b"research-runtime-key"
    assert "files/Documents/plan.txt" in c and "files/.hidden" in c
    # agents: stock hermes backup zips
    z = zipfile.ZipFile(io.BytesIO(c["agents/hermes/hermes-backup.zip"]))
    assert "config.yaml" in z.namelist() and "memories/MEMORY.md" in z.namelist()
    # apps: package + Runtipi's own per-app archive (app-data + app + user-config)
    assert "apps/notes-app/package/config.json" in c
    with tarfile.open(fileobj=io.BytesIO(c["apps/notes-app/data.tar.gz"])) as t:
        inner = t.getnames()
    assert "./app-data/data/notes.db" in inner and "./user-config/app.env" in inner and "./app/config.json" in inner
    assert "apps/hermes-agent/data.tar.gz" not in c
    app_json = json.loads(c["apps/notes-app/app.json"])
    assert app_json["form"] == {"SOME_FORM": "value"}
    # nothing readable leaks into the file
    raw = out.read_bytes()
    for s in (b"tg-secret-token", b"old-generated-pw", b"the owner's plan", b"manifest"):
        assert s not in raw
    # export changed nothing on the old box and cleaned up after itself
    assert box_digest(old.box()) == before
    assert not list((old.runtipi / "backups").rglob("*.tar.gz"))
    assert not any((old.home / "backup-work").iterdir())
    # stock hermes ran in the agent's own image with the home mounted; no key on any command line
    calls = old.tool_calls()
    runs = [c["argv"] for c in calls if c["tool"] == "docker" and c["argv"][0] == "run"]
    assert len(runs) == 2 and all("ghcr.io/example/hermes-agent:test" in r for r in runs)
    assert all("--network" in r and "none" in r for r in runs)
    assert not any("WZRK" in " ".join(c["argv"]) for c in calls)
    # Runtipi did the app data capture (as root, with its own stop/start), files-app stayed stopped
    assert ("backup", "notes-app:wizardapps") in old.rt.calls and ("backup", "files-app:wizardapps") in old.rt.calls
    assert old.rt.apps["files-app:wizardapps"]["status"] == "stopped"


def test_export_no_files_and_passphrase(world):
    out, res = _export(world, passphrase="move it", include_files=False, scrypt_n=1 << 10)
    assert res["slots"] == ["recovery", "passphrase"]
    with open(out, "rb") as f:
        r = fmt.open_reader(f, fmt.Keys(passphrases=["move it"]))
        r.read()
    assert not any(k.startswith("files/") for k in _contents(out, world["key"]))


def test_export_needs_a_key(world):
    (world["old"].home / "secrets" / "backup-key.json").unlink()
    with pytest.raises(capture.ExportError, match="key new"):
        _export(world)


# ---------------------------------------------------------------- 8. free space

def test_free_space_preflight_export_and_import(world):
    old, new = world["old"], world["new"]
    old.free = 10_000
    with pytest.raises(SpaceError, match="not enough free space"):
        _export(world)
    assert not list((world["tmp"] / "out").glob("*"))
    old.free = None
    out, _ = _export(world)
    new.free = 10_000
    before = box_digest(new.box())
    code, res = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(world["keyfile"]))
    assert code == 1 and "not enough free space" in res["error"]
    assert box_digest(new.box()) == before
    assert not (new.home / "restores").exists() and not (new.runtipi / "media" / ".wizard-restore").exists()


# ---------------------------------------------------------------- 4. import

def _flip_byte(src: Path, dst: Path, at: int) -> None:
    b = bytearray(src.read_bytes())
    b[at] ^= 0x01
    dst.write_bytes(bytes(b))


def test_verify_before_change_wrong_key_and_tampered(world):
    out, _ = _export(world)
    new = world["new"]
    before = box_digest(new.box())
    top_before = sorted(os.listdir(new.home))
    media_before = sorted(os.listdir(new.runtipi / "media"))
    wrong = world["tmp"] / "wrong.key"
    wrong.write_text(crypto.encode_recovery_key(crypto.new_recovery_key()))
    code, res = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(wrong))
    assert code == 1 and "wrong key" in res["error"]
    for at in (out.stat().st_size // 2, out.stat().st_size - 3):
        bad = world["tmp"] / f"bad-{at}.wizardbackup"
        _flip_byte(out, bad, at)
        code, res = _cli(new.box(), "import", str(bad), "--yes", "--key-file", str(world["keyfile"]))
        assert code == 1 and ("damaged" in res["error"] or "cut short" in res["error"]), res
    cut = world["tmp"] / "cut.wizardbackup"
    cut.write_bytes(out.read_bytes()[:-5000])
    code, res = _cli(new.box(), "import", str(cut), "--yes", "--key-file", str(world["keyfile"]))
    assert code == 1
    assert box_digest(new.box()) == before
    assert sorted(os.listdir(new.home)) == top_before
    assert sorted(os.listdir(new.runtipi / "media")) == media_before
    assert not (new.home / "restores").exists()
    assert ("install", "notes-app:wizardapps") not in new.rt.calls
    # verify reports the plan and changes nothing
    code, res = _cli(new.box(), "verify", str(out), "--key-file", str(world["keyfile"]))
    assert code == 0 and res["ok"] and res["plan"]["install"] and box_digest(new.box()) == before


def test_import_onto_fresh_box_then_undo_then_reimport_and_activate(world):
    out, exp = _export(world)
    new = world["new"]
    before = box_digest(new.box())
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(world["keyfile"]))
    assert code == 0, rec
    h, rt = new.home, new.runtipi
    # owner state reproduced
    assert json.loads((h / "settings.json").read_text())["name"] == "oldbox"
    assert json.loads((h / "secrets" / "notes-app.json").read_text())["DB_PASSWORD"] == "old-generated-pw"
    assert mode(h / "secrets" / "notes-app.json") == 0o600
    assert json.loads((h / "secrets" / "whoami.json").read_text()) == {"K": "kept"}   # app the backup doesn't carry
    assert (h / "links.json").is_file() and (h / "app-bindings.json").is_file()
    # this box's identity kept
    assert (h / "api-token").read_text() == "NEW-API-TOKEN\n"
    assert (h / "mcp-token").read_text() == "NEW-MCP-TOKEN\n"
    assert json.loads((h / "runtipi-operator.json").read_text())["password"] == "new-op"
    assert sorted(rec["kept_identity"]) == ["api-token", "mcp-token", "runtipi-operator.json"]
    # app data via Runtipi: notes-app installed at the bundled version, files-app data replaced
    assert rec["installed_by_restore"] == ["notes-app:wizardapps", "agent-research:wizardapps"] or \
        sorted(rec["installed_by_restore"]) == ["agent-research:wizardapps", "notes-app:wizardapps"]
    assert (rt / "app-data/wizardapps/notes-app/data/notes.db").read_text() == "old notes database"
    assert (rt / "user-config/wizardapps/notes-app/app.env").read_text() == "NOTES_EXTRA=1\n"
    assert new.rt.apps["notes-app:wizardapps"]["version"] == 3
    assert (rt / "app-data/wizardapps/files-app/data/index.json").read_text() == "old files index"
    assert (rt / "app-data/wizardapps/whoami/data/x").read_text() == "whoami data"   # left alone
    assert rec["apps"]["notes-app"] == "running" and rec["apps"]["files-app"] == "stopped"
    # files
    assert (rt / "media/Documents/plan.txt").read_text() == "the owner's plan"
    assert not (rt / "media/existing.txt").exists()
    # main agent: imported, this box's wiring kept, messaging off (tokens kept), cron paused
    home = rt / "app-data/wizardapps/hermes-agent/data"
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["marker"] == "oldmain"
    assert cfg["terminal"]["cwd"] == "/new/box/workspace"
    assert cfg["mcp_servers"]["wizard"]["url"] == "http://new-box-service:8800/mcp"
    assert cfg["platforms"]["telegram"]["enabled"] is False and cfg["platforms"]["telegram"]["extra"] == {"chat": "x"}
    assert cfg["platforms"]["slack"]["enabled"] is False and cfg["platforms"]["discord"]["enabled"] is False
    assert cfg["platforms"]["api_server"]["enabled"] is True
    env = (home / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN=tg-secret-token" in env and "API_SERVER_KEY=newmain-api-key" in env
    assert "API_SERVER_KEY=oldmain-api-key" not in env and "WIZARD_MARK=newmain" in env
    assert "OPENROUTER_API_KEY=oldmain-provider" in env
    jobs = {j["id"]: j for j in json.loads((home / "cron/jobs.json").read_text())["jobs"]}
    assert jobs["j1"]["state"] == "paused" and jobs["j2"]["state"] == "paused"
    assert (home / "memories/MEMORY.md").read_text() == "memory of oldmain"
    # per-machine files of the agent home stayed this box's own
    assert (home / "bin/tool").read_text() == "binary of newmain"
    assert (home / "logs/agent.log").read_text() == "log of newmain"
    assert (home / "plugins/wizard-os/plugin.py").read_text() == "wizard plugin of newmain"
    assert (home / "plugins/own-plugin/plugin.py").read_text() == "own plugin of oldmain"
    assert set(rec["agents"]["hermes"]["paused_jobs"]) == {"j1"}
    assert set(rec["agents"]["hermes"]["disabled_messaging"]) == {"telegram", "slack"}
    # extra agent: new folder from the bundle, cron paused
    ra = h / "agents/research"
    assert (ra / "runtime.key").read_text() == "research-runtime-key"
    assert json.loads((ra / "agent.json").read_text())["id"] == "research"
    rjobs = json.loads((ra / "home/cron/jobs.json").read_text())["jobs"]
    assert rjobs[0]["state"] == "paused"
    assert not (ra / "upload-staging").exists()
    # schedule copied but off, new box id; receipt written; archives deleted
    sched = json.loads((h / "backup.json").read_text())
    assert sched["enabled"] is False and sched["box_id"] != "oldboxid0001"
    assert sched["target"]["prefix"] == f"wizard/{sched['box_id']}/"
    stamp = rec["stamp"]
    assert json.loads((h / "restores" / stamp / "receipt.json").read_text())["ok"] is True
    assert not list((h / "restores" / stamp).rglob("*.zip"))
    assert not (h / "restores" / stamp / "stage").exists()
    assert not list((rt / "backups").rglob("*wizard-restore*"))

    # 6. activate needs --old-is-off
    code, res = _cli(new.box(), "activate")
    assert code == 1 and "old-is-off" in res["error"]
    assert yaml.safe_load((home / "config.yaml").read_text())["platforms"]["telegram"]["enabled"] is False

    # 5. undo restores the pre-import digest exactly (and uninstalls what the restore installed)
    code, res = _cli(new.box(), "undo")
    assert code == 0 and res["digest_match"] is True, res
    assert box_digest(new.box()) == before
    assert ("uninstall", "notes-app:wizardapps") in new.rt.calls
    assert ("uninstall", "agent-research:wizardapps") in new.rt.calls
    assert "notes-app:wizardapps" not in new.rt.apps
    assert (rt / "media/existing.txt").read_text() == "made on the new box"
    assert not (rt / "media/.wizard-restore").exists()
    code, res = _cli(new.box(), "undo")
    assert code == 1 and "already undone" in res["error"]

    # import again, then activate: exactly what was on at export comes back
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(world["keyfile"]))
    assert code == 0, rec
    new.systemctl_calls.clear()
    code, res = _cli(new.box(), "activate", "--old-is-off")
    assert code == 0, res
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["platforms"]["telegram"]["enabled"] is True and cfg["platforms"]["slack"]["enabled"] is True
    assert cfg["platforms"]["discord"]["enabled"] is False          # was off at export: stays off
    jobs = {j["id"]: j for j in json.loads((home / "cron/jobs.json").read_text())["jobs"]}
    assert jobs["j1"]["state"] == "scheduled" and jobs["j1"]["enabled"] is True
    assert jobs["j2"]["state"] == "paused"                           # was paused at export: stays paused
    rjobs = json.loads((h / "agents/research/home/cron/jobs.json").read_text())["jobs"]
    assert rjobs[0]["state"] == "scheduled"
    assert json.loads((h / "backup.json").read_text())["enabled"] is True
    assert ["enable", "--now", "wizard-backup.timer"] in new.systemctl_calls
    assert (Path(new.env["HOME"]) / ".config/systemd/user/wizard-backup.timer").is_file()
    code, res = _cli(new.box(), "activate", "--old-is-off")
    assert code == 1 and "already activated" in res["error"]
    code, st = _cli(new.box(), "status")
    assert st["last_restore"]["activated"] is True


def test_import_without_yes_on_non_tty_changes_nothing(world):
    out, _ = _export(world)
    new = world["new"]
    before = box_digest(new.box())
    code, res = _cli(new.box(), "import", str(out), "--key-file", str(world["keyfile"]))
    assert code == 1 and res["verified"] is True and "not confirmed" in res["error"]
    assert box_digest(new.box()) == before and not (new.home / "restores").exists()


def test_import_failure_part_way_can_be_undone(world, monkeypatch):
    out, _ = _export(world)
    new = world["new"]
    before = box_digest(new.box())
    real = new.rt.restore_backup
    calls = {"n": 0}

    def flaky(urn, filename):
        calls["n"] += 1
        if calls["n"] == 2:
            raise restore.RuntipiError("simulated Runtipi failure")
        return real(urn, filename)
    monkeypatch.setattr(new.rt, "restore_backup", flaky)
    code, res = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(world["keyfile"]))
    assert code == 1 and "wam backup undo" in res["error"]
    monkeypatch.setattr(new.rt, "restore_backup", real)
    code, res = _cli(new.box(), "undo")
    assert code == 0 and res["digest_match"], res
    assert box_digest(new.box()) == before


def test_cli_key_commands_never_put_keys_on_argv(world, tmp_path):
    new = world["new"]
    code, res = _cli(new.box(), "key", "show-id")
    assert code == 0 and res["stored"] is False
    code, res = _cli(new.box(), "key", "new", "--out", str(tmp_path / "k.txt"))
    assert code == 0 and "recovery_key" not in res and mode(tmp_path / "k.txt") == 0o600
    assert crypto.decode_recovery_key((tmp_path / "k.txt").read_text())
    code, res = _cli(new.box(), "key", "new")
    assert code == 1 and "--force" in res["error"]
    assert mode(new.home / "secrets" / "backup-key.json") == 0o600
    code, res = _cli(new.box(), "key", "set", "--key-file", str(world["keyfile"]), "--force")
    assert code == 0 and res["key_id"] == crypto.key_id(world["key"])
