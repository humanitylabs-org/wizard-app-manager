"""wizard-os#90: real 0.3.32 cut shape (missing receipt, interrupted status, target pins)."""
import json
import subprocess

import pytest

from wam import updates
from test_updates import _base, _boot, _dead_pid, _ok_runner, _pin, _running
from test_release_pins import box, git


def _target(h, version="0.3.33"):
    (h / "release-pins.json").write_text(json.dumps({"version": version,
        "components": {"wizard-app-manager": {"commit": "a" * 40}}}))


def _cut(h, monkeypatch, tmp_path, have=None):
    if have:
        _pin(h, have)
    _running(h, pid=_dead_pid(), boot_id="before")
    _target(h)
    monkeypatch.setattr(updates, "BOOT_ID", _boot(tmp_path, "after"))


@pytest.mark.parametrize("have", [None, "0.3.32", "0.3.33"])
@pytest.mark.parametrize("pointer", ["0.3.33", "0.3.34", None])
def test_update_button_retries_exact_interrupted_target_even_without_receipt_or_pointer(tmp_path, monkeypatch, have, pointer):
    h = tmp_path / "wos"
    _cut(h, monkeypatch, tmp_path, have)
    base = _base(tmp_path, pointer) if pointer else (tmp_path / "offline").as_uri()
    st = updates.status(h, base)
    assert st["interrupted"] is True and st["installing"] is False
    assert st["update_available"] is True and st["unpinned"] is False
    assert st["installed"] == have and st["latest"] == "0.3.33"
    calls = []
    res = updates.apply(h, base, _ok_runner(calls))
    assert res["started"] is True
    assert "WIZARD_RELEASE=0.3.33 bash -s start" in calls[0][-1]
    assert json.loads((h / "install-status.json").read_text())["state"] == "interrupted"
    # Successful completion clears retry, so the next ordinary check follows the channel again.
    _pin(h, "0.3.33")
    (h / "install-status.json").write_text('{"state":"done"}')
    st = updates.status(h, base)
    assert st["interrupted"] is False
    assert st["update_available"] is (pointer == "0.3.34")


@pytest.mark.parametrize("pins", [None, {"version": "bad;command"}, {"version": "0.3.33"},
                                    {"version": "0.3.33", "components": []}])
def test_missing_or_invalid_target_never_converts_an_unpinned_box(tmp_path, pins):
    h = tmp_path / "wos"; h.mkdir()
    (h / "install-status.json").write_text('{"state":"interrupted"}')
    if pins is not None:
        (h / "release-pins.json").write_text(json.dumps(pins))
    calls = []
    st = updates.apply(h, _base(tmp_path, "0.3.33"), _ok_runner(calls))
    assert st["interrupted"] is True  # health never depends on a newer release
    assert st["unpinned"] is True and st["started"] is False and calls == []


def test_explicit_developer_unpinned_receipt_wins_over_stale_target(tmp_path, monkeypatch):
    h = tmp_path / "wos"
    _cut(h, monkeypatch, tmp_path)
    (h / "release.json").write_text('{"status":"unpinned","reason":"WAM_REF=main"}')
    calls = []
    st = updates.apply(h, _base(tmp_path, "0.3.33"), _ok_runner(calls))
    assert st["unpinned"] is True and st["started"] is False and not calls


@pytest.mark.parametrize("stable,expected", [("0.3.33", True), ("0.3.34", False), ("0.3.32", False)])
def test_automatic_retries_only_when_interrupted_target_is_stable(tmp_path, monkeypatch, stable, expected):
    h = tmp_path / "wos"
    _cut(h, monkeypatch, tmp_path)
    updates.set_mode("auto", h, tmp_path / "units", systemctl=False)
    calls = []
    res = updates.apply_if_auto(h, _base(tmp_path, stable), _ok_runner(calls), is_busy=lambda: False)
    assert res["started"] is expected
    assert bool(calls) is expected


def test_held_runner_lock_is_not_bypassed_to_recover_missing_receipt(tmp_path, monkeypatch):
    import fcntl
    h = tmp_path / "wos"
    _cut(h, monkeypatch, tmp_path)
    with (h / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls = []
        st = updates.apply(h, _base(tmp_path, "0.3.33"), _ok_runner(calls))
        assert st["installing"] is True and st["started"] is False and not calls


def test_installer_power_cut_keeps_previous_receipt_until_completed_readback(box):
    h = box["home"] / ".wizard-os"; h.mkdir()
    old = '{"release":"9.9.8","status":"pinned","mode":"fresh"}\n'
    (h / "release.json").write_text(old)
    script = box["src"] / "install.sh"
    script.write_text('#!/bin/bash\nkill -KILL "$PPID"\nexit 0\n')
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "power cut", cwd=box["src"])
    release_path = box["rel"] / "9.9.9.json"
    rel = json.loads(release_path.read_text())
    rel["components"]["wizard-app-manager"]["commit"] = git("rev-parse", "HEAD", cwd=box["src"])
    release_path.write_text(json.dumps(rel))
    p, rec = box["run"]()
    assert p.returncode == -9
    assert (h / "release.json").read_text() == old
    assert rec["release"] == "9.9.8"
    assert json.loads((h / "release-pins.json").read_text())["version"] == "9.9.9"


def test_successful_installer_replaces_previous_receipt(box):
    h = box["home"] / ".wizard-os"; h.mkdir()
    (h / "release.json").write_text('{"release":"9.9.8","status":"pinned"}')
    p, rec = box["run"]()
    assert p.returncode == 0, p.stdout + p.stderr
    assert rec["release"] == "9.9.9" and rec["status"] == "pinned"
