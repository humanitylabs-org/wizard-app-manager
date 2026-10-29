#!/usr/bin/env python3
"""Parent-admitted forum build/regression stage ONLY. No API/live admission."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile

BASE = 'fead192e9fb2d560fd3b875d092819b32e7189b6'
NODE_SHA = 'e6ec2c188d83d813f81f2de8aea084d74dce603ac1abedd0a30ad941b10087b2'
OLD_IMAGE = 'wizard-app-manager:grants-fix-fead192e9fb2-amd64'
OLD_ID = 'sha256:908ba820e904091ddd38402404fdde212ca2f14b61b33c621dfe134d635f8ce9'
TEST = 'src/modules/docker/__tests__/mandatory-seal.test.ts'


def digest(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--source', required=True); args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    assert platform.machine() == 'x86_64' and str(root).startswith('/srv/work/manager-seal-')
    assert os.getuid() != 0, 'synthetic worker must be nonroot'
    manifest = json.loads((root / 'SOURCE.json').read_text())
    assert manifest['source_head'] == args.source and manifest['baseline'] == BASE
    for name, sha in manifest['files'].items():
        p = root / name
        assert not p.is_symlink() and p.resolve().is_relative_to(root) and digest(p) == sha, name
    inventory = {str(p.relative_to(root)) for p in root.rglob('*')
                 if p.is_file() and '.git' not in p.relative_to(root).parts and p.name != 'SOURCE.json'}
    assert inventory == set(manifest['files']), 'source inventory drift or an unbound file'
    assert shutil.disk_usage(root).free >= 80 * 1024**3, '80GiB scratch floor, do not delete siblings'
    qa = root / 'qa-manager-seal'; qa.mkdir()  # Never mix with an earlier attempt.
    tools = qa / 'tooling'; tools.mkdir()
    env = dict(os.environ)
    env['TMPDIR'] = str(qa / 'tmp'); Path(env['TMPDIR']).mkdir(mode=0o700)
    env['PATH'] = str(tools) + ':' + env['PATH']
    env['NODE_OPTIONS'] = '--experimental-sqlite'
    receipt = {'source_head': args.source, 'baseline': BASE, 'source_manifest_sha256': digest(root/'SOURCE.json'),
               'runner_sha256': digest(Path(__file__)), 'synthetic_only': True, 'live_touched': False,
               'RUNTIPI_ADMITTED': False, 'api_acceptance': False, 'phases': {}, 'image': None}
    def save(): (qa/'receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True)+'\n')
    def call(argv, cwd=root, timeout=600):
        return subprocess.run([str(x) for x in argv], cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=True).stdout.strip()
    def phase(name, argv, cwd=root, timeout=600, extra=None):
        log = qa / (name+'.log')
        try:
            with log.open('w') as out:
                p = subprocess.run([str(x) for x in argv], cwd=cwd, env={**env, **(extra or {})}, stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
            code = p.returncode
        except subprocess.TimeoutExpired: code = 124
        receipt['phases'][name] = {'exit_code': code, 'log_sha256': digest(log)}; save()
        return code
    try:
        node = Path('/srv/work/heavy/incognito-20261027/tooling/node')
        assert digest(node) == NODE_SHA
        (tools/'node').symlink_to(node)
        receipt['node'] = {'sha256': NODE_SHA, 'version': call([node, '--version'])}
        assert call(['docker', 'image', 'inspect', '--format', '{{.Id}}', OLD_IMAGE]) == OLD_ID
        # Copy only public binaries from the exact existing daemon and pinned Bun
        # image. Never inspect Config.Env or access running service containers.
        call(['docker', 'pull', 'oven/bun:1.3.0'], timeout=300)
        receipt['bun_image_id'] = call(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'oven/bun:1.3.0'])
        for image, member, target in [(OLD_IMAGE, '/usr/local/bin/docker-compose', tools/'docker-compose'),
                                      ('oven/bun:1.3.0', '/usr/local/bin/bun', tools/'bun')]:
            cid = call(['docker', 'create', image])
            try: call(['docker', 'cp', cid+':'+member, target])
            finally: call(['docker', 'rm', cid])
            target.chmod(0o755)
        receipt['compose'] = {'sha256': digest(tools/'docker-compose'), 'version': call([tools/'docker-compose', 'version', '--short'])}
        assert receipt['compose']['version'].lstrip('v') == '2.40.0'
        assert call([tools/'bun', '--version']) == '1.3.0'
        save()
        assert phase('dependencies', [tools/'bun', 'install', '--frozen-lockfile'], timeout=600) == 0, 'frozen dependency installation failed'
        assert phase('common-build', [tools/'bun', 'run', 'build'], cwd=root/'packages/common', timeout=180) == 0, 'common build prerequisite failed'
        backend = root/'packages/backend'
        vitest = root/'node_modules/.bin/vitest'
        full_json = qa/'backend.json'
        phase('backend-normal', [vitest, 'run', '--reporter=json', '--outputFile='+str(full_json)], cwd=backend, timeout=900)
        if full_json.exists():
            report = json.loads(full_json.read_text())
            receipt['backend_totals'] = {k:report.get(k) for k in ['success','numTotalTests','numPassedTests','numFailedTests','numPendingTests']}; save()
        # Use the exact already-present synthetic Python image ID, not an
        # invented digest or a mutable tag during the two regression runs.
        image_id = call(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'python:3.12-slim'])
        receipt['regression_fixture_image_id'] = image_id; save()
        test_env = {'RUN_MANAGER_SEAL_DOCKER':'1', 'MANAGER_SEAL_TEST_IMAGE': image_id}
        baseline = qa/'baseline'; baseline.mkdir()
        with tarfile.open(root/'BASELINE.tar') as tar: tar.extractall(baseline, filter='data')
        for rel in ['packages/backend/src/modules/docker/mandatory-seal.ts', 'packages/backend/'+TEST]:
            target = baseline/rel; target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(root/rel, target)
        # Helper and test are injected unchanged; baseline DockerService is not
        # changed. Red must be the original extra-public-network assertion.
        (baseline/'node_modules').symlink_to(root/'node_modules')
        shutil.copytree(root/'packages/common/dist', baseline/'packages/common/dist')
        for rel in ['packages/backend/node_modules', 'packages/common/node_modules']:
            if (root/rel).exists(): (baseline/rel).symlink_to(root/rel)
        baseline_report = qa/'baseline.json'
        code = phase('baseline-disabled-config-red', [vitest, 'run', TEST, '-t', 'disabled, removed', '--reporter=json', '--outputFile='+str(baseline_report)],
                     cwd=baseline/'packages/backend', timeout=240, extra=test_env)
        if baseline_report.exists():
            report = json.loads(baseline_report.read_text())
            failed = [t for suite in report['testResults'] for t in suite['assertionResults'] if t['status'] == 'failed']
            reproduced = code != 0 and len(failed) == 1 and 'disabled, removed' in failed[0]['fullName'] and '_public' in '\n'.join(failed[0]['failureMessages'])
        else: reproduced = False
        receipt['baseline_bypass_reproduced'] = reproduced; save()
        candidate_report = qa/'candidate.json'
        phase('candidate-disabled-config', [vitest, 'run', TEST, '--reporter=json', '--outputFile='+str(candidate_report)], cwd=backend, timeout=300, extra=test_env)
        if candidate_report.exists():
            report = json.loads(candidate_report.read_text())
            receipt['candidate_totals'] = {k:report.get(k) for k in ['success','numTotalTests','numPassedTests','numFailedTests','numPendingTests']}; save()
        phase('backend-types', [root/'node_modules/.bin/tsgo', '--noEmit'], cwd=backend, timeout=180)
        # Image build is independent evidence. A regression failure never turns
        # into green merely because the image packages successfully.
        tag = 'wizard-app-manager:mandatory-seal-'+args.source[:12]+'-amd64'
        code = phase('candidate-image', ['docker','build','--platform','linux/amd64','--build-arg','LOCAL=true',
                     '--label','org.opencontainers.image.revision='+args.source,
                     '--label','org.opencontainers.image.source=https://github.com/humanitylabs-org/wizard-app-manager',
                     '-t',tag,'.'], timeout=2400)
        if code == 0:
            actual = call(['docker','image','inspect','--format','{{.Id}}',tag])
            revision = call(['docker','image','inspect','--format','{{index .Config.Labels "org.opencontainers.image.revision"}}',tag])
            assert revision == args.source
            archive = qa/'candidate-image.tar'
            phase('candidate-image-save', ['docker','save','-o',archive,tag], timeout=300)
            if archive.exists(): receipt['image'] = {'tag':tag,'id':actual,'source_revision':revision,'archive':str(archive),'archive_sha256':digest(archive)}
        ordinary = all(v['exit_code'] == 0 for k,v in receipt['phases'].items() if k != 'baseline-disabled-config-red')
        receipt['build_regressions_pass'] = ordinary and receipt.get('baseline_bypass_reproduced') is True
        save()
    except Exception as e:
        # All fixture commands are synthetic. Keep a useful fixed error type,
        # never argv/environment/config dumps; phase log keeps exact tool result.
        receipt['blocked'] = {'class': type(e).__name__, 'reason': str(e) if isinstance(e, AssertionError) else 'runner command/prerequisite failed',
                              'returncode': getattr(e, 'returncode', None)}
        receipt['build_regressions_pass'] = False; save()
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt.get('build_regressions_pass') else 2


if __name__ == '__main__': raise SystemExit(main())
