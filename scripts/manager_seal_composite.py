"""Offline, exact-input aggregate. No Docker, installs, tests or source writes.

Run once into a NEW output path; original failed receipts/logs remain unchanged.
Raw archives and scoped traces stay outside Git. This is derived evidence, not a
replacement receipt from the failed worker or fresh installed/API acceptance.
"""
import argparse
import hashlib
import io
import json
import os
import stat
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

from manager_seal_baseline import baseline_failure, FIXTURE
from manager_seal_bundle import SAVED_BUNDLE, regular_bytes
from manager_seal_continuation import PINS as CANDIDATE_PINS, source_closure

SOURCE = '3cfecc950ebbd91b2bbc0cd62ba0e834b572cd49'
MANIFEST = 'e6ffbbdc8fd4b2dba9381a2ef5c93e0bfaf1212cd68668cd3972bc869c96d475'
PINS = {
    'docker-receipt.json': 'c83bd3231edbc8637720978a0712773679515d90e01208161a0b033b7cb165d9',
    'docker-artifacts.tar.gz': '91838ebcdb3f243141f92897a2a194934318ca2367d2045225e53afce92b2c4d',
    'docker.log': '902b14e4777e8f22eccb2b87d34e6cc00484cdf72f752a437a4454db17892f19',
}
TRACE = 'baseline-trace/agent-synthetic_seal3cfecc950ebb-events.json'
CANDIDATE_TRACE = 'candidate-trace/agent-synthetic_seal3116f500f4d7-events.json'
DIST = 'source/packages/backend/dist/'
QA_PATHS = {
    'scripts/manager-seal-docker.py', 'scripts/manager_seal_continuation.py', 'scripts/manager_seal_bundle.py',
    'scripts/tests/test_manager_seal_docker.py', 'scripts/manager_seal_baseline.py',
    'scripts/tests/test_manager_seal_baseline.py', 'scripts/manager_seal_composite.py',
    'docs/manager-seal-classifier-composite.json', 'docs/manager-seal-classifier-recovery.md',
    'docs/manager-seal-installed-acceptance-spec.md',
}


def digest(data): return hashlib.sha256(data).hexdigest()


def source_bytes(path):
    metadata = path.lstat()
    if metadata.st_size:
        return regular_bytes(path, 32*1024**2)
    assert stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        actual = os.fstat(stream.fileno())
        assert (actual.st_dev, actual.st_ino, actual.st_size) == (metadata.st_dev, metadata.st_ino, 0)
        assert stream.read(1) == b''
    return b''


def archive(data):
    """In-memory regular members only; no extraction or archive-controlled writes."""
    files = {}; seen = set(); total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tf:
        for member in tf:
            name = member.name
            assert name not in seen and not name.startswith('/') and '\\' not in name
            assert all(p not in ('', '.', '..') for p in name.split('/')) and str(PurePosixPath(name)) == name
            seen.add(name); total += member.size
            assert len(seen) <= 100 and 0 <= member.size <= 32*1024**2 and total <= 64*1024**2, 'archive budget'
            assert member.sparse is None and not member.linkname and not any(k.startswith('GNU.sparse') for k in member.pax_headers)
            assert member.isdir() or member.type in (tarfile.REGTYPE, tarfile.AREGTYPE), 'archive member type'
            if member.isdir():
                assert member.size == 0
                continue
            stream = tf.extractfile(member); assert stream is not None
            with stream: payload = stream.read(member.size+1)
            assert len(payload) == member.size
            files[name] = payload
    return files


def pinned_files(root, pins):
    result = {}
    for name, expected in pins.items():
        data = regular_bytes(root/name, 128*1024**2)
        assert digest(data) == expected, 'saved byte drift: '+name
        result[name] = data
    return result


def verify_artifact(receipt, files):
    assert json.loads(files['inside-result.json']) == receipt['result'], 'inside/outer receipt mismatch'
    for label, metadata in receipt['result']['steps'].items():
        if label == 'dependencies': continue
        payload = files['logs/'+label+'.log']
        assert len(payload) == metadata['bytes'] and digest(payload) == metadata['sha256'], 'step log mismatch'
    for name, metadata in receipt['dependencies'].items():
        payload = files['logs/'+name]
        assert len(payload) == metadata['bytes'] and digest(payload) == metadata['sha256'], 'dependency log mismatch'
    assert receipt['leaked_containers'] == receipt['leaked_networks'] == []
    assert receipt['cleanup_volume_absent'] is True and receipt['reserve_preserved'] is True
    assert receipt['container_state']['ExitCode'] == 1 and receipt['container_state']['OOMKilled'] is False
    for key in ['source_handler_real_docker_pass', 'packaged_api_pass', 'image_built', 'full_docker_acceptance', 'RUNTIPI_ADMITTED']:
        assert receipt[key] is False, 'historical failure labels changed'


def composite(evidence, pinned_source, repo):
    raw = pinned_files(evidence, PINS)
    old_raw = pinned_files(evidence/'candidate', CANDIDATE_PINS)
    manifest_bytes = regular_bytes(pinned_source/'SOURCE.json', 1024**2)
    assert digest(manifest_bytes) == MANIFEST
    manifest = json.loads(manifest_bytes); previous = json.loads(old_raw['SOURCE.json'])
    assert manifest['head'] == SOURCE
    for rel, expected in manifest['files'].items():
        assert digest(source_bytes(pinned_source/rel)) == expected, 'pinned source drift: '+rel
    assert digest(regular_bytes(pinned_source/'baseline.tar', 32*1024**2)) == manifest['baseline_tar_sha256']
    inherited_changes = source_closure(previous, manifest)
    tracked = set(subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=repo).decode().strip('\0').split('\0'))
    changed = {rel for rel in tracked | set(manifest['files']) if not (repo/rel).is_file() or
               digest((repo/rel).read_bytes()) != manifest['files'].get(rel)}
    assert changed <= QA_PATHS, 'production or fixture changed: '+repr(sorted(changed-QA_PATHS))
    # Git-export membership/modes are part of the preserved production closure.
    index = subprocess.check_output(['git', 'ls-files', '-s', '-z'], cwd=repo).decode().strip('\0').split('\0')
    for row in index:
        meta, rel = row.split('\t')
        if rel in manifest['files'] and rel not in QA_PATHS:
            mode = int(meta.split()[0], 8) & 0o777
            assert mode == manifest['file_modes'][rel] == ((pinned_source/rel).stat().st_mode & 0o777), 'export/Git source mode drift'
    receipt = json.loads(raw['docker-receipt.json']); candidate = json.loads(old_raw['docker-receipt.json'])
    assert receipt['source'] == SOURCE and receipt['manifest_sha256'] == MANIFEST
    assert receipt['runner_sha256'] == manifest['files']['scripts/manager-seal-docker.py']
    assert candidate['source'] == previous['head'] and candidate['manifest_sha256'] == CANDIDATE_PINS['SOURCE.json']
    assert candidate['runner_sha256'] == previous['files']['scripts/manager-seal-docker.py']
    assert receipt['artifacts']['sha256'] == PINS['docker-artifacts.tar.gz']
    assert candidate['artifacts']['sha256'] == CANDIDATE_PINS['docker-artifacts.tar.gz']
    files = archive(raw['docker-artifacts.tar.gz']); old = archive(old_raw['docker-artifacts.tar.gz'])
    verify_artifact(receipt, files); verify_artifact(candidate, old)
    result = receipt['result']; prior = candidate['result']
    assert result['source'] == SOURCE and result['manifest_sha256'] == MANIFEST
    assert result['phase'] == 'baseline-specific-failure-validation'
    assert result['error'] == {'exception_type': 'AssertionError', 'frames': [
        {'file': 'manager_seal_continuation.py', 'function': 'inside', 'line': 276},
        {'file': 'manager-seal-docker.py', 'function': 'baseline_failure', 'line': 233}],
        'phase': 'baseline-specific-failure-validation', 'reason': 'assertion_failed'}
    assert result['steps']['baseline-negative']['exit_code'] == 1
    assert all(step['exit_code'] == 0 for label, step in result['steps'].items() if label != 'baseline-negative')
    assert result['baseline_python_version'] == '3.11.2'
    assert b'Ran 44 tests' in files['logs/continuation-focused-harness.log'] and b'\nOK\n' in files['logs/continuation-focused-harness.log']
    baseline_report = json.loads(files['baseline.json'])
    assert {k: baseline_report[k] for k in result['baseline_counts']} == result['baseline_counts']
    baseline = baseline_failure(baseline_report, json.loads(files[TRACE]), (pinned_source/FIXTURE).read_bytes(), 'agent-synthetic_seal3cfecc950ebb')
    # Reuse the exact original, independently parsed machine reports and logs.
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location('composite_runner', repo/'scripts/manager-seal-docker.py')
    assert spec is not None and spec.loader is not None
    runner = module_from_spec(spec); spec.loader.exec_module(runner)
    for name, key, expected in [('candidate.json', 'candidate_counts', 19), ('adjacent.json', 'adjacent_counts', 60)]:
        counts = runner.report_counts(json.loads(old[name]))
        assert counts == prior[key] == {'numFailedTests': 0, 'numPassedTests': expected, 'numPendingTests': 0, 'numTotalTests': expected, 'success': True}
    trace = json.loads(old[CANDIDATE_TRACE])
    assert not trace.get('error') and trace['checks'] == prior['candidate_checkpoints']
    assert len(trace['checks']) == len(runner.REQUIRED_CHECKS) and set(trace['checks']) == runner.REQUIRED_CHECKS
    for label in ['candidate-real-docker', 'adjacent-lifecycle-regressions', 'changed-fixture-types', 'literal-dollar-regression', 'remediation-regression', 'candidate-backend-bundle']:
        assert prior['steps'][label]['exit_code'] == 0
    assert prior['steps']['remediation-prior-negative']['exit_code'] == 1
    runner.rollback_negative(old['logs/remediation-prior-negative.log'].decode())
    assert prior['rollback_prior_negative'] == {'source': runner.ROLLBACK_PRIOR_SOURCE, 'service_sha256': runner.ROLLBACK_PRIOR_SERVICE_SHA, 'specific_failure': True}
    assert receipt['inherited_candidate']['pins'] == CANDIDATE_PINS
    assert receipt['inherited_candidate']['candidate_counts'] == prior['candidate_counts']
    for name, (size, expected) in SAVED_BUNDLE.items():
        assert len(old[DIST+name]) == size and digest(old[DIST+name]) == expected
        assert result['saved_bundle']['files'][name] == {'bytes': size, 'sha256': expected}
    # Verify unchanged saved inputs AGAIN before publishing the derived verdict.
    assert pinned_files(evidence, PINS) == raw and pinned_files(evidence/'candidate', CANDIDATE_PINS) == old_raw
    return {'schema': 1, 'kind': 'offline immutable-artifact composite, not worker receipt',
        'source_handler_fixture_composite_pass': True, 'baseline_bypass_reproduced': True,
        'production_source': previous['head'], 'baseline_harness_source': SOURCE,
        'verifier_sha256': digest(Path(__file__).read_bytes()), 'classifier_sha256': digest((repo/'scripts/manager_seal_baseline.py').read_bytes()),
        'inputs': {'baseline': PINS, 'candidate': CANDIDATE_PINS, 'baseline_source_manifest': MANIFEST},
        'member_pins': {key: {'bytes': len(files[key]), 'sha256': digest(files[key])} for key in ['baseline.json', TRACE, 'inside-result.json']},
        'source_closure': {'candidate_to_baseline_changed_qa': sorted(inherited_changes), 'current_changed_tracked_qa': sorted(changed),
                           'unchanged_pinned_paths': len(set(manifest['files'])-changed), 'production_and_fixture_bytes_modes_preserved': True},
        'baseline': baseline, 'inherited_candidate_counts': prior['candidate_counts'], 'inherited_adjacent_counts': prior['adjacent_counts'],
        'inherited_checkpoints': trace['checks'], 'inherited_rollback_negative': prior['rollback_prior_negative'],
        'saved_bundle': result['saved_bundle'], 'baseline_original_receipt_still_failed': True, 'candidate_original_receipt_still_failed': True,
        'cleanup_and_reserve_verified_in_both_saved_receipts': True, 'docker_rerun': False, 'heavy_submitted': False,
        'packaged_api_pass': False, 'full_docker_acceptance': False, 'image_built': False, 'RUNTIPI_ADMITTED': False,
        'boundaries': ['Candidate 19/19, 22 checkpoints and adjacent 60/60 are inherited exact-source evidence, not fresh execution.',
            'Baseline worker failed in presentation-dependent classifier; source restoration after classifier was not reached. No relabelled worker success.',
            'Positive baseline attachment needs no end-window absence proof; aborted baseline has no final-drain claim.',
            'Offline verifier ran on local Python; new classifier has no new Python 3.11.2 runtime claim.',
            'Installed Nest/DB/authenticated HTTP/queue/update transaction, official image and successive accepted updates remain unproven.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--pinned-source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    verdict = composite(args.evidence.resolve(), args.pinned_source.resolve(), repo)
    # Exclusive create: never rewrite any existing receipt, including this one.
    with args.output.open('x') as stream: stream.write(json.dumps(verdict, sort_keys=True, indent=2)+'\n')
    print(json.dumps({'output': str(args.output), 'sha256': digest(args.output.read_bytes()), 'source_handler_fixture_composite_pass': True}))
