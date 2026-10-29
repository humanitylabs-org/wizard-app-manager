#!/usr/bin/env python3
"""Static source/asset closure inventory; never substitutes for native/Nest proof."""
import argparse
import hashlib
import json
from pathlib import Path

GROUPS = {
    '/app/assets': 'packages/backend/assets',
    '/app/assets/migrations': 'packages/backend/src/core/database/drizzle',
    '/app/assets/translations': 'packages/backend/src/modules/i18n/translations',
    '/app/assets/frontend': 'packages/frontend/dist/client',
}
PINS = [
    'bun.lock', 'package.json', 'packages/backend/package.json', 'packages/common/package.json',
    'Dockerfile', 'packages/backend/build.ts', 'packages/backend/tsconfig.json',
    'packages/backend/src/main.ts', 'packages/backend/src/app.module.ts', 'packages/backend/src/app.service.ts',
    'packages/backend/src/common/constants.ts', 'packages/backend/src/common/helpers/env-helpers.ts',
    'packages/backend/src/core/config/configuration.module.ts',
    'packages/backend/src/core/config/configuration.service.ts',
    'packages/backend/src/core/database/database.module.ts',
    'packages/backend/src/core/database/database.service.ts',
    'packages/backend/src/core/cache/cache.service.ts',
    'packages/backend/src/core/password/password.service.ts',
    'packages/backend/src/modules/auth/auth.module.ts',
    'packages/backend/src/modules/app-lifecycle/app-lifecycle.module.ts',
    'packages/backend/src/modules/app-lifecycle/app-lifecycle.controller.ts',
    'packages/backend/src/modules/app-lifecycle/app-lifecycle.service.ts',
    'packages/backend/src/modules/app-lifecycle/app-lifecycle-command.factory.ts',
    'packages/backend/src/modules/app-lifecycle/app-status-sync.service.ts',
    'packages/backend/src/modules/app-lifecycle/handlers/install-app.handler.ts',
    'packages/backend/src/modules/app-lifecycle/commands/command.ts',
    'packages/backend/src/modules/app-lifecycle/commands/install-app-command.ts',
    'packages/backend/src/modules/app-lifecycle/dto/app-lifecycle.dto.ts',
    'packages/backend/src/modules/app-lifecycle/services/status-manager.service.ts',
    'packages/backend/src/modules/apps/apps.module.ts',
    'packages/backend/src/modules/marketplace/marketplace.module.ts',
    'packages/backend/src/modules/docker/docker.module.ts',
    'packages/backend/src/modules/docker/docker.service.ts',
    'packages/backend/src/modules/docker/mandatory-seal.ts',
    'packages/backend/src/modules/docker/wizard-storage.ts',
    'packages/backend/src/modules/network/network.module.ts',
    'packages/backend/src/modules/network/subnet-manager.service.ts',
    'packages/backend/src/modules/queue/queue.module.ts',
    'packages/backend/src/modules/queue/queue.factory.ts',
    'packages/backend/src/modules/queue/queue.entity.ts',
    'packages/backend/src/cli/enroll-mandatory-seal.ts',
]


def inventory(root):
    def row(path):
        assert path.is_file() and not path.is_symlink(), f'nonregular source asset: {path}'
        blob = path.read_bytes()
        return {'source': str(path.relative_to(root)), 'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
    groups = {}
    for destination, relative in GROUPS.items():
        directory = root/relative
        files = [row(p) for p in sorted(directory.rglob('*')) if p.is_file()] if directory.exists() else []
        groups[destination] = {'source': relative, 'exists': directory.exists(), 'files': files,
                               'file_count': len(files), 'bytes': sum(f['bytes'] for f in files)}
    return {
        'schema': 1, 'kind': 'static source and asset inventory, not a runtime/image closure',
        'source_pins': [row(root/p) for p in PINS], 'asset_groups': groups,
        'not_measured': ['new backend bundle and native outputs', 'donor runtime external module closure',
                         'dynamic loader/ABI compatibility', 'real root Nest DI and HTTP readiness'],
        'installed_path_pass': False, 'RUNTIPI_ADMITTED': False,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    evidence = inventory(Path(args.root))
    with Path(args.output).open('x') as output:
        json.dump(evidence, output, indent=2)
        output.write('\n')
    print(json.dumps({k: {'source': v['source'], 'exists': v['exists'], 'files': v['file_count'], 'bytes': v['bytes']}
                      for k, v in evidence['asset_groups'].items()}, indent=2))
