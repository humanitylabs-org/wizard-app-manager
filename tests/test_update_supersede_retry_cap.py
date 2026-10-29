"""PR #185 review: a newer release supersedes a failed/interrupted one, and Automatic retries a failure once."""
import json

import pytest

from wam import updates
from test_updates import _base, _ok_runner, _pin


def _pins(h, version):
    (h / "release-pins.json").write_text(json.dumps({"version": version,
        "components": {"wizard-app-manager": {"commit": "a" * 40}}}))


def _failed_run(h, have="0.3.32", target="0.3.33"):
    _pin(h, have)
    _pins(h, target)
    (h / "install-status.json").write_text('{"state":"failed","exit_code":1}')


def _failed_readback(h, status="mismatch"):
    h.mkdir(exist_ok=True)
    (h / "release.json").write_text(json.dumps({"release": "0.3.33", "status": status}))
    (h / "install-status.json").write_text('{"state":"done"}')
    _pins(h, "0.3.33")


@pytest.mark.parametrize("shape", ["failed-run", "mismatch", "install-failed"])
def test_newer_release_supersedes_a_failed_target_for_update_and_automatic(tmp_path, shape):
    h = tmp_path / "wos"
    _failed_run(h) if shape == "failed-run" else _failed_readback(h, shape)
    base = _base(tmp_path, "0.3.34")
    st = updates.status(h, base)
    assert st["latest"] == "0.3.34" and st["update_available"] is True and st["superseded"] is True
    calls = []
    assert updates.apply(h, base, _ok_runner(calls))["started"] is True
    assert "WIZARD_RELEASE=0.3.34 bash -s start" in calls[-1][-1]
    updates.set_mode("auto", h, tmp_path / "units", systemctl=False)
    res = updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: False)
    assert res["started"] is True and res.get("skipped") is None
    assert "WIZARD_RELEASE=0.3.34 bash -s start" in calls[-1][-1]


def test_failed_target_is_still_retried_when_nothing_newer(tmp_path):
    h = tmp_path / "wos"
    _failed_run(h)
    calls = []
    assert updates.apply(h, _base(tmp_path, "0.3.33"), _ok_runner(calls))["started"] is True
    assert "WIZARD_RELEASE=0.3.33 bash -s start" in calls[0][-1]


def test_failed_target_is_not_moved_backwards_to_an_older_stable(tmp_path):
    h = tmp_path / "wos"
    _failed_run(h, have="0.3.31", target="0.3.33")
    updates.set_mode("auto", h, tmp_path / "units", systemctl=False)
    calls = []
    res = updates.apply_if_auto(h, _base(tmp_path, "0.3.32"), _ok_runner(calls), is_busy=lambda: False)
    assert res["started"] is False and res["skipped"] == "interrupted-target" and not calls


@pytest.mark.parametrize("status", ["mismatch", "install-failed"])
def test_automatic_retries_a_lasting_failed_readback_only_once(tmp_path, status):
    h = tmp_path / "wos"
    _failed_readback(h, status)
    updates.set_mode("auto", h, tmp_path / "units", systemctl=False)
    base = _base(tmp_path, "0.3.33")
    calls = []
    results = [updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: False) for _ in range(3)]
    assert len(calls) == 1                                   # not a reinstall every night
    assert results[0]["started"] is True
    assert [r.get("skipped") for r in results[1:]] == ["retry-cap", "retry-cap"]
    # The manual Update button stays usable and re-arms Automatic's single retry.
    assert updates.apply(h, base, _ok_runner(calls))["started"] is True
    assert len(calls) == 2
    assert updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: False)["started"] is True
    assert updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: False)["skipped"] == "retry-cap"
    assert len(calls) == 3
    # A newer stable release is not capped.
    assert updates.apply_if_auto(h, _base(tmp_path, "0.3.34"), _ok_runner(calls), is_busy=lambda: False)["started"]
    assert "WIZARD_RELEASE=0.3.34" in calls[-1][-1]


def test_retry_cap_resets_after_the_release_is_repaired(tmp_path):
    h = tmp_path / "wos"
    _failed_readback(h)
    updates.set_mode("auto", h, tmp_path / "units", systemctl=False)
    base = _base(tmp_path, "0.3.33")
    calls = []
    assert updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: False)["started"] is True
    _pin(h, "0.3.33")                                         # the retry succeeded
    assert updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: False)["started"] is False
    assert not (h / updates.AUTO_RETRY).exists()
