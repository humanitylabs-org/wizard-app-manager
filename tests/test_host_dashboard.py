"""Alongside: the host Hermes dashboard for apps that sign in through it (ChatGPT)."""
from pathlib import Path

from wam import cli, host_dashboard as hd

CHATGPT_COMPOSE = """services:
  wizard-chatgpt-engine:
    environment:
      WIZARD_CHATGPT_HERMES_URL: ${WIZARD_CHATGPT_HERMES_URL:-http://hermes-agent:9119}
      WIZARD_CHATGPT_HERMES_PASSWORD: ${HERMES_DASHBOARD_BASIC_AUTH_PASSWORD:-}
"""


def box(tmp_path):
    rt = tmp_path / "runtipi"
    for app, compose, config in (("wizard-chatgpt", CHATGPT_COMPOSE, '{"form_fields":[{"env_variable":'
                                  '"HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"}]}'),
                                 ("files", "services:\n  files:\n    image: x\n", "{}")):
        d = rt / "apps" / "wizardapps" / app
        d.mkdir(parents=True)
        (d / "docker-compose.yml").write_text(compose)
        (d / "config.json").write_text(config)
    return rt


def test_consumers_are_apps_that_sign_in_through_the_dashboard(tmp_path):
    rt = box(tmp_path)
    assert hd.consumers(rt, "wizardapps", {"wizard-chatgpt", "files", "absent"}) == {
        "wizard-chatgpt": ["WIZARD_CHATGPT_HERMES_URL"]}


def test_ensure_runs_tailnet_only_dashboard_and_points_apps_at_it(tmp_path, monkeypatch):
    rt, wh, units = box(tmp_path), tmp_path / "wos", tmp_path / "units"
    calls = []
    monkeypatch.setattr(hd, "tailnet_ip", lambda: "100.64.0.7")
    monkeypatch.setattr(hd, "_systemctl", lambda *a: calls.append(a) or type("R", (), {"returncode": 0})())
    out = hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt", "files"}, "/usr/bin/hermes", "/home/o/.hermes",
                    cli.write_user_env, unit_dir=units)
    assert out["state"] == "ready" and out["url"] == "http://100.64.0.7:9119" and out["changed_apps"] == ["wizard-chatgpt"]
    unit = (units / hd.UNIT).read_text()
    assert "--host 100.64.0.7 --port 9119" in unit and "127.0.0.1" not in unit and "0.0.0.0" not in unit
    secret = hd.env_path(wh)
    assert (secret.stat().st_mode & 0o777) == 0o600
    creds = hd.read_env(secret)
    assert len(creds[hd.PASSWORD_VAR]) >= 24 and creds[hd.PASSWORD_VAR] not in unit   # password only in the 0600 file
    env = hd.read_env(rt / "user-config" / "wizardapps" / "wizard-chatgpt" / "app.env")
    assert env["WIZARD_CHATGPT_HERMES_URL"] == "http://100.64.0.7:9119"
    assert env[hd.PASSWORD_VAR] == creds[hd.PASSWORD_VAR]
    assert ("enable", hd.UNIT) in calls
    # A re-run keeps the same login and changes nothing.
    again = hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/home/o/.hermes",
                      cli.write_user_env, unit_dir=units)
    assert again["changed_apps"] == [] and hd.read_env(secret) == creds


def test_ensure_skips_or_fails_closed(tmp_path, monkeypatch):
    rt, wh = box(tmp_path), tmp_path / "wos"
    assert hd.ensure(wh, rt, "wizardapps", {"files"}, "/usr/bin/hermes", "/h", cli.write_user_env)["state"] == "skipped"
    monkeypatch.setattr(hd, "tailnet_ip", lambda: None)
    out = hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/h", cli.write_user_env)
    assert out["state"] == "error" and not hd.env_path(wh).exists()   # never binds a public or loopback address


def _run(tmp_path, monkeypatch, wh):
    rt = tmp_path / "runtipi" if (tmp_path / "runtipi").is_dir() else box(tmp_path)
    units, calls = tmp_path / "units", []
    monkeypatch.setattr(hd, "tailnet_ip", lambda: "100.64.0.7")
    monkeypatch.setattr(hd, "_systemctl", lambda *a: calls.append(a) or type("R", (), {"returncode": 0})())
    hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/home/o/.hermes", cli.write_user_env,
              unit_dir=units)
    return calls


def test_account_changes_are_on_for_the_dashboard(tmp_path, monkeypatch):
    """The Claude app adds/removes any number of subscriptions through the plugin broker, which ships off."""
    wh = tmp_path / "wos"
    _run(tmp_path, monkeypatch, wh)
    assert hd.read_env(hd.env_path(wh))[hd.ACCOUNT_CHANGES_VAR] == "1"
    assert (hd.env_path(wh).stat().st_mode & 0o777) == 0o600


def test_upgraded_box_keeps_its_login_and_restarts_once_for_account_changes(tmp_path, monkeypatch):
    wh = tmp_path / "wos"
    p = hd.env_path(wh)
    p.parent.mkdir(parents=True)
    old = {"HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "hermes", hd.PASSWORD_VAR: "kept-password-abcdefghijklmnop",
           "HERMES_DASHBOARD_BASIC_AUTH_SECRET": "kept-secret"}
    p.write_text("".join(f"{k}={v}\n" for k, v in old.items()))
    units = tmp_path / "units"
    units.mkdir()
    (units / hd.UNIT).write_text(hd.unit_text("/usr/bin/hermes", "/home/o/.hermes", "100.64.0.7", wh))
    calls = _run(tmp_path, monkeypatch, wh)
    env = hd.read_env(p)
    assert {k: env[k] for k in old} == old and env[hd.ACCOUNT_CHANGES_VAR] == "1"
    assert ("restart", hd.UNIT) in calls   # the unit is unchanged, but the new environment needs a restart
    calls = _run(tmp_path, monkeypatch, wh)
    assert ("restart", hd.UNIT) not in calls and ("start", hd.UNIT) in calls


def test_owner_can_keep_account_changes_off(tmp_path, monkeypatch):
    wh = tmp_path / "wos"
    p = hd.env_path(wh)
    p.parent.mkdir(parents=True)
    p.write_text(f"{hd.PASSWORD_VAR}=pw-abcdefghijklmnopqrstuvwxyz\nHERMES_DASHBOARD_BASIC_AUTH_SECRET=s\n"
                 f"{hd.ACCOUNT_CHANGES_VAR}=0\n")
    _run(tmp_path, monkeypatch, wh)
    assert hd.read_env(p)[hd.ACCOUNT_CHANGES_VAR] == "0"


def test_install_reloads_the_dashboard_after_link_refreshes_the_plugin():
    src = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    link = src.index('"$HOME/.local/bin/wizard" link')
    reload = src.index("systemctl --user try-restart wizard-host-dashboard.service")
    bootstrap = src.index("# 3) Fill every empty parking spot")
    assert link < reload < bootstrap
    assert '[ ${#FAILED[@]} -gt 0 ] || systemctl --user try-restart wizard-host-dashboard.service' in src
