"""Alongside mode: the main agent is the host's own Hermes (stock `hermes backup` / `hermes import --force` with
HERMES_HOME, gateway stopped and started around the import)."""
import contextlib
import io
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backup_fakes import FakeBox, _install_now, _package, _w, hermes_home, write_tools  # noqa: E402
from wam.backup import crypto, keys as keylib  # noqa: E402
from wam.backup import cli as bcli  # noqa: E402
from wam.backup.box import box_digest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k, v in {"HOME": tmp_path / "proc-home", "WIZARD_OS_HOME": tmp_path / "proc-home/.wizard-os",
                 "RUNTIPI_ROOT": tmp_path / "proc-home/runtipi", "HERMES_HOME": tmp_path / "proc-home/.hermes",
                 "TMPDIR": tmp_path}.items():
        monkeypatch.setenv(k, str(v))


def _alongside_box(tmp_path, tools, name, marker, messaging, jobs):
    b = FakeBox(tmp_path, tools, name)
    hh = b.homedir / ".hermes"
    _w(b.home / "alongside.json", json.dumps({"mode": "alongside", "agent": {"external": True, "id": "host-hermes",
                                                                            "hermes_home": str(hh)}}))
    _w(b.home / "settings.json", json.dumps({"name": marker}))
    _w(b.home / "api-token", f"{marker}-token\n", 0o600)
    hermes_home(hh, wiring=True, messaging=messaging, marker=marker, jobs=jobs)
    _w(hh / "hermes-agent" / "run_agent.py", f"code checkout of {marker}")
    _package(b.runtipi, "wizardapps", "notes-app", 2, "1.0")
    _install_now(b.runtipi, b.rt, "notes-app:wizardapps")
    _w(b.runtipi / "app-data/wizardapps/notes-app/data/db", f"notes of {marker}")
    _w(b.runtipi / "media" / f"{marker}.txt", marker)
    return b


def _cli(bx, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = bcli.main([*argv, "--json"], box=bx)
    return code, json.loads(buf.getvalue().strip().splitlines()[-1])


def test_alongside_host_agent_round_trip(tmp_path):
    tools = write_tools(tmp_path / "tools")
    old = _alongside_box(tmp_path, tools, "old", "oldhost", True,
                         [{"id": "j1", "enabled": True, "state": "scheduled"}])
    new = _alongside_box(tmp_path, tools, "new", "newhost", False, [])
    key = crypto.new_recovery_key()
    keylib.store_key(old.home, key)
    kf = tmp_path / "k"
    kf.write_text(crypto.encode_recovery_key(key))
    out = tmp_path / "a.wizardbackup"
    code, res = _cli(old.box(), "export", "-o", str(out))
    assert code == 0 and res["agents"] == ["hermes"], res
    hermes_calls = [c["argv"] for c in old.tool_calls() if c["tool"] == "hermes"]
    assert hermes_calls and hermes_calls[0][0] == "backup"
    # the host agent goes through the host `hermes`, never a container (docker only archives app data)
    assert not any(c["tool"] == "docker" and "/opt/hermes/.venv/bin/hermes" in c["argv"] for c in old.tool_calls())

    before = box_digest(new.box())
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(kf))
    assert code == 0, rec
    hh = new.homedir / ".hermes"
    cfg = yaml.safe_load((hh / "config.yaml").read_text())
    assert cfg["marker"] == "oldhost"
    assert cfg["platforms"]["telegram"]["enabled"] is False
    assert json.loads((hh / "cron/jobs.json").read_text())["jobs"][0]["state"] == "paused"
    assert (hh / "hermes-agent/run_agent.py").read_text() == "code checkout of newhost"   # the box's own install
    assert "API_SERVER_KEY=newhost-api-key" in (hh / ".env").read_text()
    assert (new.home / "alongside.json").is_file() and (new.home / "api-token").read_text() == "newhost-token\n"
    gw = [c["argv"] for c in new.tool_calls() if c["tool"] == "hermes" and c["argv"][0] == "gateway"]
    assert ["gateway", "stop"] in gw and ["gateway", "start"] in gw
    imp = [c["argv"] for c in new.tool_calls() if c["tool"] == "hermes" and c["argv"][0] == "import"]
    assert imp and imp[0][1] == "--force"
    assert not Path(imp[0][-1]).exists()                                   # archive deleted after import

    code, res = _cli(new.box(), "undo")
    assert code == 0 and res["digest_match"], res
    assert box_digest(new.box()) == before
    assert yaml.safe_load((hh / "config.yaml").read_text())["marker"] == "newhost"
