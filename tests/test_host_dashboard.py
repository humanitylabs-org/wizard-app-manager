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


def test_owner_edit_of_the_environment_restarts_the_running_dashboard(tmp_path, monkeypatch):
    """An owner who flips WIZARD_ACCOUNT_CHANGES and reruns `wam alongside` gets it applied (start would not)."""
    wh = tmp_path / "wos"
    _run(tmp_path, monkeypatch, wh)
    p = hd.env_path(wh)
    p.write_text(p.read_text().replace(f"{hd.ACCOUNT_CHANGES_VAR}=1", f"{hd.ACCOUNT_CHANGES_VAR}=0"))
    calls = _run(tmp_path, monkeypatch, wh)
    assert ("restart", hd.UNIT) in calls and hd.read_env(p)[hd.ACCOUNT_CHANGES_VAR] == "0"
    assert ("restart", hd.UNIT) not in _run(tmp_path, monkeypatch, wh)


def test_a_failed_restart_is_retried_on_the_next_run(tmp_path, monkeypatch):
    wh = tmp_path / "wos"
    rt, units = box(tmp_path), tmp_path / "units"
    monkeypatch.setattr(hd, "tailnet_ip", lambda: "100.64.0.7")
    monkeypatch.setattr(hd, "_systemctl", lambda *a: type("R", (), {"returncode": 1, "stderr": "boom", "stdout": ""})())
    out = hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/h", cli.write_user_env, unit_dir=units)
    assert out["state"] == "error" and not hd.applied_path(wh).exists()
    assert ("restart", hd.UNIT) in _run(tmp_path, monkeypatch, wh)


def test_install_reloads_the_dashboard_after_link_refreshes_the_plugin():
    src = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    link = src.index('"$HOME/.local/bin/wizard" link')
    reload = src.index("systemctl --user restart wizard-host-dashboard.service")
    bootstrap = src.index("# 3) Fill every empty parking spot")
    assert link < reload < bootstrap
    block = src[src.rindex("\n", 0, reload) - 400:reload + 600]
    assert "is-enabled --quiet wizard-host-dashboard.service" in block  # only an installed dashboard is restarted
    assert "DEGRADED+=" in block and "|| true" not in block           # a failed restart is reported


def _reload_block():
    src = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    start = src.index("  if [ ${#FAILED[@]} -eq 0 ] && { systemctl --user is-enabled --quiet wizard-host-dashboard.service")
    return src[start:src.index("\n  fi\n", start) + 6]


def test_install_reload_block_behaviour(tmp_path):
    import subprocess
    # (enabled, active before, restart ok, active after) -> (degraded, restarted)
    cases = (((False, False, True, True), (False, False)),   # not installed: nothing to do
             ((True, True, True, True), (False, True)),       # running: restarted, back up
             ((True, True, False, False), (True, True)),      # restart fails
             ((True, True, True, False), (True, True)),       # restarted but does not stay up
             ((True, False, False, False), (True, True)),     # installed but left down by `wam alongside`
             ((False, True, True, True), (False, True)))      # running but not enabled
    for (enabled, active, restart_ok, after), (degraded, restarted) in cases:
        log = tmp_path / "log"
        log.write_text("")
        stub = tmp_path / "systemctl"
        stub.write_text(f"""#!/bin/bash
echo "$*" >> {log}
case "$*" in
  *is-enabled*) exit {0 if enabled else 1} ;;
  *is-active*) if grep -q " restart" {log}; then exit {0 if after else 3}; else exit {0 if active else 3}; fi ;;
  *restart*) exit {0 if restart_ok else 1} ;;
esac
""")
        stub.chmod(0o755)
        script = "FAILED=(); DEGRADED=(); sleep() { :; }\n" + _reload_block() + 'echo "DEG=${#DEGRADED[@]}"\n'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
        assert out.stdout.strip() == f"DEG={1 if degraded else 0}", (enabled, active, restart_ok, after, out.stdout, out.stderr)
        assert ("restart wizard-host-dashboard.service" in log.read_text()) == restarted


def test_running_container_with_an_old_address_needs_a_recreate(tmp_path, monkeypatch):
    """Heidi's box: a tailnet move rewrote app.env, but the ChatGPT/Claude containers kept the old dashboard
    address. A later run whose app.env is already current must still flag the stale running container."""
    rt, wh, units = box(tmp_path), tmp_path / "wos", tmp_path / "units"
    monkeypatch.setattr(hd, "tailnet_ip", lambda: "100.126.185.50")
    first = hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/h", cli.write_user_env,
                      unit_dir=units)
    assert first["changed_apps"] == ["wizard-chatgpt"]
    seen = []
    def env(app_id, store):
        seen.append((app_id, store))
        return [{"WIZARD_CHATGPT_HERMES_URL": "http://100.91.73.83:9119", "PATH": "/bin"}, {"OTHER": "x"}]
    monkeypatch.setattr(hd, "running_env", env)
    again = hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/h", cli.write_user_env,
                      unit_dir=units)
    assert again["changed_apps"] == ["wizard-chatgpt"] and seen == [("wizard-chatgpt", "wizardapps")]
    monkeypatch.setattr(hd, "running_env", lambda a, s: [{"WIZARD_CHATGPT_HERMES_URL": "http://100.126.185.50:9119"}])
    assert hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/h", cli.write_user_env,
                     unit_dir=units)["changed_apps"] == []
    monkeypatch.setattr(hd, "running_env", lambda a, s: None)   # Docker unreadable: no false alarm
    assert hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/h", cli.write_user_env,
                     unit_dir=units)["changed_apps"] == []


def test_running_env_reads_the_apps_compose_project(monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        out = "abc\ndef\n" if argv[1] == "ps" else '["A=1","B=x=y"]\n["C="]\n'
        return type("R", (), {"returncode": 0, "stdout": out})()
    monkeypatch.undo()   # drop the suite guard's running_env stub (this test fakes Docker itself)
    monkeypatch.setattr(hd.shutil, "which", lambda n: "/usr/bin/docker")
    monkeypatch.setattr(hd.subprocess, "run", run)
    assert hd.running_env("wizard-chatgpt", "wizardapps") == [{"A": "1", "B": "x=y"}, {"C": ""}]
    assert calls[0][-1] == "label=com.docker.compose.project=wizard-chatgpt_wizardapps"
    assert calls[1][:4] == ["/usr/bin/docker", "inspect", "--format", "{{json .Config.Env}}"]


def test_alongside_rerun_recreates_running_connection_apps(tmp_path, monkeypatch):
    """`wam alongside` (run by every install/rerun) restarts each running app whose dashboard address changed or
    whose container still holds an old one; stopped apps are left for their next start; failures are reported."""
    from wam import alongside
    home = tmp_path / "wos"
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.setenv("RUNTIPI_ROOT", str(tmp_path / "runtipi"))
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    c = cli.Config()
    status = {"wizard-chatgpt:wizardapps": "running", "wizard-claude:wizardapps": "running",
              "tinfoil:wizardapps": "stopped"}

    class RT:
        def app(self, urn): return {"app": {"status": status[urn]}}
    restarted = []
    def lifecycle(c, verb, urn, timeout=300):
        restarted.append((verb, urn))
        return {"status": "running"} if urn != "wizard-claude:wizardapps" else {"error": "restart did not finish"}
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: RT())
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set())
    monkeypatch.setattr(cli, "_alongside_apps", lambda c, rt=None: {"wizard-ux": "shell"})
    monkeypatch.setattr(alongside, "enable", lambda *a, **k: {"detected": {}, "agent": {}, "service": {}})
    monkeypatch.setattr(alongside, "write_shell_override", lambda *a, **k: None)
    monkeypatch.setattr(cli, "host_dashboard_cmd", lambda c, rt=None: {
        "state": "ready", "changed_apps": ["wizard-chatgpt", "wizard-claude", "tinfoil"]})
    monkeypatch.setattr(cli, "claude_login_cmd", lambda c, apps: {"changed_apps": ["wizard-claude"]})
    monkeypatch.setattr(cli, "registry_apps", lambda c: {})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: {"apps": []})
    monkeypatch.setattr(cli, "app_lifecycle", lifecycle)
    out = cli.alongside_cmd(c, False, False, None, None)
    assert restarted == [("restart", "wizard-chatgpt:wizardapps"), ("restart", "wizard-claude:wizardapps")]
    assert out["recreated"]["wizard-chatgpt"] == "recreated"
    assert out["recreated"]["wizard-claude"].startswith("error")
    assert out["recreated"]["tinfoil"].startswith("not running")
    assert "wam app update wizard-claude" in out["next"] and "wizard-chatgpt" not in out["next"]


def _alongside_block():
    src = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    start = src.index('  if ! al_out="$("$HOME/.local/bin/wam" alongside')
    return src[start:src.index("\n  fi\n", start) + 6]


def test_install_reports_apps_it_could_not_recreate(tmp_path):
    import json as _json
    import subprocess
    for rec, deg, said in (({}, 0, ""), ({"wizard-chatgpt": "recreated"}, 0, "wizard-chatgpt"),
                           ({"wizard-chatgpt": "recreated", "wizard-claude": "error: boom"}, 1, "wizard-chatgpt"),
                           ({"tinfoil": "not running (stopped); applies on its next start"}, 0, "")):
        wam = tmp_path / ".local" / "bin" / "wam"
        wam.parent.mkdir(parents=True, exist_ok=True)
        wam.write_text("#!/bin/sh\necho '" + _json.dumps({"mode": "alongside", "recreated": rec}) + "'\n")
        wam.chmod(0o755)
        script = "FAILED=(); DEGRADED=()\n" + _alongside_block() + 'echo "DEG=${#DEGRADED[@]} F=${#FAILED[@]}"\n'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
        assert out.stdout.strip().endswith(f"DEG={deg} F=0"), (rec, out.stdout, out.stderr)
        assert (f"Restarted with the current agent address: {said}" in out.stdout) == bool(said)


def test_stale_login_in_a_running_container_also_needs_a_recreate(tmp_path, monkeypatch):
    """A rotated dashboard login already written to app.env, but the container still has the old one."""
    rt, wh, units = box(tmp_path), tmp_path / "wos", tmp_path / "units"
    monkeypatch.setattr(hd, "tailnet_ip", lambda: "100.64.0.7")
    hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/h", cli.write_user_env, unit_dir=units)
    monkeypatch.setattr(hd, "running_env", lambda a, s: [{"WIZARD_CHATGPT_HERMES_URL": "http://100.64.0.7:9119",
                                                         hd.PASSWORD_VAR: "old-login"}])
    assert hd.ensure(wh, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/h", cli.write_user_env,
                     unit_dir=units)["changed_apps"] == ["wizard-chatgpt"]


def test_alongside_does_not_recreate_apps_when_the_dashboard_failed(tmp_path, monkeypatch):
    from wam import alongside
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "wos"))
    monkeypatch.setenv("RUNTIPI_ROOT", str(tmp_path / "runtipi"))
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    c = cli.Config()
    restarted = []
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: type("RT", (), {"app": lambda s, u: {"app": {"status": "running"}}})())
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set())
    monkeypatch.setattr(cli, "_alongside_apps", lambda c, rt=None: {})
    monkeypatch.setattr(alongside, "enable", lambda *a, **k: {"detected": {}, "agent": {}, "service": {}})
    monkeypatch.setattr(cli, "host_dashboard_cmd", lambda c, rt=None: {
        "state": "error", "error": "restart failed", "changed_apps": ["wizard-chatgpt"]})
    monkeypatch.setattr(cli, "claude_login_cmd", lambda c, apps: {"changed_apps": []})
    monkeypatch.setattr(cli, "registry_apps", lambda c: {})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: {"apps": []})
    monkeypatch.setattr(cli, "app_lifecycle", lambda *a, **k: restarted.append(a) or {"status": "running"})
    out = cli.alongside_cmd(c, False, False, None, None)
    assert restarted == [] and "recreated" not in out and out["host_dashboard"]["state"] == "error"
