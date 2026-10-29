#!/usr/bin/env python3
"""Parent-admitted forum stages. Focused verification never builds an image.

No execution on VPS, pruning, capacity changes, real Private data or deployment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

BASE = 'fead192e9fb2d560fd3b875d092819b32e7189b6'
NODE = Path('/srv/work/heavy/incognito-20261027/tooling/node')
NODE_SHA = 'e6ec2c188d83d813f81f2de8aea084d74dce603ac1abedd0a30ad941b10087b2'
OLD_IMAGE = 'wizard-app-manager:grants-fix-fead192e9fb2-amd64'
OLD_ID = 'sha256:908ba820e904091ddd38402404fdde212ca2f14b61b33c621dfe134d635f8ce9'
GIB = 1024 ** 3
RESERVE = 200 * GIB
# Conservative peak incremental working sets: two dependency closures, compiler
# output, tests/trace/second Python image; separately BuildKit + image export.
FOOTPRINT = {'focused': 16 * GIB, 'image': 40 * GIB}


def digest(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def capacity(path, footprint):
    free = shutil.disk_usage(path).free
    needed = RESERVE + footprint
    print(json.dumps({'free_bytes': free, 'reserve_bytes': RESERVE,
                      'additional_footprint_bytes': footprint, 'required_bytes': needed}), flush=True)
    assert free >= needed, f'capacity blocked: {free} < reserve {RESERVE} + footprint {footprint}; no cleanup authorized'
    return free


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--phase', choices=FOOTPRINT, required=True)
    args = p.parse_args()
    root = Path.cwd().resolve()
    assert str(root).startswith('/srv/work/manager-seal-'), root
    assert os.uname().machine == 'x86_64', 'forum native amd64 only'
    assert os.getuid() != 0, 'synthetic worker must be nonroot'
    assert NODE.is_file() and digest(NODE) == NODE_SHA, 'node toolchain drift'
    manifest = json.loads((root / 'SOURCE.json').read_text())
    assert manifest['head'] == args.source and manifest['baseline'] == BASE
    for name, expected in manifest['files'].items():
        file = root / name
        assert file.is_file() and not file.is_symlink() and digest(file) == expected, f'source drift: {name}'
    baseline_archive = root / 'baseline.tar'
    assert digest(baseline_archive) == manifest['baseline_tar_sha256']
    # Initial closure must contain only the source artifact. Subsequent image
    # stage reuses the verified focused workspace's installed dependencies.
    if args.phase == 'focused':
        inventory = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
        assert inventory == set(manifest['files']) | {'SOURCE.json', 'baseline.tar'}, 'unexpected staged files'
    free = capacity(root, FOOTPRINT[args.phase])
    qa = root / 'qa'
    qa.mkdir(exist_ok=args.phase == 'image')
    os.environ['TMPDIR'] = str(qa / 'tmp')
    Path(os.environ['TMPDIR']).mkdir(exist_ok=True)
    owner = f'manager-seal-{args.source[:12]}'
    receipt = {'source_head': args.source, 'baseline': BASE, 'source_manifest_sha256': digest(root/'SOURCE.json'),
               'runner_sha256': digest(Path(__file__)),
               'phase': args.phase, 'node_sha256': NODE_SHA, 'disk_free_before': free,
               'reserve_bytes': RESERVE, 'footprint_bytes': FOOTPRINT[args.phase],
               'RUNTIPI_ADMITTED': False, 'started': int(time.time()), 'steps': {}}
    tools = qa / 'bin'
    tools.mkdir(exist_ok=True)
    node_link = tools / 'node'
    if not node_link.exists(): node_link.symlink_to(NODE)
    env = dict(os.environ, PATH=f'{tools}:{os.environ["PATH"]}', DOCKER_BUILDKIT='1', NODE_OPTIONS='--experimental-sqlite', RUN_MANAGER_SEAL_DOCKER='0')

    def run(label, command, cwd=root, timeout=900, allow_failure=False):
        print(label + ': ' + ' '.join(map(str, command)), flush=True)
        with (qa / f'{label}.log').open('w') as log:
            proc = subprocess.run(list(map(str, command)), cwd=cwd, env=env, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout)
        receipt['steps'][label] = proc.returncode
        (qa/f'{args.phase}-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
        if proc.returncode and not allow_failure:
            raise RuntimeError(f'{label} failed: {proc.returncode}; see {qa/label}.log')
        return proc.returncode

    target = 'src/modules/docker/__tests__/mandatory-seal.test.ts'
    owned = False
    try:
        if args.phase == 'focused':
            for command in [['docker', 'ps', '-aq'], ['docker', 'network', 'ls', '-q']]:
                assert not subprocess.check_output(command+['--filter', f'label=wizard.qa.manager-seal={owner}'], text=True).strip(), 'refuse pre-existing job resources'
            owned = True
            assert subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', OLD_IMAGE], text=True).strip() == OLD_ID
            run('bun-image', ['docker', 'pull', 'oven/bun:1.3.0'], timeout=300)
            receipt['bun_image_id'] = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'oven/bun:1.3.0'], text=True).strip()
            # Public tool binaries from disposable, never-started image creates;
            # no running service/container env or owner state is inspected.
            for image, member, name in [(OLD_IMAGE, '/usr/local/bin/docker-compose', 'docker-compose'),
                                         ('oven/bun:1.3.0', '/usr/local/bin/bun', 'bun')]:
                cid = subprocess.check_output(['docker', 'create', '--label', f'wizard.qa.manager-seal={owner}', image], text=True).strip()
                try: subprocess.run(['docker', 'cp', cid+':'+member, str(tools/name)], check=True, timeout=60)
                finally: subprocess.run(['docker', 'rm', cid], check=True, timeout=60)
                (tools/name).chmod(0o755)
                receipt[name+'_sha256'] = digest(tools/name)
            run('bun-version', ['bun', '--version'])
            assert (qa/'bun-version.log').read_text().strip() == '1.3.0'
            run('compose-version', ['docker-compose', 'version', '--short'])
            assert (qa/'compose-version.log').read_text().strip().lstrip('v') == '2.40.0'
            run('dependencies', ['bun', 'install', '--frozen-lockfile'], timeout=660)
            run('common-build', ['bun', 'run', 'build'], cwd=root/'packages/common', timeout=300)
            # First diagnostic gate: no fixture activation or manager image
            # packaging before types + units (public tool binaries are needed).
            run('backend-types', [root/'node_modules/.bin/tsgo', '--noEmit'], cwd=root/'packages/backend', timeout=300)
            run('backend-unit', [root/'node_modules/.bin/vitest', 'run', '--reporter=json', '--outputFile='+str(qa/'backend-unit.json')], cwd=root/'packages/backend')
            capacity(root, 8 * GIB)
            for version in ('3.12', '3.13'):
                run(f'fixture-pull-{version}', ['docker', 'pull', f'python:{version}-slim'], timeout=300)
            for key, version in [('MANAGER_SEAL_TEST_IMAGE', '3.12'), ('MANAGER_SEAL_TEST_IMAGE_V2', '3.13')]:
                env[key] = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', f'python:{version}-slim'], text=True).strip()
                receipt[key.lower()] = env[key]
            env.update(RUN_MANAGER_SEAL_DOCKER='1', MANAGER_SEAL_QA_OWNER=owner,
                       MANAGER_SEAL_TRACE_DIR=str(qa/'candidate-trace'))
            run('candidate-real-manager', [root/'node_modules/.bin/vitest', 'run', target,
                '-t', 'real Docker', '--reporter=json', '--outputFile='+str(qa/'candidate.json')], cwd=root/'packages/backend', timeout=300)
            # Unchanged tested DockerService/Storage, only helper+fixture injected.
            baseline = qa/'baseline'
            capacity(root, 4 * GIB)
            baseline.mkdir()
            run('baseline-extract', ['tar', '-xf', baseline_archive, '-C', baseline], timeout=120)
            for relative in [target, 'src/modules/docker/mandatory-seal.ts']:
                dst = baseline/'packages/backend'/relative
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root/'packages/backend'/relative, dst)
            run('baseline-dependencies', ['bun', 'install', '--frozen-lockfile'], cwd=baseline, timeout=660)
            run('baseline-common-build', ['bun', 'run', 'build'], cwd=baseline/'packages/common', timeout=300)
            env['MANAGER_SEAL_TRACE_DIR'] = str(qa/'baseline-trace')
            code = run('baseline-negative', [baseline/'node_modules/.bin/vitest', 'run', target,
                '-t', 'real Docker', '--reporter=json', '--outputFile='+str(qa/'baseline.json')], cwd=baseline/'packages/backend', timeout=300, allow_failure=True)
            assert code != 0, 'baseline unexpectedly passed'
            report = json.loads((qa/'baseline.json').read_text())
            failed = [t for suite in report['testResults'] for t in suite['assertionResults'] if t['status'] == 'failed']
            assert len(failed) == 1 and 'disabled, removed' in failed[0]['fullName'] and '_public_app' in '\n'.join(failed[0]['failureMessages']), 'baseline red was not the original network bypass'
            receipt['baseline_bypass_reproduced'] = True
            # No active test resources may survive; no global prune or deletion.
            leftovers = subprocess.check_output(['docker', 'ps', '-aq', '--filter', f'label=wizard.qa.manager-seal={owner}'], text=True).strip()
            assert not leftovers, 'synthetic containers leaked'
            assert not subprocess.check_output(['docker', 'network', 'ls', '-q', '--filter', f'label=wizard.qa.manager-seal={owner}'], text=True).strip(), 'synthetic networks leaked'
            receipt['focused_pass'] = True
            receipt['image_built'] = False
            receipt['api_acceptance'] = 'PENDING: separately admitted image-bound disposable API lifecycle and rollback'
        else:
            focused_path = qa/'focused-receipt.json'
            focused = json.loads(focused_path.read_text())
            assert focused['source_head'] == args.source and focused['source_manifest_sha256'] == receipt['source_manifest_sha256']
            assert focused.get('focused_pass') is True, 'focused tests must pass before image packaging'
            receipt['focused_receipt_sha256'] = digest(focused_path)
            image = f'wizard-manager-seal:{args.source[:12]}'
            run('image-build', ['docker', 'build', '--platform', 'linux/amd64', '--build-arg', 'LOCAL=true',
                '--build-arg', 'TIPI_VERSION=manager-seal-stage', '--build-arg', 'DOCKER_COMPOSE_VERSION=v2.40.0',
                '--label', 'org.opencontainers.image.revision='+args.source,
                '--label', 'org.opencontainers.image.source=https://github.com/humanitylabs-org/wizard-app-manager',
                '-t', image, '.'], timeout=3600)
            run('image-inspect', ['docker', 'image', 'inspect', '--format', '{{.Id}}', image], timeout=60)
            receipt['image_id'] = (qa/'image-inspect.log').read_text().strip()
            assert subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{index .Config.Labels "org.opencontainers.image.revision"}}', image], text=True).strip() == args.source
            capacity(root, 10 * GIB)
            artifact = qa/'manager-image.tar.gz'
            with artifact.open('wb') as output:
                docker = subprocess.Popen(['docker', 'save', image], stdout=subprocess.PIPE)
                assert docker.stdout is not None
                gzip = subprocess.Popen(['gzip', '-1'], stdin=docker.stdout, stdout=output)
                docker.stdout.close()
                assert gzip.wait(timeout=600) == 0 and docker.wait(timeout=60) == 0
            receipt['image_archive_sha256'] = digest(artifact)
            receipt['image_built'] = True
            receipt['api_acceptance'] = 'PENDING, no aggregate admission'
    except Exception as exc:
        receipt['error'] = str(exc)
        raise
    finally:
        if owned:
            # Aborted/failed fixtures get exact job-owned fallback cleanup only.
            # Leaks never become a green run merely because cleanup succeeded.
            for command, remove, kind in [(['docker', 'ps', '-aq'], ['docker', 'rm', '-f'], 'containers'),
                                          (['docker', 'network', 'ls', '-q'], ['docker', 'network', 'rm'], 'networks')]:
                ids = subprocess.check_output(command+['--filter', f'label=wizard.qa.manager-seal={owner}'], text=True).split()
                if ids:
                    receipt['focused_pass'] = False
                    subprocess.run(remove+ids, check=True, timeout=60)
                remaining = subprocess.check_output(command+['--filter', f'label=wizard.qa.manager-seal={owner}'], text=True).split()
                receipt['cleanup_'+kind] = {'fallback_removed': len(ids), 'remaining': len(remaining)}
                assert not remaining, 'job-owned resource cleanup failed'
        receipt['finished'] = int(time.time())
        receipt['disk_free_after'] = shutil.disk_usage(root).free
        (qa/f'{args.phase}-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
        print(json.dumps(receipt, indent=2), flush=True)
    if args.phase == 'focused': assert receipt.get('focused_pass') is True


if __name__ == '__main__':
    main()
