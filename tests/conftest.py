import pytest


@pytest.fixture(autouse=True)
def _isolated_box(tmp_path, monkeypatch):
    """Never resolve an owner home, Runtipi install or Mac admission state in tests."""
    from wam import managed_runtime
    for name, path in {"HOME": tmp_path / "home", "WIZARD_OS_HOME": tmp_path / ".wizard-os",
                       "RUNTIPI_ROOT": tmp_path / "runtipi", "HERMES_HOME": tmp_path / "home/.hermes",
                       "TMPDIR": tmp_path}.items():
        monkeypatch.setenv(name, str(path))
    monkeypatch.setattr(managed_runtime, "MANIFEST", tmp_path / "managed-runtime.json")
    monkeypatch.setattr(managed_runtime, "STATE", str(tmp_path / "managed"))


@pytest.fixture(autouse=True)
def _no_host_tailnet(monkeypatch):
    """Owner identity comes only from what a test sets, never from the test host's own Tailscale login."""
    from wam import alongside
    monkeypatch.delenv("WIZARD_APP_OWNER_LOGINS", raising=False)
    monkeypatch.delenv("WIZARD_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(alongside, "_tailscale_status", lambda: {})
"""Suite-wide guard: no test may touch the real box's host dashboard service.

`alongside --off` calls host_dashboard.disable(), which defaults to ~/.config/systemd/user and runs real
`systemctl --user disable --now`. Run on a live alongside box, the suite deleted wizard-host-dashboard.service and
silently broke the ChatGPT/Tinfoil apps. Default every unit dir to a temp folder and stub systemctl."""

from wam import host_dashboard as hd
from wam import mirrors


@pytest.fixture(autouse=True)
def _no_real_host_dashboard(tmp_path, monkeypatch):
    # Unit installs must not query live registries. Mirror tests inject their own
    # probe and real-git fixture, rather than changing a live Docker daemon.
    monkeypatch.setattr(mirrors, "image_available", lambda image: True)
    units = tmp_path / "_systemd_user"
    real_disable, real_ensure = hd.disable, hd.ensure
    monkeypatch.setattr(hd, "_systemctl", lambda *a: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(hd, "disable", lambda unit_dir=None: real_disable(unit_dir or units))
    monkeypatch.setattr(hd, "ensure", lambda *a, unit_dir=None, **k: real_ensure(*a, unit_dir=unit_dir or units, **k))
