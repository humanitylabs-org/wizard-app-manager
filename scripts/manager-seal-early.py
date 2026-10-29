#!/usr/bin/env python3
"""Parent-admitted early checks only, in a no-swap, memory/storage-capped container.

No Docker socket, owner homes/caches, baseline closure, image pull/build or API
acceptance. Dependency closure is ephemeral; this receipt cannot admit image phase.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

from manager_seal_install_evidence import dependency_step, recover_artifacts

BASE = 'fead192e9fb2d560fd3b875d092819b32e7189b6'
NODE = Path('/srv/work/heavy/incognito-20261027/tooling/node')
NODE_SHA = 'e6ec2c188d83d813f81f2de8aea084d74dce603ac1abedd0a30ad941b10087b2'
IMAGE = 'sha256:ae4e8b7c575fabea88a029b6cae757f342ca2525e07f7c22272d5105c346b35e'
BUN_URL = 'https://github.com/oven-sh/bun/releases/download/bun-v1.3.0/bun-linux-x64.zip'
BUN_ZIP_SHA = '60c39d92b8bd090627524c98b3012f0c08dc89024cfdaa7c9c98cb5fd4359376'
GIB = 1024 ** 3
RESERVE = 200 * GIB
SCRATCH = 8 * GIB
MEMORY = 12 * GIB
# No package/cache/test output can allocate host disk: /work is tmpfs, rootfs
# read-only, swap disabled, daemon logs disabled. Reserve this extra GiB for
# bounded exported evidence and Docker create/control metadata, not dependencies.
HOST_ALLOWANCE = GIB
LOG_CAP = 96 * 1024 ** 2
RESULT_PREFIX = 'EARLY_RESULT='


def digest(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''): h.update(b)
    return h.hexdigest()


def verify_source(root, source, manifest_sha):
    assert digest(root/'SOURCE.json') == manifest_sha, 'manifest drift'
    manifest = json.loads((root/'SOURCE.json').read_text())
    assert manifest['head'] == source and manifest['baseline'] == BASE
    for rel, expected in manifest['files'].items():
        file = root/rel
        assert file.is_file() and not file.is_symlink() and digest(file) == expected, rel
    assert digest(root/'baseline.tar') == manifest['baseline_tar_sha256']
    return manifest


def bunx_node_gyp_cache(uid):
    # Bun 1.3.0 bunx_command.zig uses platformTempDir(), not TMPDIR.
    return f'bunx-{uid}-node-gyp@latest'


def prepare_bunx_tmp_alias(output, uid):
    """One read-only /tmp entry; all payload writes stay on the SAME /work cap.

    Do not add another tmpfs or make /tmp/root writable. Docker bind-mounts this
    directory read-only; the absolute link resolves only INSIDE the container.
    """
    alias = output/'bootstrap-tmp'
    alias.mkdir(mode=0o700)
    name = bunx_node_gyp_cache(uid)
    (alias/name).symlink_to('/work/tmp/'+name, target_is_directory=True)
    return alias


def verify_bunx_tmp_alias(tmp, target_parent, uid):
    name = bunx_node_gyp_cache(uid)
    target = target_parent/name
    assert target.is_dir() and not target.is_symlink(), 'missing capped bunx target'
    assert sorted(p.name for p in tmp.iterdir()) == [name], 'unexpected /tmp entries'
    assert (tmp/name).is_symlink() and os.readlink(tmp/name) == str(target), 'bunx alias drift'
    assert (tmp/name).resolve() == target.resolve(), 'bunx alias escaped capped target'
    assert (tmp/name).stat().st_dev == target_parent.stat().st_dev, 'bunx cache is not on /work tmpfs'
    probe = tmp/name/'.manager-seal-alias-probe'
    with probe.open('xb') as stream:
        stream.write(b'synthetic-bunx-alias\n')
    try:
        assert (target/probe.name).read_bytes() == b'synthetic-bunx-alias\n'
    finally:
        probe.unlink()
    return {'path': str(tmp/name), 'target': str(target), 'same_tmpfs': True, 'write_through_verified': True}


def create_command(root, name, uid, gid, source, manifest_sha):
    return ['docker', 'create', '--pull=never', '--name', name,
            '--label', 'wizard.qa.manager-seal.early='+source, '--read-only',
            '--user', f'{uid}:{gid}', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
            '--memory', str(MEMORY), '--memory-swap', str(MEMORY), '--pids-limit=256',
            '--cpus=4', '--shm-size=67108864', '--log-driver=none', '--network=bridge',
            '--ulimit', 'core=0:0', '--ulimit', 'fsize=268435456:268435456',
            '--tmpfs', f'/work:rw,exec,nosuid,nodev,size={SCRATCH},mode=0700,uid={uid},gid={gid}',
            '--mount', f'type=bind,src={root},dst=/source,readonly',
            '--mount', f'type=bind,src={NODE},dst=/tooling/node,readonly',
            '--mount', f'type=bind,src={root}/manager-seal-early-results/bootstrap-tmp,dst=/tmp,readonly',
            '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'HOME=/work/home',
            '--env', 'TMPDIR=/work/tmp', '--env', 'XDG_CACHE_HOME=/work/cache',
            '--env', 'BUN_INSTALL_CACHE_DIR=/work/cache/bun', '--env', 'npm_config_cache=/work/cache/npm',
            '--env', 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1', '--env', 'RUN_MANAGER_SEAL_DOCKER=0',
            '--env', 'NODE_OPTIONS=--experimental-sqlite', '--env', 'PATH=/work/bin:/usr/local/bin:/usr/bin:/bin',
            '--workdir', '/work', '--entrypoint', 'python3', IMAGE,
            '/source/scripts/manager-seal-early.py', '--inside', '--source', source, '--manifest-sha256', manifest_sha]


def inside(args):
    # Verify actual kernel enforcement, not just Docker flag intent. Refuse v1,
    # missing cgroup visibility, extra swap or weakened limits before downloads.
    assert os.getuid() != 0
    assert int(Path('/sys/fs/cgroup/memory.max').read_text()) == MEMORY
    assert int(Path('/sys/fs/cgroup/memory.swap.max').read_text()) == 0
    assert int(Path('/sys/fs/cgroup/pids.max').read_text()) == 256
    mounts = [line.split() for line in Path('/proc/mounts').read_text().splitlines()]
    work = next(m for m in mounts if m[1] == '/work')
    assert work[2] == 'tmpfs' and os.statvfs('/work').f_blocks * os.statvfs('/work').f_frsize <= SCRATCH
    assert 'ro' in next(m for m in mounts if m[1] == '/')[3].split(',')
    assert 'ro' in next(m for m in mounts if m[1] == '/tmp')[3].split(',')
    for path in ('/source', '/tooling/node'):
        assert 'ro' in next(m for m in mounts if m[1] == path)[3].split(',')
    assert not Path('/var/run/docker.sock').exists()
    root = Path('/source')
    manifest = verify_source(root, args.source, args.manifest_sha256)
    assert digest('/tooling/node') == NODE_SHA
    checkout = Path('/work/source')
    checkout.mkdir()
    for rel in manifest['files']:
        dst = checkout/rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/rel, dst)
    for rel in ('bin', 'tmp', 'home', 'cache', 'logs'):
        Path('/work', rel).mkdir(mode=0o700)
    Path('/work/tmp', bunx_node_gyp_cache(os.getuid())).mkdir(mode=0o700)
    bunx_alias = verify_bunx_tmp_alias(Path('/tmp'), Path('/work/tmp'), os.getuid())
    Path('/work/bin/node').symlink_to('/tooling/node')
    result = {'source_head': args.source, 'source_manifest_sha256': args.manifest_sha256,
              'phase': 'early', 'scratch_bytes': SCRATCH, 'memory_bytes': MEMORY,
              'swap_bytes': 0, 'steps': {}, 'early_pass': False, 'focused_pass': False,
              'full_docker_acceptance': False, 'image_built': False, 'RUNTIPI_ADMITTED': False}
    result['bunx_tmp_alias'] = bunx_alias

    def step(label, argv, cwd=checkout, timeout=300):
        log = Path('/work/logs', label+'.log')
        print('STEP '+label, flush=True)
        with log.open('wb') as f:
            proc = subprocess.run(list(map(str, argv)), cwd=cwd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
        result['steps'][label] = {'exit_code': proc.returncode, 'log_sha256': digest(log), 'log_bytes': log.stat().st_size}
        if proc.returncode:
            with log.open('rb') as f:
                f.seek(max(0, log.stat().st_size-16384))
                print(f.read(16384).decode(errors='replace'), flush=True)
            raise RuntimeError(label+' failed: '+str(proc.returncode))

    try:
        # Public exact-version binary download, bounded compressed/uncompressed.
        archive = Path('/work/bun.zip')
        with urllib.request.urlopen(BUN_URL, timeout=60) as response, archive.open('wb') as out:
            n = 0
            while block := response.read(1024 * 1024):
                n += len(block)
                assert n <= 64 * 1024 ** 2, 'Bun download exceeded cap'
                out.write(block)
        assert digest(archive) == BUN_ZIP_SHA, 'Bun archive drift'
        with zipfile.ZipFile(archive) as z:
            info = z.getinfo('bun-linux-x64/bun')
            assert info.file_size <= 200 * 1024 ** 2
            with z.open(info) as src, Path('/work/bin/bun').open('wb') as dst:
                shutil.copyfileobj(src, dst)
        Path('/work/bin/bun').chmod(0o755)
        result['bun_zip_sha256'] = BUN_ZIP_SHA
        result['bun_sha256'] = digest('/work/bin/bun')
        step('bun-version', ['bun', '--version'])
        assert Path('/work/logs/bun-version.log').read_text().strip() == '1.3.0'
        step('runner-regressions', ['python3', '-m', 'unittest', 'discover', '-s', 'scripts/tests', '-p', 'test_manager_seal_runners.py'])
        print('STEP dependencies', flush=True)
        dependencies = {}
        result['steps']['dependencies'] = dependencies
        dependency_step(checkout, Path('/work/logs'), manifest['files']['bun.lock'], dependencies)
        if dependencies['exit_code'] or not dependencies['log_complete']:
            raise RuntimeError('dependencies failed: '+str(dependencies['exit_code'])+'; termination='+str(dependencies['termination_reason']))
        assert dependencies['comparison_complete'], 'dependency comparison failed; inspect exported evidence'
        assert digest(checkout/'bun.lock') == manifest['files']['bun.lock'], 'frozen lock changed'
        step('common-build', ['bun', 'run', 'build'], cwd=checkout/'packages/common')
        step('backend-types', [checkout/'node_modules/.bin/tsgo', '--noEmit'], cwd=checkout/'packages/backend')
        step('backend-unit', [checkout/'node_modules/.bin/vitest', 'run', '--maxWorkers=2', '--reporter=json',
                             '--outputFile=/work/backend-unit.json'], cwd=checkout/'packages/backend', timeout=900)
        report = json.loads(Path('/work/backend-unit.json').read_text())
        result['unit_counts'] = {k: report[k] for k in ('numTotalTests', 'numPassedTests', 'numFailedTests', 'numPendingTests', 'success')}
        assert report['success'] is True and report['numFailedTests'] == 0
        result['early_pass'] = True
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        result['tmpfs_used_bytes'] = (os.statvfs('/work').f_blocks-os.statvfs('/work').f_bfree)*os.statvfs('/work').f_frsize
        print(RESULT_PREFIX+json.dumps(result, sort_keys=True), flush=True)


def main(args):
    root = Path.cwd().resolve()
    assert str(root).startswith('/srv/work/manager-seal-') and os.uname().machine == 'x86_64'
    assert os.getuid() != 0
    manifest = verify_source(root, args.source, args.manifest_sha256)
    inventory = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
    assert inventory == set(manifest['files']) | {'SOURCE.json', 'baseline.tar'}, 'not a fresh stage'
    assert digest(NODE) == NODE_SHA
    free = shutil.disk_usage(root).free
    assert free >= RESERVE + HOST_ALLOWANCE, f'capacity blocked: {free} < {RESERVE + HOST_ALLOWANCE}'
    available = int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:'))) * 1024
    assert available >= MEMORY + 4 * GIB, 'insufficient available RAM for capped early worker'
    # Inspect only the exact pre-existing public fixture image; never pull.
    assert subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', IMAGE], text=True).strip() == IMAGE
    name = 'manager-seal-early-'+args.source[:12]
    assert not subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=^/'+name+'$'], text=True).strip(), 'existing owned name'
    output = root/'manager-seal-early-results'
    output.mkdir(exist_ok=False)
    prepare_bunx_tmp_alias(output, os.getuid())
    receipt = {'source_head': args.source, 'source_manifest_sha256': args.manifest_sha256,
               'runner_sha256': digest(__file__), 'image_id': IMAGE, 'phase': 'early',
               'reserve_bytes': RESERVE, 'host_allowance_bytes': HOST_ALLOWANCE,
               'disk_free_before': free, 'memory_available_before': available,
               'early_pass': False, 'focused_pass': False, 'RUNTIPI_ADMITTED': False}
    cid = None
    attached = None
    try:
        cid = subprocess.check_output(create_command(root, name, os.getuid(), os.getgid(), args.source, args.manifest_sha256), text=True, timeout=30).strip()
        config = json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .HostConfig}}', cid], text=True))
        assert config['ReadonlyRootfs'] and config['Memory'] == MEMORY and config['MemorySwap'] == MEMORY
        assert config['LogConfig']['Type'] == 'none' and config['PidsLimit'] == 256
        assert config['CapDrop'] == ['ALL'] and config['SecurityOpt'] == ['no-new-privileges:true']
        attached = subprocess.Popen(['docker', 'start', '--attach', cid], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert attached.stdout is not None
        sel = selectors.DefaultSelector()
        sel.register(attached.stdout, selectors.EVENT_READ)
        total = 0
        deadline = time.monotonic()+2400
        with (output/'early.log').open('wb') as log:
            while sel.get_map():
                assert time.monotonic() < deadline, 'early phase timeout'
                for key, _ in sel.select(timeout=1):
                    block = os.read(key.fd, 65536)
                    if not block:
                        sel.unregister(key.fileobj)
                        continue
                    total += len(block)
                    assert total <= LOG_CAP, 'host evidence byte cap'
                    log.write(block)
        code = attached.wait(timeout=10)
        receipt['attach_exit_code'] = code
        state = json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .State}}', cid], text=True))
        receipt['container_exit'] = {k: state[k] for k in ('Running', 'OOMKilled', 'ExitCode')}
        lines = (output/'early.log').read_text().splitlines()
        receipt['dependency_artifacts'] = recover_artifacts(output/'early.log', output)
        result_lines = [l[len(RESULT_PREFIX):] for l in lines if l.startswith(RESULT_PREFIX)]
        assert len(result_lines) == 1 and len(result_lines[0].encode()) <= 65536, 'missing/oversize early result; inspect capped log and OOM state'
        results = [json.loads(result_lines[0])]
        receipt['result'] = results[0]
        if 'dependencies' in results[0]['steps']:
            assert receipt['dependency_artifacts'] == results[0]['steps']['dependencies'].get('artifacts'), 'dependency export omitted/drifted'
        assert code == 0 and state['ExitCode'] == 0 and not state['OOMKilled'] and results[0]['early_pass'] is True
        assert results[0]['source_head'] == args.source and results[0]['source_manifest_sha256'] == args.manifest_sha256
        receipt['early_pass'] = True
    except Exception as exc:
        receipt['error'] = str(exc)
        raise
    finally:
        try:
            if cid:
                # Only our newly created synthetic container/tmpfs; no retained
                # files or other resources are deleted. No prune/cache/mount edits.
                subprocess.run(['docker', 'rm', '-f', cid], check=True, timeout=30, stdout=subprocess.DEVNULL)
                assert not subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=^/'+name+'$'], text=True).strip()
                receipt['owned_container_removed'] = True
            if attached is not None:
                attached.wait(timeout=10)
        except Exception as exc:
            receipt['early_pass'] = False
            receipt['cleanup_error'] = str(exc)
        receipt['disk_free_after'] = shutil.disk_usage(root).free
        receipt['reserve_preserved_after'] = receipt['disk_free_after'] >= RESERVE
        if not receipt['reserve_preserved_after']: receipt['early_pass'] = False
        payload = json.dumps(receipt, indent=2)+'\n'
        if len(payload.encode()) > 131072:
            receipt.pop('result', None)
            receipt.update(early_pass=False, error='receipt byte cap exceeded')
            payload = json.dumps(receipt, indent=2)+'\n'
        assert len(payload.encode()) <= 131072
        (output/'early-receipt.json').write_text(payload)
        print(payload, flush=True)
    assert receipt['early_pass'] is True


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--inside', action='store_true')
    args = parser.parse_args()
    if args.inside:
        inside(args)
    else:
        main(args)
