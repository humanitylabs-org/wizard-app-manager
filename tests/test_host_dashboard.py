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
