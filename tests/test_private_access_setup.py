"""Exercise the actual host setup script with isolated command doubles; never touch the host's tailnet."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup-private-access.sh"


@pytest.fixture
def sandbox(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Only allowlisted utilities: no real host tailscale can leak into the fresh-install test.
    for utility in ("bash", "sh", "cat", "mkdir", "mktemp", "rm", "cp", "chmod", "date", "sleep", "grep", "tail", "touch"):
        (bin_dir / utility).symlink_to(shutil.which(utility))
    (bin_dir / "python3").symlink_to(sys.executable)
    log = tmp_path / "calls"
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.write_text("Running")

    def tool(name, body):
        p = bin_dir / name
        p.write_text("#!/bin/bash\nset -eu\n" + body)
        p.chmod(0o755)

    tool("id", 'echo 1000\n')
    tool("sudo", 'printf "sudo %s\\n" "$*" >> "$CALLS"; [ "${1:-}" != -n ] || shift; exec "$@"\n')
    tool("systemctl", 'printf "systemctl %s\\n" "$*" >> "$CALLS"; exit "${SERVICE_RC:-0}"\n')
    tailscale_body = '''printf "tailscale %s\\n" "$*" >> "$CALLS"
case "$1" in
status)
  st="$(cat "$STATE_FILE")"; url=""
  if [ "$st" = NeedsLogin ] && [ -f "$STATE_FILE.up" ]; then url="https://login.tailscale.com/a/${LINK_ID:-abc123}"; fi
  certs='["box.tail1.ts.net"]'; [ "${CERTS:-on}" = on ] || [ -f "$STATE_FILE.certs" ] || certs=null
  printf '{"BackendState":"%s","AuthURL":"%s","CurrentTailnet":{"MagicDNSEnabled":%s},"CertDomains":%s}\\n' \\
    "$st" "$url" "${MAGICDNS:-true}" "$certs" ;;
cert)
  [ -f "$STATE_FILE.certs" ] || [ "${CERTS:-on}" = on ] || exit 1
  [ ! -f "$STATE_FILE.certfail" ] || exit 1 ;;
login)
  touch "$STATE_FILE.up"; echo "login keeps settings"
  if [ -n "${LOGIN_AFTER:-}" ]; then sleep "$LOGIN_AFTER"; echo Running > "$STATE_FILE"; exit 0; fi
  exit 1 ;;
up)
  if [ -n "${UP_OK_STARTING:-}" ]; then
    # The owner approves; `up` exits 0 while the daemon still reports Starting, Running a few seconds later.
    # A second `up` in that window starts a new sign-in (fresh link, NeedsLogin) and the first never completes.
    if [ -f "$STATE_FILE.first" ]; then touch "$STATE_FILE.second"; echo NeedsLogin > "$STATE_FILE"; exit 1; fi
    touch "$STATE_FILE.first" "$STATE_FILE.up"; sleep "${LOGIN_AFTER:-1}"; echo Starting > "$STATE_FILE"
    ( sleep "$UP_OK_STARTING"; [ -f "$STATE_FILE.second" ] || echo Running > "$STATE_FILE" ) >/dev/null 2>&1 &
    exit 0
  fi
  if [ -n "${NONDEFAULT:-}" ] && [ "$#" -eq 2 ]; then
    echo "Error: changing settings via 'tailscale up' requires mentioning all non-default flags"
    echo "  tailscale up $2 ${PRESERVE_FLAGS:---accept-dns=false --accept-routes --hostname=fixture}"
    exit 1
  fi
  touch "$STATE_FILE.up"
  echo "To authenticate, visit:"
  echo "login link from mock"
  if [ "${UP_STATE:-NeedsLogin}" = "Running" ]; then echo Running > "$STATE_FILE"; exit 0; fi
  if [ -n "${LOGIN_AFTER:-}" ]; then sleep "$LOGIN_AFTER"; echo Running > "$STATE_FILE"; exit 0; fi
  exit 1
  ;;
esac
'''
    tool("tailscale", tailscale_body)
    # Fresh installation: download a test script that records the install and provides the mock binary.
    tool("curl", '''printf "curl %s\\n" "$*" >> "$CALLS"
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then cp "$INSTALL_FIXTURE" "$2"; break; fi
  shift
done
''')
    fixture = tmp_path / "install-fixture.sh"
    saved = tmp_path / "tailscale-fixture"
    saved.write_text((bin_dir / "tailscale").read_text())
    fixture.write_text('echo "package installed" >> "$CALLS"\ncp "$TAILSCALE_FIXTURE" "$TEST_BIN/tailscale"\nchmod +x "$TEST_BIN/tailscale"\n')
    env = {"PATH": str(bin_dir), "HOME": str(home), "CALLS": str(log),
           "STATE_FILE": str(state), "INSTALL_FIXTURE": str(fixture), "TAILSCALE_FIXTURE": str(saved),
           "TEST_BIN": str(bin_dir), "TMPDIR": str(tmp_path)}
    return bin_dir, log, home, state, env


def run(sandbox, timeout=10, **overrides):
    *_, env = sandbox
    return subprocess.run(["bash", str(SCRIPT)], env={**env, **overrides}, capture_output=True, text=True,
                          timeout=timeout)


def test_existing_connection_is_not_reconfigured(sandbox):
    _, log, home, _, _ = sandbox
    p = run(sandbox)
    assert p.returncode == 0, p.stderr
    calls = log.read_text()
    assert "tailscale up" not in calls
    assert "package installed" not in calls
    assert "is-active --quiet tailscaled" in calls
    assert "enable --now tailscaled" not in calls
    assert "sudo -n systemctl" not in calls, "a running daemon needs no privileged systemctl"
    assert json.loads((home / ".wizard-os/private-access.json").read_text()) == {
        "provider": "tailscale", "runtime": "host", "state": "Running", "installed": True, "serve_ready": True}


def test_operator_without_sudo_reuses_daemon_and_runs_each_up_only_once(sandbox):
    bindir, log, _, state, _ = sandbox
    (bindir / "sudo").write_text('#!/bin/bash\nprintf "sudo %s\\n" "$*" >> "$CALLS"\nexit 1\n')
    state.write_text("NeedsLogin")
    # Let the successful preserved invocation complete instead of racing a zero-second deadline.
    p = run(sandbox, NONDEFAULT="1", WIZARD_TAILSCALE_WAIT="2", UP_STATE="Running")
    assert p.returncode == 0, p.stderr
    calls = log.read_text()
    assert "sudo -n tailscale" not in calls and "enable --now tailscaled" not in calls
    assert sum(line.startswith("tailscale up ") for line in calls.splitlines()) == 2
    assert "--accept-dns=false --accept-routes --hostname=fixture" in calls


def test_fresh_install_can_leave_login_pending(sandbox):
    bin_dir, log, home, state, _ = sandbox
    state.write_text("NeedsLogin")
    (bin_dir / "tailscale").unlink()
    p = run(sandbox)
    assert p.returncode == 0, p.stderr
    assert "package installed" in log.read_text()
    assert "tailscale up --timeout=" in log.read_text()
    assert "not connected yet" in p.stdout
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["state"] == "NeedsLogin"


def test_login_completes(sandbox):
    _, _, home, state, _ = sandbox
    state.write_text("NeedsLogin")
    p = run(sandbox, UP_STATE="Running")
    assert p.returncode == 0, p.stderr
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["state"] == "Running"


@pytest.mark.parametrize("provider", ["cloudflare", "none"])
def test_alternative_does_not_touch_tailscale(sandbox, provider):
    _, log, home, _, _ = sandbox
    p = run(sandbox, WIZARD_PRIVATE_ACCESS=provider)
    assert p.returncode == 0
    assert not log.exists()
    assert not (home / ".wizard-os/private-access.json").exists()


def test_stopped_existing_node_is_not_reset(sandbox):
    _, log, _, state, _ = sandbox
    state.write_text("Stopped")
    assert run(sandbox).returncode == 0
    assert "tailscale up" not in log.read_text()


def test_daemon_failure_is_not_reported_as_installed(sandbox):
    _, _, home, _, _ = sandbox
    assert run(sandbox, SERVICE_RC="1").returncode != 0
    assert not (home / ".wizard-os/private-access.json").exists()


def test_invalid_provider_fails_without_mutations(sandbox):
    _, log, _, _, _ = sandbox
    assert run(sandbox, WIZARD_PRIVATE_ACCESS="invalid").returncode != 0
    assert not log.exists()


def test_login_link_is_the_one_owner_action_and_is_waited_for(sandbox):
    """Logged out, no terminal, detached: `up` runs in the background, its AuthURL is surfaced as the owner action,
    and the install continues once the owner has signed in."""
    _, log, home, state, _ = sandbox
    state.write_text("NeedsLogin")
    p = run(sandbox, WIZARD_DETACHED="1", WIZARD_TAILSCALE_WAIT="20", LOGIN_AFTER="3")
    assert p.returncode == 0, p.stderr
    out = p.stdout.splitlines()
    link = "https://login.tailscale.com/a/abc123"
    assert f"[wizard-os] owner-action: Open this link to connect your Wizard to your Tailscale: {link}" in out
    assert out.index(link) < out.index("[wizard-os] owner-action-done")
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["state"] == "Running"
    calls = log.read_text()
    assert "tailscale up --timeout=" in calls and "--auth-key" not in calls and "--reset" not in calls


def test_no_terminal_plain_run_does_not_wait_for_login(sandbox):
    _, log, home, state, _ = sandbox
    state.write_text("NeedsLogin")
    p = run(sandbox, LOGIN_AFTER="30")
    assert p.returncode == 0, p.stderr
    assert "tailscale up --timeout=1s" in log.read_text()


@pytest.mark.parametrize("env,what", [({"MAGICDNS": "false"}, "MagicDNS"), ({"CERTS": "off"}, "HTTPS certificates")])
def test_tailnet_without_magicdns_or_https_finishes_box_only_with_the_admin_link(sandbox, env, what):
    _, log, home, _, _ = sandbox
    p = run(sandbox, **env)
    assert p.returncode == 0, p.stderr
    assert "https://login.tailscale.com/admin/dns" in p.stdout
    assert f"[wizard-os] note: {what} is off" in p.stdout
    assert "tailscale up" not in log.read_text(), "a logged-in node is never re-authenticated"
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["serve_ready"] is False


def test_logged_out_node_with_custom_settings_signs_in_without_changing_them(sandbox):
    _, log, home, state, _ = sandbox
    state.write_text("NeedsLogin")
    p = run(sandbox, WIZARD_DETACHED="1", WIZARD_TAILSCALE_WAIT="20", LOGIN_AFTER="3", NONDEFAULT="1")
    assert p.returncode == 0, p.stderr
    calls = log.read_text()
    assert "tailscale login" not in calls, "login switches to an empty profile"
    assert "--accept-dns=false --accept-routes --hostname=fixture" in calls
    assert "https://login.tailscale.com/a/abc123" in p.stdout
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["state"] == "Running"


@pytest.mark.parametrize("flags", ["--reset", "--force-reauth", "--auth-key=not-a-key",
                                  "--hostname=fixture; touch injected", "'unterminated"])
def test_preservation_fallback_rejects_unsafe_or_unrecognized_output(sandbox, flags):
    _, log, _, state, _ = sandbox
    state.write_text("NeedsLogin")
    p = run(sandbox, NONDEFAULT="1", PRESERVE_FLAGS=flags, WIZARD_TAILSCALE_WAIT="0")
    assert p.returncode == 0, p.stderr
    calls = log.read_text()
    assert "tailscale login" not in calls
    assert sum(line.startswith("tailscale up ") for line in calls.splitlines()) == 1


def test_unavailable_daemon_status_is_not_reported_as_success(sandbox):
    _, _, home, state, _ = sandbox
    state.write_text("Unknown")
    p = run(sandbox)
    assert p.returncode != 0
    assert "status is unavailable" in p.stderr
    assert not (home / ".wizard-os/private-access.json").exists()


def test_joined_node_never_changes_custom_flags_or_serve_443(sandbox):
    _, log, _, _, _ = sandbox
    assert run(sandbox, NONDEFAULT="1").returncode == 0
    commands = [line for line in log.read_text().splitlines() if line.startswith("tailscale ")]
    assert commands and all(line == "tailscale status --json" for line in commands)


def test_https_off_is_an_owner_step_and_the_install_continues_once_certs_work(sandbox):
    """Heidi's brand-new tailnet had HTTPS certificates off: the install must stop on one explicit owner step with
    the admin link (needs_owner), then continue by itself as soon as `tailscale cert` succeeds."""
    import threading, time
    _, log, home, state, env = sandbox
    def owner_turns_https_on():
        time.sleep(2)
        Path(str(state) + ".certs").write_text("")
    threading.Thread(target=owner_turns_https_on, daemon=True).start()
    p = run(sandbox, WIZARD_DETACHED="1", WIZARD_TAILSCALE_WAIT="20", WIZARD_CERT_POLL_SECS="1", CERTS="off")
    assert p.returncode == 0, p.stderr
    out = p.stdout.splitlines()
    step = next(l for l in out if l.startswith("[wizard-os] owner-step:"))
    assert "HTTPS" in step and step.endswith("https://login.tailscale.com/admin/dns")
    assert out.index(step) < out.index("[wizard-os] owner-action-done")
    assert not any(l.startswith("[wizard-os] note:") for l in out), "continued, so it is not box-only"
    assert "tailscale cert --cert-file" in log.read_text()
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["serve_ready"] is True


def test_https_still_off_at_the_deadline_finishes_box_only_with_the_note(sandbox):
    _, log, home, state, _ = sandbox
    p = run(sandbox, WIZARD_DETACHED="1", WIZARD_TAILSCALE_WAIT="3", WIZARD_CERT_POLL_SECS="1", CERTS="off")
    assert p.returncode == 0, p.stderr
    assert "[wizard-os] owner-step:" in p.stdout and "[wizard-os] owner-action-done" in p.stdout
    assert "[wizard-os] note: HTTPS certificates is off" in p.stdout
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["serve_ready"] is False


def test_certificate_domains_without_a_working_cert_keep_waiting(sandbox):
    """CertDomains can appear before a certificate can actually be issued; only a real `tailscale cert` counts."""
    import threading, time
    _, log, home, state, _ = sandbox
    Path(str(state) + ".certfail").write_text("")
    threading.Thread(target=lambda: (time.sleep(1), Path(str(state) + ".certs").write_text("")), daemon=True).start()
    p = run(sandbox, WIZARD_DETACHED="1", WIZARD_TAILSCALE_WAIT="4", WIZARD_CERT_POLL_SECS="1", CERTS="off")
    assert p.returncode == 0, p.stderr
    assert "tailscale cert --cert-file" in log.read_text()
    assert "HTTPS certificates are on" not in p.stdout
    assert "[wizard-os] note: HTTPS certificates is off" in p.stdout
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["serve_ready"] is False


def test_an_approved_login_is_waited_for_while_the_daemon_is_still_starting(sandbox):
    """Heidi's rerun after `tailscale logout`: the owner approved, but the install finished box-only ("not connected
    yet") and Tailscale showed connected moments later. A successful `up` must be followed by waiting for Running,
    never by a second `up` that restarts the sign-in."""
    _, log, home, state, _ = sandbox
    state.write_text("NeedsLogin")
    p = run(sandbox, timeout=40, WIZARD_DETACHED="1", WIZARD_TAILSCALE_WAIT="10", LOGIN_AFTER="1",
            UP_OK_STARTING="4", WIZARD_TAILSCALE_GRACE="20")
    assert p.returncode == 0, p.stderr
    assert sum(line.startswith("tailscale up") for line in log.read_text().splitlines()) == 1
    assert "not connected yet" not in p.stdout
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["state"] == "Running"
