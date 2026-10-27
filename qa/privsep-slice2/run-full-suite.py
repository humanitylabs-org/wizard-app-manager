#!/usr/bin/env python3
"""Run the complete suite in an indexed disposable export on the chosen worker.

Exit 0 means exact parity with the independently recorded sole baseline failure,
not a green test suite. Preserve pytest's real exit and unmodified log in JSON.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


EXPECTED = 'tests/test_chats_views.py::test_typescript_public_index_path_has_identical_seal'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', required=True)
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(args.manifest.read_text())
    assert manifest['source_revision'] == args.source_revision
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
    # Test portability guards enumerate git's index; a bare export is insufficient.
    subprocess.run(['git', 'ls-files', '--error-unmatch', 'wam/host_policy.py'], cwd=root, check=True, capture_output=True)
    baseline = root / 'qa/privsep-slice2/forum-baseline.log'
    assert re.findall(r'^FAILED (\S+)', baseline.read_text(), re.M) == [EXPECTED]
    args.output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env['PATH'] = str(Path(args.python).parent) + os.pathsep + env.get('PATH', '')
    start = time.monotonic()
    log = args.output / 'pytest.log'
    with log.open('w') as stream:
        result = subprocess.run([args.python, '-m', 'pytest', '-q', 'tests'], cwd=root, env=env,
                                stdout=stream, stderr=subprocess.STDOUT, timeout=1200)
    text = log.read_text()
    failures = re.findall(r'^FAILED (\S+)', text, re.M)
    summaries = [line for line in text.splitlines() if re.search(r'\b\d+ passed\b.*\bin [\d.]+s\b', line)]
    report = {'source_revision': args.source_revision, 'pytest_exit_code': result.returncode,
              'elapsed_seconds': round(time.monotonic() - start, 2), 'failures': failures,
              'summary': summaries[-1] if summaries else None,
              'baseline_failure': EXPECTED, 'baseline_sha256': hashlib.sha256(baseline.read_bytes()).hexdigest(),
              'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest(),
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'baseline_parity': result.returncode == 1 and failures == [EXPECTED] and bool(summaries)}
    (args.output / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['baseline_parity'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
