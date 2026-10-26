#!/usr/bin/env python3
"""Disposable-guest bootstrap QA, not exact-release acceptance.

Select a pushed candidate CLI commit using a separately recorded manifest copy.
Keep every image/default-app pin unchanged and WIZARD_STRICT=1. Restore the
original source expectation in release.json afterward: that check MUST be red.
Run as a non-root sudo user in an isolated VM; never on an owner's live install.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def stage(original, candidate, evidence):
    raw = original.read_bytes()
    release = json.loads(raw)
    original_sha = release['components']['wizard-app-manager']['commit']
    if original_sha == candidate:
        raise ValueError('candidate must differ from the original release source')
    # Only this single field changes. Original file and all component/image pins are retained.
    release['components']['wizard-app-manager']['commit'] = candidate
    evidence.mkdir(parents=True, exist_ok=False)
    (evidence / 'original-release.json').write_bytes(raw)
    releases = evidence / 'candidate-manifests'
    releases.mkdir()
    (releases / 'hermes-lock.json').write_bytes((Path(__file__).resolve().parents[1] / 'releases/hermes-lock.json').read_bytes())
    (releases / (release['version'] + '.json')).write_text(json.dumps(release, indent=2) + '\n')
    record = {'kind': 'candidate-bootstrap-not-release-acceptance', 'release_exact': False,
              'original_manifest_sha256': hashlib.sha256(raw).hexdigest(),
              'source_exception': {'expected': original_sha, 'candidate': candidate, 'ok': False},
              'strict': True, 'status': 'pending'}
    (evidence / 'candidate-exception.json').write_text(json.dumps(record, indent=2) + '\n')
    return release, record, releases


def record_readback(record, receipt, actual):
    record['source_exception']['actual'] = actual
    source = next((c for c in receipt.get('checks', []) if c['name'] == 'wizard-app-manager commit'), None)
    record['candidate_checks_passed'] = (receipt.get('status') == 'pinned' and
                                        source is not None and source.get('ok') is True and
                                        source.get('observed') == actual and
                                        actual == record['source_exception']['candidate'])
    record['status'] = 'candidate-checked' if record['candidate_checks_passed'] else 'failed'
    # Do not leave the ordinary release receipt falsely green against the original release.
    if source is None:
        source = {'name': 'wizard-app-manager commit', 'core': True}
        receipt.setdefault('checks', []).append(source)
    source.update(expected=record['source_exception']['expected'], observed=actual, ok=False)
    receipt.update(status='mismatch', release_exact=False, candidate_exception=record)
    return receipt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True, help='unaltered original release JSON')
    p.add_argument('--evidence', type=Path, required=True, help='new directory; refuses reuse')
    args = p.parse_args()
    if os.getuid() == 0:
        p.error('run as the disposable guest sudo user, not root')
    repo = Path(__file__).resolve().parents[1]
    def git(*argv, cwd=repo):
        return subprocess.check_output(['git', '-C', str(cwd), *argv], text=True).strip()
    candidate = git('rev-parse', 'HEAD')
    if git('status', '--porcelain'):
        p.error('candidate checkout must be clean')
    remote = git('ls-remote', 'origin', 'refs/heads/integration/e2e-022-bootstrap')
    if not remote.startswith(candidate + '\t'):
        p.error('candidate HEAD must be the pushed integration/e2e-022-bootstrap branch tip')
    release, record, releases = stage(args.manifest.resolve(), candidate, args.evidence.resolve())
    home = Path.home()
    installed = home / 'wizard-app-manager'
    env = {**os.environ, 'WIZARD_RELEASE_BASE': releases.as_uri(), 'WIZARD_RELEASE': release['version'],
           'WIZARD_STRICT': '1', 'WIZARD_PRIVATE_ACCESS': 'none', 'WAM_DIR': str(installed)}
    # Explicit existing overrides must not weaken this candidate experiment.
    for key in ('WAM_REF', 'WAM_IMAGE', 'WIZARD_UX_IMAGE', 'WIZARD_AGENT_SERVICE_IMAGE', 'WIZARD_SHELL_IMAGE',
                'WIZARD_RELEASE_PINS', 'WAM_REPO'):
        env.pop(key, None)
    print('CANDIDATE TEST ONLY: original WAM source check will remain a mismatch.', flush=True)
    rc = subprocess.run(['bash', str(repo / 'get.sh')], env=env).returncode
    record['installer_exit_code'] = rc
    receipt_path = home / '.wizard-os/release.json'
    receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
    (args.evidence / 'candidate-release.json').write_text(json.dumps(receipt, indent=2) + '\n')
    actual = git('rev-parse', 'HEAD', cwd=installed) if (installed / '.git').exists() else 'not installed'
    receipt = record_readback(record, receipt, actual)
    if rc:
        record['candidate_checks_passed'] = False
        record['status'] = 'failed'
    (args.evidence / 'candidate-exception.json').write_text(json.dumps(record, indent=2) + '\n')
    (args.evidence / 'original-release-readback.json').write_text(json.dumps(receipt, indent=2) + '\n')
    if receipt_path.is_file():
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(record, indent=2), flush=True)
    return rc or (0 if record['candidate_checks_passed'] else 1)


if __name__ == '__main__':
    sys.exit(main())
