"""The failed q2 retry must clear only its index service and never claim a failed release is installed."""
import json

import pytest

from wam import updates
from test_install_docker import sandbox
from test_release_bootstrap import manifest
from test_updates import _base, _ok_runner


@pytest.mark.parametrize("docker_access", [True, False])
@pytest.mark.parametrize("leftover", [True, False])
def test_index_recreate_is_idempotent_and_scoped(tmp_path, docker_access, leftover):
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps(manifest()))

    def before(home):
        if leftover:
            (home / "stale-release-index").touch()
        (home / "unrelated-app").write_text("owner data")

    p, home, root, calls, _ = sandbox(tmp_path / "box", docker_access, before,
                                    WIZARD_RELEASE_PINS=str(pins))
    assert p.returncode == 0, p.stdout + p.stderr
    log = calls.read_text()
    prefix = "compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml "
    assert log.index(prefix + "rm --stop --force wizard-release-index") < log.index(
        prefix + "up -d --force-recreate --wait wizard-release-index")
    assert not (home / "stale-release-index").exists()
    assert (home / "unrelated-app").read_text() == "owner data"
    assert "--remove-orphans" not in log and " down" not in log and "--volumes" not in log
    assert "wam -m wam bootstrap" in log


def test_index_cleanup_failure_is_not_ignored(tmp_path):
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps(manifest()))
    p, _, _, calls, _ = sandbox(tmp_path / "box", WIZARD_RELEASE_PINS=str(pins), INDEX_REMOVE_RC="9")
    assert p.returncode == 9
    assert "--wait wizard-release-index" not in calls.read_text()
    assert "wam -m wam bootstrap" not in calls.read_text()


@pytest.mark.parametrize("receipt_status", ["install-failed", "mismatch"])
@pytest.mark.parametrize("pointer", ["0.3.33", "0.3.34", None])
def test_failed_readback_is_not_installed_and_update_retries_the_failed_target(tmp_path, receipt_status, pointer):
    h = tmp_path / "wos"
    h.mkdir()
    (h / "release.json").write_text(json.dumps({"release": "0.3.33", "status": receipt_status}))
    (h / "install-status.json").write_text('{"state":"failed"}')
    base = _base(tmp_path, pointer) if pointer else (tmp_path / "offline").as_uri()
    assert updates.installed(h) is None
    st = updates.status(h, base)
    assert st["installed"] is None and st["failed"] is True
    assert st["update_available"] is True and st["unpinned"] is False
    assert st["latest"] == "0.3.33"
    calls = []
    assert updates.apply(h, base, _ok_runner(calls))["started"] is True
    assert "WIZARD_RELEASE=0.3.33 bash -s start" in calls[0][-1]
    # Read-back, not the install-status label alone, is the successful release authority.
    (h / "install-status.json").write_text('{"state":"done"}')
    assert updates.installed(h) is None
    (h / "release.json").write_text('{"release":"0.3.33","status":"pinned"}')
    assert updates.status(h, base)["failed"] is False
    assert updates.installed(h) == "0.3.33"


@pytest.mark.parametrize("receipt_status", ["pinned", "drift", "unpinned", "unknown", None])
def test_installed_requires_a_successful_release_readback(tmp_path, receipt_status):
    (tmp_path / "release.json").write_text(json.dumps({"release": "0.3.33", "status": receipt_status}))
    assert updates.installed(tmp_path) == ("0.3.33" if receipt_status in ("pinned", "drift") else None)


@pytest.mark.parametrize("stable,gate", [("0.3.33", "busy"), ("0.3.34", "interrupted-target"),
                                         ("0.3.32", "interrupted-target")])
def test_failed_retry_keeps_automatic_stable_and_busy_gates(tmp_path, stable, gate):
    h = tmp_path / "wos"
    h.mkdir()
    (h / "release.json").write_text('{"release":"0.3.33","status":"install-failed"}')
    (h / "install-status.json").write_text('{"state":"failed"}')
    updates.set_mode("auto", h, tmp_path / "units", systemctl=False)
    calls = []
    st = updates.apply_if_auto(h, _base(tmp_path, stable), _ok_runner(calls), is_busy=lambda: True)
    assert st["started"] is False and st["skipped"] == gate and not calls


@pytest.mark.parametrize("promoted,expected", [(True, True), (False, False)])
def test_failed_automatic_retry_needs_promotion(tmp_path, promoted, expected):
    h = tmp_path / "wos"
    h.mkdir()
    (h / "release.json").write_text('{"release":"0.3.33","status":"install-failed"}')
    updates.set_mode("auto", h, tmp_path / "units", systemctl=False)
    calls = []
    st = updates.apply_if_auto(h, _base(tmp_path, "0.3.33", promoted), _ok_runner(calls), is_busy=lambda: False)
    assert st["started"] is expected and bool(calls) is expected


@pytest.mark.parametrize("target,available", [("0.3.34", True), ("0.3.32", False)])
def test_interrupted_retry_after_an_earlier_failed_receipt_uses_current_target(tmp_path, target, available):
    h = tmp_path / "wos"
    h.mkdir()
    (h / "release.json").write_text('{"release":"0.3.33","status":"install-failed"}')
    (h / "install-status.json").write_text('{"state":"interrupted"}')
    (h / "release-pins.json").write_text(json.dumps({"version": target,
        "components": {"wizard-app-manager": {"commit": "a" * 40}}}))
    st = updates.status(h, (tmp_path / "offline").as_uri())
    assert st["update_available"] is available
    if available:
        assert st["latest"] == target


@pytest.mark.parametrize("release", ["bad;command", None, [], ""])
def test_malformed_failure_receipt_does_not_authorize_install(tmp_path, release):
    (tmp_path / "release.json").write_text(json.dumps({"release": release, "status": "install-failed"}))
    calls = []
    st = updates.apply(tmp_path, _base(tmp_path, "0.3.33"), _ok_runner(calls))
    assert st["started"] is False and not calls
