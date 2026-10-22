"""The detached dispatch is terminal even when a projected administrator runner returns normally."""
from pathlib import Path
import subprocess

GET = Path(__file__).resolve().parents[1] / 'get.sh'


def test_returned_administrator_runner_cannot_fall_through_to_a_second_install():
    source = GET.read_text()
    start = source.index('case "${1:-}" in\n  start)')
    dispatch = source[start:source.index('# ---- end detached install ----', start)]
    script = 'install_runner() { printf "runner-finished\\n"; return 0; }\n' + dispatch
    script += '\nprintf "ERROR-second-install-fallthrough\\n"\n'
    result = subprocess.run(['bash', '-c', script, 'sandbox', '__runner'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'runner-finished\n'
