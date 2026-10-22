"""Progress projection is a real unprivileged process; marker text below is deliberately synthetic."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

GET = Path(__file__).resolve().parents[1] / 'get.sh'


def test_projected_status_is_owner_readable_locked_and_reports_the_real_exit(tmp_path):
    code = GET.read_text().split("STATUS_PY='\n", 1)[1].split("\n'\ninstall_status()", 1)[0]
    home = tmp_path / 'owner'; state = home / '.wizard-os'; state.mkdir(parents=True)
    status, log = state / 'install-status.json', state / 'install.log'
    process = subprocess.Popen([sys.executable, '-c', code, str(status), str(log), 'stream', str(os.getpid())],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdin is not None and process.stderr is not None
    try:
        process.stdin.write('==> Named owner host preparation\n[wizard-os] owner-action: https://login.tailscale.com/a/fixture-only\n')
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if status.exists() and json.loads(status.read_text()).get('state') == 'needs_owner':
                break
            time.sleep(.02)
        projected = json.loads(status.read_text())
        assert projected['step'] == 'Named owner host preparation'
        assert projected['state'] == 'needs_owner'
        assert status.stat().st_uid == os.getuid() and status.stat().st_mode & 0o777 == 0o600
        with (state / 'install.lock').open('r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError('the named owner projection must hold the ordinary install lock')
        result = subprocess.run(['bash', str(GET), 'status'], env={**os.environ, 'HOME': str(home)},
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert 'Wizard OS install: needs_owner' in result.stdout
        assert 'https://login.tailscale.com/a/fixture-only' in result.stdout
        process.stdin.close(); process.wait(timeout=5)
        assert process.returncode == 0, process.stderr.read()
        result = subprocess.run([sys.executable, '-c', code, str(status), str(log), 'end', '7'],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(status.read_text())['state'] == 'failed'
        assert json.loads(status.read_text())['exit_code'] == 7
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
