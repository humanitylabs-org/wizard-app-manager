"""The deferred Hermes restart must outlast a live chat turn.

Hermes >= 0.22 refuses `gateway restart` (exit 1) while a chat turn is live. An agent-run install schedules the
restart while the installing agent's own turn is still live, so a single attempt left Wizard OS linked but with the
Hermes API never started (demo box, 26 Oct 2026: shell showed "Your agent (Hermes) isn't responding").
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _job(script: str, hermes: Path, home: Path) -> str:
    src = (ROOT / script).read_text()
    m = re.search(r"^hermes_restart_job\(\) \{.*?^\}\n", src, re.S | re.M)
    assert m, f"{script} has no hermes_restart_job"
    out = subprocess.run(
        ["bash", "-c", m.group(0) + f'hermes_restart_job "{hermes}"'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "WIZARD_RESTART_RETRY_SECS": "0",
             "WIZARD_RESTART_TRIES": "5"},
        capture_output=True, text=True, check=True)
    return out.stdout


def _fake_hermes(tmp: Path, refusals: int) -> Path:
    log, count = tmp / "calls.log", tmp / "count"
    count.write_text("0")
    h = tmp / "hermes"
    h.write_text(f"""#!/bin/sh
echo "$*" >> '{log}'
n=$(cat '{count}'); echo $((n+1)) > '{count}'
[ "$3" = --yes ] && exit 0
[ "$n" -lt {refusals} ] && {{ echo "Refusing to restart the gateway: 1 live session(s) would be interrupted."; exit 1; }}
exit 0
""")
    h.chmod(0o755)
    return h


@pytest.mark.parametrize("script", ["install.sh", "get.sh"])
def test_restart_waits_for_the_live_turn_then_restarts(tmp_path, script):
    h = _fake_hermes(tmp_path, refusals=2)
    job = _job(script, h, tmp_path)
    subprocess.run(["bash", "-c", job], check=True, capture_output=True, text=True)
    assert (tmp_path / "calls.log").read_text().splitlines() == ["gateway restart"] * 3


@pytest.mark.parametrize("script", ["install.sh", "get.sh"])
def test_restart_interrupts_only_after_the_wait_runs_out(tmp_path, script):
    h = _fake_hermes(tmp_path, refusals=99)
    job = _job(script, h, tmp_path)
    subprocess.run(["bash", "-c", job], check=True, capture_output=True, text=True)
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert calls == ["gateway restart"] * 5 + ["gateway restart --yes"]


@pytest.mark.parametrize("script", ["install.sh", "get.sh"])
def test_old_hermes_restarts_once(tmp_path, script):
    h = _fake_hermes(tmp_path, refusals=0)
    subprocess.run(["bash", "-c", _job(script, h, tmp_path)], check=True, capture_output=True, text=True)
    assert (tmp_path / "calls.log").read_text().splitlines() == ["gateway restart"]


@pytest.mark.parametrize("script", ["install.sh", "get.sh"])
def test_scheduled_job_uses_the_retrying_restart(script):
    src = (ROOT / script).read_text()
    assert re.search(r'^\s*job="[^\n]*\$\(hermes_restart_job "\$(hermes|real)"\)', src, re.M), \
        "scheduled job bypasses the retry"
    assert "' gateway restart; sleep 20" not in src


def _watchdog(tmp: Path, hermes: Path, healthy_after: int) -> list[str]:
    """Run get.sh's watchdog job with a fake curl that answers after `healthy_after` health probes."""
    src = (ROOT / "get.sh").read_text()
    m = re.search(r"^hermes_api_watchdog_job\(\) \{.*?^\}\n", src, re.S | re.M)
    assert m, "get.sh has no hermes_api_watchdog_job"
    env = {"HOME": str(tmp), "PATH": f"{tmp}/bin:/usr/bin:/bin", "WIZARD_RESTART_RETRY_SECS": "0",
           "WIZARD_RESTART_TRIES": "5"}
    job = subprocess.run(["bash", "-c", m.group(0) + f'hermes_api_watchdog_job "{hermes}" http://127.0.0.1:8642'],
                         env=env, capture_output=True, text=True, check=True).stdout
    (tmp / "bin").mkdir()
    (tmp / "probes").write_text("0")
    curl = tmp / "bin/curl"
    curl.write_text(f"""#!/bin/sh
n=$(cat '{tmp}/probes'); echo $((n+1)) > '{tmp}/probes'
[ "$n" -ge {healthy_after} ]
""")
    curl.chmod(0o755)
    subprocess.run(["bash", "-c", job], env=env, check=True, capture_output=True, text=True)
    log = tmp / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def test_watchdog_leaves_an_answering_hermes_alone(tmp_path):
    assert _watchdog(tmp_path, _fake_hermes(tmp_path, refusals=0), healthy_after=0) == []


def test_watchdog_restarts_a_hermes_left_down_by_a_refused_restart(tmp_path):
    # API down; Hermes refuses twice (installing agent's turn still live), then restarts and the API answers.
    calls = _watchdog(tmp_path, _fake_hermes(tmp_path, refusals=2), healthy_after=4)
    assert calls == ["gateway restart"] * 3


def test_watchdog_interrupts_only_after_the_wait_runs_out(tmp_path):
    calls = _watchdog(tmp_path, _fake_hermes(tmp_path, refusals=99), healthy_after=999)
    assert calls == ["gateway restart"] * 5 + ["gateway restart --yes"]


def test_watchdog_is_scheduled_after_every_successful_agent_install():
    src = (ROOT / "get.sh").read_text()
    assert '[ "$rc" -ne 0 ] || api_watchdog' in src
    assert 'then deferred_restart; api_watchdog; fi' in src
