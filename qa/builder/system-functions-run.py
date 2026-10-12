"""Worker receipt runner: bounded commands under the shared heavy-worker lock."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

p = argparse.ArgumentParser()
p.add_argument('--source', type=Path, required=True)
p.add_argument('--out', type=Path, required=True)
p.add_argument('--sha', required=True)
p.add_argument('--tree-sha256', required=True)
p.add_argument('--core', type=Path, required=True)
p.add_argument('--node', required=True)
p.add_argument('--lock', type=Path, default=Path('/Volumes/ThalesMedia/AndroidBuilds/worker.lock'))
a = p.parse_args()
a.out.mkdir(parents=True, exist_ok=True)
commands = []
start = time.monotonic()
(a.out/'tmp').mkdir(exist_ok=True)
env = dict(os.environ, PYTHONPATH=str(a.source), QA_PYTHON=sys.executable,
           TMPDIR=str(a.out/'tmp'), PATH=str(Path(sys.executable).parent) + ':/opt/homebrew/bin:' + os.environ.get('PATH', ''))

def run(name, cmd, expect=0):
    begin = time.monotonic()
    r = subprocess.run(cmd, cwd=a.source, env=env, capture_output=True, text=True, timeout=300)
    (a.out / (name + '.log')).write_text(r.stdout + r.stderr)
    rec = {'name': name, 'command': cmd, 'exit_code': r.returncode, 'expected': expect, 'seconds': round(time.monotonic()-begin, 3)}
    commands.append(rec)
    print(json.dumps(rec), flush=True)
    if r.returncode != expect:
        print(r.stdout + r.stderr, flush=True)
        raise RuntimeError(name + ' failed')
    return r

def tree_digest(root):
    h = hashlib.sha256()
    files = [x for x in root.rglob('*') if x.is_file() and not any(k in {'.git', '__pycache__', '.pytest_cache'} for k in x.relative_to(root).parts)]
    for x in sorted(files, key=lambda x: x.relative_to(root).as_posix()):
        h.update(x.relative_to(root).as_posix().encode() + b'\0' + hashlib.sha256(x.read_bytes()).digest())
    return h.hexdigest()


result = {'host': socket.gethostname(), 'source_sha': a.sha, 'source_tree_sha256': a.tree_sha256, 'source': str(a.source), 'core': str(a.core), 'lock': str(a.lock), 'commands': commands, 'utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
try:
    with a.lock.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if tree_digest(a.source) != a.tree_sha256:
            raise RuntimeError('Transferred source does not match supplied exact tree hash')
        result['lock_wait_seconds'] = round(time.monotonic()-start, 3)
        run('pytest', [sys.executable, '-m', 'pytest', '-q', 'tests', '--junitxml=' + str(a.out/'pytest.xml')])
        run('node-syntax', [a.node, '--check', 'qa/builder/system-functions-browser.cjs'])
        run('actual-core-audit', [sys.executable, '-c',
            "import hashlib,json; from pathlib import Path; from wam.system_functions import check; p=Path(" + repr(str(a.core)) + "); trust={str(x.resolve()):hashlib.sha256(x.read_bytes()).hexdigest() for x in p.glob('app-*.js')}; rows=check(p,trust); print(json.dumps(rows)); assert all(r['ok'] for r in rows)"])
        new = run('scaffold', [sys.executable, '-m', 'wam', 'new', 'system-gate-demo', '--dev-root', str(a.out/'dev')])
        generated = a.out/'dev/system-gate-demo'
        run('check', [sys.executable, '-m', 'wam', 'check', str(generated)])
        run('check-proposal', [sys.executable, '-m', 'wam', 'check', str(generated), '--proposal'])
        env['WIZARD_OS_HOME'] = str(a.out/'os-home')
        run('stage', [sys.executable, '-m', 'wam', 'install-local', str(generated), '--stage-only'])
        run('skill-install', [sys.executable, '-m', 'wam', 'builder-skill', '--hermes-home', str(a.out/'hermes-home')])
        installed = a.out/'os-home/system-skills/wizard-builder'
        ref = installed/'references/system-functions.md'
        if not ref.is_file() or 'references/system-functions.md' not in (installed/'SKILL.md').read_text():
            raise RuntimeError('Installed system skill lost mandatory reference')
        result['installed_reference_sha256'] = hashlib.sha256(ref.read_bytes()).hexdigest()
        # Use the exact staged source copy in the browser, never the mutable scaffold.
        stage = json.loads((a.out/'stage.log').read_text())
        run('browser', [a.node, 'qa/builder/system-functions-browser.cjs', '--source', stage['source'], '--core', str(a.core), '--out', str(a.out/'browser')])
        bad = generated/'web/local-platform.js'; bad.write_text('navigator.share({text:"app duplicate"}); new MediaRecorder(stream);')
        run('negative-cli', [sys.executable, '-m', 'wam', 'check', str(generated)], expect=1)
        bad.unlink()
        duplicate = generated/'web/app-client.js'
        duplicate.write_bytes((a.core/'app-client.js').read_bytes())
        run('negative-actual-client-copy', [sys.executable, '-m', 'wam', 'check', str(generated)], expect=1)
        duplicate.unlink()
        if tree_digest(a.source) != a.tree_sha256:
            raise RuntimeError('Source changed during verification')
        result['verified_source_tree_sha256'] = tree_digest(a.source)
        result['ok'] = True
except Exception as e:
    result['ok'] = False
    result['error'] = str(e)
finally:
    result['seconds'] = round(time.monotonic()-start, 3)
    (a.out/'receipt.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2), flush=True)
sys.exit(0 if result['ok'] else 1)
