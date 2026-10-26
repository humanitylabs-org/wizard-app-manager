"""Suite-wide guard: no test may touch the real box's host dashboard service.

`alongside --off` calls host_dashboard.disable(), which defaults to ~/.config/systemd/user and runs real
`systemctl --user disable --now`. Run on a live alongside box, the suite deleted wizard-host-dashboard.service and
silently broke the ChatGPT/Tinfoil apps. Default every unit dir to a temp folder and stub systemctl."""
import pytest

from wam import host_dashboard as hd
from wam import mirrors


@pytest.fixture(autouse=True)
def _no_real_host_dashboard(tmp_path, monkeypatch):
    # Unit installs must not query live registries. Mirror tests inject their own
    # probe and real-git fixture, rather than changing a live Docker daemon.
    monkeypatch.setattr(mirrors, "image_available", lambda image: True)
    units = tmp_path / "_systemd_user"
    real_disable, real_ensure = hd.disable, hd.ensure
    # Never read the real box's containers: a test that needs running-container state injects it.
    monkeypatch.setattr(hd, "running_env", lambda app_id, store: [])
    monkeypatch.setattr(hd, "_systemctl", lambda *a: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(hd, "disable", lambda unit_dir=None: real_disable(unit_dir or units))
    monkeypatch.setattr(hd, "ensure", lambda *a, unit_dir=None, **k: real_ensure(*a, unit_dir=unit_dir or units, **k))
