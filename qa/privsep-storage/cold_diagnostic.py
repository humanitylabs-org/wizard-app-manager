#!/usr/bin/python3
"""One saved-disk bootstrap diagnostic, parent native forum admission only.

REUSE accepted VM lifecycle and lease checks. No production acceptance, no
transaction/request replay, no Docker/Service phases, no repeated old tests.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET

ORIGIN = Path('/srv/work/privsep/storage-production/a4197c8ac84d5654f17e9f1cb4cb8d44008898e5')
ORIGIN_REV = 'a4197c8ac84d5654f17e9f1cb4cb8d44008898e5'
ORIGIN_MANIFEST = 'bedc88cd2eb77db950427734d7842a33b4064c5dbd6712ad3a52d96866f8149f'
ORIGIN_RECEIPT = '0e8e1e413093e67e6429e4960c1241c6b6d9f32de34114a0bd6b65e453d6362c'
ORIGIN_XML = '67f7c26d4a5a5e709a0d5c2ce620ad060a7cdf150a258a0ea99db5e80808655a'
RESERVE = 217179869184
PENDING = 68719476736  # unchanged conservative 56GiB + 4GiB + packaging/log peak
PRIOR = Path('/srv/work/privsep/storage-cold-diagnostic/e4fc293a843176878111b5f44fdb2e666a057137')
PRIOR_RECEIPT = 'ea821935205e381869ee18d987726c9b70d47cb9a53abc245d28deacb1c493eb'
PRIOR_XML = '8c0404310ee1695a5ff01bfd8bf199066c936f9d067244e2da9d7aa70e6168b3'
PRIOR_MANIFEST = 'bd6dc034bb8066024c21df5fa35cf4a9d57d90cf33c508baa5aefd72f077851f'
NEW = ('qa/privsep-storage/cold_diagnostic.py', 'qa/privsep-storage/cold_probe.py',
       'tests/test_storage_cold_diagnostic.py', 'tests/test_storage_cold_process.py')
BOOT = '/usr/local/lib/wizard-storage/maintenance-init'
ROOT = '/root/storage-cold-qa'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def process_absence(paths):
    found = []
    for p in Path('/proc').iterdir():
        if not p.name.isdecimal(): continue
        try: args = (p/'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError, PermissionError): continue
        if args and Path(os.fsdecode(args[0])).name.startswith('qemu-system-'):
            if any(str(path).encode() in arg for path in paths for arg in args):
                found.append(int(p.name))
    assert not found, 'referenced disk already in use'


def failure_location(exc):
    allowed = {}
    for path in (Path(__file__), ORIGIN/'storage-runner.py'):
        allowed[str(path)] = {n.name for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.FunctionDef)}
    frames = []
    tb = exc.__traceback__
    while tb:
        code = tb.tb_frame.f_code
        if code.co_name in allowed.get(code.co_filename, set()):
            frames.append({'file': Path(code.co_filename).name, 'function': code.co_name, 'line': tb.tb_lineno})
        tb = tb.tb_next
    classes = {AssertionError: 'AssertionError', ValueError: 'ValueError', OSError: 'OSError',
               FileNotFoundError: 'FileNotFoundError', TimeoutError: 'TimeoutError', KeyError: 'KeyError'}
    return {'type': classes.get(type(exc), 'other-exception'), 'frames': frames[-16:]}


def validate(stage, expected):
    assert digest(stage/'manifest.json') == expected
    d = json.loads((stage/'manifest.json').read_text())
    assert d['schema'] == 'storage-cold-diagnostic-v1' and d['full_acceptance'] is False and d['review_required'] is True
    assert stage == Path('/srv/work/privsep/storage-cold-diagnostic')/d['revision']
    assert Path.cwd() == stage
    for p in [stage, *(stage/name for name in d['files'])]:
        st = p.lstat()
        assert not stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022
    for name, sha in d['files'].items():
        assert '/' not in name and digest(stage/name) == sha
    assert digest(__file__) == d['files']['runner.py']
    assert digest(ORIGIN/'manifest.json') == ORIGIN_MANIFEST
    assert digest(ORIGIN/'evidence/receipt.json') == ORIGIN_RECEIPT
    assert digest(ORIGIN/'evidence/pytest.xml') == ORIGIN_XML
    old_doc = json.loads((ORIGIN/'manifest.json').read_text())
    old_receipt = json.loads((ORIGIN/'evidence/receipt.json').read_text())
    assert old_doc['revision'] == ORIGIN_REV and old_receipt['revision'] == ORIGIN_REV
    assert old_receipt['focused_pytest']['xml_sha256'] == ORIGIN_XML
    cases = ET.parse(ORIGIN/'evidence/pytest.xml')
    ids = sorted(c.get('classname').replace('.', '/')+'.py::'+c.get('name') for c in cases.findall('.//testcase'))
    assert len(ids) == len(set(ids)) == 234 and ids == old_doc['expected_pytest_cases']
    assert not any(cases.findall('.//'+tag) for tag in ('failure', 'error', 'skipped'))
    assert len(old_receipt['boots']) == 18 and all(b['stopped'] for b in old_receipt['boots'])
    assert all(not Path('/proc', str(b['pid'])).exists() for b in old_receipt['boots'])
    assert old_receipt['cleanup_errors'] == old_receipt['diagnostic_errors'] == []
    for name, sha in d['origin_files'].items():
        assert digest(ORIGIN/name) == sha
    for name, expected_stat in d['origin_disk_stats'].items():
        p = ORIGIN/'evidence'/name
        st = p.lstat()
        actual = {k: getattr(st, k) for k in ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_size', 'st_mtime_ns')}
        assert actual == expected_stat
    prior = validate_prior(stage, d)
    base = load('accepted_cold_vm_lifecycle', ORIGIN/'storage-runner.py')
    assert base.BASE.is_file() and digest(base.BASE) == base.BASE_SHA
    chain = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', '--backing-chain', str(ORIGIN/'evidence/disk.qcow2')], text=True, timeout=30))
    assert len(chain) == 2 and chain[0]['full-backing-filename'] == str(base.BASE)
    assert chain[1]['filename'] == str(base.BASE) and all(row['format'] == 'qcow2' for row in chain)
    assert chain[0]['virtual-size'] == 56*1024**3 and all(not row.get('dirty-flag', False) for row in chain)
    previous = json.loads((ORIGIN/'source-members.json').read_text())
    current = base.inventory(stage/'source.tar')
    a, b = ({r['path']: r for r in rows} for rows in (previous, current))
    assert set(b)-set(a) == set(NEW) and not set(a)-set(b)
    assert all(b[k] == v for k, v in a.items()), 'passed source dependencies changed'
    assert current == json.loads((stage/'source-members.json').read_text())
    with tarfile.open(stage/'source.tar') as archive:
        for member, standalone in ((NEW[0], 'runner.py'), (NEW[1], 'probe.py'), (NEW[2], 'test_probe.py'), (NEW[3], 'test_process.py')):
            assert hashlib.sha256(archive.extractfile(member).read()).hexdigest() == d['files'][standalone]
        probe = load('cold_probe_static', stage/'probe.py')
        generated = probe.build_probe(archive.extractfile('scripts/storage-maintenance-init.py').read(),
                                      archive.extractfile('scripts/protected-agent-storage.py').read())
        assert generated == (stage/'diagnostic-init.py').read_bytes()
        compile(generated, BOOT, 'exec')  # compile only; no product/guest execution
    heads = subprocess.check_output(['git', 'bundle', 'list-heads', str(stage/'source.bundle')], text=True, timeout=30)
    assert d['revision']+' refs/heads/fix/storage-production-entry-service' in heads
    process_absence([ORIGIN, PRIOR, stage])
    free = shutil.disk_usage(stage).free
    return d, base, {'static_only': True, 'heavy_executed': False, 'inherited_python_cases': len(ids),
        'inherited_xml_sha256': ORIGIN_XML, 'free_bytes': free, 'reserve_bytes': RESERVE,
        'pending_bytes': PENDING, 'capacity_admissible': free >= RESERVE+PENDING,
        'full_acceptance': False, 'review_required': True,
        'prior_diagnostic_cases': prior, 'prior_cases_reexecuted': False}


def validate_prior(stage, doc):
    # Reuse only the saved bounded diagnostic, not its cold refusal as acceptance.
    assert digest(PRIOR/'manifest.json') == PRIOR_MANIFEST
    assert digest(PRIOR/'evidence/receipt.json') == PRIOR_RECEIPT
    assert digest(PRIOR/'evidence/subset.xml') == PRIOR_XML
    for name, sha in doc['prior_files'].items():
        assert digest(PRIOR/name) == sha
    m = json.loads((PRIOR/'manifest.json').read_text())
    r = json.loads((PRIOR/'evidence/receipt.json').read_text())
    assert m['revision'] == r['revision'] == PRIOR.name
    assert r['full_acceptance'] is False and r['review_required'] is True and r['passed'] is False
    assert r['request_reader_executed'] is False and r['transaction_executed'] is False
    assert r['owned_qemu_absent'] is True and r['original_disks_preserved'] is True
    assert r['original_disk_sha256_before'] == r['original_disk_sha256_after']
    assert all(not Path('/proc', str(b['pid'])).exists() and b['stopped'] for b in r['boots'])
    assert len(r['boots']) == 2 and r['cleanup_errors'] == r['diagnostic_errors'] == []
    assert len(r['steps']) == 10
    required = {'manifest.json', 'source.tar', 'source-members.json', 'runner.py', 'probe.py',
                'test_probe.py', 'diagnostic-init.py', 'evidence/receipt.json',
                'evidence/subset.xml', 'evidence/before.json', 'evidence/boot-2-serial.log'}
    assert set(doc['prior_files']) == required | {'evidence/'+step['log'] for step in r['steps']}
    for step in r['steps']:
        assert step['exit'] == 0 and digest(PRIOR/'evidence'/step['log']) == step['sha256']
    tree = ET.parse(PRIOR/'evidence/subset.xml')
    cases = sorted(c.get('classname').replace('.', '/')+'.py::'+c.get('name') for c in tree.findall('.//testcase'))
    assert len(cases) == len(set(cases)) == 10 and cases == m['new_test_cases'] == r['new_regressions']['actual_cases']
    assert r['new_regressions']['xml_sha256'] == PRIOR_XML
    assert doc['prior_test_cases'] == cases
    assert not any(tree.findall('.//'+tag) for tag in ('failure', 'error', 'skipped'))
    previous = json.loads((PRIOR/'source-members.json').read_text())
    current = json.loads((stage/'source-members.json').read_text())
    a, b = ({row['path']: row for row in rows} for rows in (previous, current))
    assert set(b)-set(a) == {NEW[3]} and not set(a)-set(b)
    assert {k for k in a if a[k] != b[k]} == set(NEW[:2])
    # The ten earlier test definitions remain byte/mode-identical; changed helper
    # coverage is the new focused inventory, not a claim they ran at this head.
    with tarfile.open(stage/'source.tar') as archive:
        tests = ast.parse(archive.extractfile(NEW[3]).read())
    expected = sorted(NEW[3]+'::'+n.name for n in tests.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith('test_'))
    assert expected == doc['new_test_cases'] and len(expected) == len(set(expected))
    assert expected and not any(isinstance(n, ast.Call) and
        isinstance(n.func, ast.Attribute) and n.func.attr == 'parametrize' for n in ast.walk(tests))
    return cases


def make_runner(base):
    class Diagnostic(base.Runner):
        def capacity(self):
            allocated = sum(p.stat().st_blocks*512 for p in self.evidence.iterdir() if p.is_file() and not p.is_symlink())
            assert shutil.disk_usage(self.stage).free >= RESERVE+max(0, PENDING-allocated)

        def execute(self):
            self.receipt.update(schema='storage-cold-diagnostic-v1', full_acceptance=False, review_required=True,
                scope='saved synthetic disk bootstrap diagnostic only; no transaction, runtime acceptance or Service proof',
                origin_receipt_sha256=ORIGIN_RECEIPT, inherited_python_cases=234, inherited_xml_sha256=ORIGIN_XML,
                transaction_executed=False, request_reader_executed=False, original_runtime_acceptance=False,
                prior_diagnostic_receipt_sha256=PRIOR_RECEIPT, prior_diagnostic_xml_sha256=PRIOR_XML,
                prior_diagnostic_cases=self.doc['prior_test_cases'], prior_cases_reexecuted=False)
            self.save()
            self.original_disks = {name: digest(ORIGIN/'evidence'/name) for name in ('disk.qcow2', 'secondary.qcow2')}
            self.receipt['original_disk_sha256_before'] = self.original_disks; self.save()
            for name, dest in (('id_ed25519', self.key), ('seed.iso', self.evidence/'seed.iso'),
                               ('vmlinuz', self.evidence/'vmlinuz'), ('initrd', self.evidence/'initrd')):
                shutil.copyfile(ORIGIN/'evidence'/name, dest); dest.chmod(0o600)
            for name, dest in (('disk.qcow2', self.disk), ('secondary.qcow2', self.secondary)):
                self.run('overlay-'+name, ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', ORIGIN/'evidence'/name, dest])
            self.start(1)
            self.guest('prepare-directory', 'umask 077\ninstall -d -m700 '+ROOT)
            self.copy(self.stage/'source.tar', ROOT+'/source.tar')
            self.copy(self.stage/'diagnostic-init.py', ROOT+'/diagnostic-init')
            self.guest('new-source-regressions', 'umask 022\ntar --no-same-permissions -xf '+ROOT+'/source.tar -C '+ROOT+'\n'
                'cd '+ROOT+'\nPYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m pytest -q tests/test_storage_cold_process.py --junitxml='+ROOT+'/subset.xml', 180)
            self.copy(self.evidence/'subset.xml', ROOT+'/subset.xml', True)
            tree = ET.parse(self.evidence/'subset.xml')
            ids = sorted(c.get('classname').replace('.', '/')+'.py::'+c.get('name') for c in tree.findall('.//testcase'))
            assert ids == self.doc['new_test_cases'] and len(ids) == len(set(ids))
            assert not any(tree.findall('.//'+tag) for tag in ('failure', 'error', 'skipped'))
            self.receipt['new_regressions'] = {'actual_cases': ids, 'xml_sha256': digest(self.evidence/'subset.xml')}; self.save()
            # First inspect all fixed durable metadata; never infer no transaction
            # merely from absent serial/completion. Even ambiguity cannot replay:
            # the diagnostic stops unconditionally before the request-reader call.
            script = "import importlib.util,json,hashlib,os;from pathlib import Path;s=importlib.util.spec_from_file_location('probe', '"+ROOT+"/qa/privsep-storage/cold_probe.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);assert hashlib.sha256(Path(m.BOOT).read_bytes()).hexdigest()==m.BOOT_SHA;assert hashlib.sha256(Path(m.PRODUCT).read_bytes()).hexdigest()==m.PRODUCT_SHA;Path('"+ROOT+"/before.json').write_text(json.dumps(m.metadata_snapshot(),sort_keys=True));p=Path(m.BOOT);assert not p.is_symlink();assert p.stat().st_uid==0 and p.stat().st_nlink==1;p.write_bytes(Path('"+ROOT+"/diagnostic-init').read_bytes());p.chmod(0o755)"
            import shlex
            self.guest('durable-state-and-probe-install', '/usr/bin/python3 -I -B -c '+shlex.quote(script))
            self.copy(self.evidence/'before.json', ROOT+'/before.json', True)
            self.receipt['durable_state_before'] = json.loads((self.evidence/'before.json').read_text()); self.save()
            self.guest('normal-shutdown', 'sync\nsystemctl poweroff', 60, False)
            self.proc.wait(timeout=90); self.stop()
            boot = json.loads((ORIGIN/'evidence/boot.json').read_text())
            self.start(2, True, boot, init=BOOT, root_rw=True)
            end = time.monotonic()+120
            observation = None
            while time.monotonic() < end:
                assert self.proc.poll() is None, 'diagnostic init exited'
                raw = self.serial.read_bytes() if self.serial.exists() else b''
                assert len(raw) <= 1048576, 'diagnostic serial bound exceeded'
                assert b'QA_PRIVATE_CANARY' not in raw, 'synthetic private bytes leaked'
                for line in raw.splitlines():
                    try: row = json.loads(line)
                    except (ValueError, UnicodeError): continue
                    if type(row) is dict and row.get('diagnostic') == 'storage-cold-bootstrap-v1': observation = row
                if observation is not None: break
                time.sleep(0.25)
            assert observation is not None, 'diagnostic observation timeout'
            assert observation['full_acceptance'] is False and observation['review_required'] is True
            assert observation['transaction_executed'] is False and observation['request_reader_executed'] is False
            assert observation['replay_authorized'] is False
            snapshot = observation['sole_init_process_snapshot']
            if snapshot is not None:
                assert snapshot['replay_authorized'] is False
            # Captured metadata is diagnostic evidence even if missing/raced.
            # Never wait/retry for a better identity or turn a clean fence into replay.
            self.receipt['observation'] = observation
            self.receipt['serial_sha256'] = digest(self.serial)
            self.receipt['diagnostic_collected'] = True
            self.save(); self.stop()
            # A complete diagnostic deliberately never sets passed=True.

        def diagnostics(self):
            self.receipt['full_acceptance'] = False; self.receipt['review_required'] = True
            self.save()
    return Diagnostic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--preflight', action='store_true')
    group.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    doc, base, report = validate(args.stage, args.manifest_sha256)
    if args.preflight:
        print(json.dumps(report, sort_keys=True)); return 0
    base.native_lock()
    assert report['capacity_admissible'], 'floor plus unchanged pending unavailable'
    r = make_runner(base)(args.stage, doc)
    error = None
    try: r.execute()
    except BaseException as exc:
        error = exc
        # Fixed error class only; no arbitrary exception strings or subprocess logs.
        r.receipt['first_error'] = failure_location(exc); r.save()
    finally:
        try: r.stop()
        except BaseException: r.receipt['cleanup_errors'].append({'operation': 'stop-guest'})
        try:
            process_absence([args.stage])
            r.receipt['owned_qemu_absent'] = True
            if hasattr(r, 'original_disks'):
                after = {name: digest(ORIGIN/'evidence'/name) for name in r.original_disks}
                r.receipt['original_disk_sha256_after'] = after
                assert after == r.original_disks
                # Hashes do not replace mode/inode/mtime preservation checks.
                for name, wanted in doc['origin_disk_stats'].items():
                    st = (ORIGIN/'evidence'/name).lstat()
                    assert {k: getattr(st, k) for k in wanted} == wanted
                r.receipt['original_disks_preserved'] = True
            assert digest(ORIGIN/'evidence/receipt.json') == ORIGIN_RECEIPT
            for name, sha in doc['prior_files'].items():
                assert digest(PRIOR/name) == sha
        except BaseException: r.receipt['cleanup_errors'].append({'operation': 'preservation-readback'})
        r.diagnostics()
    print(json.dumps({'diagnostic_collected': r.receipt.get('diagnostic_collected', False),
        'full_acceptance': False, 'review_required': True, 'receipt': str(r.evidence/'receipt.json')}, sort_keys=True))
    return 1 if error is not None or r.receipt['cleanup_errors'] else 0


if __name__ == '__main__': sys.exit(main())
