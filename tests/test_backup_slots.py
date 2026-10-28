"""Split/bundled system-slot moves, through isolated fake boxes and the public CLI."""
import io
import json
import tarfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from backup_fakes import _install_now, _package, _w
from test_backup_flow import _cli, _export, isolated, world  # noqa: F401 (fixtures)
from wam.backup import restore
from wam.backup.appdata import UnsafeMember

SPLIT = {"wizard-ux": ["shell"], "wizard-agent-service": ["agent-service"]}
BUNDLED = {"wizard-shell": []}  # old registry rows need BUNDLE_FILLS


def add_system(fake, providers, marker):
    reg = json.loads((fake.home / "apps.json").read_text())
    for aid, slots in providers.items():
        _package(fake.runtipi, "wizardapps", aid, 1, "1.0")
        _install_now(fake.runtipi, fake.rt, aid + ":wizardapps")
        root = fake.runtipi / "app-data/wizardapps" / aid
        _w(root / "data/state.db", marker + aid)
        _w(root / "app.env", marker + "-env")
        _w(fake.runtipi / "user-config/wizardapps" / aid / "app.env", marker + "-config")
        reg["apps"].append({"id": aid, "urn": aid + ":wizardapps", "provides": slots})
    _w(fake.home / "apps.json", json.dumps(reg))
    # FakeRuntipi's agent install must mount the same destination that Box resolves, just like compose does.
    fake.agent_homes["agent-research:wizardapps"] = fake.box().agents_root() / "research/home"


@pytest.mark.parametrize("source,destination,src,dst", [
    (SPLIT, BUNDLED, "wizard-agent-service", "wizard-shell"),
    (BUNDLED, SPLIT, "wizard-shell", "wizard-agent-service"),
])
def test_move_system_slots_and_undo(world, source, destination, src, dst):
    old, new = world["old"], world["new"]
    add_system(old, source, "old-")
    add_system(new, destination, "new-")
    target = new.runtipi / "app-data/wizardapps" / dst
    _w(target / "data/only-here", "undo must bring this back")
    _w(target / "data/wizard-os/mcp-token", "destination-token")
    _w(old.runtipi / "app-data/wizardapps" / src / "data/wizard-os/mcp-token", "source-token")
    out, _ = _export(world)
    code, rep = _cli(new.box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 0, rep
    p = rep["plan"]
    assert sorted(x["slot"] for x in p["kept_here"]) == ["agent-service", "shell"]
    assert not (set(source) & set(p["install"]))
    assert not (set(source) & set(p["replace"]))
    assert p["warnings"]
    assert (target / "data/state.db").read_text() == "old-" + src
    assert not (target / "data/only-here").exists()
    assert (target / "data/wizard-os/mcp-token").read_text() == "destination-token"
    assert (target / "app.env").read_text() == "new--env"
    assert (new.runtipi / "user-config/wizardapps" / dst / "app.env").read_text() == "new--config"
    assert json.loads((new.runtipi / "apps/wizardapps" / dst / "config.json").read_text())["id"] == dst
    if "wizard-ux" in destination:
        assert (new.runtipi / "app-data/wizardapps/wizard-ux/data/state.db").read_text() == "new-wizard-ux"
    # Mapping replaces data, not the destination's availability: it ran before
    # restore and must be running again once restore returns.
    assert new.rt.apps[dst + ":wizardapps"]["status"] == "running"
    assert not any(c[0] == "install" and c[1].split(":")[0] in source for c in new.rt.calls)
    assert sum(u.split(":")[0] in ("wizard-shell", "wizard-agent-service") for u in new.rt.apps) == 1
    _, journal = restore.last_restore(new.box())
    assert any(s["kind"] == "app_snapshot" and s["urn"] == dst + ":wizardapps" for s in journal["steps"])
    assert any(s["kind"] == "app_data_mapped" and s["source"] == src for s in journal["steps"])
    code, undone = _cli(new.box(), "undo")
    assert code == 0 and undone["digest_match"], undone
    assert (target / "data/state.db").read_text() == "new-" + dst
    assert (target / "data/only-here").read_text() == "undo must bring this back"
    assert restore.last_restore(new.box())[1]["undo_notes"] == undone["notes"]


@pytest.mark.parametrize("source,destination,src,dst", [
    (SPLIT, BUNDLED, "wizard-agent-service", "wizard-shell"),
    (BUNDLED, SPLIT, "wizard-shell", "wizard-agent-service"),
])
def test_provider_agent_homes_are_imported_separately_and_undo_exactly(world, source, destination, src, dst):
    import shutil
    old, new = world["old"], world["new"]
    add_system(old, source, "old-")
    add_system(new, destination, "new-")
    source_root = old.runtipi / "app-data/wizardapps" / src / "data/wizard-os/agents"
    source_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(old.home / "agents", source_root)
    target_root = new.box().agents_root()
    shutil.copytree(source_root, target_root)
    old.agent_homes["agent-research:wizardapps"] = source_root / "research/home"
    new.agent_homes["agent-research:wizardapps"] = target_root / "research/home"
    _install_now(new.runtipi, new.rt, "agent-research:wizardapps")
    _w(source_root / ".main-agent.json", '{"marker": "old"}')
    _w(target_root / ".main-agent.json", '{"marker": "new"}')
    _w(target_root / "research/home/state.db", "destination research")
    assert old.box().agents_root() == source_root
    out, _ = _export(world)
    code, rep = _cli(new.box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 0, rep
    assert (target_root / "research/home/state.db").read_text() == "sessions of research"
    assert json.loads((target_root / ".main-agent.json").read_text())["marker"] == "old"
    code, rep = _cli(new.box(), "undo")
    assert code == 0 and rep["digest_match"], rep
    assert (target_root / "research/home/state.db").read_text() == "destination research"
    assert json.loads((target_root / ".main-agent.json").read_text())["marker"] == "new"


def test_same_providers_still_replace(world):
    old, new = world["old"], world["new"]
    add_system(old, SPLIT, "old-")
    add_system(new, SPLIT, "new-")
    out, _ = _export(world)
    code, rep = _cli(new.box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 0, rep
    assert rep["plan"]["kept_here"] == []
    assert set(SPLIT) <= set(rep["plan"]["replace"])
    for aid in SPLIT:
        assert (new.runtipi / "app-data/wizardapps" / aid / "data/state.db").read_text() == "old-" + aid
    assert _cli(new.box(), "undo")[1]["digest_match"]


def test_plan_fallback_and_partial_overlap(world):
    new = world["new"]
    add_system(new, SPLIT, "new-")
    _w(new.home / "apps.json", '{"apps": []}')
    box = new.box()
    manifest = {"apps": [{"id": "wizard-shell", "urn": "wizard-shell:wizardapps"}]}
    p = restore.plan(box, manifest)
    assert len(p["kept_here"]) == 2 and p["install"] == []
    new.rt.apps.pop("wizard-agent-service:wizardapps")
    p = restore.plan(box, manifest)
    assert p["install"] == ["wizard-shell"]  # not all its slots are kept
    assert p["kept_here"] == [{"slot": "shell", "here": "wizard-ux", "bundle": "wizard-shell"}]


@pytest.mark.parametrize("failure", [None, "missing", "installing"])
def test_undo_missing_installed_app_cleans_only_its_project(world, monkeypatch, failure):
    from wam.backup.box import RuntipiError
    out, _ = _export(world)
    new = world["new"]
    box = new.box()
    if failure:
        install, wait = new.rt.install, box.wait_status
        def failed_install(urn, form=None):
            rep = install(urn, form)
            if urn == "notes-app:wizardapps":
                new.rt.apps.pop(urn)
                if failure == "installing":
                    raise RuntipiError("install lost its Runtipi record")
            return rep
        def failed_wait(urn, *args, **kwargs):
            return "missing" if urn == "notes-app:wizardapps" else wait(urn, *args, **kwargs)
        monkeypatch.setattr(new.rt, "install", failed_install)
        monkeypatch.setattr(box, "wait_status", failed_wait)
    code, rep = _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert (code != 0) if failure else (code == 0), rep
    if failure == "missing":
        assert "status missing" in rep["error"]
    new.rt.apps.pop("notes-app:wizardapps", None)
    state = json.loads(new.docker_state.read_text())
    state.update({"notes-app_wizardapps": {"id": "c-notes", "name": "notes-app_wizardapps-main-1", "orphan": True},
                  "unrelated_wizardapps": {"id": "c-other", "name": "unrelated_wizardapps-main-1", "orphan": True}})
    new.docker_state.write_text(json.dumps(state))
    uninstall = new.rt.uninstall
    def reject_missing(urn, **kw):
        assert urn != "notes-app:wizardapps", "do not uninstall a missing Runtipi record"
        return uninstall(urn, **kw)
    monkeypatch.setattr(new.rt, "uninstall", reject_missing)
    code, rep = _cli(box, "undo")
    assert code == 0 and rep["digest_match"], rep
    notes = restore.last_restore(box)[1]["undo_notes"]
    assert notes == rep["notes"] and any("notes-app" in n and "missing" in n for n in notes)
    calls = [c["argv"] for c in new.tool_calls() if c["tool"] == "docker"]
    remaining = json.loads(new.docker_state.read_text())
    assert "notes-app_wizardapps" not in remaining and "unrelated_wizardapps" in remaining
    assert any(a[:2] == ["rm", "-f"] and a[2:] == ["c-notes"] for a in calls)
    assert not any(a[:2] == ["rm", "-f"] and "c-other" in a for a in calls)


@pytest.mark.parametrize("name,kind,target", [
    ("app-data/data/../../escape", "file", ""),
    ("app-data/data/link", "symlink", "../app.env"),
    ("app-data/data/link", "symlink", "/escape"),
    ("app-data/data", "symlink", "elsewhere"),
    ("app-data/data/link", "hardlink", "app-data/app.env"),
])
def test_data_mapping_rejects_escape(world, name, kind, target):
    new = world["new"]
    add_system(new, BUNDLED, "new-")
    dest = new.runtipi / "app-data/wizardapps/wizard-shell/data/state.db"
    archive = world["tmp"] / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        ti = tarfile.TarInfo("app-data/app.env")
        ti.size = 1
        t.addfile(ti, io.BytesIO(b"x"))
        ti = tarfile.TarInfo(name)
        if kind == "symlink":
            ti.type, ti.linkname = tarfile.SYMTYPE, target
        elif kind == "hardlink":
            ti.type, ti.linkname = tarfile.LNKTYPE, target
        t.addfile(ti)
    with pytest.raises(UnsafeMember):
        new.box().appdata.restore("wizardapps", "wizard-shell", archive, data_only=True)
    assert dest.read_text() == "new-wizard-shell"


@pytest.mark.parametrize("target", ["alias/../app.env", "wizard-os/agents/unknown"])
def test_data_mapping_rejects_symlink_chain_escape(world, target):
    new = world["new"]
    add_system(new, BUNDLED, "new-")
    archive = world["tmp"] / "chain.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        # Forward reference deliberately passes lexical checks, but resolves outside data/ at runtime.
        for name, link in (("escape", target), ("alias", ".")):
            ti = tarfile.TarInfo("app-data/data/" + name)
            ti.type, ti.linkname = tarfile.SYMTYPE, link
            t.addfile(ti)
    with pytest.raises(UnsafeMember):
        new.box().appdata.restore("wizardapps", "wizard-shell", archive, data_only=True)
    assert not any(c["argv"][0] == "run" for c in new.tool_calls())


def test_custom_slot_providers_and_empty_destination(world):
    new = world["new"]
    add_system(new, {"custom-service": ["agent-service"], "custom-shell": ["shell"]}, "new-")
    manifest = {"apps": [{"id": "wizard-shell", "urn": "wizard-shell:wizardapps"}]}
    p = restore.plan(new.box(), manifest)
    assert p["install"] == []
    assert {x["here"] for x in p["kept_here"]} == {"custom-service", "custom-shell"}
    for aid in ("custom-service", "custom-shell"):
        new.rt.apps.pop(aid + ":wizardapps")
    p = restore.plan(new.box(), manifest)
    assert p["kept_here"] == [] and p["install"] == ["wizard-shell"]  # stale registry is not installed


def test_partial_provider_conflict_refused_before_install(world):
    old, new = world["old"], world["new"]
    add_system(old, BUNDLED, "old-")
    add_system(new, {"wizard-agent-service": ["agent-service"]}, "new-")
    out, _ = _export(world)
    code, rep = _cli(new.box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code != 0 and "more than one Agent Service" in rep["error"], rep
    assert not any(c[0] in ("install", "start") for c in new.rt.calls)
    assert (new.runtipi / "app-data/wizardapps/wizard-agent-service/data/state.db").read_text() == "new-wizard-agent-service"


def test_duplicate_provider_added_during_apply_blocks_start(world, monkeypatch):
    old, new = world["old"], world["new"]
    add_system(old, SPLIT, "old-")
    add_system(new, BUNDLED, "new-")
    out, _ = _export(world)
    original = restore._import_agent
    def concurrent_install(*args, **kwargs):
        rep = original(*args, **kwargs)
        new.rt.add("wizard-agent-service:wizardapps", status="stopped")
        return rep
    monkeypatch.setattr(restore, "_import_agent", concurrent_install)
    code, rep = _cli(new.box(), "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code != 0 and "more than one Agent Service" in rep["error"], rep
    assert not any(c[0] == "start" for c in new.rt.calls)


def test_activate_starts_mapped_provider_but_refuses_duplicates(world):
    old, new = world["old"], world["new"]
    add_system(old, SPLIT, "old-")
    add_system(new, BUNDLED, "new-")
    out, _ = _export(world)
    box = new.box()
    code, rep = _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
    assert code == 0, rep
    new.rt.add("wizard-agent-service:wizardapps", status="stopped")
    calls = len(new.rt.calls)
    code, rep = _cli(box, "activate", "--old-is-off")
    assert code != 0 and "more than one Agent Service" in rep["error"]
    assert len(new.rt.calls) == calls
    new.rt.apps.pop("wizard-agent-service:wizardapps")
    code, rep = _cli(box, "activate", "--old-is-off")
    assert code == 0, rep
    assert new.rt.apps["wizard-shell:wizardapps"]["status"] == "running"


def test_data_mapping_keeps_safe_links_and_rejects_destination_symlink(world):
    from wam.backup.appdata import AppDataError
    old, new = world["old"], world["new"]
    add_system(old, SPLIT, "old-")
    add_system(new, BUNDLED, "new-")
    source = old.runtipi / "app-data/wizardapps/wizard-agent-service/data"
    (source / "alias").symlink_to("state.db")
    import os
    os.link(source / "state.db", source / "hard")
    archive = world["tmp"] / "data.tar.gz"
    old.box().appdata.archive("wizardapps", "wizard-agent-service", archive)
    box = new.box()
    box.appdata.restore("wizardapps", "wizard-shell", archive, data_only=True)
    data = new.runtipi / "app-data/wizardapps/wizard-shell/data"
    assert (data / "alias").is_symlink() and (data / "alias").read_text() == "old-wizard-agent-service"
    assert (data / "hard").stat().st_ino == (data / "state.db").stat().st_ino
    held = data.with_name("held")
    data.rename(held)
    data.symlink_to(held, target_is_directory=True)
    with pytest.raises(AppDataError, match="symlinked"):
        box.appdata.restore("wizardapps", "wizard-shell", archive, data_only=True)
    assert data.is_symlink() and (held / "state.db").read_text() == "old-wizard-agent-service"
