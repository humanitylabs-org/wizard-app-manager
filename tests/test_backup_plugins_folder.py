import shutil

from wam.backup import restore
from test_backup_flow import _cli, _export, isolated, world  # noqa: F401 (fixtures)


def test_undo_is_exact_when_the_destination_agent_had_no_plugins_folder(world):
    """A fresh agent may have no plugins/ folder yet. The import brings the owner's own plugins, and the running agent
    then writes Wizard's per-machine plugin (plugins/wizard-os) there. Undo takes the owner's plugins away again; the
    plugins/ folder that now only holds per-machine entries is part of the machine, not of what Undo promises
    (a real Mac -> Linux move: Undo reported plugins/ as extra)."""
    new = world["new"]
    box = new.box()
    homes = [a.home for a in box.agents()]
    for h in homes:
        shutil.rmtree(h / "plugins", ignore_errors=True)
    out, _ = _export(world)
    code, rep = _cli(new.box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 0, rep
    for h in homes:
        (h / "plugins" / "wizard-os").mkdir(parents=True, exist_ok=True)
        (h / "plugins" / "wizard-os" / "plugin.py").write_text("this machine's Wizard plugin")
    code, undone = _cli(new.box(), "undo")
    assert code == 0 and undone["digest_match"], undone
    for h in homes:
        assert (h / "plugins" / "wizard-os" / "plugin.py").read_text() == "this machine's Wizard plugin"
        assert not (h / "plugins" / "own-plugin").exists()


def test_undo_still_sees_an_owner_plugin_left_behind(world):
    """Hiding the bare plugins/ folder never hides what is inside it."""
    from wam.backup import box as boxlib
    new = world["new"]
    h = new.box().agents()[0].home
    before = boxlib.box_digest(new.box())
    (h / "plugins" / "left-behind").mkdir(parents=True)
    (h / "plugins" / "left-behind" / "x.py").write_text("x")
    assert boxlib.box_digest(new.box()) != before
