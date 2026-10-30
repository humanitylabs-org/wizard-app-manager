#!/usr/bin/python3
"""Static source audit ONLY: no test imports, VM, Docker, install or job submission.

Use python3 -I -B. Inputs are saved synthetic build metadata and public source.
This does not make a native diagnostic admissible or identify the failed service.
"""
import argparse
import ast
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import urllib.request

BACKEND_REV = '595523fb8f930d387d5cfb0a7aa6c7d7031c05e2'
PRODUCT_REV = '610716410029f2f70fe4233102584c30e9b9022f'
UPSTREAM_REV = 'd377b7c52567c8a852f095d5b6dd8194c66f5a62'
CLI_REV = 'c5d951ed0c92c57964b607d4e9e74db820a94a84'
BACKEND_ID = 'sha256:1c10570477becada8c613a55ff402f12b49d54e74fd9e16fefba678d57ead426'
BACKEND_ARCHIVE_SHA = 'a953c4ce3590bc1448bacb0b5b259334a99a608fb0377af425daf87c888d49c0'
PUBLIC_PATHS = ('internal/assets/docker-compose.yml', 'internal/assets/assets.go',
                'internal/commands/start.go', 'internal/commands/prepare.go',
                'internal/utils/env.go')
TRACE_PATHS = ('install.sh', 'scripts/prepare-named-user.sh',
               'scripts/prepare-alongside.py', 'wam/release_pins.py', 'Dockerfile',
               'docker-compose.prod.yml', 'packages/backend/src/main.ts',
               'packages/backend/src/core/health/health.controller.ts',
               'packages/backend/src/core/config/configuration.service.ts',
               'packages/backend/src/core/cache/cache.service.ts',
               'packages/backend/src/core/database/database.service.ts',
               'packages/backend/src/modules/queue/queue.factory.ts',
               'packages/backend/src/modules/queue/queue.health.ts')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def git(*args):
    return subprocess.check_output(['git', *args], timeout=60)


def put(path, raw):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never replace a previously sealed public snapshot with different bytes.
    if path.exists():
        assert path.read_bytes() == raw, 'immutable-static-snapshot-collision'
    else:
        path.write_bytes(raw)
        path.chmod(0o600)


def audit(args):
    root = Path(git('rev-parse', '--show-toplevel').decode().strip())
    rows = json.loads(args.backend_inputs.read_text())
    assert len(rows) == 622 and len({r['path'] for r in rows}) == 622
    with tarfile.open(fileobj=io.BytesIO(git('archive', 'HEAD')), mode='r:') as archive:
        members = {m.name: m for m in archive.getmembers()}
        for row in rows:
            member = members[row['path']]
            if member.issym():
                raw = member.linkname.encode()
            else:
                stream = archive.extractfile(member)
                assert stream is not None
                raw = stream.read()
            actual = {'path': member.name, 'kind': 'symlink' if member.issym() else 'file',
                      'mode': member.mode, 'sha256': sha(raw)}
            assert actual == row, 'backend-input-closure-mismatch'
    backend = json.loads(args.backend_json.read_text())
    fields = json.loads(args.image_fields.read_text())
    assert backend['archive_sha256'] == BACKEND_ARCHIVE_SHA
    assert backend['config_sha256'] == BACKEND_ID.split(':')[1]
    assert fields['source_record_sha256'] == backend['proof_files']['evidence/backend-image-inspect.json']
    assert fields['Id'] == BACKEND_ID and fields['Architecture'] == 'amd64'
    assert fields['RootFS']['Layers'] == backend['rootfs']['diff_ids']
    assert fields['config_source_bound_fields'] == {
        'Healthcheck': None, 'Cmd': ['node', './main.mjs'],
        'Entrypoint': ['docker-entrypoint.sh'], 'User': '', 'WorkingDir': '/app'}
    assert git('ls-tree', UPSTREAM_REV, 'cli').decode().split()[2] == CLI_REV
    product_delta = git('diff', '--name-only', PRODUCT_REV, 'HEAD').decode().splitlines()
    assert all(p.startswith(('qa/', 'tests/')) for p in product_delta)
    snapshots = []
    for revision, paths in ((PRODUCT_REV, TRACE_PATHS), (BACKEND_REV, TRACE_PATHS[4:]),
                            (UPSTREAM_REV, ('docker-compose.prod.yml', 'scripts/install.sh',
                              'packages/backend/src/core/database/drizzle/0032_app_migrations.sql',
                              'packages/backend/src/core/database/drizzle/0033_migration_recovery.sql'))):
        for path in paths:
            raw = git('show', revision + ':' + path)
            if path.endswith('.py'):
                ast.parse(raw)
            target = args.public_dir / revision / path
            put(target, raw)
            snapshots.append({'revision': revision, 'path': path, 'sha256': sha(raw),
                              'bytes': len(raw), 'snapshot': str(target)})
    for path in PUBLIC_PATHS:
        url = 'https://raw.githubusercontent.com/runtipi/cli/' + CLI_REV + '/' + path
        target = args.public_dir / CLI_REV / path
        if args.fetch_public:
            with urllib.request.urlopen(url, timeout=30) as response:
                raw = response.read(131073)
            assert len(raw) <= 131072
            put(target, raw)
        raw = target.read_bytes()
        snapshots.append({'revision': CLI_REV, 'path': path, 'url': url,
                          'sha256': sha(raw), 'bytes': len(raw), 'snapshot': str(target)})
    health = git('show', BACKEND_REV + ':packages/backend/src/core/health/health.controller.ts')
    assert b'queueHealthIndicator.isHealthy' in health
    assert b'curl' in (args.public_dir / CLI_REV / PUBLIC_PATHS[0]).read_bytes()
    capacity = json.loads(args.capacity.read_text())
    assert capacity['reserve_bytes'] == 217179869184
    assert capacity['original_pending_bytes'] == 60011827200
    assert capacity['conservative_required_bytes'] == 217179869184 + 60011827200 + 8589934592
    return {'schema': 'installer-health-static-preflight-v1',
            'static_only': True, 'runtime_executed': False, 'tests_executed': 0,
            'native_job_prepared': False, 'native_job_submitted': False,
            'revision': git('rev-parse', 'HEAD').decode().strip(),
            'branch': git('branch', '--show-current').decode().strip(),
            'product_revision': PRODUCT_REV, 'product_delta_qa_tests_only': True,
            'backend_revision': BACKEND_REV, 'backend_inputs_equal': len(rows),
            'backend_inputs_sha256': sha(args.backend_inputs.read_bytes()),
            'backend_archive_hash_inherited_not_rehashed': BACKEND_ARCHIVE_SHA,
            'backend_id': BACKEND_ID, 'backend_layer_count': len(backend['rootfs']['diff_ids']),
            'backend_config_fields_verified': True,
            'service_archive_not_accessed_or_rebuilt': True,
            'public_upstream_release': 'v4.11.0', 'public_upstream_revision': UPSTREAM_REV,
            'public_cli_gitlink': CLI_REV, 'historical_610_upstream_identity_proven': False,
            'historical_610_base_images_pinned': False,
            'failed_service_identified': False, 'product_fix_proposed': False,
            'source_snapshots': snapshots, 'capacity': capacity,
            'status': 'BLOCKED_CAPACITY_AND_HISTORICAL_BASE_IDENTITY'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('backend-inputs', 'backend-json', 'image-fields', 'capacity', 'public-dir', 'out'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--fetch-public', action='store_true')
    args = parser.parse_args()
    report = audit(args)
    put(args.out, (json.dumps(report, sort_keys=True, indent=2) + '\n').encode())
    print(json.dumps({k: report[k] for k in ('static_only', 'backend_inputs_equal',
          'backend_layer_count', 'tests_executed', 'native_job_prepared', 'status')}, sort_keys=True))


if __name__ == '__main__':
    main()
