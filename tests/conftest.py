import pytest


@pytest.fixture(autouse=True)
def _isolated_box(tmp_path, monkeypatch):
    """Never resolve an owner home, Runtipi install or Mac admission state in tests."""
    from wam import managed_runtime
    for name, path in {"HOME": tmp_path / "home", "WIZARD_OS_HOME": tmp_path / "home/.wizard-os",
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
