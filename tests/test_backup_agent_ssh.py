import os

from wam.backup import restore
from test_backup_flow import _cli, _contents, _export, isolated, world  # noqa: F401 (fixtures)


def test_agent_ssh_key_stays_with_its_machine(world):
    """$WIZARD_OS_HOME/agent-ssh is the agent's key for a terminal on THIS box (Wizard for Mac). It never travels
    and a restore never moves it: Docker re-creates a missing bind-mount source root-owned, which Undo then could
    not move back (a real 0.4.0 Mac: PermissionError on agent-ssh)."""
    old, new = world["old"], world["new"]
    for box_, label in ((old, "old"), (new, "new")):
        d = box_.home / "agent-ssh"
        d.mkdir(mode=0o700)
        (d / "id_ed25519").write_text(label + "-key")
    out, _ = _export(world)
    assert not any(n.startswith("wizard-os/agent-ssh") for n in _contents(out, world["key"]))
    keep = new.home / "agent-ssh"
    ino = keep.stat().st_ino
    keep.chmod(0o500)          # like a root-owned folder: it cannot be renamed into another folder
    try:
        code, rep = _cli(new.box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
        assert code == 0, rep
        assert keep.stat().st_ino == ino and (keep / "id_ed25519").read_text() == "new-key"
        code, undone = _cli(new.box(), "undo")
        assert code == 0 and undone["digest_match"], undone
        assert keep.stat().st_ino == ino and (keep / "id_ed25519").read_text() == "new-key"
        assert "undo_error" not in restore.last_restore(new.box())[1]
    finally:
        keep.chmod(0o700)
