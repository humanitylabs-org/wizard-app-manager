#!/usr/bin/python3
"""Fixed three cold observations, unchanged probe/fence. Parent forum admission only.

No retry-until-green, request execution, transaction, Service or release proof.
One synthetic normal preparation; each cold boot gets fresh thin overlays.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET

BASELINE = Path('/srv/work/privsep/storage-cold-diagnostic/26bd3d1ef6de108ae53246417ce4544bf2145b73')
BASELINE_MANIFEST = '27c05c0d797bb75a7999eef6327699e11d32b2d8f28a0babfe62f879f16543e5'
BASELINE_RECEIPT = '4b4209a00a19113bc40d1634792033f766e8b06ddc362ceb96bdd4bdb33ec799'
BASELINE_XML = 'fe79851145e5ac246549481d5a0f28784c8b84eab78df10662e7259942635331'
BASELINE_RUNNER = '2d8db976ce3c830bd799b2585a3de01e132c9daa1bf7d436dfd798efc51573aa'
ADDED = ('qa/privsep-storage/cold_intermittent.py', 'tests/test_storage_cold_intermittent.py')
BOOT = '/usr/local/lib/wizard-storage/maintenance-init'
ROOT = '/root/storage-cold-qa'
RESERVE = 217179869184
PENDING = 68719476736
COLD_BOOTS = (1, 2, 3)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def junit(path, expected):
    tree = ET.parse(path)
    ids = sorted(c.attrib['classname'].replace('.', '/')+'.py::'+c.attrib['name'] for c in tree.findall('.//testcase'))
    result = {'actual_cases': ids, 'xml_sha256': digest(path),
              **{key: len(tree.findall('.//'+tag)) for key, tag in
                 (('failures', 'failure'), ('errors', 'error'), ('skipped', 'skipped'))}}
    result['valid'] = ids == expected and len(ids) == len(set(ids)) and not any(result[k] for k in ('failures', 'errors', 'skipped'))
    return result


def parse_observation(raw):
    assert len(raw) <= 1048576, 'serial-bound'
    assert b'QA_PRIVATE_CANARY' not in raw, 'canary'
    found = []
    for line in raw.splitlines():
        try: row = json.loads(line)
        except (ValueError, UnicodeError): continue
        if type(row) is dict and row.get('diagnostic') == 'storage-cold-bootstrap-v1': found.append(row)
    assert len(found) <= 1, 'ambiguous-observation'
    if not found: return None
    row = found[0]
    for key, value in (('full_acceptance', False), ('review_required', True),
                       ('transaction_executed', False), ('request_reader_executed', False), ('replay_authorized', False)):
        assert row[key] is value, 'non-diagnostic-output'
    assert row['state'] in ('pre-request-stop', 'bootstrap-refused'), 'unknown-state'
    snapshot = row['sole_init_process_snapshot']
    if snapshot is not None: assert snapshot['replay_authorized'] is False
    return row


def finite_campaign(observe, record):
    """Three independent observations, regardless of guard outcome; no fourth."""
    for index in COLD_BOOTS:
        record(observe(index))


def failure_location(exc):
    # Fixed trusted code names/locations only, never exception text or arguments.
    allowed = {}
    for path in (Path(__file__), BASELINE/'runner.py', Path('/srv/work/privsep/storage-production/a4197c8ac84d5654f17e9f1cb4cb8d44008898e5/storage-runner.py')):
        allowed[str(path)] = {n.name for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.FunctionDef)}
    frames = []
    tb = exc.__traceback__
    while tb:
        code = tb.tb_frame.f_code
        if code.co_name in allowed.get(code.co_filename, set()):
            frames.append({'file': Path(code.co_filename).name, 'function': code.co_name, 'line': tb.tb_lineno})
        tb = tb.tb_next
    kinds = {AssertionError: 'AssertionError', TimeoutError: 'TimeoutError', FileNotFoundError: 'FileNotFoundError',
             ValueError: 'ValueError', KeyError: 'KeyError', OSError: 'OSError'}
    return {'class': kinds.get(type(exc), 'other-exception'), 'frames': frames[-16:]}


def validate(stage, expected):
    assert stage == Path('/srv/work/privsep/storage-cold-intermittent')/stage.name and Path.cwd() == stage
    assert digest(stage/'manifest.json') == expected
    d = json.loads((stage/'manifest.json').read_text())
    assert d['schema'] == 'storage-cold-intermittent-v1' and d['revision'] == stage.name
    assert d['full_acceptance'] is False and d['review_required'] is True
    assert d['cold_boots'] == list(COLD_BOOTS) and d['reserve_bytes'] == RESERVE and d['pending_bytes'] == PENDING
    for p in [stage, *(stage/name for name in d['files'])]:
        st = p.lstat()
        assert not stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022
    for name, sha in d['files'].items():
        assert '/' not in name and digest(stage/name) == sha
    assert digest(__file__) == d['files']['runner.py']
    assert digest(BASELINE/'runner.py') == BASELINE_RUNNER
    assert digest(BASELINE/'manifest.json') == BASELINE_MANIFEST
    assert digest(BASELINE/'evidence/receipt.json') == BASELINE_RECEIPT
    assert digest(BASELINE/'evidence/subset.xml') == BASELINE_XML
    previous = load('unchanged_cold_diagnostic', BASELINE/'runner.py')
    # Reuse its entire existing 234+10/source/disk/old-process validation.
    os.chdir(BASELINE)
    try: old_doc, base, report = previous.validate(BASELINE, BASELINE_MANIFEST)
    finally: os.chdir(stage)
    assert d['origin_disk_stats'] == old_doc['origin_disk_stats']
    assert d['product_sha256'] == old_doc['product_sha256']
    receipt = json.loads((BASELINE/'evidence/receipt.json').read_text())
    required = {'manifest.json', *old_doc['files'], 'evidence/receipt.json', 'evidence/subset.xml', 'evidence/before.json', 'evidence/boot-2-serial.log'}
    required |= {'evidence/'+step['log'] for step in receipt['steps']}
    assert set(d['baseline_files']) == required
    for name, sha in d['baseline_files'].items(): assert digest(BASELINE/name) == sha
    assert len(receipt['steps']) == 10
    assert all(step['exit'] == 0 and digest(BASELINE/'evidence'/step['log']) == step['sha256'] for step in receipt['steps'])
    tests = junit(BASELINE/'evidence/subset.xml', old_doc['new_test_cases'])
    assert tests['valid'] and len(tests['actual_cases']) == 22
    assert receipt['new_regressions']['actual_cases'] == tests['actual_cases']
    assert receipt['new_regressions']['xml_sha256'] == BASELINE_XML
    assert receipt['cleanup_errors'] == receipt['diagnostic_errors'] == []
    assert receipt['owned_qemu_absent'] is True and receipt['original_disks_preserved'] is True
    assert receipt['original_disk_sha256_before'] == receipt['original_disk_sha256_after']
    assert len(receipt['boots']) == 2 and all(b['stopped'] and not Path('/proc', str(b['pid'])).exists() for b in receipt['boots'])
    assert receipt['request_reader_executed'] is False and receipt['transaction_executed'] is False
    assert receipt['full_acceptance'] is False and receipt['passed'] is False
    assert parse_observation(json.dumps(receipt['observation']).encode()) == receipt['observation']
    current = base.inventory(stage/'source.tar')
    assert current == json.loads((stage/'source-members.json').read_text())
    a, b = ({r['path']: r for r in rows} for rows in (json.loads((BASELINE/'source-members.json').read_text()), current))
    assert set(b)-set(a) == set(ADDED) and not set(a)-set(b)
    assert all(b[k] == v for k, v in a.items()), 'inherited-source-drift'
    with tarfile.open(stage/'source.tar') as archive:
        for member, standalone in ((ADDED[0], 'runner.py'), (ADDED[1], 'test_intermittent.py')):
            stream = archive.extractfile(member); assert stream is not None
            assert hashlib.sha256(stream.read()).hexdigest() == d['files'][standalone]
        stream = archive.extractfile(ADDED[1]); assert stream is not None
        tree = ast.parse(stream.read())
        cases = sorted(ADDED[1]+'::'+n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith('test_'))
        assert cases == d['new_test_cases'] and len(cases) == len(set(cases)) and cases
        assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'parametrize' for n in ast.walk(tree))
    assert digest(stage/'diagnostic-init.py') == old_doc['files']['diagnostic-init.py']
    heads = subprocess.check_output(['git', 'bundle', 'list-heads', str(stage/'source.bundle')], text=True, timeout=30)
    assert d['revision']+' refs/heads/fix/storage-production-entry-service' in heads
    previous.process_absence([previous.ORIGIN, previous.PRIOR, BASELINE, stage])
    free = shutil.disk_usage(stage).free
    report.update(schema=d['schema'], inherited_process_cases=22, inherited_process_xml_sha256=BASELINE_XML,
                  new_test_count=len(cases), cold_boots=list(COLD_BOOTS), preparation_boots=1,
                  free_bytes=free, required_bytes=RESERVE+PENDING, shortfall_bytes=max(0, RESERVE+PENDING-free),
                  capacity_admissible=free >= RESERVE+PENDING)
    return d, previous, base, report


def make_runner(previous, base):
    class Intermittent(previous.make_runner(base)):
        def execute(self):
            self.receipt.update(schema='storage-cold-intermittent-v1', full_acceptance=False, review_required=True,
                passed=False, scope='fixed three cold-only diagnostic observations; no replay or acceptance',
                request_reader_executed=False, transaction_executed=False, replay_authorized=False,
                inherited_cases={'234': previous.ORIGIN_XML, '10': previous.PRIOR_XML, '22': BASELINE_XML},
                inherited_cases_reexecuted=False, observations=[], cold_boot_budget=list(COLD_BOOTS))
            self.save(); self.capacity()
            self.original_disks = {name: digest(previous.ORIGIN/'evidence'/name) for name in ('disk.qcow2', 'secondary.qcow2')}
            baseline = json.loads((BASELINE/'evidence/receipt.json').read_text())
            assert self.original_disks == baseline['original_disk_sha256_after']
            self.receipt['original_disk_sha256_before'] = self.original_disks; self.save()
            for name, dest in (('id_ed25519', self.key), ('seed.iso', self.evidence/'seed.iso'),
                               ('vmlinuz', self.evidence/'vmlinuz'), ('initrd', self.evidence/'initrd')):
                shutil.copyfile(previous.ORIGIN/'evidence'/name, dest); dest.chmod(0o600)
            self.prepared = {name: self.evidence/('prepared-'+name) for name in self.original_disks}
            self.disk, self.secondary = self.prepared['disk.qcow2'], self.prepared['secondary.qcow2']
            for name, dest in self.prepared.items():
                self.run('prepare-overlay-'+name, ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', previous.ORIGIN/'evidence'/name, dest])
            self.start('prepare')
            self.guest('prepare-directory', 'umask 077\ninstall -d -m700 '+ROOT)
            self.copy(self.stage/'source.tar', ROOT+'/source.tar')
            self.copy(self.stage/'diagnostic-init.py', ROOT+'/diagnostic-init')
            # Only changed/new host-runner logic tests. The unchanged 22+10+234 are inherited.
            rc = self.guest('new-runner-regressions', 'umask 022\ntar --no-same-permissions -xf '+ROOT+'/source.tar -C '+ROOT+'\ncd '+ROOT+
                '\nPYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m pytest -q tests/test_storage_cold_intermittent.py --junitxml='+ROOT+'/intermittent.xml', 180, False)
            self.copy(self.evidence/'intermittent.xml', ROOT+'/intermittent.xml', True)
            test_result = junit(self.evidence/'intermittent.xml', self.doc['new_test_cases'])
            self.receipt['new_regressions'] = test_result; self.save()
            assert rc == 0 and test_result['valid'], 'new-runner-tests'
            script = "import importlib.util,json,hashlib;from pathlib import Path;s=importlib.util.spec_from_file_location('probe','"+ROOT+"/qa/privsep-storage/cold_probe.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);assert hashlib.sha256(Path(m.BOOT).read_bytes()).hexdigest()==m.BOOT_SHA;assert hashlib.sha256(Path(m.PRODUCT).read_bytes()).hexdigest()==m.PRODUCT_SHA;Path('"+ROOT+"/before.json').write_text(json.dumps(m.metadata_snapshot(),sort_keys=True));p=Path(m.BOOT);assert not p.is_symlink();assert p.stat().st_uid==0 and p.stat().st_nlink==1;p.write_bytes(Path('"+ROOT+"/diagnostic-init').read_bytes());p.chmod(0o755)"
            self.guest('durable-state-and-probe-install', '/usr/bin/python3 -I -B -c '+shlex.quote(script))
            self.copy(self.evidence/'before.json', ROOT+'/before.json', True)
            self.receipt['durable_state_before'] = json.loads((self.evidence/'before.json').read_text()); self.save()
            self.guest('normal-shutdown', 'sync\nsystemctl poweroff', 60, False)
            self.proc.wait(timeout=90); self.stop()
            self.prepared_hashes = {name: digest(path) for name, path in self.prepared.items()}
            self.receipt['prepared_sha256_before'] = self.prepared_hashes; self.save()
            finite_campaign(self.observe, self.record)
            self.receipt['diagnostic_collected'] = len(self.receipt['observations']) == 3
            self.receipt['campaign_complete'] = self.receipt['diagnostic_collected']
            self.receipt['automatic_further_campaign_authorized'] = False
            self.save()

        def record(self, row):
            self.receipt['observations'].append(row); self.save()

        def observe(self, index):
            row = {'index': index, 'observation': None, 'error': None}
            self.serial = None
            # This is a new independent cold observation, NEVER a retry of a boot.
            self.disk = self.evidence/('cold-'+str(index)+'-disk.qcow2')
            self.secondary = self.evidence/('cold-'+str(index)+'-secondary.qcow2')
            try:
                for name, dest in (('disk.qcow2', self.disk), ('secondary.qcow2', self.secondary)):
                    self.run('cold-'+str(index)+'-overlay-'+name, ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', self.prepared[name], dest])
                boot = json.loads((previous.ORIGIN/'evidence/boot.json').read_text())
                self.start('cold-'+str(index), True, boot, init=BOOT, root_rw=True)
                end = time.monotonic()+120
                while time.monotonic() < end:
                    assert self.proc.poll() is None, 'diagnostic-init-exited'
                    raw = self.serial.read_bytes() if self.serial.exists() else b''
                    row['observation'] = parse_observation(raw)
                    if row['observation'] is not None: break
                    time.sleep(0.25)
                assert row['observation'] is not None, 'diagnostic-observation-timeout'
            except Exception as exc:
                row['error'] = failure_location(exc)
                if self.receipt['first_error'] is None: self.receipt['first_error'] = row['error']
            finally:
                # Cleanup failure invalidates safety and halts the finite campaign.
                self.stop()
                previous.process_absence([self.disk, self.secondary])
                row['owned_qemu_absent'] = True
                row['serial_sha256'] = digest(self.serial) if self.serial is not None and self.serial.exists() else None
            return row
    return Intermittent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--preflight', action='store_true'); group.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    doc, previous, base, report = validate(args.stage, args.manifest_sha256)
    if args.preflight:
        print(json.dumps(report, sort_keys=True)); return 0
    base.native_lock()
    # Persist capacity refusal metadata, but never allocate disks or weaken floors.
    r = make_runner(previous, base)(args.stage, doc)
    r.receipt.update(full_acceptance=False, review_required=True, admission=report); r.save()
    def interrupt(sig, frame): raise TimeoutError('bounded-native-signal')
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM): signal.signal(sig, interrupt)
    signal.alarm(2100)  # native 2400s timeout leaves a cleanup margin
    try:
        assert report['capacity_admissible'], 'capacity-refused'
        r.execute()
    except BaseException as exc:
        if r.receipt['first_error'] is None: r.receipt['first_error'] = failure_location(exc)
        r.save()
    finally:
        try: r.stop()
        except BaseException: r.receipt['cleanup_errors'].append({'operation': 'stop-guest'})
        try:
            previous.process_absence([args.stage]); r.receipt['owned_qemu_absent'] = True
            r.receipt['all_owned_vms_stopped'] = all(b['stopped'] for b in r.receipt['boots'])
            assert r.receipt['all_owned_vms_stopped']
            if hasattr(r, 'original_disks'):
                after = {name: digest(previous.ORIGIN/'evidence'/name) for name in r.original_disks}
                r.receipt['original_disk_sha256_after'] = after
                assert after == r.original_disks
                for name, wanted in doc['origin_disk_stats'].items():
                    st = (previous.ORIGIN/'evidence'/name).lstat()
                    assert {k: getattr(st, k) for k in wanted} == wanted
                r.receipt['original_disks_preserved'] = True
            if hasattr(r, 'prepared_hashes'):
                after = {name: digest(path) for name, path in r.prepared.items()}
                r.receipt['prepared_sha256_after'] = after
                assert after == r.prepared_hashes
            validate(args.stage, args.manifest_sha256)
        except BaseException: r.receipt['cleanup_errors'].append({'operation': 'preservation-readback'})
        r.diagnostics()
        signal.alarm(0)
    print(json.dumps({'full_acceptance': False, 'review_required': True, 'receipt': str(r.evidence/'receipt.json'),
                      'campaign_complete': r.receipt.get('campaign_complete', False)}, sort_keys=True))
    return 0 if r.receipt.get('campaign_complete') and r.receipt['first_error'] is None and not r.receipt['cleanup_errors'] else 1


if __name__ == '__main__': sys.exit(main())
