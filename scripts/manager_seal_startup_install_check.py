#!/usr/bin/env python3
"""Tiny offline exact-method regressions; no dependency install or runtime start.

Candidate and pinned pre-fix sources execute the same Node method tests. The
facades are test doubles, NOT installed Nest/HTTP/DB/Rabbit acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

BASELINE = '118608e135ffa1d7f5114a2e9b7a059c50c04cf2'
FILES = ('packages/backend/src/app.service.ts',
         'packages/backend/src/modules/app-lifecycle/handlers/install-app.handler.ts')
TEST = 'scripts/tests/test_manager_seal_startup_install.mjs'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def check(root):
    root = Path(root).resolve()
    node = subprocess.check_output(['node', '--version'], text=True).strip()
    candidate_hashes = {rel: sha((root/rel).read_bytes()) for rel in FILES}
    command = ['node', '--test', '--test-reporter=tap', str(root/TEST)]
    candidate = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15)
    assert candidate.returncode == 0, candidate.stdout + candidate.stderr
    assert re.search(r'^# pass 7$', candidate.stdout, re.M)
    assert re.search(r'^# fail 0$', candidate.stdout, re.M)
    scratch = os.environ.get('TMPDIR')
    assert scratch, 'set TMPDIR to the approved task scratch directory'
    with tempfile.TemporaryDirectory(prefix='manager-safety-negative-', dir=scratch) as temp:
        baseline_hashes = {}
        for rel in FILES:
            data = subprocess.check_output(['git', 'show', f'{BASELINE}:{rel}'], cwd=root, timeout=10)
            target = Path(temp)/rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            baseline_hashes[rel] = sha(data)
        baseline = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15,
                                  env={**os.environ, 'MANAGER_SEAL_SAFETY_ROOT': temp})
    assert baseline.returncode == 1, baseline.stdout + baseline.stderr
    failed = re.findall(r'^not ok \d+ - (.*)$', baseline.stdout, re.M)
    assert failed == [
        'bootstrap: no daemon-wide mutation; migration, assets and TLS still complete',
        'install: HTTP form true survives omitted override; queue and final status agree',
    ], failed
    assert re.search(r'^# pass 5$', baseline.stdout, re.M)
    assert re.search(r'^# fail 2$', baseline.stdout, re.M)
    assert 'bootstrap invoked daemon-wide network prune' in baseline.stdout
    assert 'validated form skipRun lost before queue' in baseline.stdout
    assert "error: 'bootstrap invoked daemon-wide network prune'" in baseline.stdout
    assert 'actual: false' in baseline.stdout and 'expected: true' in baseline.stdout
    return {
        'kind': 'offline exact-source methods with dependency facades; NOT installed acceptance',
        'baseline': BASELINE, 'node': node,
        'source_sha256': candidate_hashes, 'baseline_source_sha256': baseline_hashes,
        'test_sha256': sha((root/TEST).read_bytes()), 'runner_sha256': sha(Path(__file__).read_bytes()),
        'candidate': {'exit': candidate.returncode, 'pass': 7, 'fail': 0, 'tap': candidate.stdout},
        'baseline_negative': {'exit': baseline.returncode, 'pass': 5, 'fail': 2,
                              'specific_failures': failed, 'tap': baseline.stdout},
        'installed_pass': False, 'RUNTIPI_ADMITTED': False,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = check(args.root)
    with Path(args.output).open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
    print(json.dumps({key: result[key] for key in ('kind', 'baseline', 'source_sha256')}))
    print('candidate 7/7; pinned baseline 5 pass + exactly 2 expected seam failures; installed NOT run')
