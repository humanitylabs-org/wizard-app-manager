#!/usr/bin/env python3
"""Durable Thales full-suite runner; caller supplies the staged source manifest.

Run via wizard_heavy_submit, never as a detached SSH child. Preparation (installing
workspace dependencies and a pytest/PyYAML venv) may be done before admission.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
manifest_path = Path(sys.argv[1])
manifest = json.loads(manifest_path.read_text())
for name, digest in manifest['files'].items():
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
        raise SystemExit(f'Staged source mismatch: {name}')

env = dict(os.environ)
env['PATH'] = '/opt/homebrew/bin:' + env.get('PATH', '')
evidence = root / 'qa-evidence'
evidence.mkdir(exist_ok=True)
phases = {}

def phase(name, argv, cwd=root, timeout=600):
    with (evidence / f'{name}.log').open('w') as log:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    data = (evidence / f'{name}.log').read_bytes()
    text = re.sub(r'\x1b\[[0-9;]*m', '', data.decode(errors='replace'))
    phases[name] = {'exit_code': result.returncode, 'log_sha256': hashlib.sha256(data).hexdigest(),
                    'summary': text[-2000:]}
    print(json.dumps({'phase': name, **phases[name]}), flush=True)
    (evidence / 'receipt.json').write_text(json.dumps({'source_revision':manifest['revision'], 'phases':phases}, indent=2)+'\n')
    return result.returncode == 0

if phase('common-build', ['bun','run','--cwd','packages/common','build']):
    phase('backend-types', ['bun','run','--cwd','packages/backend','tsc'])
    phase('backend-full', ['node',str(root/'node_modules/vitest/vitest.mjs'),'run','--maxWorkers=2'], root/'packages/backend', 1200)
phase('python-full', [str(root/'.venv/bin/python'),'-m','pytest','tests','-q'], timeout=1200)
# No Docker copy runs in the full-suite job; that is a separate explicit canary.
print(json.dumps({'source_revision':manifest['revision'],'passed':all(p['exit_code']==0 for p in phases.values()),'phases':phases}), flush=True)
raise SystemExit(0 if all(p['exit_code']==0 for p in phases.values()) else 1)
