"""#708: updating an app must never take it from working to 403 "Unknown host".

Before: the update regenerated app-data/<store>/<id>/app.env from the app's settings and the user-config app.env.
When links.json had an empty host (state "missing"), trusted_origin_env() returned {}, nothing was written to the
user-config app.env, and the hosts the app trusted (only in the generated file) were gone after the update.
"""
import json

from wam import cli, links

HOST = "wizard.tail1.ts.net"
REG = {"apps": [{"id": "wizard-ux", "provides": ["shell"], "health": "http://127.0.0.1:8796/shell-health"}]}
LIVE = (f"APP_PORT=8810\nWIZARD_TRUSTED_HOSTS={HOST}:8796,{HOST}\n"
        f"WIZARD_TRUSTED_ORIGINS=https://{HOST}:8796\n")


def _box(tmp_path, monkeypatch, links_host="", tailnet_host="", live=LIVE,
         compose="environment:\n  WIZARD_TRUSTED_HOSTS: ${WIZARD_TRUSTED_HOSTS:-}\n"):
    c = cli.Config()
    c.runtipi_root, c.home = tmp_path / "rt", tmp_path / "home"
    comp = c.runtipi_root / "repos" / cli.STORE / "apps/tracker/docker-compose.yml"
    comp.parent.mkdir(parents=True)
    comp.write_text(compose)
    gen = c.runtipi_root / "app-data" / cli.STORE / "tracker" / "app.env"
    gen.parent.mkdir(parents=True)
    gen.write_text(live)
    c.home.mkdir()
    (c.home / "links.json").write_text(json.dumps({"state": "missing" if not links_host else "Running",
                                                  "host": links_host}))
    (c.home / "apps.json").write_text(json.dumps(REG))
    monkeypatch.setattr(cli.alongside, "load", lambda h: None)
    monkeypatch.setattr(links, "tailnet", lambda: {"state": "Running" if tailnet_host else "missing",
                                                   "host": tailnet_host})
    return c


def _user_env(c):
    from wam.runtipi import read_env
    return read_env(c.runtipi_root / "user-config" / cli.STORE / "tracker" / "app.env")


def test_update_keeps_the_hosts_the_app_trusts_when_links_json_has_no_host(tmp_path, monkeypatch):
    c = _box(tmp_path, monkeypatch)
    assert cli.trusted_origin_env(c, "tracker", 8810) == {}   # the state the issue found on the box
    cli.keep_trusted_hosts(c, "tracker")
    env = _user_env(c)
    assert env["WIZARD_TRUSTED_HOSTS"] == f"{HOST}:8796,{HOST}"
    assert env["WIZARD_TRUSTED_ORIGINS"] == f"https://{HOST}:8796"


def test_an_app_that_already_lost_its_hosts_gets_them_from_tailscale(tmp_path, monkeypatch):
    c = _box(tmp_path, monkeypatch, tailnet_host=HOST, live="APP_PORT=8810\n")
    cli.keep_trusted_hosts(c, "tracker")
    env = _user_env(c)
    assert env["WIZARD_TRUSTED_HOSTS"] == f"{HOST}:8796,{HOST}:8810,{HOST}"
    assert env["WIZARD_TRUSTED_ORIGINS"] == f"https://{HOST}:8796,https://{HOST}:8810"


def test_derived_and_kept_hosts_are_merged_never_dropped(tmp_path, monkeypatch):
    c = _box(tmp_path, monkeypatch, links_host="new.tail1.ts.net")
    user = c.runtipi_root / "user-config" / cli.STORE / "tracker" / "app.env"
    user.parent.mkdir(parents=True)
    user.write_text("OTHER=1\nWIZARD_TRUSTED_HOSTS=old.tail1.ts.net\n")
    cli.keep_trusted_hosts(c, "tracker")
    env = _user_env(c)
    assert env["OTHER"] == "1"
    assert env["WIZARD_TRUSTED_HOSTS"].split(",") == [
        "new.tail1.ts.net:8796", "new.tail1.ts.net:8810", "new.tail1.ts.net", "old.tail1.ts.net"]
    # the generated env's hosts are not revived over the owner's user-config list
    assert cli.keep_trusted_hosts(c, "tracker") == {}   # nothing new: the file is left alone


def test_apps_without_trusted_hosts_and_bad_values_are_left_alone(tmp_path, monkeypatch):
    c = _box(tmp_path, monkeypatch, compose="plain: app\n")
    assert cli.keep_trusted_hosts(c, "tracker") == {}
    assert not (c.runtipi_root / "user-config").exists()
    c = _box(tmp_path / "b", monkeypatch, live="WIZARD_TRUSTED_HOSTS=ok.ts.net,evil host;x\n")
    assert cli.keep_trusted_hosts(c, "tracker") == {"WIZARD_TRUSTED_HOSTS": "ok.ts.net"}


def test_app_update_keeps_trusted_hosts_before_runtipi_regenerates_the_env(tmp_path, monkeypatch):
    c = _box(tmp_path, monkeypatch)
    seen = {}

    class RT:
        def app(self, urn):
            return {"app": {"status": "running", "version": "1"}}

        def update(self, urn, perform_backup=True):
            seen["at_update"] = _user_env(c).get("WIZARD_TRUSTED_HOSTS")

        def wait_status(self, *a, **k):
            return "running"

    monkeypatch.setattr(c, "runtipi", lambda: RT(), raising=False)
    monkeypatch.setattr(cli, "host_access_risks", lambda *a, **k: [])
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: {})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    cli._app_update(c, "tracker", backup=False)
    assert seen["at_update"] == f"{HOST}:8796,{HOST}"
