#!/usr/bin/env python3
"""Worker-only actual pinned custom-tree rehearsal; never touches the source tree."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from wam import builder_bundle as bb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--local-copy', type=Path, required=True)
    ap.add_argument('--state-root', type=Path, required=True)
    a = ap.parse_args()
    spec = bb._local_spec('local-builder-20261103')
    bb._matching_local(a.local_copy, spec['variant'])
    a.state_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=a.state_root, prefix='local-builder-proof-') as td:
        root = Path(td)
        osh, hh = root / 'os', root / 'hermes'
        skill = osh / 'system-skills/wizard-builder'
        shutil.copytree(a.local_copy, skill)
        hh.mkdir()
        (hh / 'config.yaml').write_text('model: unchanged\n')
        (hh / 'skills').mkdir()
        alias = hh / 'skills/wizard-builder'
        alias.symlink_to(skill)
        before = bb._local_snapshot(skill)
        os.environ['WIZARD_OS_HOME'] = str(osh)
        try:
            bb.install(osh)
        except ValueError as e:
            assert 'Unmanaged wizard-builder' in str(e)
        else:
            raise AssertionError('Normal install overwrote custom tree')
        bb.check_collisions(hh, osh, migrate_local_copy=spec['variant'])
        out = bb.install(osh, migrate_local_copy=spec['variant'])
        saved = Path(out['preserved_local_copy'])
        assert bb._local_snapshot(saved) == before == spec['entries']
        bb.register(hh, osh)
        assert all(r[1] for r in bb.health(osh, hh))
        assert alias.resolve() == skill.resolve()
        for source in (bb.SOURCE / 'system-skills/wizard-builder').rglob('*'):
            if source.is_file():
                assert source.read_bytes() == (skill / source.relative_to(bb.SOURCE / 'system-skills/wizard-builder')).read_bytes()
        assert bb.install(osh)['sha256'] == out['sha256']
        result = bb.restore_local_copy(osh, saved.name)
        assert bb._local_snapshot(skill) == before
        assert alias.is_symlink() and alias.resolve() == skill
        assert (osh / 'builder/releases' / out['release']).is_dir()
        print(json.dumps({'state': 'pass', 'variant': spec['variant'], 'source': str(bb.SOURCE),
                          'package_sha256': out['sha256'], 'preserved_snapshot': before,
                          'normal_refusal': True, 'installed_exact_source': True,
                          'doctor_pass': True, 'idempotent': True, 'restored_original': result['state']}, indent=2))


if __name__ == '__main__':
    main()
