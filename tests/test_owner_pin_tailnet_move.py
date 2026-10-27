"""A box moved to another tailnet must not keep the previous owner's login pinned in the shell override."""
from wam import alongside


def _box(tmp_path, monkeypatch):
    hh = tmp_path / "hermes"; hh.mkdir(); (hh / "cache" / "images").mkdir(parents=True)
    (hh / "config.yaml").write_text("platforms:\n  api_server:\n    extra:\n      port: 8650\n")
    wh = tmp_path / "wos"; wh.mkdir()
    monkeypatch.setattr(alongside, "hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(alongside, "api_up", lambda origin, timeout=3.0: False)
    for var in ("WIZARD_APP_OWNER_LOGINS", "WIZARD_ALLOWED_HOSTS"):
        monkeypatch.delenv(var, raising=False)
    return wh, hh


def test_rerun_while_logged_out_drops_the_old_owner(tmp_path, monkeypatch):
    wh, hh = _box(tmp_path, monkeypatch)
    monkeypatch.setattr(alongside, "_tailscale_status", lambda: {
        "Self": {"UserID": 1, "DNSName": "box.old-tailnet.ts.net."}, "User": {"1": {"LoginName": "old@example"}}})
    st = alongside.enable(wh, hh, 18796)
    assert st["shell"]["owner_logins"] == ["old@example"]
    monkeypatch.setattr(alongside, "_tailscale_status", lambda: {})   # `tailscale logout`, rerun before re-login
    st = alongside.enable(wh, hh, 18796)
    assert "owner_logins" not in st["shell"] and "owner_hosts" not in st["shell"]


def test_rerun_on_the_new_tailnet_pins_the_new_owner(tmp_path, monkeypatch):
    wh, hh = _box(tmp_path, monkeypatch)
    monkeypatch.setattr(alongside, "_tailscale_status", lambda: {
        "Self": {"UserID": 1, "DNSName": "box.old-tailnet.ts.net."}, "User": {"1": {"LoginName": "old@example"}}})
    alongside.enable(wh, hh, 18796)
    monkeypatch.setattr(alongside, "_tailscale_status", lambda: {
        "Self": {"UserID": 2, "DNSName": "box.customer.ts.net."}, "User": {"2": {"LoginName": "customer@example"}}})
    st = alongside.enable(wh, hh, 18796)
    assert st["shell"]["owner_logins"] == ["customer@example"] and st["shell"]["owner_hosts"] == ["box.customer.ts.net"]
