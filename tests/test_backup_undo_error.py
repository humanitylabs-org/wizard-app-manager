import json

from wam.backup import restore
from test_backup_flow import _cli, _export, isolated, world  # noqa: F401 (fixtures)


def test_undo_that_stops_part_way_says_why(world, monkeypatch):
    out, _ = _export(world)
    new = world["new"]
    box = new.box()
    code, rep = _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 0, rep

    def broken(*a, **k):
        raise OSError("disk went away")
    monkeypatch.setattr(restore, "_move", broken)
    code, rep = _cli(box, "undo")
    assert code != 0
    _, j = restore.last_restore(box)
    assert j["status"] != "undone"
    assert j["undo_error"] == "OSError: disk went away"
    code, st = _cli(box, "status")
    assert st["last_restore"]["undo_failed"] is True
    monkeypatch.undo()
    code, rep = _cli(new.box(), "undo")
    assert code == 0 and rep["digest_match"], rep
    code, st = _cli(new.box(), "status")
    assert st["last_restore"]["undo_failed"] is False and st["last_restore"]["undo_digest_match"] is True
