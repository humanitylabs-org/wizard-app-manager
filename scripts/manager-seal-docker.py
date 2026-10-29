#!/usr/bin/env python3
"""Bounded PUBLIC synthetic direct-manager Docker proof; parent submits on forum.

Reuses the passing early bootstrap, not manager-seal-build.py. No image pulls,
image builds, live service changes, second dependency installation or full units.
The Docker socket is intentional host authority, NOT a security sandbox claim.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
import zipfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location('early', Path(__file__).with_name('manager-seal-early.py'))
assert spec is not None and spec.loader is not None
early = importlib.util.module_from_spec(spec)
spec.loader.exec_module(early)
from manager_seal_install_evidence import dependency_step, recover_artifacts
from manager_seal_bundle import validate_bundle, failure_record
from manager_seal_baseline import baseline_failure, FIXTURE
import manager_seal_continuation as continuation

GIB = 1024**3
MIB = 1024**2
RESERVE = 200*GIB
HOST_ALLOWANCE = GIB
FIXTURE_BYTES = 64*MIB
ARTIFACT_BYTES = 128*MIB
WIRE_BYTES = 384*MIB
EARLY_SOURCE = 'c535a5cf2b1593b9b85c7d98b5a39ba34ff9e496'
EARLY_MANIFEST = 'ef2f083205d855b5ba46d308610de3c6137efe59631e8b8b0cc89d79941c974c'
EARLY_RECEIPT = '9d4bb8b4790056a413ec11a101fc28706a23b18d9daa5e48a8403f96b8d6777d'
EARLY_LOG = '383745ca214e18ab6f7c4ce4e0dbd9524fb76af9bbfefec19ed8f2e41f54f6cb'
MEASURED_SCRATCH = 1390694400
PYTHON_IMAGE = 'sha256:ae4e8b7c575fabea88a029b6cae757f342ca2525e07f7c22272d5105c346b35e'
PYTHON_DIGEST = 'python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159'
DOCKER_SHA = '32b607f4bef9c7d0a5e717ed812590bfc5253a9dbabcd7210f03c5316dcfa44f'
OLD_IMAGE = 'sha256:908ba820e904091ddd38402404fdde212ca2f14b61b33c621dfe134d635f8ce9'
ROLLBACK_PRIOR_SOURCE = '72792604532056b2d988adab47ed55785f6170bc'
ROLLBACK_PRIOR_SERVICE_SHA = 'a04e8d8e50c529d9ee257704a376b1eff2d0f438c705fe68500a81bf7e35f332'
TARGET = 'src/modules/docker/__tests__/mandatory-seal.test.ts'
PRODUCT_DELTA = ['packages/backend/src/modules/docker/docker.service.ts', 'packages/backend/src/modules/docker/wizard-storage.ts']
ALLOWED_DELTA = {TARGET.replace('src/', 'packages/backend/src/', 1), 'packages/backend/src/modules/docker/mandatory-seal.ts',
                 'packages/backend/src/modules/docker/docker.service.ts', 'scripts/tests/test_manager_seal_dollars.mjs',
                 'scripts/tests/test_manager_seal_remediation.mjs',
                 'scripts/manager-seal-docker.py', 'scripts/tests/test_manager_seal_docker.py', 'docs/mandatory-manager-privacy.md'}
ALLOWED_DELTA |= continuation.QA_DELTA
RESULT = 'DOCKER_RESULT='
REQUIRED_CHECKS = frozenset({
    'cross-process-enrollment-exclusion', 'disabled-config-original-bypass', 'launch-excludes-enrollment',
    'factory-registered-restart-synthetic-generation-real-down-up-docker', 'prefixed-and-shortcut-refusals',
    'grant-revoked-no-resurrection', 'unapproved-runtime-before-destructive-callback',
    'factory-registered-restart-invalid-runtime-leaves-real-containers', 'optional-config-removed-revocation-sticky',
    'malicious-optional-config-new-manager-object', 'unsupported-request-refusal', 'noop-stage-byte-preservation',
    'malicious-optional-config-registered-restart-down-up', 'malicious-optional-config-standalone-stop-recovery',
    'stale-cas-and-envelope-revision-negatives', 'distinct-pinned-image-real-update-revoked-grant',
    'forced-down-failure-prior-revision-recovery-revoked-grant', 'successive-distinct-revision-refused-anchor-preserved',
    'explicit-grant-reapproval-only', 'missing-policy-before-destructive-callback',
    'missing-policy-remediation-still-available',
    'exact-live-create-connect-coverage-no-transient-public-network',
})


def sha(p): return early.digest(p)


def run(argv, **kwargs):
    return subprocess.check_output(list(map(str, argv)), text=True, timeout=30, **kwargs).strip()


def capacity(root):
    free = shutil.disk_usage(root).free
    value = {'free_bytes': free, 'reserve_bytes': RESERVE, 'host_allowance_bytes': HOST_ALLOWANCE,
             'required_bytes': RESERVE+HOST_ALLOWANCE, 'shortfall_bytes': max(0, RESERVE+HOST_ALLOWANCE-free),
             'measured_early_tmpfs_bytes': MEASURED_SCRATCH, 'scratch_tmpfs_cap_bytes': early.SCRATCH,
             'fixture_tmpfs_cap_bytes': FIXTURE_BYTES, 'artifact_cap_bytes': ARTIFACT_BYTES,
             'wire_cap_bytes': WIRE_BYTES, 'disk_dependencies_bytes': 0, 'image_pull_build_bytes': 0}
    print(json.dumps(value), flush=True)
    assert free >= RESERVE+HOST_ALLOWANCE, 'capacity blocked; no deletion/reserve reduction authorized'
    return value


def inherited(root, manifest):
    old = root/'inherited'
    assert sha(old/'SOURCE.json') == EARLY_MANIFEST
    assert sha(old/'early-receipt.json') == EARLY_RECEIPT
    assert sha(old/'early.log') == EARLY_LOG
    assert sha(old/'rollback-72792604-docker.service.ts') == ROLLBACK_PRIOR_SERVICE_SHA
    prior = json.loads((old/'SOURCE.json').read_text())
    receipt = json.loads((old/'early-receipt.json').read_text())
    assert receipt['early_pass'] and receipt['source_head'] == EARLY_SOURCE
    assert receipt['result']['unit_counts'] == {'numFailedTests': 0, 'numPassedTests': 389, 'numPendingTests': 1, 'numTotalTests': 390, 'success': True}
    assert receipt['result']['tmpfs_used_bytes'] == MEASURED_SCRATCH
    changes = {p for p in set(prior['files']) | set(manifest['files']) if prior['files'].get(p) != manifest['files'].get(p)}
    assert changes <= ALLOWED_DELTA, 'inherited source closure changed: '+repr(sorted(changes-ALLOWED_DELTA))
    for name, metadata in receipt['dependency_artifacts'].items():
        assert sha(old/name) == metadata['sha256'] and (old/name).stat().st_size == metadata['bytes']
    return {'receipt_sha256': EARLY_RECEIPT, 'log_sha256': EARLY_LOG, 'source': EARLY_SOURCE,
            'source_manifest_sha256': EARLY_MANIFEST, 'changed_files': sorted(changes),
            'unchanged_files': len(set(prior['files'])-changes), 'prior_unit_counts': receipt['result']['unit_counts'],
            'invalidated_product_boundary': 'mandatory-seal snapshot and remediation routing; rerun complete mandatory-seal file, remediation method and adjacent lifecycle tests',
            'classification': 'historical full-suite counts only; unchanged setup/source evidence retained, changed helper and fixture rerun, not fresh full-suite proof'}


def inside_limits(fixture):
    assert os.getuid() != 0
    for file, expected in [('memory.max', early.MEMORY), ('memory.swap.max', 0), ('pids.max', 256)]:
        assert int(Path('/sys/fs/cgroup', file).read_text()) == expected, file
    mounts = [l.split() for l in Path('/proc/mounts').read_text().splitlines()]
    for target in ('/', '/source', '/tooling/node', '/tooling/docker', '/tooling/compose', '/tmp'):
        assert 'ro' in next(m for m in mounts if m[1] == target)[3].split(','), target
    for target, cap in [('/work', early.SCRATCH), (str(fixture), FIXTURE_BYTES)]:
        assert next(m for m in mounts if m[1] == target)[2] == 'tmpfs'
        v = os.statvfs(target)
        assert v.f_blocks*v.f_frsize <= cap
    assert Path('/var/run/docker.sock').is_socket()
    # Test this runtime's actual AF_UNIX byte boundary, not a guessed path limit.
    for size in (107, 108):
        name = '/work/'+('x'*(size-len('/work/')))
        s = socket.socket(socket.AF_UNIX)
        try:
            try: s.bind(name)
            except OSError:
                assert size == 108, 'short AF_UNIX path refused'
            else:
                assert size == 107, 'unexpected AF_UNIX boundary'
                os.unlink(name)
        finally: s.close()
    probe = fixture/'bind-probe.sock'
    s = socket.socket(socket.AF_UNIX)
    try: s.bind(str(probe))
    finally: s.close(); probe.unlink(missing_ok=True)
    return {'unix_path_max_payload_bytes': 107, 'docker_socket_bytes': len(b'/var/run/docker.sock'),
            'fixture_root': str(fixture), 'fixture_socket_probe_bytes': len(os.fsencode(probe)),
            'fixture_socket_bind': True, 'app_socket_grants_exercised': False}


def bootstrap(root, manifest, step):
    checkout = Path('/work/source'); checkout.mkdir()
    for rel in manifest['files']:
        dst = checkout/rel; dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(root/rel, dst)
    for rel in ('bin', 'tmp', 'home', 'cache', 'logs'): Path('/work', rel).mkdir(mode=0o700, exist_ok=True)
    Path('/work/tmp', early.bunx_node_gyp_cache(os.getuid())).mkdir(mode=0o700)
    early.verify_bunx_tmp_alias(Path('/tmp'), Path('/work/tmp'), os.getuid())
    Path('/work/bin/node').symlink_to('/tooling/node')
    Path('/work/bin/docker').symlink_to('/tooling/docker')
    Path('/work/bin/docker-compose').symlink_to('/tooling/compose')
    wrapper = Path('/work/bin/node-gyp')
    wrapper.write_text('#!/bin/sh\nexec /tooling/node '+early.NODE_GYP+' "$@"\n'); wrapper.chmod(0o755)
    # Reuse real compiler/link/load and exact header bootstrap that passed early.
    early.toolchain_preflight(Path('/work'), step)
    archive = Path('/work/bun.zip')
    early.download_public(early.BUN_URL, archive, early.BUN_ZIP_SHA, 64*MIB)
    with zipfile.ZipFile(archive) as z:
        assert z.getinfo('bun-linux-x64/bun').file_size <= 200*MIB
        with z.open('bun-linux-x64/bun') as src, Path('/work/bin/bun').open('xb') as dst: shutil.copyfileobj(src, dst)
    Path('/work/bin/bun').chmod(0o755); archive.unlink()
    step('bun-version', ['bun', '--version'], cwd=checkout)
    assert Path('/work/logs/bun-version.log').read_text().strip() == '1.3.0'
    return checkout


def report_counts(report):
    return {k: report[k] for k in ('numTotalTests', 'numPassedTests', 'numFailedTests', 'numPendingTests', 'success')}


def rollback_negative(log):
    assert 'not ok 1 - forced rollback: nested down receives exact snapshot' in log
    assert 'optional input reached remediation' in log and 'synthetic forced rollback' in log
    assert '# fail 1' in log.splitlines() and '# pass 0' in log.splitlines()


def fixture_identity(fixture):
    # No DAC-bypass capability: every reader must match the 0700 tmpfs owner.
    uid, gid = os.getuid(), os.getgid()
    metadata = fixture.lstat()
    assert uid != 0 and gid != 0, 'fixture requires explicit nonroot uid/gid'
    assert stat.S_ISDIR(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o700, 'fixture root must be a real 0700 directory'
    assert (metadata.st_uid, metadata.st_gid) == (uid, gid), 'fixture root owner must match worker uid/gid'
    return f'{uid}:{gid}'


def daemon_bind_preflight(fixture, owner):
    # Real daemon, same canonical bounded volume path, no dependency install.
    identity = fixture_identity(fixture)
    marker = 'PUBLIC synthetic manager daemon bind proof'
    with (fixture/'bind-probe.txt').open('x') as probe:
        os.fchmod(probe.fileno(), 0o600)
        probe.write(marker+'\n')
    versions = {}
    try:
        for image, version in [(PYTHON_IMAGE, '3.12.'), (early.IMAGE, '3.11.2')]:
            value = run(['/tooling/docker', 'run', '--rm', '--pull=never', '--name', owner+'-version',
                         '--user', identity,
                         '--label', 'wizard.qa.manager-seal='+owner, '--read-only', '--network=none', '--log-driver=none',
                         '--memory=33554432', '--memory-swap=33554432', '--pids-limit=16', '--cap-drop=ALL',
                         '--security-opt=no-new-privileges:true', '--mount', f'type=bind,src={fixture},dst=/probe,readonly',
                         '--entrypoint=python3', image, '-c', 'import os,sys; print(sys.version.split()[0]); print(str(os.getuid())+":"+str(os.getgid())); print(open("/probe/bind-probe.txt").read().strip())'])
            lines = value.splitlines()
            assert len(lines) == 3 and lines[0].startswith(version) and lines[1] == identity and lines[2] == marker, value
            versions[image] = lines[0]
    finally: (fixture/'bind-probe.txt').unlink(missing_ok=True)
    return {'versions': versions, 'user': identity, 'fixture_root_mode': '0700', 'marker_mode': '0600',
            'daemon_reads_same_synthetic_tmpfs_bytes': True}


def network_driver_preflight(owner):
    network = run(['/tooling/docker', 'network', 'create', '--internal', '--driver=bridge',
        '--label', 'wizard.qa.manager-seal='+owner, '--opt', 'com.docker.network.bridge.inhibit_ipv4=true', owner+'-driver-probe'])
    try:
        data = json.loads(run(['/tooling/docker', 'network', 'inspect', '--format', '{{json .}}', network]))
        assert data['Internal'] and not data['EnableIPv6']
        assert data['Options']['com.docker.network.bridge.inhibit_ipv4'] == 'true'
        return {'internal_bridge_option_accepted': True, 'docker_server': run(['/tooling/docker', 'version', '--format', '{{.Server.Version}}'])}
    finally: run(['/tooling/docker', 'network', 'rm', network])



def export_bundle(paths):
    archive = Path('/work/docker-artifacts.tar.gz')
    with tarfile.open(archive, 'w:gz') as tf:
        for p in paths:
            # Keep original raw HTTP headers confined to ephemeral RAM.
            if p.exists(): tf.add(p, arcname=str(p.relative_to('/work')), recursive=True,
                                 filter=lambda m: None if m.name == 'logs/dependencies.log' else m)
    assert archive.stat().st_size <= ARTIFACT_BYTES, 'artifact cap'
    print('DOCKER_EXPORT_BEGIN='+json.dumps({'bytes': archive.stat().st_size, 'sha256': sha(archive)}), flush=True)
    with archive.open('rb') as f:
        for block in iter(lambda: f.read(49152), b''): print('DOCKER_EXPORT_DATA='+base64.b64encode(block).decode(), flush=True)
    print('DOCKER_EXPORT_END='+sha(archive), flush=True)


def inside(args):
    root = Path('/source'); fixture = Path(args.fixture_root)
    manifest = early.verify_source(root, args.source, args.manifest_sha256)
    inheritance = inherited(root, manifest)
    Path('/work/logs').mkdir(exist_ok=True)
    result = {'source': args.source, 'manifest_sha256': args.manifest_sha256, 'steps': {}, 'inherited': inheritance,
              'source_handler_real_docker_pass': False, 'packaged_api_pass': False,
              'image_built': False, 'full_docker_acceptance': False, 'RUNTIPI_ADMITTED': False, 'phase': 'inside-preflight'}
    checkout = Path('/work/source')
    def step(label, argv, cwd=checkout, timeout=300, allow_failure=False):
        result['phase'] = label
        print('STEP '+label, flush=True)
        log = Path('/work/logs', label+'.log')
        with log.open('wb') as f:
            p = subprocess.run(list(map(str, argv)), cwd=cwd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
        assert log.stat().st_size <= 32*MIB, 'step log cap'
        result['steps'][label] = {'exit_code': p.returncode, 'bytes': log.stat().st_size, 'sha256': sha(log)}
        if p.returncode and not allow_failure:
            raise RuntimeError(label+' failed: '+str(p.returncode))
        return p.returncode
    try:
        result['limits'] = inside_limits(fixture)
        assert sha('/tooling/node') == early.NODE_SHA and sha('/tooling/docker') == DOCKER_SHA
        # ELF/library and real socket access fail BEFORE installing dependencies.
        assert run(['/tooling/docker', '--version']).startswith('Docker version 26.1.5')
        run(['/tooling/docker', 'version', '--format', '{{.Server.Version}}'])
        assert run(['/tooling/compose', 'version', '--short']).lstrip('v') == '2.40.0'
        # Python 3.13 is not required by the fixture. Explicitly exercise the
        # cached distinct v2's Python 3.11.2; do not label it Python 3.13 proof.
        result['daemon_bind_preflight'] = daemon_bind_preflight(fixture, args.owner)
        result['network_driver_preflight'] = network_driver_preflight(args.owner)
        checkout = bootstrap(root, manifest, step)
        step('runner-focused', ['python3', '-m', 'unittest', 'test_manager_seal_docker', '-v'], cwd=checkout/'scripts/tests')
        deps = {}; result['steps']['dependencies'] = deps
        dependency_step(checkout, Path('/work/logs'), manifest['files']['bun.lock'], deps)
        assert deps['exit_code'] == 0 and deps['log_complete'] and deps['comparison_complete']
        assert sha(checkout/'bun.lock') == manifest['files']['bun.lock']
        step('common-build', ['bun', 'run', 'build'], cwd=checkout/'packages/common')
        backend = checkout/'packages/backend'
        vitest = early.workspace_executable(checkout, 'backend', 'vitest')
        step('changed-fixture-types', [early.workspace_executable(checkout, 'backend', 'tsgo'), '--noEmit'], cwd=backend)
        step('literal-dollar-regression', ['node', '--test', 'scripts/tests/test_manager_seal_dollars.mjs'], cwd=checkout)
        step('remediation-regression', ['node', '--test', 'scripts/tests/test_manager_seal_remediation.mjs'], cwd=checkout)
        prior = root/'inherited/rollback-72792604-docker.service.ts'
        assert sha(prior) == ROLLBACK_PRIOR_SERVICE_SHA
        os.environ['MANAGER_SEAL_REMEDIATION_SOURCE'] = str(prior)
        try:
            code = step('remediation-prior-negative', ['node', '--test', '--test-name-pattern=^forced rollback:', 'scripts/tests/test_manager_seal_remediation.mjs'], cwd=checkout, allow_failure=True)
            assert code != 0
            rollback_negative(Path('/work/logs/remediation-prior-negative.log').read_text())
            result['rollback_prior_negative'] = {'source': ROLLBACK_PRIOR_SOURCE, 'service_sha256': sha(prior), 'specific_failure': True}
        finally: os.environ.pop('MANAGER_SEAL_REMEDIATION_SOURCE', None)
        step('adjacent-lifecycle-regressions', [vitest, 'run', 'src/modules/app-lifecycle', 'src/modules/docker/__tests__/wizard-restart-incident.test.ts', '--maxWorkers=1', '--reporter=json', '--outputFile=/work/adjacent.json'], cwd=backend, timeout=180)
        adjacent = json.loads(Path('/work/adjacent.json').read_text())
        result['adjacent_counts'] = report_counts(adjacent)
        assert adjacent['success'] and adjacent['numTotalTests'] > 0 and adjacent['numFailedTests'] == 0 and adjacent['numPendingTests'] == 0
        os.environ.update(RUN_MANAGER_SEAL_DOCKER='1', MANAGER_SEAL_BASELINE_NEGATIVE='0', TMPDIR=str(fixture),
                          MANAGER_SEAL_TEST_IMAGE=PYTHON_IMAGE, MANAGER_SEAL_TEST_IMAGE_V2=early.IMAGE,
                          MANAGER_SEAL_TRACE_DIR='/work/candidate-trace', MANAGER_SEAL_QA_OWNER=args.owner,
                          MANAGER_SEAL_PROJECT_SUFFIX=args.source[:12])

        step('candidate-real-docker', [vitest, 'run', TARGET, '--maxWorkers=1', '--reporter=json', '--outputFile=/work/candidate.json'], cwd=backend, timeout=300)
        candidate = json.loads(Path('/work/candidate.json').read_text()); result['candidate_counts'] = report_counts(candidate)
        assert candidate['success'] and candidate['numFailedTests'] == 0 and candidate['numPendingTests'] == 0
        traces = list(Path('/work/candidate-trace').glob('*-events.json')); assert len(traces) == 1
        trace = json.loads(traces[0].read_text()); assert not trace.get('error')
        assert set(trace['checks']) == REQUIRED_CHECKS and len(trace['checks']) == len(REQUIRED_CHECKS), 'missing/duplicate real-Docker checkpoint'
        result['candidate_checkpoints'] = trace['checks']
        # Bundle exact candidate BEFORE replacing baseline product files. This
        # prepares a backend artifact, NOT an official Dockerfile/API proof.
        step('candidate-backend-bundle', ['bun', 'run', 'bundle'], cwd=backend)
        result['phase'] = 'candidate-bundle-inventory'
        result['candidate_bundle_inventory'] = validate_bundle(backend/'dist', root, installed=checkout)
        bundle = backend/'dist/main.js'
        step('candidate-bundle-node-syntax', ['node', '--check', bundle], cwd=backend)
        result['candidate_backend_bundle_sha256'] = sha(bundle)
        result['phase'] = 'baseline-source-closure'
        baseline = Path('/work/baseline'); baseline.mkdir(mode=0o700)
        result['baseline_source_bytes'] = continuation.extract_baseline(root/'baseline.tar', baseline)
        # Every preexisting base file is identical except the two intentional
        # runtime changes and one unrelated test adaptation. Added helper/fixture
        # is the deliberate injection; package/lock/common/config are exact.
        permitted = set(PRODUCT_DELTA+['packages/backend/src/modules/docker/__tests__/wizard-restart-incident.test.ts'])
        for p in baseline.rglob('*'):
            if p.is_file():
                rel = str(p.relative_to(baseline))
                assert rel in permitted or sha(p) == manifest['files'].get(rel), 'baseline shared closure drift: '+rel
        for rel in PRODUCT_DELTA: shutil.copy2(baseline/rel, checkout/rel)
        # One existing dependency closure, fresh Vitest process, baseline's real
        # DockerService AND storage bytes, candidate policy helper/fixture only.
        os.environ.update(MANAGER_SEAL_BASELINE_NEGATIVE='1', MANAGER_SEAL_TRACE_DIR='/work/baseline-trace')
        code = step('baseline-negative', [vitest, 'run', TARGET, '-t', 'real Docker', '--maxWorkers=1', '--reporter=json', '--outputFile=/work/baseline.json'], cwd=backend, allow_failure=True)
        result['phase'] = 'baseline-specific-failure-validation'
        report = json.loads(Path('/work/baseline.json').read_text()); result['baseline_counts'] = report_counts(report)
        assert code != 0
        project = 'agent-synthetic_seal'+args.source[:12]
        result['baseline_evidence'] = baseline_failure(report,
            json.loads(Path('/work/baseline-trace', project+'-events.json').read_text()),
            (checkout/FIXTURE).read_bytes(), project)
        result['baseline_bypass_reproduced'] = True
        for rel in PRODUCT_DELTA: shutil.copy2(root/rel, checkout/rel)
        assert all(sha(checkout/rel) == manifest['files'][rel] for rel in PRODUCT_DELTA)
        result['source_handler_real_docker_pass'] = True
    except Exception as e:
        result['error'] = failure_record(e, result['phase'])
        raise
    finally:
        result['tmpfs_used_bytes'] = (os.statvfs('/work').f_blocks-os.statvfs('/work').f_bfree)*os.statvfs('/work').f_frsize
        Path('/work/inside-result.json').write_text(json.dumps(result, indent=2)+'\n')
        export_bundle([Path('/work/inside-result.json'), Path('/work/logs'), Path('/work/candidate.json'), Path('/work/baseline.json'), Path('/work/adjacent.json'),
                       Path('/work/candidate-trace'), Path('/work/baseline-trace'), checkout/'packages/backend/dist'])
        print(RESULT+json.dumps(result, sort_keys=True), flush=True)


def create_command(root, name, volume, mountpoint, uid, gid, sock_gid, source, manifest, continue_saved=False):
    cmd = early.create_command(root, name, uid, gid, source, manifest)
    cmd[cmd.index('wizard.qa.manager-seal.early='+source)] = 'wizard.qa.manager-seal='+name
    pos = cmd.index('--workdir')
    cmd[pos:pos] = ['--group-add', str(sock_gid),
        '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
        '--mount', 'type=bind,src=/usr/bin/docker,dst=/tooling/docker,readonly',
        '--mount', f'type=bind,src={root}/manager-seal-early-results/compose,dst=/tooling/compose,readonly',
        '--mount', f'type=volume,src={volume},dst={mountpoint},volume-nocopy',
        '--env', 'DOCKER_HOST=unix:///var/run/docker.sock', '--env', 'DOCKER_CONFIG=/work/docker-config']
    i = cmd.index('/source/scripts/manager-seal-early.py')
    cmd[i:] = ['/source/scripts/manager-seal-docker.py', '--inside', '--source', source, '--manifest-sha256', manifest,
               '--fixture-root', mountpoint, '--owner', name]
    if continue_saved: cmd.append('--continue-saved')
    return cmd


def receive(log, output):
    metadata = None; end = None; count = 0; h = hashlib.sha256()
    with log.open('r') as inp, (output/'docker-artifacts.tar.gz').open('xb') as out:
        for line in inp:
            if line.startswith('DOCKER_EXPORT_BEGIN='):
                assert metadata is None
                metadata = json.loads(line.split('=', 1)[1]); assert 0 < metadata['bytes'] <= ARTIFACT_BYTES
            elif line.startswith('DOCKER_EXPORT_DATA='):
                assert metadata and end is None
                block = base64.b64decode(line.split('=', 1)[1].strip(), validate=True)
                count += len(block); assert count <= metadata['bytes']
                out.write(block); h.update(block)
            elif line.startswith('DOCKER_EXPORT_END='):
                assert end is None
                end = line.strip().split('=', 1)[1]
    assert metadata and count == metadata['bytes'] and h.hexdigest() == metadata['sha256'] == end, 'incomplete artifact transport'
    return metadata


def main(args):
    root = Path.cwd().resolve()
    assert str(root).startswith('/srv/work/manager-seal-') and os.uname().machine == 'x86_64' and os.getuid() != 0
    manifest = early.verify_source(root, args.source, args.manifest_sha256)
    inherit = inherited(root, manifest)
    saved = continuation.verify_saved(root, manifest, sys.modules[__name__]) if args.continue_saved else None
    admission = capacity(root)
    assert sha(early.NODE) == early.NODE_SHA and sha('/usr/bin/docker') == DOCKER_SHA
    available = int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))*1024
    assert available >= early.MEMORY+5*GIB, 'RAM: 12GiB worker + 5GiB fixture/control/host margin'
    for image, size, digest in [(early.IMAGE, early.IMAGE_BYTES, early.IMAGE_REPO_DIGEST), (PYTHON_IMAGE, 119488133, PYTHON_DIGEST)]:
        data = json.loads(run(['docker', 'image', 'inspect', image]))[0]
        assert data['Id'] == image and data['Size'] == size and data['Architecture'] == 'amd64' and data['Os'] == 'linux'
        assert digest in data['RepoDigests'] and not data['Config'].get('Volumes'), 'unexpected image volume allocation'
    donor_config = json.loads(run(['docker', 'image', 'inspect', OLD_IMAGE]))[0]
    assert donor_config['Id'] == OLD_IMAGE and not donor_config['Config'].get('Volumes')
    assert run(['docker', 'info', '--format', '{{.DockerRootDir}}']) == '/var/lib/docker'
    assert Path('/var/run/docker.sock').is_socket()
    name = 'manager-seal-docker-'+args.source[:12]
    project = 'agent-synthetic_seal'+args.source[:12]
    volume = 'm'+args.source[:8]
    for command in [['docker', 'ps', '-aq'], ['docker', 'network', 'ls', '-q'], ['docker', 'volume', 'ls', '-q']]:
        assert not run(command+['--filter', 'label=wizard.qa.manager-seal='+name]), 'preexisting owned resources'
        assert not run(command+['--filter', 'label=com.docker.compose.project='+project]), 'preexisting synthetic project'
    assert not run(['docker', 'volume', 'ls', '-q', '--filter', 'name=^'+volume+'$']), 'preexisting short volume'
    if args.preflight:
        print(json.dumps({'preflight': True, 'inherited': inherit, 'capacity': admission, 'memory_available_bytes': available,
                          'fixture_images': [PYTHON_IMAGE, early.IMAGE], 'docker_group': Path('/var/run/docker.sock').stat().st_gid,
                          'inherited_candidate': saved, 'execution_pending': True, 'RUNTIPI_ADMITTED': False}, indent=2)); return
    output = root/'manager-seal-early-results'; output.mkdir(exist_ok=False)
    early.prepare_bunx_tmp_alias(output, os.getuid())
    receipt = {'source': args.source, 'manifest_sha256': args.manifest_sha256, 'runner_sha256': sha(__file__), 'inherited': inherit,
               'capacity': admission, 'source_handler_real_docker_pass': False, 'packaged_api_pass': False,
               'full_docker_acceptance': False, 'image_built': False, 'RUNTIPI_ADMITTED': False,
               'phase': 'tool-donor', 'inherited_candidate': saved}
    cid = None; donor = None; attached = None; owned_volume = False
    try:
        # Cached PUBLIC tool donor, never started. No old product runtime reused.
        donor = run(['docker', 'create', '--pull=never', '--label', 'wizard.qa.manager-seal='+name, OLD_IMAGE])
        subprocess.run(['docker', 'cp', donor+':/usr/local/bin/docker-compose', str(output/'compose')], check=True, timeout=30)
        assert (output/'compose').stat().st_size <= 100*MIB
        (output/'compose').chmod(0o755); receipt['compose_sha256'] = sha(output/'compose')
        assert run([output/'compose', 'version', '--short']).lstrip('v') == '2.40.0'
        run(['docker', 'rm', donor]); donor = None
        # One hard-capped RAM volume visible at the SAME canonical daemon path
        # inside the worker. /work is never used as a daemon bind source.
        receipt['phase'] = 'fixture-volume-create'
        run(['docker', 'volume', 'create', '--driver=local', '--label', 'wizard.qa.manager-seal='+name,
             '--opt', 'type=tmpfs', '--opt', 'device=tmpfs', '--opt', f'o=size={FIXTURE_BYTES},uid={os.getuid()},gid={os.getgid()},mode=0700,nosuid,nodev', volume])
        owned_volume = True
        mountpoint = run(['docker', 'volume', 'inspect', '--format', '{{.Mountpoint}}', volume])
        assert mountpoint == '/var/lib/docker/volumes/'+volume+'/_data' and len(os.fsencode(mountpoint+'/bind-probe.sock')) <= 107
        cmd = create_command(root, name, volume, mountpoint, os.getuid(), os.getgid(), Path('/var/run/docker.sock').stat().st_gid, args.source, args.manifest_sha256, args.continue_saved)
        capacity(root)
        receipt['phase'] = 'worker-create'
        cid = run(cmd)
        config = json.loads(run(['docker', 'inspect', '--format', '{{json .HostConfig}}', cid]))
        assert config['ReadonlyRootfs'] and config['Memory'] == early.MEMORY and config['MemorySwap'] == early.MEMORY
        assert config['PidsLimit'] == 256 and config['LogConfig']['Type'] == 'none'
        receipt['phase'] = 'worker-execution'
        attached = subprocess.Popen(['docker', 'start', '--attach', cid], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert attached.stdout is not None
        sel = selectors.DefaultSelector(); sel.register(attached.stdout, selectors.EVENT_READ)
        total = 0; deadline = time.monotonic()+2400
        with (output/'docker.log').open('wb') as log:
            while sel.get_map():
                assert time.monotonic() < deadline, 'bounded stage timeout'
                assert shutil.disk_usage(root).free >= RESERVE, 'reserve breached by concurrent writer; abort'
                for key, _ in sel.select(timeout=1):
                    block = os.read(key.fd, 65536)
                    if not block: sel.unregister(key.fileobj); continue
                    total += len(block); assert total <= WIRE_BYTES, 'host wire cap'
                    log.write(block)
        code = attached.wait(timeout=10)
        receipt['container_state'] = json.loads(run(['docker', 'inspect', '--format', '{{json .State}}', cid]))
        receipt['phase'] = 'worker-result-transport'
        results = [json.loads(l[len(RESULT):]) for l in (output/'docker.log').read_text().splitlines() if l.startswith(RESULT)]
        assert len(results) == 1; receipt['result'] = results[0]
        receipt['artifacts'] = receive(output/'docker.log', output)
        receipt['dependencies'] = recover_artifacts(output/'docker.log', output)
        receipt['phase'] = 'worker-result-acceptance'
        assert code == 0 and receipt['container_state']['ExitCode'] == 0 and not receipt['container_state']['OOMKilled']
        assert results[0]['source_handler_real_docker_pass'] and results[0]['source'] == args.source and results[0]['manifest_sha256'] == args.manifest_sha256
        receipt['source_handler_real_docker_pass'] = True
    except Exception as e:
        receipt['error'] = failure_record(e, receipt['phase'])
        raise
    finally:
        # Worker/donor are expected here; fixture resources surviving their own
        # teardown FAIL acceptance even if exact-label fallback cleanup works.
        try:
            receipt['phase'] = 'owned-resource-cleanup'
            if cid: run(['docker', 'rm', '-f', cid])
            if donor: run(['docker', 'rm', '-f', donor])
            if attached is not None: attached.wait(timeout=15)
            for command, remove, kind in [(['docker', 'ps', '-aq'], ['docker', 'rm', '-f'], 'containers'),
                                           (['docker', 'network', 'ls', '-q'], ['docker', 'network', 'rm'], 'networks')]:
                ids = sorted(set(run(command+['--filter', 'label=wizard.qa.manager-seal='+name]).split()) |
                             set(run(command+['--filter', 'label=com.docker.compose.project='+project]).split()))
                receipt['leaked_'+kind] = ids
                if ids:
                    receipt['source_handler_real_docker_pass'] = False
                    run(remove+ids)
                assert not run(command+['--filter', 'label=wizard.qa.manager-seal='+name])
                assert not run(command+['--filter', 'label=com.docker.compose.project='+project])
            if owned_volume: run(['docker', 'volume', 'rm', volume])
            receipt['cleanup_volume_absent'] = not run(['docker', 'volume', 'ls', '-q', '--filter', 'name=^'+volume+'$'])
            receipt['disk_free_after'] = shutil.disk_usage(root).free
            receipt['reserve_preserved'] = receipt['disk_free_after'] >= RESERVE
            assert receipt['cleanup_volume_absent'] and receipt['reserve_preserved'], 'cleanup final bounds'
            assert not receipt['leaked_containers'] and not receipt['leaked_networks'], 'fixture leaked owned resources'
        except Exception as cleanup_error:
            receipt['source_handler_real_docker_pass'] = False
            receipt['cleanup_error'] = failure_record(cleanup_error, receipt['phase'])
            raise
        finally:
            (output/'docker-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
            print(json.dumps(receipt, indent=2), flush=True)
    assert receipt['source_handler_real_docker_pass'] and receipt['cleanup_volume_absent']


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True); p.add_argument('--manifest-sha256', required=True)
    p.add_argument('--inside', action='store_true'); p.add_argument('--preflight', action='store_true')
    p.add_argument('--continue-saved', action='store_true', help='reuse source-bound saved candidate proof; run baseline/bundle validation only')
    p.add_argument('--fixture-root'); p.add_argument('--owner')
    args = p.parse_args()
    try:
        if args.inside and args.continue_saved: continuation.inside(args, sys.modules[__name__])
        elif args.inside: inside(args)
        else: main(args)
    except Exception as exc:
        print('DOCKER_TRUSTED_FAILURE='+json.dumps(failure_record(exc, 'entrypoint-or-propagated-failure'), sort_keys=True), flush=True)
        raise
