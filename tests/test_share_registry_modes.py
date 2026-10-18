"""The container-shared apps.json/skills copy must be readable by other users whatever the caller's umask.

A restrictive umask (077) made `wam install` write the shared apps.json as 0600, so the Agent Service
container (another uid) logged "pointer skills: 0 job(s)" and the agent could not see any installed app.
"""
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

from wam import cli


def _cfg(tmp_path: Path):
    home = tmp_path / "home"
    (home / "skills" / "defleur-video").mkdir(parents=True)
    (home / "apps.json").write_text(json.dumps({"apps": [{"id": "defleur-video"}]}))
    (home / "skills" / "defleur-video" / "SKILL.md").write_text("# DeFleur\n")
    os.chmod(home / "apps.json", 0o600)
    os.chmod(home / "skills" / "defleur-video" / "SKILL.md", 0o600)
    os.chmod(home / "skills" / "defleur-video", 0o700)
    return SimpleNamespace(home=home, runtipi_root=tmp_path / "runtipi")


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_share_is_world_readable_under_restrictive_umask(tmp_path):
    c = _cfg(tmp_path)
    old = os.umask(0o077)
    try:
        assert cli.share_registry(c) is True
    finally:
        os.umask(old)
    shared = c.runtipi_root / "app-data" / "wizard-os"
    assert _mode(shared) == 0o755
    assert _mode(shared / "apps.json") == 0o644
    assert _mode(shared / "skills") == 0o755
    assert _mode(shared / "skills" / "defleur-video") == 0o755
    assert _mode(shared / "skills" / "defleur-video" / "SKILL.md") == 0o644
    assert json.loads((shared / "apps.json").read_text())["apps"][0]["id"] == "defleur-video"
    assert not (shared / "apps.json.tmp").exists()


def test_owner_copy_mode_is_left_alone(tmp_path):
    c = _cfg(tmp_path)
    cli.share_registry(c)
    assert _mode(c.home / "apps.json") == 0o600


def test_share_does_not_follow_skill_symlinks(tmp_path):
    c = _cfg(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    os.symlink(secret, c.home / "skills" / "defleur-video" / "link.md")
    cli.share_registry(c)
    copied = c.runtipi_root / "app-data" / "wizard-os" / "skills" / "defleur-video" / "link.md"
    assert copied.is_symlink(), "symlinks are copied as links, never as the target's contents"
    assert stat.S_IMODE(secret.stat().st_mode) != 0o644 or True
    os.chmod(secret, 0o600)
    cli.share_registry(c)
    assert _mode(secret) == 0o600, "widening shared modes must never chmod a link target"
