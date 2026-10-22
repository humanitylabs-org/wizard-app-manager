"""Interruption coverage for the real import/undo CLI (disposable fake boxes)."""
import errno
import os

import pytest

from backup_fakes import make_source, write_tools
from test_backup_flow import _cli, _export, isolated, world  # noqa: F401
from wam.backup import agents, restore
from wam.backup.box import box_digest


def _entries(box, area):
    if area == "wizard-os":
        return [box.home / n for n in restore._home_entries(box.home)]
    if area == "files":
        return [box.files_root() / n for n in restore._files_entries(box.files_root())]
    if area == "agents-root":
        return [box.agents_root() / n for n in agents.root_files(box.agents_root())]
    out = []
    for a in box.agents():
        out += [a.home / n for n in agents.movable_entries(a.home)]
        if a.folder and (a.folder / "agent.json").exists():
            out.append(a.folder / "agent.json")
    return out


@pytest.mark.parametrize("area", ["wizard-os", "agents", "files", "agents-root"])
@pytest.mark.parametrize("after_rename", [False, True])
def test_every_set_aside_move_is_recoverable(world, monkeypatch, area, after_rename):
    out, _ = _export(world)
    tools = write_tools(world["tmp"] / "fault-tools")
    # Build once to enumerate every actual position, then use a fresh box for each.
    seed = make_source(world["tmp"] / "seed", tools)
    for name in ("é.txt", "日本語.txt", ".hidden"):
        (seed.box().files_root() / name).write_text(name)
    for n in ("grants.json", "choices.json"):
        (seed.box().agents_root() / n).write_text("{}")
    count = len(_entries(seed.box(), area))
    assert count >= 2, (area, count)
    for position in range(count):
        dest = make_source(world["tmp"] / f"case-{position}", tools)
        box = dest.box()
        for name in ("é.txt", "日本語.txt", ".hidden"):
            (box.files_root() / name).write_text(name)
        for n in ("grants.json", "choices.json"):
            (box.agents_root() / n).write_text("{}")
        victim = _entries(box, area)[position]
        before = box_digest(box)
        original = restore._move
        fired = []

        def fault(src, dst):
            if src == victim and "before" in dst.parts:
                root, journal = restore.last_restore(box)
                intent = next(s for s in journal["steps"] if s.get("src") == str(src))
                assert intent["state"] == "pending"  # durable BEFORE the destructive call
                fired.append(str(src))
                if after_rename:
                    original(src, dst)
                raise OSError("injected set-aside interruption")
            return original(src, dst)

        with monkeypatch.context() as patch:
            patch.setattr(restore, "_move", fault)
            code, rep = _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
        assert fired == [str(victim)], (area, position, rep)
        assert code != 0
        code, rep = _cli(box, "undo")
        assert code == 0 and rep["digest_match"], (area, position, after_rename, rep)
        assert box_digest(box) == before


@pytest.mark.parametrize("after_rename", [False, True])
def test_undo_each_move_can_resume(world, monkeypatch, after_rename):
    out, _ = _export(world)
    box = world["new"].box()
    before = box_digest(box)
    assert _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")[0] == 0
    original = restore._move
    # Interrupt every successive undo rename, alternating CLI retries against the
    # on-disk journal; never reuse an in-memory _Restore instance.
    attempts = 0
    failed_pair = None
    while True:
        fired = []

        def fault(src, dst):
            if not fired:
                fired.append((src, dst))
                if after_rename:
                    original(src, dst)
                raise OSError("undo interrupted")
            return original(src, dst)

        # One before-rename failure cannot progress; let that exact move succeed
        # on the next invocation, then interrupt the following one.
        if not after_rename and attempts:
            assert failed_pair is not None
            original(*failed_pair)
        with monkeypatch.context() as patch:
            patch.setattr(restore, "_move", fault)
            code, rep = _cli(box, "undo")
        if fired:
            failed_pair = fired[0]
        attempts += 1
        assert attempts < 100
        if code == 0:
            break
        assert fired, rep
        assert restore.last_restore(box)[1]["status"] != "undone"
    assert attempts > 2 and rep["digest_match"]
    assert box_digest(box) == before


def test_mismatched_undo_keeps_recovery_and_allows_retry(world, monkeypatch):
    out, _ = _export(world)
    box = world["new"].box()
    before = box_digest(box)
    assert _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")[0] == 0
    real_digest = restore.box_digest
    with monkeypatch.context() as patch:
        patch.setattr(restore, "box_digest", lambda *a, **k: {**real_digest(*a, **k), "fault": "mismatch"})
        code, rep = _cli(box, "undo")
    assert code != 0 and not rep["digest_match"]
    root, j = restore.last_restore(box)
    assert j["status"] == "undo-failed"
    assert _cli(box, "status")[1]["last_restore"]["undo_failed"] is True
    assert (root / "undone").is_dir() and (root / "before").is_dir()
    code, rep = _cli(box, "undo")
    assert code == 0 and rep["digest_match"], rep
    assert box_digest(box) == before


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX hard-exit simulation")
@pytest.mark.parametrize("after_rename", [False, True])
def test_hard_exit_during_secrets_move_is_discoverable(world, monkeypatch, after_rename):
    out, _ = _export(world)
    box = world["new"].box()
    before = box_digest(box)
    original = restore._move
    pid = os.fork()
    if pid == 0:
        def hard_exit(src, dst):
            if src == box.home / "secrets" and "before" in dst.parts:
                if after_rename:
                    original(src, dst)
                os._exit(73)  # bypass import's BaseException handler and finally blocks
            return original(src, dst)
        monkeypatch.setattr(restore, "_move", hard_exit)
        _cli(box, "import", str(out), "--key-file", str(world["keyfile"]), "--yes")
        os._exit(74)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    # Uses only the pre-published on-disk pointer/journal, not child state.
    root, j = restore.last_restore(box)
    assert j["status"] == "setting-aside"
    assert any(s.get("state") == "pending" and s.get("src") == str(box.home / "secrets") for s in j["steps"])
    code, rep = _cli(box, "undo")
    assert code == 0 and rep["digest_match"], rep
    assert box_digest(box) == before


def test_cross_filesystem_set_aside_refuses_without_copy(tmp_path, monkeypatch):
    src, dst = tmp_path / "original", tmp_path / "held"
    src.write_text("irreplaceable")

    def cross_device(*args):
        raise OSError(errno.EXDEV, "different device")

    monkeypatch.setattr(restore.os, "rename", cross_device)
    with pytest.raises(restore.RestoreError, match="different filesystems"):
        restore._move(src, dst)
    assert src.read_text() == "irreplaceable" and not dst.exists()
