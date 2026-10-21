"""Fresh boxes: a Store-installed builder app must answer its own private address.

Before: trusted_origin_env() returned {} without alongside.json, so on a fresh box an app like Morning Report got
empty WIZARD_TRUSTED_HOSTS and answered its published https://<box>:<port>/ with 403 "Unknown host".
"""
import json

from wam import cli

HOST = "box.tail1.ts.net"
REG = {"apps": [{"id": "wizard-ux", "provides": ["shell"], "health": "http://127.0.0.1:8796/shell-health"},
                {"id": "wizard-agent-service", "provides": ["agent-service"], "health": "http://127.0.0.1:8797/wizard/health"}]}


def _box(tmp_path, monkeypatch, alongside_state=None, compose="environment:\n  WIZARD_TRUSTED_HOSTS: ${WIZARD_TRUSTED_HOSTS:-}\n"):
    c = cli.Config()
    c.runtipi_root, c.home = tmp_path / "rt", tmp_path / "home"
    comp = c.runtipi_root / "repos" / cli.STORE / "apps/mr/docker-compose.yml"
    comp.parent.mkdir(parents=True)
    comp.write_text(compose)
    c.home.mkdir()
    (c.home / "links.json").write_text(json.dumps({"host": HOST}))
    (c.home / "apps.json").write_text(json.dumps(REG))
    monkeypatch.setattr(cli.alongside, "load", lambda h: alongside_state)
    return c


def test_fresh_box_trusts_the_shell_origin_and_the_apps_own_private_address(tmp_path, monkeypatch):
    c = _box(tmp_path, monkeypatch)
    assert cli.trusted_origin_env(c, "mr", 8802) == {
        "WIZARD_TRUSTED_HOSTS": f"{HOST}:8796,{HOST}:8802,{HOST}",
        "WIZARD_TRUSTED_ORIGINS": f"https://{HOST}:8796,https://{HOST}:8802"}


def test_fresh_box_without_app_port_still_trusts_the_shell(tmp_path, monkeypatch):
    c = _box(tmp_path, monkeypatch)
    assert cli.trusted_origin_env(c, "mr") == {"WIZARD_TRUSTED_HOSTS": f"{HOST}:8796,{HOST}",
                                              "WIZARD_TRUSTED_ORIGINS": f"https://{HOST}:8796"}


def test_alongside_is_unchanged(tmp_path, monkeypatch):
    c = _box(tmp_path, monkeypatch, {"mode": "alongside", "shell": {"port": 8806}})
    assert cli.trusted_origin_env(c, "mr", 8802) == {"WIZARD_TRUSTED_HOSTS": f"{HOST}:8806,{HOST}",
                                                    "WIZARD_TRUSTED_ORIGINS": f"https://{HOST}:8806"}


def test_nothing_without_a_tailnet_host_shell_or_trust_variables(tmp_path, monkeypatch):
    c = _box(tmp_path, monkeypatch)
    (c.home / "links.json").write_text("{}")
    assert cli.trusted_origin_env(c, "mr", 8802) == {}
    c = _box(tmp_path / "b", monkeypatch)
    (c.home / "apps.json").write_text(json.dumps({"apps": []}))
    assert cli.trusted_origin_env(c, "mr", 8802) == {}
    c = _box(tmp_path / "c", monkeypatch, compose="plain: app\n")
    assert cli.trusted_origin_env(c, "mr", 8802) == {}
    c = _box(tmp_path / "d", monkeypatch)
    (c.home / "links.json").write_text(json.dumps({"host": "evil host/x"}))
    assert cli.trusted_origin_env(c, "mr", 8802) == {}
