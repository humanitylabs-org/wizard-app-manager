"""Suite-wide guard: no test may touch the real box's host dashboard service.

`alongside --off` calls host_dashboard.disable(), which defaults to ~/.config/systemd/user and runs real
`systemctl --user disable --now`. Run on a live alongside box, the suite deleted wizard-host-dashboard.service and
silently broke the ChatGPT/Tinfoil apps. Default every unit dir to a temp folder and stub systemctl."""
import pytest

from wam import feature_switches
from wam import gateway_keys
from wam import host_dashboard as hd
from wam import mirrors
from wam import network
from wam import session_mounts
from wam import store_install


@pytest.fixture(autouse=True)
def _no_real_host_dashboard(request, tmp_path, monkeypatch):
    # Never write the real box's Wizard home or Runtipi folder: a test that builds Config() without its own
    # WIZARD_OS_HOME gets a throwaway one (app networks write app-manager.json and a lock there), and the App
    # Manager's network policy is only ever written under the test's temp folders.
    import os
    from pathlib import Path
    if "WIZARD_OS_HOME" not in os.environ:
        monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "_wizard_os_home"))
    base, real_policy = Path(tmp_path).parents[1], network.policy_path
    monkeypatch.setattr(network, "policy_path", lambda root: real_policy(root) if base in Path(root).resolve().parents
                        else tmp_path / "_runtipi_root" / "user-config" / network.POLICY_FILE)
    # Unit installs must not query live registries. Mirror tests inject their own
    # probe and real-git fixture, rather than changing a live Docker daemon.
    monkeypatch.setattr(mirrors, "image_available", lambda image: True)
    # Feature switches (#726): never probe a real Agent Service on the box's default port or read its process
    # environment; tests that need either set their own.
    monkeypatch.setattr(feature_switches, "_looks_like_service", lambda origin, timeout=2: False)
    monkeypatch.setattr(feature_switches, "_listener_proof_file", lambda port: None)
    monkeypatch.delenv(feature_switches.SERVICE_ORIGIN_ENV, raising=False)
    monkeypatch.delenv(feature_switches.OWNER_PROOF_ENV, raising=False)
    # Signed releases (#565, always on): never fetch a real release signature or the real installer; a test's own
    # file:// mirror still works. No cached signature result crosses tests.
    from wam import release_sig
    real_get = release_sig._get
    monkeypatch.setattr(release_sig, "_get", lambda url, limit, timeout: None if url.startswith(("http:", "https:"))
                        else real_get(url, limit, timeout))
    release_sig._cache.clear()
    if request.module.__name__.rsplit(".", 1)[-1] != "test_release_signatures":
        monkeypatch.setattr(release_sig, "download", lambda url, *a, **k: b"#!/bin/sh\necho installer\n")
        # Update-flow tests check WHICH installer command runs (release, env, retries); the hashed-file handover is
        # tested in tests/test_release_signatures.py, so elsewhere it hands over the same command as an unhashed run.
        from wam import updates as _updates
        monkeypatch.setattr(release_sig, "checked_installer", lambda home, rec, env, url=None:
                            (_updates.installer_script(env), release_sig.write(home, {**rec, "installer": "unchecked"})))
    # The owner listener asks Tailscale for this node's own addresses; tests are a box at 100.64.0.1.
    monkeypatch.setattr(store_install, "own_ips", lambda: {"100.64.0.1", "fd7a:115c:a1e0::1"}, raising=False)
    # App networks: never create, connect or remove a real Docker network from a unit test (tests that need
    # Docker state inject a fake through network.docker_cmd/_run/observe).
    def _no_docker():
        raise network.NetworkError("Docker is disabled in unit tests")
    monkeypatch.setattr(network, "docker_cmd", _no_docker)
    units = tmp_path / "_systemd_user"
    real_disable, real_ensure = hd.disable, hd.ensure
    # Never read the real box's containers: a test that needs running-container state injects it.
    monkeypatch.setattr(hd, "running_env", lambda app_id, store: [])
    monkeypatch.setattr(hd, "_systemctl", lambda *a: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(hd, "disable", lambda unit_dir=None: real_disable(unit_dir or units))
    monkeypatch.setattr(hd, "ensure", lambda *a, unit_dir=None, **k: real_ensure(*a, unit_dir=unit_dir or units, **k))
    # Same for the --share-sessions remount timer (#577): temp unit folder, no real systemctl.
    real_timer = session_mounts.ensure_timer
    monkeypatch.setattr(session_mounts, "_systemctl", lambda *a: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(session_mounts, "ensure_timer",
                        lambda home, root, on, unit_dir=None: real_timer(home, root, on, unit_dir or units))
    # Gateway keys (#387) run after every registry write: never read or write the real runtipi container's files.
    class _NoRuntipiFiles:
        def __init__(self, root):
            pass

        def _no(self, *a, **k):
            raise RuntimeError("the runtipi container is disabled in unit tests")
        read = stat = write = _no
    monkeypatch.setattr(gateway_keys, "RuntipiFiles", _NoRuntipiFiles)


@pytest.fixture(autouse=True)
def _no_restart_gate(request, monkeypatch):
    """An Agent Service restart or update waits at the service's restart gate on every box (#642). Tests about
    something else take the plain path, so they never poll a loopback port or wait for /wizard/health;
    tests/test_graceful_restart.py tests the gate and its wiring itself."""
    if request.module.__name__.endswith(("test_graceful_restart", "test_browser_boundary_new_box")):
        return
    from wam import graceful_restart
    monkeypatch.setattr(graceful_restart, "applies", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _no_switch_feedback_sends(request, monkeypatch):
    """The hourly tick's Try new features counts (wam/switch_feedback.py, #701) never read the real box or send in
    tests; tests/test_switch_feedback.py tests the module itself."""
    if request.module.__name__.endswith("test_switch_feedback"):
        return
    from wam import switch_feedback
    monkeypatch.setattr(switch_feedback, "tick", lambda *a, **k: {"sent": False})


@pytest.fixture(autouse=True)
def _no_real_human_handoff(request, monkeypatch):
    """Human Handoff (wizard-os#775) never installs anything or reads the box's switch in tests;
    tests/test_human_handoff.py tests the module itself."""
    if request.module.__name__.endswith("test_human_handoff"):
        return
    from wam import human_handoff
    monkeypatch.setattr(human_handoff, "tick", lambda *a, **k: {"state": "off"})
    monkeypatch.setattr(human_handoff, "spawn", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_real_private_first_download(monkeypatch):
    """`wam private-first on` starts a detached `wam prefetch --private-first` (WAM#836); a test never does.
    tests/test_recommended.py keeps its own reference to the real function to test it with a fake Popen."""
    from wam import recommended
    monkeypatch.setattr(recommended, "spawn_private_first", lambda c, popen=None: None)


@pytest.fixture(autouse=True)
def _nothing_running(request, monkeypatch):
    """Update decisions see an idle box unless a test says otherwise: the running-work inventory (wam/work.py)
    would otherwise read this machine's own Hermes (~/.hermes), which is busy while a developer's Hermes runs.
    test_work.py tests the inventory itself, against its own fixture homes."""
    if request.module.__name__.endswith("test_work"):
        return
    from wam import work
    monkeypatch.setattr(work, "inventory", lambda home=None, now=None, max_age=10: [])


def pytest_collection_modifyitems(config, items):
    """CI shards (#854): WAM_TEST_SHARD=<i>/<n> keeps only the tests whose id hashes to shard i, so the test
    workflow runs the suite as n parallel jobs. Unset (a local run), every test runs."""
    import os
    import zlib
    shard = os.environ.get("WAM_TEST_SHARD", "").strip()
    if not shard:
        return
    i, n = (int(x) for x in shard.split("/"))
    if not 1 <= i <= n:
        raise pytest.UsageError(f"WAM_TEST_SHARD={shard}: want <i>/<n> with 1 <= i <= n")
    keep, drop = [], []
    for item in items:
        (keep if zlib.crc32(item.nodeid.encode()) % n == i - 1 else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
