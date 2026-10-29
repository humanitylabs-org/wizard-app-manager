#!/usr/bin/env python3
"""Vendor a verified immutable Wizard OS style snapshot, or check it for drift.

No network dependency in app generation. Run this only when refreshing the style pin.
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAPPING = {'style/tokens.css': 'web/wizard-tokens.css', 'style/ui.css': 'web/wizard-ui.css', 'style/boot.js': 'web/boot.js',
           'style/sheet.js': 'web/wizard-sheet.js', 'style/standalone-tokens.css': 'web/standalone-tokens.css'}
TEMPLATE = ROOT / 'templates/hello-wizard'
LOCK = TEMPLATE / 'appearance-provenance.json'


def sha(b):
    return hashlib.sha256(b).hexdigest()


def check(source=None):
    lock = json.loads(LOCK.read_text())
    assert lock['schema'] == 3 and lock['files'].keys() == MAPPING.keys()
    assert lock['origin_repo'] == 'https://github.com/humanitylabs-org/wizard-os'
    assert len(lock['revision']) == 40 and all(c in '0123456789abcdef' for c in lock['revision'])
    for src, dst in MAPPING.items():
        item = lock['files'][src]
        assert item['destination'] == dst
        assert sha((TEMPLATE / dst).read_bytes()) == item['sha256'], dst + ' drifted'
        if source:
            assert sha((source / src).read_bytes()) == item['sha256'], src + ' diverged from source'
    for src, dst in [('style/check_style.py', 'wam/style_check.py'), ('style/style-policy.json', 'wam/style-policy.json')]:
        item = lock.get('gate_files', {}).get(src, {})
        assert item.get('destination') == dst and sha((ROOT / dst).read_bytes()) == item.get('sha256'), dst + ' gate drifted'
        if source: assert (ROOT / dst).read_bytes() == (source / src).read_bytes(), src + ' gate diverged'
    print('PASS: template runtime assets and style gate match the immutable style/ pin.')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path)
    ap.add_argument('--revision')
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    if args.check:
        check(args.source)
        return
    if not args.source or not args.revision:
        ap.error('refresh requires --source <wizard-os checkout> --revision <full SHA>')
    actual = subprocess.check_output(['git', '-C', str(args.source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != args.revision:
        ap.error('source HEAD differs from requested immutable revision')
    subprocess.run(['python3', str(args.source / 'style/generate_tokens.py'), '--check'], check=True)
    for src in MAPPING:
        committed = subprocess.check_output(['git', '-C', str(args.source), 'show', f'{actual}:{src}'])
        if committed != (args.source / src).read_bytes():
            ap.error(src + ' has uncommitted changes')
    lock = {'schema': 3, 'component': 'wizard-ui-appearance-v1', 'origin_repo': 'https://github.com/humanitylabs-org/wizard-os', 'revision': actual, 'source_thread': 'wizard_1791807054943_dc70b2bccb22', 'license': 'GPL-3.0-only', 'modified': False, 'files': {}}
    for src, dst in MAPPING.items():
        content = (args.source / src).read_bytes()
        (TEMPLATE / dst).write_bytes(content)
        lock['files'][src] = {'destination': dst, 'sha256': sha(content)}
    lock['gate_files'] = {}
    for src, dst in [('style/check_style.py', 'wam/style_check.py'), ('style/style-policy.json', 'wam/style-policy.json')]:
        content = (args.source / src).read_bytes()
        (ROOT / dst).write_bytes(content)
        lock['gate_files'][src] = {'destination': dst, 'sha256': sha(content)}
    LOCK.write_text(json.dumps(lock, indent=2) + '\n')
    import sys
    sys.path.insert(0, str(ROOT))
    from wam.builder_assets import package_inline
    package_inline(TEMPLATE)
    check(args.source)


if __name__ == '__main__':
    main()
