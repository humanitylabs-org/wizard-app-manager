"""Mac admission on fake boxes: real flock reservations, no live Docker/Runtipi."""
import fcntl
from contextlib import contextmanager
from pathlib import Path

import pytest

from test_backup_flow import _cli, world  # noqa: F401
from wam import managed_runtime
from wam.backup import capture, cli, fmt, restore, schedule, snapshot
from wam.backup.box import HermesError

IMAGE = "sha256:" + "a" * 64


def _contents(root):
    return {p.relative_to(root).as_posix(): (p.lstat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in root.rglob("*")}


@pytest.fixture
def managed(tmp_path, monkeypatch):
    state = tmp_path / "managed"
    state.mkdir()
    (state / "operations").mkdir()
    (state / "admission.lock").touch()
    cfg = {"image": IMAGE}
    monkeypatch.setattr(managed_runtime, "configuration", lambda: cfg)
    monkeypatch.setattr(managed_runtime, "STATE", str(state))
    actual = managed_runtime.operation
    seen = {"entered": 0, "exited": 0, "checks": 0}

    @contextmanager
    def counted():
        with actual() as reservation:
            seen["entered"] += 1
            try:
                yield reservation
            finally:
                seen["exited"] += 1

    monkeypatch.setattr(managed_runtime, "operation", counted)

    def held():
        paths = list((state / "operations").iterdir())
        assert len(paths) == 1
        with paths[0].open() as competing:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seen["checks"] += 1

    return state, seen, held, cfg


def _guard_runner(box, managed, monkeypatch):
    state, _, held, _ = managed
    box.env["FAKE_IMAGE"] = IMAGE
    runner = box.runner
    real = runner._run
    calls = []

    def checked(argv, *args, **kwargs):
        held()
        if argv[:2] == ["docker", "run"]:
            assert str(state) + ":/wizard-managed-state" in argv
            assert argv[argv.index("--entrypoint") + 2] == IMAGE
            calls.append(argv)
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(runner, "_run", checked)
    return calls


def test_managed_export_import_activate_undo_hold_one_reservation(world, managed, monkeypatch):
    state, seen, held, _ = managed
    old, new = world["old"].box(), world["new"].box()
    calls = _guard_runner(old, managed, monkeypatch)
    target_calls = _guard_runner(new, managed, monkeypatch)
    out = world["tmp"] / "managed.wizardbackup"
    code, res = _cli(old, "export", "--out", str(out))
    assert code == 0 and res["ok"], res
    assert seen["entered"] == seen["exited"] == 1
    assert calls

    # Guard both edges of staging, mutations, regeneration and final receipts,
    # not just the individual Docker launches. All must share the same flock.
    for name in ("stage", "_apply", "_move", "_write_json", "_stop_agent", "_start_agent", "_regenerate"):
        real = getattr(restore, name)

        def checked(*args, _real=real, **kwargs):
            held()
            result = _real(*args, **kwargs)
            held()
            return result

        monkeypatch.setattr(restore, name, checked)

    for count, args in enumerate([
        ("import", str(out), "--yes", "--key-file", str(world["keyfile"])),
        ("activate", "--old-is-off"),
        ("undo",),
    ], start=2):
        code, res = _cli(new, *args)
        assert code == 0 and res["ok"], res
        assert seen["entered"] == seen["exited"] == count
        assert not list((state / "operations").iterdir())
    assert target_calls and seen["checks"] > 20
    assert any("import" in argv for argv in target_calls)
    assert any("python" in argv[argv.index("--entrypoint") + 1] for argv in target_calls)


@pytest.mark.parametrize("marker", ["lease.json", "resume.json"])
@pytest.mark.parametrize("command", ["export", "import", "undo", "activate", "run"])
def test_maintenance_cli_refuses_before_any_change(world, managed, marker, command):
    state, seen, _, _ = managed
    (state / marker).write_text("{}")
    fake = world["new"]
    before = _contents(fake.base)
    rt_calls = list(fake.rt.calls)
    extras = {"export": ["--out", str(fake.base / "new-dir/out.wizardbackup")],
              "import": [str(fake.base / "not-even-readable.wizardbackup"), "--yes"],
              "activate": ["--old-is-off"], "run": ["--scheduled"], "undo": []}
    code, res = _cli(fake.box(), command, *extras[command])
    assert code == 1 and "Wizard maintenance is active" in res["error"]
    assert _contents(fake.base) == before
    assert fake.rt.calls == rt_calls
    assert not fake.tool_calls() and not fake.systemctl_calls
    assert seen["entered"] == seen["exited"] == 0
    assert not list((state / "operations").iterdir())


@pytest.mark.parametrize("entry", ["export", "import", "undo", "activate", "run", "snapshot", "repo-import"])
def test_direct_engine_entries_refuse_before_work(world, managed, entry):
    state, _, _, _ = managed
    (state / "lease.json").write_text("{}")
    fake = world["new"]
    box = fake.box()
    before = _contents(fake.base)
    actions = {
        "export": lambda: capture.export(box, fake.base / "out", passphrase="example"),
        "import": lambda: restore.import_bundle(box, fake.base / "missing", fmt.Keys()),
        "undo": lambda: restore.undo(box),
        "activate": lambda: restore.activate(box, True),
        "run": lambda: schedule.run(box, scheduled=True),
        "snapshot": lambda: snapshot.backup(box, None, "test"),
        "repo-import": lambda: snapshot.import_from_repo(box, None),
    }
    with pytest.raises(HermesError, match="maintenance is active"):
        actions[entry]()
    assert _contents(fake.base) == before
    assert not fake.tool_calls()


def test_schedule_holds_one_reservation_through_capture_and_status(world, managed, monkeypatch):
    state, seen, held, _ = managed
    box = world["old"].box()
    calls = _guard_runner(box, managed, monkeypatch)

    def fake_run(b, cfg, now):
        held()
        out = world["tmp"] / "scheduled.wizardbackup"
        assert capture.export(b, out)["ok"]
        held()
        schedule.write_status(b, {"ok": True})
        return {"ok": True}

    monkeypatch.setattr(schedule, "_run", fake_run)
    code, res = _cli(box, "run", "--scheduled")
    assert code == 0 and res["ok"] and calls
    assert seen["entered"] == seen["exited"] == 1
    assert not list((state / "operations").iterdir())


def test_reservation_released_on_error(world, managed, monkeypatch):
    state, seen, held, _ = managed
    box = world["new"].box()

    def broken(*a, **kw):
        held()
        raise OSError("test staging failure")

    monkeypatch.setattr(restore, "stage", broken)
    with pytest.raises(OSError, match="test staging failure"):
        restore.import_bundle(box, Path("missing"), fmt.Keys())
    assert seen["entered"] == seen["exited"] == 1
    assert not list((state / "operations").iterdir())


def test_managed_launch_requires_reservation_and_manifest_image(world, managed, monkeypatch):
    box = world["old"].box()
    agent = box.agents()[0]
    with pytest.raises(HermesError, match="owned operation reservation"):
        box.runner.python(agent, "print('ok')", "")
    with box.operation():
        monkeypatch.setattr(box.runner, "image", lambda agent: "wrong:image")
        with pytest.raises(HermesError, match="does not match"):
            box.runner.python(agent, "print('ok')", "")
    assert not world["old"].tool_calls()


def test_manifest_changes_fail_closed(world, managed):
    box = world["old"].box()
    agent = box.agents()[0]
    with box.operation():
        managed[3]["image"] = "sha256:" + "b" * 64
        with pytest.raises(HermesError, match="manifest changed"):
            box.runner.python(agent, "print('ok')", "")


def test_no_manifest_keeps_container_argv_identical(world, monkeypatch):
    assert managed_runtime.configuration() is None

    def unexpected():
        pytest.fail("no manifest must not acquire managed admission")

    monkeypatch.setattr(managed_runtime, "operation", unexpected)
    box = world["old"].box()
    agent = box.agents()[0]
    # Both the direct runner and a top-level operation retain the old argv.
    for wrapped in (False, True):
        if wrapped:
            with box.operation():
                box.runner.python(agent, "print('ok')", "")
        else:
            box.runner.python(agent, "print('ok')", "")
    runs = [c["argv"] for c in world["old"].tool_calls() if c["argv"][0] == "run"]
    assert len(runs) == 2 and runs[0] == runs[1]
    assert not any("wizard-managed-state" in arg for arg in runs[0])
    assert runs[0][runs[0].index("--entrypoint") + 2] == "ghcr.io/example/hermes-agent:test"


def test_main_cli_dispatches_backup(monkeypatch):
    from wam import cli as wam_cli
    monkeypatch.setattr(cli, "main", lambda args: 17 if args == ["status"] else 99)
    assert wam_cli.main(["backup", "status"]) == 17
