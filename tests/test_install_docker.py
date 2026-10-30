"""Execute install.sh only in an allowlisted PATH with fake privileged/provider commands."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]


def sandbox(tmp_path, docker_access=True, before=None, **extra_env):
    home = tmp_path / "home"
    root = home / "runtipi"
    (root / "user-config").mkdir(parents=True)
    (root / ".env").write_text("NGINX_PORT=8480\n")
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "user-config/tipi-compose.yml").write_text(
        "services:\n  runtipi-reverse-proxy:\n    ports: !override\n      - 127.0.0.1:8480:80\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("bash", "sh", "cat", "mkdir", "cp", "chmod", "rm", "date", "dirname", "seq", "grep", "cut", "sed",
                 "mv", "mktemp", "find", "touch"):
        (bin_dir / name).symlink_to(shutil.which(name))
    calls = tmp_path / "calls"

    def tool(name, body):
        target = bin_dir / name
        target.write_text("#!/bin/bash\nset -eu\n" + body)
        target.chmod(0o755)

    tool("id", 'case "${1:-}" in -un) echo sandbox;; *) echo 1000;; esac\n')
    tool("sudo", 'printf "sudo %s\\n" "$*" >> "$CALLS"; [ "${1:-}" != -n ] || shift; export MOCK_SUDO=1; exec "$@"\n')
    tool("docker", '''printf "docker sudo=%s %s\\n" "${MOCK_SUDO:-0}" "$*" >> "$CALLS"
if [ "$1" = info ] && [ "$DOCKER_ACCESS" = 0 ]; then exit 1; fi
if [ "$1" != info ] && [ "$DOCKER_ACCESS" = 0 ] && [ "${MOCK_SUDO:-0}" != 1 ]; then exit 1; fi
if [ "$1" = image ]; then exit 1; fi
# Model Compose's interrupted recreate: both the canonical name and a temporary
# replacement exist. Only service-scoped rm clears them; unrelated apps survive.
case "$*" in
  *"rm --stop --force wizard-release-index")
    [ "${INDEX_REMOVE_RC:-0}" = 0 ] || exit "$INDEX_REMOVE_RC"
    rm -f "$HOME/stale-release-index";;
  *"--no-deps --wait runtipi")
    [ "${MANAGER_WAIT_RC:-0}" = 0 ] || exit "$MANAGER_WAIT_RC"
    # Backend initialization writes Traefik's static entrypoints before health.
    touch "$HOME/manager-initialized";;
  *"--no-deps runtipi-reverse-proxy")
    if [ "${REQUIRE_MANAGER_HEALTH:-0}" = 1 ] && [ ! -f "$HOME/manager-initialized" ]; then
      echo 'Traefik started without static entrypoints: 404 page not found' >&2; exit 1
    fi;;
  *"up -d --force-recreate --wait wizard-release-index")
    if [ -f "$HOME/stale-release-index" ]; then echo 'Conflict: runtipi-wizard-release-index-1' >&2; exit 1; fi;;
esac
case "$*" in *"status = 'running'"*) printf '%s' "${RUNNING_ROWS:-}";; esac
''')
    for name in ("systemctl", "loginctl", "chown"):  # chown: the stand-in id says uid 1000, which the runner isn't
        tool(name, f'printf "{name} %s\\n" "$*" >> "$CALLS"\n')
    # HEALTH=down makes every health check fail (the App Manager never comes up).
    # The Runtipi setup download: write a stand-in setup script (creates the base's .env) to curl's -o target.
    tool("curl", '''printf "curl %s\\n" "$*" >> "$CALLS"
case "$*" in *setup.runtipi.io*)
  out=""; prev=""; for a in "$@"; do [ "$prev" = -o ] && out="$a"; prev="$a"; done
  printf '#!/bin/bash\\nmkdir -p runtipi && echo NGINX_PORT=8480 > runtipi/.env && echo "services: {}" > runtipi/docker-compose.yml\\n' > "$out"
  exit 0;; esac
[ "${HEALTH:-up}" = up ]
''')
    tool("tailscale", '''printf "tailscale %s\\n" "$*" >> "$CALLS"
[ "$1" = status ] && echo '{"BackendState":"Running"}'
''')
    tool("git", "exit 0\n")
    tool("sleep", "exit 0\n")
    # The stand-in `wam`: logs the call; `bootstrap` prints $BOOTSTRAP_OUT (default: every app installed).
    tool("python3", '''if [ "${1:-}" = -c ]; then
  case "${2:-}" in
    'from wam.approval_custody import require_custody; require_custody()')
      printf 'custody require\\n' >> "$CALLS"; exit "${CUSTODY_RC:-0}";;
    'from wam.approval_custody import restart_worker; restart_worker()')
      printf 'custody restart\\n' >> "$CALLS"; exit 0;;
    'import socket; socket.create_connection(("127.0.0.1",7792),timeout=5).close()')
      printf 'custody activate\\n' >> "$CALLS"; exit 0;;
  esac
fi
if [ "${1:-}" = -m ] && [ "${2:-}" = wam ]; then
  printf "wam %s\\n" "$*" >> "$CALLS"
  if [ "${3:-}" = bootstrap ]; then
    if [ -n "${BOOTSTRAP_OUT:-}" ]; then printf '%s\\n' "$BOOTSTRAP_OUT"; else echo '{"installed": [], "failed": {}}'; fi
  fi
  if [ "${3:-}" = troubleshooter ]; then exit "${TROUBLESHOOTER_RC:-0}"; fi
  if [ "${3:-}" = recover ]; then
    if [ -n "${RECOVER_OUT:-}" ]; then printf '%s\\n' "$RECOVER_OUT"; else echo '{"started": [], "failed": {}, "core": []}'; fi
  fi
  exit 0
fi
exec "PYEXE" "$@"
'''.replace("PYEXE", sys.executable))
    env = {"PATH": str(bin_dir), "HOME": str(home), "USER": "sandbox", "TMPDIR": str(tmp_path),
           "CALLS": str(calls), "DOCKER_ACCESS": str(int(docker_access)), "RUNTIPI_ROOT": str(root),
           "PYTHONDONTWRITEBYTECODE": "1", **extra_env}
    if before:
        before(home)
    p = subprocess.run(["bash", str(REPO / "install.sh")], env=env, capture_output=True, text=True, timeout=60)
    outcome_file = home / ".wizard-os/install-outcome.json"
    outcome = json.loads(outcome_file.read_text()) if outcome_file.is_file() else None
    return p, home, root, calls, outcome


@pytest.mark.parametrize('image', ['privsep-wam:595523fb', None])
def test_explicit_backend_pin_or_default_reaches_real_install_entry(tmp_path, image):
    expected = image or 'ghcr.io/humanitylabs-org/wizard-app-manager:latest'
    p, _, root, calls, _ = sandbox(tmp_path, **({'WAM_IMAGE': image} if image else {}))
    assert p.returncode == 0, p.stdout + p.stderr
    log = calls.read_text()
    assert 'image inspect ' + expected in log and 'pull ' + expected in log
    assert 'image: ' + expected in (root/'user-config/tipi-compose.yml').read_text()
    if image:
        assert 'wizard-app-manager:latest' not in log


def test_missing_custody_never_stops_old_listener_or_links_agent(tmp_path):
    p, home, root, calls, outcome = sandbox(tmp_path, CUSTODY_RC="1")
    assert p.returncode != 0
    log = calls.read_text()
    assert 'custody require' in log
    assert 'disable --now wizard-app-manager-api.service' not in log
    assert 'custody restart' not in log and 'custody activate' not in log
    assert 'wam -m wam bootstrap' not in log
    assert not (home / '.config/systemd/user/wizard-app-manager-api.service').exists()


@pytest.mark.parametrize("docker_access", [True, False])
def test_existing_install_uses_consistent_docker_privileges(tmp_path, docker_access):
    p, home, root, calls, outcome = sandbox(tmp_path, docker_access)
    if not docker_access:
        assert p.returncode == 2, p.stdout + p.stderr
        assert 'administrator to run host preparation' in p.stderr
        assert 'sudo -n docker' not in calls.read_text()
        return
    assert p.returncode == 0, p.stdout + p.stderr
    log = calls.read_text().splitlines()
    docker_calls = [line for line in log if line.startswith("docker ")]
    assert docker_calls[0] == "docker sudo=0 info"
    actual = [line.split(" ", 2)[2] for line in docker_calls[1:]]
    assert actual[0] == ("compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d --no-recreate "
                         "--wait runtipi-db runtipi-queue")
    assert actual[1].startswith("exec runtipi-db psql ") and "status = 'running'" in actual[1]
    assert actual[2].startswith("exec runtipi-db psql ")
    assert actual[3:] == [
        "image inspect ghcr.io/humanitylabs-org/wizard-app-manager:latest",
        "pull ghcr.io/humanitylabs-org/wizard-app-manager:latest",
        "compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d --no-deps --wait runtipi",
        "compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d --no-recreate --no-deps "
        "runtipi-reverse-proxy",
        "rm -f wizard-store wizard-skin",
    ]
    privilege = "0" if docker_access else "1"
    assert all(line.startswith(f"docker sudo={privilege} ") for line in docker_calls[1:])
    assert "tailscale up" not in "\n".join(log)
    assert any("is-active --quiet tailscaled" in line for line in log)
    assert not any("enable --now tailscaled" in line for line in log)
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["state"] == "Running"
    assert log.index("tailscale status --json") < log.index("docker sudo=0 info")
    compose = (root / "user-config/tipi-compose.yml").read_text()
    assert "ports: !override" in compose and "127.0.0.1:8480:80" in compose
    assert "image: ghcr.io/humanitylabs-org/wizard-app-manager:latest" in compose
    assert any(line.startswith("wam -m wam bootstrap") for line in log)
    # wizard-os#28: a clean run says so, and records it
    assert outcome is not None
    assert (outcome["status"], outcome["failed"], outcome["degraded"]) == ("ok", [], [])
    assert "Wizard OS is installed (box-only" in p.stdout


# ---- wizard-os#28: the installer never reports success over a failed required step

def test_a_failed_link_fails_the_install_and_leaves_hermes_alone(tmp_path):
    # Alongside mode with a Hermes home that has no config: the real `wizard link` refuses, so the install fails.
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    (hermes / "SOUL.md").write_text("sentinel\n")
    p, home, root, calls, outcome = sandbox(tmp_path, WIZARD_MODE="alongside", HERMES_HOME=str(hermes))
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["status"] == "failed"
    assert any("'wizard link' failed" in f for f in outcome["failed"])
    assert "Wizard OS did NOT finish installing" in p.stderr
    assert "Wizard OS is installed" not in p.stdout
    assert sorted(x.name for x in hermes.iterdir()) == ["SOUL.md"]   # nothing written into the owner's Hermes


def test_failed_default_apps_are_reported_as_degraded(tmp_path):
    boot = json.dumps({"installed": ["files"], "failed": {"transcriber": "image pull denied"}})
    p, home, root, calls, outcome = sandbox(tmp_path, BOOTSTRAP_OUT=boot)
    assert p.returncode == 0, p.stdout + p.stderr        # default: unattended image builds keep going
    assert outcome["status"] == "degraded"
    assert outcome["degraded"] == ["some default apps did not install: transcriber: image pull denied"]
    assert "not everything is working" in p.stdout and "transcriber: image pull denied" in p.stdout
    assert "Wizard OS is installed (box-only" not in p.stdout
    p, *_ = sandbox(tmp_path / "strict", BOOTSTRAP_OUT=boot, WIZARD_STRICT="1")
    assert p.returncode == 3


def test_an_app_manager_that_never_answers_fails_the_install(tmp_path):
    p, home, root, calls, outcome = sandbox(tmp_path, HEALTH="down", WIZARD_HEALTH_TRIES="2")
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["status"] == "failed"
    assert any("health check" in f for f in outcome["failed"])
    assert not any(line.startswith("wam -m wam bootstrap") for line in calls.read_text().splitlines())


def owner_hermes(tmp_path):
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    (hermes / "config.yaml").write_text("model:\n  default: some/model\n")
    (hermes / ".env").write_text("OPENROUTER_API_KEY=x\n")
    return hermes, {p.name: p.read_bytes() for p in hermes.iterdir()}


def test_a_failed_install_never_links_the_owners_hermes(tmp_path):
    # PR19 review P2: App Manager down -> FAILED; alongside must not link Hermes to a gateway that isn't up.
    hermes, before = owner_hermes(tmp_path)
    p, home, root, calls, outcome = sandbox(tmp_path, HEALTH="down", WIZARD_HEALTH_TRIES="2",
                                            WIZARD_MODE="alongside", HERMES_HOME=str(hermes))
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["status"] == "failed"
    assert "your Hermes" in p.stdout and "was not touched" in p.stdout
    log = calls.read_text().splitlines()
    assert not any(line.startswith("wam -m wam alongside") for line in log)
    assert {x.name: x.read_bytes() for x in hermes.iterdir()} == before
    assert not (home / ".wizard-os/linked.json").exists()


def test_a_failed_shell_or_agent_service_fails_the_install(tmp_path):
    # PR19 review P2: the shell / agent-service spots are the only way to use Wizard OS.
    # read the spot defaults, so the test follows wam/defaults.json (shell + agent-service are separate apps now)
    spots = json.loads((REPO / "wam/defaults.json").read_text())["slots"]
    shell, service = spots["shell"]["app"], spots["agent-service"]["app"]
    boot = json.dumps({"installed": ["files"], "failed": {shell: "image pull denied", "browser": "timeout"}})
    p, home, root, calls, outcome = sandbox(tmp_path, BOOTSTRAP_OUT=boot)
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["status"] == "failed"
    assert outcome["failed"] == ["the Wizard shell / Agent Service did not install, so Wizard OS can't be used: "
                                 f"{shell}: image pull denied"]
    assert outcome["degraded"] == ["some default apps did not install: browser: timeout"]
    assert "Wizard OS did NOT finish installing" in p.stderr
    boot = json.dumps({"installed": [shell], "failed": {service: "health check timed out"}})
    p, home, root, calls, outcome = sandbox(tmp_path / "service", BOOTSTRAP_OUT=boot)
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["failed"] == ["the Wizard shell / Agent Service did not install, so Wizard OS can't be used: "
                                 f"{service}: health check timed out"]


def test_the_outcome_path_is_printed_only_when_it_was_written(tmp_path):
    # PR19 review P3: the outcome write is best-effort; don't point the owner at a file that isn't there.
    def block(home):
        (home / ".wizard-os/install-outcome.json").mkdir(parents=True)   # a directory: the write fails
    p, home, root, calls, outcome = sandbox(tmp_path, before=block, HEALTH="down", WIZARD_HEALTH_TRIES="2")
    assert p.returncode == 1, p.stdout + p.stderr
    assert "Wizard OS did NOT finish installing" in p.stderr
    assert "Recorded in" not in p.stderr
    p, *_ = sandbox(tmp_path / "ok", HEALTH="down", WIZARD_HEALTH_TRIES="2")
    assert "Recorded in" in p.stderr


# ---- An agent's own bundled python3 (no PyYAML) first on PATH must not break the install (demo root run, 0.3.0)

def test_a_python3_without_yaml_first_on_path_is_skipped(tmp_path):
    system = tmp_path / "system-python3"
    block = tmp_path / "noyaml"

    def agent_python_first(home):
        (tmp_path / "bin" / "python3").rename(system)    # the sandbox's stand-in python3 becomes "the system one"
        block.mkdir()
        (block / "yaml.py").write_text("raise ImportError('No module named yaml')\n")
        bad = tmp_path / "bin" / "python3"               # a real Python that cannot import yaml, like Hermes' bundled one
        bad.write_text('#!/bin/bash\nprintf "agent-python %s\\n" "$*" >> "$CALLS"\n'
                       f'PYTHONPATH="{block}" exec "{sys.executable}" "$@"\n')
        bad.chmod(0o755)

    p, home, root, calls, outcome = sandbox(tmp_path, before=agent_python_first, WIZARD_PYTHON=str(system))
    assert p.returncode == 0, p.stdout + p.stderr
    log = calls.read_text()
    assert "agent-python -m wam " not in log            # no wam work ran under the yaml-less python
    assert "wam " in log
    assert f'exec "{system}" -m wam' in (home / ".local/bin/wam").read_text()
    assert not (home / ".config/systemd/user/wizard-app-manager-api.service").exists()
    assert 'custody require' in log and 'custody activate' in log
    assert (home / ".local/bin/wizard").read_text().splitlines()[0] == f"#!{system}"


# ---- wam#95: records survive, base and app containers are gone

def test_a_rerun_recreates_missing_base_containers_before_the_app_manager(tmp_path):
    p, home, root, calls, outcome = sandbox(tmp_path, RUNNING_ROWS="files:wizardapps\nwizard-ux:wizardapps\n")
    assert p.returncode == 0, p.stdout + p.stderr
    log = calls.read_text().splitlines()
    base = next(i for i, l in enumerate(log) if l.endswith("--no-recreate --wait runtipi-db runtipi-queue"))
    manager = next(i for i, l in enumerate(log) if l.endswith("up -d --no-deps --wait runtipi"))
    proxy = next(i for i, l in enumerate(log) if l.endswith("--no-recreate --no-deps runtipi-reverse-proxy"))
    intent = next(i for i, l in enumerate(log) if "status = 'running'" in l)
    assert base < intent < manager < proxy
    # Installed apps whose containers vanished are started after the App Manager answers, before bootstrap; only the
    # apps recorded running before the App Manager's status sync could mark them stopped.
    rec = next(i for i, l in enumerate(log) if l.startswith("wam -m wam recover"))
    assert log[rec] == "wam -m wam recover --running files:wizardapps --running wizard-ux:wizardapps"
    assert rec < next(i for i, l in enumerate(log) if l.startswith("wam -m wam bootstrap"))
    assert outcome["status"] == "ok"


def test_partial_base_waits_for_manager_static_config_before_first_proxy_start(tmp_path):
    # Upstream may leave .env and Compose files after a failed Hub pull, without
    # ever starting the backend that writes Traefik's static entrypoints.
    p, home, _, calls, outcome = sandbox(tmp_path, REQUIRE_MANAGER_HEALTH='1')
    assert p.returncode == 0, p.stdout + p.stderr
    assert (home / 'manager-initialized').is_file()
    log = calls.read_text()
    assert log.index('--no-deps --wait runtipi') < log.index('--no-deps runtipi-reverse-proxy')
    assert outcome is not None and outcome['status'] == 'ok'


def test_unhealthy_manager_never_starts_a_configless_proxy(tmp_path):
    p, _, _, calls, _ = sandbox(tmp_path, MANAGER_WAIT_RC='17', REQUIRE_MANAGER_HEALTH='1')
    assert p.returncode == 17, p.stdout + p.stderr
    log = calls.read_text()
    assert '--wait runtipi' in log
    assert '--no-deps runtipi-reverse-proxy' not in log
    assert 'wam -m wam bootstrap' not in log


def test_failed_core_recovery_fails_the_install_and_other_apps_degrade(tmp_path):
    out = json.dumps({"started": [], "failed": {"wizard-ux": "start did not finish", "files": "start_error"},
                      "core": ["wizard-ux", "wizard-agent-service"]})
    p, home, root, calls, outcome = sandbox(tmp_path, RECOVER_OUT=out)
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["failed"] == ["the Wizard shell / Agent Service containers could not be restarted: "
                                 "wizard-ux: start did not finish"]
    assert outcome["degraded"] == ["some installed apps could not be restarted: files: start_error"]


def test_recovery_is_skipped_when_the_app_manager_never_answers(tmp_path):
    p, home, root, calls, outcome = sandbox(tmp_path, HEALTH="down")
    assert not any(l.startswith("wam -m wam recover") for l in calls.read_text().splitlines())


def test_every_install_sets_up_the_native_troubleshooter_after_the_links(tmp_path):
    p, home, root, calls, outcome = sandbox(tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    log = [l for l in calls.read_text().splitlines() if l.startswith("wam ")]
    assert "wam -m wam troubleshooter" in log
    assert log.index("wam -m wam links") < log.index("wam -m wam troubleshooter")


def test_a_troubleshooter_problem_never_fails_the_install(tmp_path):
    p, home, root, calls, outcome = sandbox(tmp_path, TROUBLESHOOTER_RC="1")
    assert p.returncode == 0, p.stdout + p.stderr
    assert outcome["status"] == "ok"
    assert "the Troubleshooter is not fully set up" in p.stdout


def test_a_failed_install_does_not_set_up_the_troubleshooter(tmp_path):
    p, home, root, calls, outcome = sandbox(tmp_path, HEALTH="down", WIZARD_HEALTH_TRIES="1")
    assert p.returncode == 1
    assert "wam -m wam troubleshooter" not in calls.read_text()


def test_first_install_downloads_runtipi_setup_to_a_private_temp_dir(tmp_path):
    # Demo-box fresh install (0.3.23): a root-owned /tmp/runtipi-setup.sh left by an earlier install made curl exit 23
    # for the new wizard user, and the whole install failed before Runtipi existed. The download must never use a
    # fixed shared path.
    def no_base(home):
        (home / "runtipi/.env").unlink()
        (home / "runtipi/docker-compose.yml").unlink()
    p, home, root, calls, outcome = sandbox(tmp_path, before=no_base)
    assert p.returncode == 0, p.stdout + p.stderr
    log = calls.read_text().splitlines()
    dl = next(l for l in log if "setup.runtipi.io" in l)
    target = dl.split(" -o ", 1)[1]
    assert not target.startswith("/tmp/runtipi-setup.sh")
    assert target.startswith(str(tmp_path)), target          # mktemp -d under this run's TMPDIR
    assert any(l.startswith("sudo -n bash " + target) for l in log)
    assert any(l.startswith("chown 1000:1000 ") for l in log), "the base is handed to the owner"
    assert (root / ".env").is_file()
    assert not Path(target).parent.exists(), "the private setup dir is removed afterwards"
    assert outcome["status"] == "ok"


def test_fresh_install_sets_up_the_store_routes_after_links(tmp_path):
    """Fresh boxes: the Store's store-data / store-install routes are set up after `wam links` (alongside boxes do it
    in their own block). Before: only alongside ran `wam store-data`, so the fresh Store had no installed status."""
    p, home, root, calls, outcome = sandbox(tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    log = [line for line in calls.read_text().splitlines() if line.startswith("wam ")]
    names = [line.split()[3] for line in log]
    assert "store-data" in names, names
    assert names.index("links") < names.index("store-data") < names.index("troubleshooter")
