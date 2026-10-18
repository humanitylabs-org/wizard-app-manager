"""Regression tests for what the backup engine met on real Wizard boxes (disposable VMs), reproduced on fake boxes.

- Runtipi's per-app backup silently drops data (fs.cp stops at the first socket), skips every path containing
  "backups", and its restore refuses any archive with a symlink: app data is archived as root by the engine instead,
  keeping in-folder symlinks (a Hugging Face model cache), skipping and reporting sockets.
- Named Docker volumes an app declares (an AI account login shared with the agents) live outside app-data.
- Extra agents live in the Agent Service's own app data (wizard-agent-service/data/wizard-os/agents), not in the
  Wizard home: they must be found as agents (hermes import, cron paused, messaging off) and not travel twice.
- Runtipi chmods an app's data a+rwx on every start: undo's digest must not depend on those bits.
"""
import json
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backup_fakes import _package, _w, make_fresh, make_source, write_tools  # noqa: E402
from test_backup_flow import _cli, _contents, isolated  # noqa: E402,F401
from wam.backup import capture, crypto, keys as keylib  # noqa: E402
from wam.backup.box import SpaceError, box_digest, need_spaces  # noqa: E402

SVC = "app-data/wizardapps/wizard-agent-service/data"
VOL = "wizard-ai-accounts-example"


def _service_layout(b, mcp: str) -> None:
    """Move the box's extra agent into the Agent Service's data (where real boxes keep it) and install the service."""
    rt = b.runtipi
    _package(rt, "wizardapps", "wizard-agent-service", 2, "0.3.0")
    shutil.copytree(rt / "repos/wizardapps/apps/wizard-agent-service", rt / "apps/wizardapps/wizard-agent-service")
    b.rt.add("wizard-agent-service:wizardapps", "running", 2)
    _w(rt / SVC / "wizard-os/mcp-token", mcp, 0o600)
    _w(rt / SVC / "wizard-os/settings.json", json.dumps({"owner": mcp + "-settings"}))
    old = b.home / "agents" / "research"
    if old.is_dir():
        (rt / SVC / "wizard-os/agents").mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(rt / SVC / "wizard-os/agents/research"))
        (b.home / "agents").rmdir()
    b.agent_homes["agent-research:wizardapps"] = rt / SVC / "wizard-os/agents/research/home"


@pytest.fixture
def realbox(tmp_path):
    tools = write_tools(tmp_path / "tools")
    old = make_source(tmp_path, tools)
    new = make_fresh(tmp_path, tools)
    _service_layout(old, "OLD-SERVICE-MCP")
    _service_layout(new, "NEW-SERVICE-MCP")
    rt = old.runtipi
    nd = rt / "app-data/wizardapps/notes-app/data"
    # a model cache: snapshot files are relative symlinks into blobs/
    _w(nd / "hub/blobs/abc123", "model weights")
    (nd / "hub/snapshots/rev1").mkdir(parents=True)
    os.symlink("../../blobs/abc123", nd / "hub/snapshots/rev1/model.bin")
    os.link(nd / "hub/blobs/abc123", nd / "hub/blobs/hardcopy")
    _w(nd / "backups/2026-10-01.db", "the app's own backups folder")   # Runtipi's backup skips "backups" paths
    s = socket.socket(socket.AF_UNIX)
    cwd = os.getcwd()
    os.chdir(nd)                                                       # AF_UNIX paths are short: bind relative
    try:
        s.bind("live.sock")
    finally:
        os.chdir(cwd)
    s.close()
    os.symlink("/etc/passwd", nd / "escape-link")                      # leaves the folder: skipped, reported
    # a named volume the app declares (shared with an agent app on real boxes)
    compose = rt / "apps/wizardapps/notes-app/docker-compose.yml"
    doc = yaml.safe_load(compose.read_text())
    doc["volumes"] = {"login": {"name": VOL}, "scratch": {"external": True}}
    compose.write_text(yaml.safe_dump(doc))
    _w(old.base / "volumes" / VOL / "credentials.json", "OLD-LOGIN")
    _w(new.base / "volumes" / VOL / "credentials.json", "NEW-EMPTY-LOGIN")
    # this box's own install in the Wizard home: never travels, never replaced
    for bx, tag in ((old, "OLD"), (new, "NEW")):
        _w(bx.home / "builder/releases/r1/bundle.json", tag)
        os.symlink("releases/r1", bx.home / "builder/current")
        _w(bx.home / "release.json", tag)
        _w(bx.home / "bin/wam", tag)
    key = crypto.new_recovery_key()
    keylib.store_key(old.home, key)
    kf = tmp_path / "recovery.key"
    kf.write_text(crypto.encode_recovery_key(key) + "\n")
    return {"tmp": tmp_path, "old": old, "new": new, "key": key, "keyfile": kf}


def test_real_box_layout_round_trip(realbox):
    old, new = realbox["old"], realbox["new"]
    out = realbox["tmp"] / "b.wizardbackup"
    before_old = box_digest(old.box())
    res = capture.export(old.box(), out)
    assert box_digest(old.box()) == before_old            # Runtipi's chmod a+rwx on start does not count
    assert sorted(a for a in res["agents"]) == ["hermes", "research"]
    assert any("live.sock (socket)" in x for x in res["skipped_in_apps"]), res["skipped_in_apps"]
    assert any("escape-link" in x for x in res["skipped_in_apps"])
    c = _contents(out, realbox["key"])
    m = json.loads(c["manifest.json"])
    apps = {a["id"]: a for a in m["apps"]}
    assert apps["notes-app"]["volumes"] == [VOL]
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(c["apps/wizard-agent-service/data.tar.gz"])) as t:
        svc = t.getnames()
    assert not any("wizard-os/agents" in n for n in svc), "the agent travels once, as an agent"
    assert not any(n.endswith("mcp-token") for n in svc), "the service's MCP token is this box's"
    assert "app-data/data/wizard-os/settings.json" in svc
    assert not any(n.startswith(("wizard-os/builder", "wizard-os/bin", "wizard-os/release.json")) for n in c)

    before_new = box_digest(new.box())
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(realbox["keyfile"]))
    assert code == 0, rec
    nd = new.runtipi / "app-data/wizardapps/notes-app/data"
    assert os.readlink(nd / "hub/snapshots/rev1/model.bin") == "../../blobs/abc123"
    assert (nd / "hub/snapshots/rev1/model.bin").read_text() == "model weights"
    assert os.stat(nd / "hub/blobs/hardcopy").st_ino == os.stat(nd / "hub/blobs/abc123").st_ino
    assert (nd / "backups/2026-10-01.db").read_text() == "the app's own backups folder"
    assert not (nd / "escape-link").exists() and not (nd / "live.sock").exists()
    assert (new.base / "volumes" / VOL / "credentials.json").read_text() == "OLD-LOGIN"
    svc = new.runtipi / SVC / "wizard-os"
    assert (svc / "mcp-token").read_text() == "NEW-SERVICE-MCP"           # this box's wiring kept
    assert json.loads((svc / "settings.json").read_text())["owner"] == "OLD-SERVICE-MCP-settings"
    rhome = svc / "agents/research/home"
    jobs = json.loads((rhome / "cron/jobs.json").read_text())["jobs"]
    assert [j["state"] for j in jobs] == ["paused"]                         # the extra agent went through import
    assert (svc / "agents/research/runtime.key").read_text() == "research-runtime-key"
    assert (new.home / "builder/current/bundle.json").read_text() == "NEW"
    assert (new.home / "release.json").read_text() == "NEW" and (new.home / "bin/wam").read_text() == "NEW"

    code, res = _cli(new.box(), "undo")
    assert code == 0 and res["digest_match"], res
    assert box_digest(new.box()) == before_new
    assert (new.base / "volumes" / VOL / "credentials.json").read_text() == "NEW-EMPTY-LOGIN"

    # a second import after undo works the same
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(realbox["keyfile"]))
    assert code == 0, rec
    assert (new.base / "volumes" / VOL / "credentials.json").read_text() == "OLD-LOGIN"


def test_free_space_adds_up_per_filesystem(realbox, tmp_path):
    b = realbox["new"]
    b.free = 300
    box = b.box()
    need_spaces(box, [(tmp_path / "a", 100, "x"), (tmp_path / "b", 100, "y")])
    with pytest.raises(SpaceError) as e:
        need_spaces(box, [(tmp_path / "a", 200, "the backup file"), (tmp_path / "b", 200, "the work folder")])
    assert "the backup file and the work folder" in str(e.value)
