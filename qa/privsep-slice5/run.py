#!/usr/bin/env python3
"""Clean Debian 12/KVM Slice 5 acceptance, on the Linux worker only.

Usage: run.py EVIDENCE_DIR SOURCE_EXPORT CANDIDATE_SHA [--smoke|--preflight]
Reuses Slice 4's stock-Hermes, pinned bootstrap, registry mirror and finally-stop
VM implementation. Only the post-install actions differ. --preflight validates
real positional arguments and source metadata without creating evidence or a VM.
"""
import argparse
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence_dir', type=Path)
    parser.add_argument('source_export', type=Path)
    parser.add_argument('candidate_sha')
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args(argv)
    root, source = args.evidence_dir.resolve(), args.source_export.resolve()
    assert re.fullmatch('[0-9a-f]{40}', args.candidate_sha)
    assert str(root).startswith('/srv/work/privsep/slice5/') and not root.exists()
    assert source.is_dir() and (source / 'qa/privsep-slice5/guest.py').is_file()
    assert json.loads((source.parent / 'source-export.json').read_text())['head_sha'] == args.candidate_sha
    return args, root, source


def load_skeleton(source):
    """Load definitions only; never run a previous slice's argument/main block."""
    first = source / 'qa/privsep-slice1/run.py'
    tree = ast.parse(first.read_text())
    names = {'save', 'run', 'free_port', 'guest_cloud_config', 'guest_install_command'}
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[]), str(first), 'exec'), globals())
    fourth = source / 'qa/privsep-slice4/run.py'
    text = fourth.read_text(); tree = ast.parse(text)
    functions = {'require_step', 'guest_bootstrap_command', 'suite', 'interrupt', 'doctor_rows'}
    constants = {'INSTALL_PRIVATE_ACCESS', 'HERMES_SETUP'}
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in functions) or
             (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets))]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(fourth), 'exec'), globals())
    vm = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'guest_vm')
    definition = ast.get_source_segment(text, vm)
    start = definition.index("        if label=='candidate':")
    finish = definition.index('    finally:', start)
    # Keep the proven boot/mirror/source/doctor prelude and exact finally-stop.
    definition = definition[:start] + '''        script = (source / 'qa/privsep-slice5/guest.py').read_text()
        guest('slice5-script', "cat > /root/slice5-guest.py <<'SLICE5_SCRIPT'\\n" + script + '\\nSLICE5_SCRIPT\\n', 60, True)
        for phase in ('before', 'grant', 'expiry', 'off'):
            guest('slice5-' + phase, 'python3 /root/slice5-guest.py ' + phase, 300, True)
''' + definition[finish:]
    exec(compile(definition.replace('privsep4-', 'privsep5-'), str(fourth), 'exec'), globals())


def finalize(value):
    required = {'candidate-' + name for name in ('fresh', 'tools', 'stock-hermes', 'install', 'mirror',
                'source', 'doctor', 'slice5-script', 'slice5-before', 'slice5-grant', 'slice5-expiry', 'slice5-off')}
    steps = {row['name']: row['exit'] for row in value['steps']}
    vms = value['vms']
    value['candidate_passed'] = (all(steps.get(name) == 0 for name in required)
        and value.get('candidate_doctor_passed') is True and len(vms) == 1 and vms[0]['stopped'])
    suite_ok = value['smoke'] or value.get('pytest', {}).get('failures') == [
        'tests.test_chats_views::test_typescript_public_index_path_has_identical_seal']
    value['passed'] = value['candidate_passed'] and suite_ok and not value['failures']


def main(argv):
    global root, source, candidate, receipt, private
    args, root, source = arguments(argv)
    if args.preflight:
        load_skeleton(source)
        print(json.dumps({'preflight': 'passed', 'head_sha': args.candidate_sha,
                          'argv': [str(root), str(source), args.candidate_sha]}))
        return 0
    candidate = args.candidate_sha
    root.mkdir(mode=0o700, parents=True)
    private = root / 'private'; private.mkdir(mode=0o700)
    receipt = {'started': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'candidate': candidate,
        'smoke': args.smoke, 'steps': [], 'vms': [], 'failures': [], 'candidate_passed': False,
        'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    load_skeleton(source)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM): signal.signal(sig, interrupt)
    signal.alarm(10500)
    try:
        hermes = subprocess.check_output(['git', 'ls-remote', 'https://github.com/NousResearch/hermes-agent.git', 'HEAD'], text=True, timeout=60).split()[0]
        assert re.fullmatch('[0-9a-f]{40}', hermes)
        receipt['hermes_commit'] = hermes
        image = Path('/srv/work/images/debian-12-genericcloud-amd64.qcow2')
        if not image.is_file():
            image = root / 'debian-12.qcow2'
            run('download', ['curl', '-fL', '--retry', '3', 'https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2', '-o', str(image)], timeout=600)
        run('image-hash', ['sha256sum', str(image)])
        key = private / 'id_ed25519'; run('key', ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)])
        guest_vm('candidate', candidate, image, key)
        rows = doctor_rows('candidate')
        receipt['candidate_doctor_passed'] = bool(rows) and all(row['ok'] for row in rows)
        save()
        if not args.smoke: suite()
    except Exception:
        receipt['exception'] = traceback.format_exc(); receipt['failures'].append('runner exception')
    finally:
        receipt['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if not all(row['stopped'] for row in receipt['vms']): receipt['failures'].append('VM teardown incomplete')
        finalize(receipt); save()
    print(json.dumps(receipt, indent=2))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
