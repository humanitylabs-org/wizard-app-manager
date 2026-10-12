import pytest


@pytest.fixture(autouse=True)
def _no_host_tailnet(monkeypatch):
    """Owner identity comes only from what a test sets, never from the test host's own Tailscale login."""
    from wam import alongside
    monkeypatch.delenv("WIZARD_APP_OWNER_LOGINS", raising=False)
    monkeypatch.delenv("WIZARD_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(alongside, "_tailscale_status", lambda: {})
