"""Suite-wide guard: no test may touch the real box's host dashboard service.

`alongside --off` calls host_dashboard.disable(), which defaults to ~/.config/systemd/user and runs real
`systemctl --user disable --now`. Run on a live alongside box, the suite deleted wizard-host-dashboard.service and
silently broke the ChatGPT/Tinfoil apps. Default every unit dir to a temp folder and stub systemctl."""
import pytest

from wam import host_dashboard as hd
from wam import mirrors
from wam import network


@pytest.fixture(autouse=True)
def _no_real_host_dashboard(tmp_path, monkeypatch):
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
