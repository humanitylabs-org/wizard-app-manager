"""Agent import rules (adapted from try-wizard wizard-import-hermes): zip checks, per-machine excludes, the active
record read at export."""
import json
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wam.backup import agents  # noqa: E402


def _zip(path: Path, files: dict) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return path


@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape", "memories/../../x", "a\\b"])
def test_unsafe_zip_paths_refused(tmp_path, bad):
    src = _zip(tmp_path / "in.zip", {"config.yaml": "a: 1", bad: "x"})
    with pytest.raises(agents.ImportRefused, match="unsafe"):
        agents.check_and_filter(src, tmp_path / "out.zip")


def test_not_a_hermes_backup_refused(tmp_path):
    src = _zip(tmp_path / "in.zip", {"memories/MEMORY.md": "x"})
    with pytest.raises(agents.ImportRefused, match="config.yaml"):
        agents.check_and_filter(src, tmp_path / "out.zip")
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    with pytest.raises(agents.ImportRefused):
        agents.check_and_filter(tmp_path / "junk.zip", tmp_path / "out.zip")


def test_per_machine_entries_filtered(tmp_path):
    src = _zip(tmp_path / "in.zip", {
        "config.yaml": "a: 1", ".env": "K=v", "state.db": "db", "memories/MEMORY.md": "m", "skills/x/SKILL.md": "s",
        "bin/tool": "b", "logs/a.log": "l", "runtime/sock": "r", "cache/c": "c", "audio_cache/a": "a",
        "plugins/wizard-os/p.py": "w", "plugins/mine/p.py": "mine", "gateway.lock": "", "gateway.pid": "1",
        "state.db-wal": "", "hermes-agent/run_agent.py": "code", "node_modules/x/i.js": "n"})
    rep = agents.check_and_filter(src, tmp_path / "out.zip")
    kept = sorted(zipfile.ZipFile(tmp_path / "out.zip").namelist())
    assert kept == [".env", "config.yaml", "memories/MEMORY.md", "plugins/mine/p.py", "skills/x/SKILL.md", "state.db"]
    assert rep["files"] == 6


def test_movable_entries_keep_machine_parts_in_place(tmp_path):
    h = tmp_path / "home"
    for rel in ("config.yaml", "bin/x", "logs/y", "plugins/wizard-os/p", "plugins/mine/p", "hermes-agent/r.py",
                "memories/M.md", "state.db-shm"):
        (h / rel).parent.mkdir(parents=True, exist_ok=True)
        (h / rel).write_text("x")
    assert agents.movable_entries(h) == ["config.yaml", "memories", "plugins/mine"]


def test_active_record_reads_only_what_was_on():
    cfg = yaml.safe_dump({"platforms": {"telegram": {"enabled": True}, "discord": {"enabled": False},
                                        "api_server": {"enabled": True}, "signal": {"enabled": False}}})
    env = "DISCORD_BOT_TOKEN=x\nSLACK_BOT_TOKEN=y\nSIGNAL_HTTP_URL=\n"
    jobs = json.dumps({"jobs": [{"id": "a", "enabled": True, "state": "scheduled"},
                                {"id": "b", "enabled": False, "state": "paused"},
                                {"id": "c", "enabled": True, "state": "paused", "paused_at": "t"},
                                {"id": "d", "enabled": True, "state": "completed"}]})
    rec = agents.active_record(cfg.encode(), env.encode(), jobs.encode())
    assert rec == {"messaging": ["slack", "telegram"], "cron": ["a"]}
    assert agents.active_record(None, None, None) == {"messaging": [], "cron": []}


def test_env_merge_keeps_box_wizard_keys(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    (h / ".env").write_text("API_SERVER_KEY=imported\nWIZARD_X=imported\nHERMES_DASHBOARD_TOKEN=imp\nOPENAI_API_KEY=keep\n")
    replaced = agents.env_merge(h, ["API_SERVER_KEY=box", "WIZARD_X=box"])
    text = (h / ".env").read_text()
    assert "OPENAI_API_KEY=keep" in text and "API_SERVER_KEY=box" in text and "imported" not in text
    assert "HERMES_DASHBOARD_TOKEN" not in text
    assert replaced == ["API_SERVER_KEY", "HERMES_DASHBOARD_TOKEN", "WIZARD_X"]
    assert (h / ".env").stat().st_mode & 0o777 == 0o600
