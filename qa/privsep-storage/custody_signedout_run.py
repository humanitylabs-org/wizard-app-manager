#!/usr/bin/python3
"""Static-sealed ONE-VM signed-out custody prerequisite; parent admission only.

No test collection, product entry/main/Transaction, install.sh, storage replay,
backend/Service build/load or prior partial VM/container cleanup. The inherited
VM transport is source-bound; only Runner start/run/copy/stop are reused.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import traceback

PRODUCT_REV = 'f18dd0f948a9dc8b00392912b0083ec357d7c834'
PRIOR_REV = '851c2e6c6ec7c905d1a95ac1aad40f9ad41070d7'
PRIOR = Path('/srv/work/privsep/storage-zombie-reap')/PRIOR_REV
OLD = Path('/srv/work/privsep/storage-service-continuation/474414caf8199c1912fd3343abcc796ec0d34973')
RESERVE = 217179869184
OLD_REMAINING = 66220945408
OLD_ALLOCATED = 2949689344
REMAINING = OLD_REMAINING - OLD_ALLOCATED
MAX_NEW = 2147483648  # whole stage + TWO capped COW disks and QEMU logs
QEMU_FILE_CAP = 402653184  # each writable disk/serial file <=384MiB
ROOT = '/root/custody-signedout'
STATE_ROOT = '/root/storage-service-continuation'
QA_FILES = ('qa/privsep-storage/custody_signedout_run.py',
            'qa/privsep-storage/custody_signedout_guest.py',
            'qa/privsep-storage/custody_signedout.md')


def digest(p):
    with Path(p).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def put(p, d):
    p.write_text(json.dumps(d, sort_keys=True, indent=2)+'\n')
    p.chmod(0o600)


def load(name, p):
    s = importlib.util.spec_from_file_location(name, p)
    assert s and s.loader
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def allocated(root):
    rows = []
    for p in sorted(root.rglob('*')):
        s = p.lstat()
        assert not stat.S_ISLNK(s.st_mode)
        assert s.st_uid == os.getuid() and not s.st_mode & 0o022
        if stat.S_ISREG(s.st_mode):
            assert s.st_nlink == 1
            rows.append({'path': str(p.relative_to(root)), 'dev': s.st_dev,
                'ino': s.st_ino, 'size': s.st_size, 'blocks': s.st_blocks,
                'mtime_ns': s.st_mtime_ns, 'ctime_ns': s.st_ctime_ns})
    assert len({(r['dev'], r['ino']) for r in rows}) == len(rows)
    return {'allocated_bytes': sum(r['blocks']*512 for r in rows), 'files': rows}


def absence(paths):
    for p in Path('/proc').iterdir():
        if not p.name.isdecimal():
            continue
        try:
            args = (p/'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if args and Path(os.fsdecode(args[0])).name.startswith('qemu-system-'):
            assert not any(str(v).encode() in a for v in paths for a in args), 'original or owned VM busy'


def disk_binding(p):
    s = p.lstat()
    assert stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and s.st_nlink == 1
    assert not s.st_mode & 0o022
    return {'sha256': digest(p), 'stat': {k: getattr(s, k) for k in
        ('st_dev','st_ino','st_size','st_blocks','st_mtime_ns','st_ctime_ns','st_mode','st_uid','st_gid','st_nlink')}}


def inventory(p):
    rows = []
    with tarfile.open(p) as t:
        names = set()
        for m in t:
            assert m.name not in names
            names.add(m.name)
            assert not m.name.startswith('/') and '..' not in Path(m.name).parts
            if m.isdir():
                continue
            assert m.isfile() or m.issym()
            stream = t.extractfile(m) if m.isfile() else None
            raw = stream.read() if stream else os.fsencode(m.linkname)
            rows.append({'path': m.name, 'mode': m.mode,
                'kind': 'file' if m.isfile() else 'symlink',
                'sha256': hashlib.sha256(raw).hexdigest()})
    return sorted(rows, key=lambda r: r['path'])


def validate(stage, expected):
    assert digest(stage/'manifest.json') == expected
    d = json.loads((stage/'manifest.json').read_text())
    assert stage == Path('/srv/work/privsep/custody-signedout')/d['revision']
    assert Path.cwd() == stage
    assert d['schema'] == 'custody-signedout-prerequisite-v1'
    assert d['product_revision'] == PRODUCT_REV
    assert d['review_required'] is True and d['full_acceptance'] is False
    assert d['reserve_bytes'] == RESERVE and d['campaign_remaining_bytes'] == REMAINING
    assert d['max_new_allocation_bytes'] == MAX_NEW
    assert not (stage/'evidence').exists(), 'one-shot evidence already exists; no duplicate'
    assert set(d['files']) == {'runner.py','guest.py','state.py','storage-runner.py',
        'source.tar','source.bundle','product-members.json','source-members.json',
        'inherited.json','binding.json','old-allocation.json'}
    for p in (stage, stage/'manifest.json', *(stage/n for n in d['files'])):
        s = p.lstat()
        assert not stat.S_ISLNK(s.st_mode) and s.st_uid == os.getuid() and not s.st_mode & 0o022
    for n, h in d['files'].items():
        assert '/' not in n and digest(stage/n) == h
    assert digest(__file__) == d['files']['runner.py']
    for name in ('runner.py', 'guest.py', 'state.py', 'storage-runner.py'):
        ast.parse((stage/name).read_text())
    tree = ast.parse((stage/'guest.py').read_text())
    checks = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == 'CHECKS' for t in n.targets))
    assert list(checks) == d['expected_checks'] and len(checks) == len(set(checks))
    current = inventory(stage/'source.tar')
    assert current == json.loads((stage/'source-members.json').read_text())
    before = {r['path']: r for r in json.loads((stage/'product-members.json').read_text())}
    after = {r['path']: r for r in current}
    assert set(after)-set(before) == set(QA_FILES) and not set(before)-set(after)
    assert all(before[n] == after[n] for n in before), 'f18 COMPLETE product/source closure changed'
    with tarfile.open(stage/'source.tar') as t:
        for source, target in ((QA_FILES[0], 'runner.py'), (QA_FILES[1], 'guest.py'),
                ('qa/privsep-storage/service_state.py','state.py'),
                ('qa/privsep-storage/run.py','storage-runner.py')):
            stream = t.extractfile(source)
            assert stream is not None
            assert hashlib.sha256(stream.read()).hexdigest() == d['files'][target]
    with (stage/'source.tar').open('rb') as stream:
        commit = subprocess.check_output(['git', 'get-tar-commit-id'], stdin=stream, timeout=30).decode().strip()
    assert commit == d['revision']
    heads = subprocess.check_output(['git','bundle','list-heads',str(stage/'source.bundle')], text=True, timeout=30)
    assert heads.splitlines() == [d['revision']+' refs/heads/'+d['branch']]
    snapshot = json.loads((stage/'inherited.json').read_text())
    assert digest(PRIOR/'manifest.json') == '36aee5669e95cadc17e2c8c8323e938864ceaf89ffe168ffbb053a02923ffb58'
    assert digest(PRIOR/'evidence/receipt.json') == 'b17229bac856b319d6fa36488f017d71e919cac09373a16743b235ca36668067'
    assert json.loads((PRIOR/'evidence/receipt.json').read_text()) == snapshot['receipt']
    assert snapshot['receipt']['all_owned_vms_stopped'] and snapshot['receipt']['all_boot_pids_absent']
    assert digest(PRIOR/'storage-runner.py') == d['files']['storage-runner.py']
    assert digest(OLD/'inherited.json') == d['files']['inherited.json']
    assert digest(OLD/'binding.json') == d['files']['binding.json']
    old_alloc = allocated(OLD/'evidence')
    assert old_alloc == json.loads((stage/'old-allocation.json').read_text())
    assert old_alloc['allocated_bytes'] == OLD_ALLOCATED
    # One credit only: OLD_REMAINING already excluded original 851's allocation.
    # Neither original 851 allocation nor the retained Service archive is credited.
    absence([PRIOR, OLD, stage])
    for n, row in snapshot['disks'].items():
        assert disk_binding(PRIOR/'evidence'/n) == row
    for n, row in snapshot['files'].items():
        assert digest(PRIOR/n) == row['sha256']
        assert stat.S_IMODE((PRIOR/n).lstat().st_mode) == row['mode']
    for n in ('seed.iso', 'id_ed25519'):
        p = PRIOR/'evidence'/n
        assert p.is_file() and not p.is_symlink() and p.stat().st_uid == os.getuid()
        assert digest(p) == d['transport_sha256'][n]
    amount = allocated(stage)['allocated_bytes']
    free = shutil.disk_usage(stage).free
    report = {'static_only': True, 'heavy_executed': False, 'review_required': True,
        'product_revision': PRODUCT_REV, 'source_revision': d['revision'],
        'expected_checks': list(checks), 'campaign_old_allocated_once': OLD_ALLOCATED,
        'reserve_bytes': RESERVE, 'campaign_remaining_bytes': REMAINING,
        'new_stage_allocated_bytes': amount, 'max_new_allocation_bytes': MAX_NEW,
        'remaining_pending_bytes': max(0, REMAINING-amount), 'free_bytes': free,
        'required_bytes': RESERVE+max(0, REMAINING-amount),
        'capacity_admissible': amount < MAX_NEW and free >= RESERVE+max(0, REMAINING-amount),
        'full_acceptance': False, 'one_vm_two_thin_disks_no_storage_replay': True}
    return d, snapshot, report


def make_runner(base):
    class CustodyRunner(base.Runner):
        def capacity(self):
            amount = allocated(self.stage)['allocated_bytes']
            free = shutil.disk_usage(self.stage).free
            self.receipt['capacity'] = {'allocated_bytes': amount, 'free_bytes': free,
                'reserve_bytes': RESERVE, 'remaining_pending_bytes': max(0, REMAINING-amount)}
            self.save()
            assert amount <= MAX_NEW, 'new proof allocation cap'
            assert free >= RESERVE+max(0, REMAINING-amount), 'campaign reserve plus unallocated original peak'

        def start(self, index):
            # Hard per-file cap inherited by QEMU. Root+secondary COW <=768MiB;
            # each QEMU log <=384MiB; stage/SSH remainder <512MiB, monitored
            # at every bounded call under the unchanged campaign reserve gate.
            old = resource.getrlimit(resource.RLIMIT_FSIZE)
            resource.setrlimit(resource.RLIMIT_FSIZE, (QEMU_FILE_CAP, old[1]))
            try:
                super().start(index)
            finally:
                resource.setrlimit(resource.RLIMIT_FSIZE, old)

        def run(self, *a, **kw):
            self.capacity()
            value = super().run(*a, **kw)
            self.capacity()
            return value

        def perform(self):
            for n, dst in (('id_ed25519', self.key), ('seed.iso', self.evidence/'seed.iso')):
                shutil.copyfile(PRIOR/'evidence'/n, dst)
                dst.chmod(0o600)
            for n, dst in (('disk.qcow2',self.disk), ('secondary.qcow2',self.secondary)):
                self.run('thin-'+n, ['qemu-img','create','-f','qcow2','-F','qcow2','-b',PRIOR/'evidence'/n,dst])
            self.start(1)  # exactly ONE ordinary systemd boot, no diagnostic reboot
            self.guest('synthetic-marker', 'touch /run/wizard-storage-production-synthetic\numask 077\nmkdir -m700 '+ROOT+' '+STATE_ROOT)
            self.copy(self.stage/'state.py', STATE_ROOT+'/state.py')
            self.copy(self.stage/'binding.json', STATE_ROOT+'/binding.json')
            self.guest('committed-before', '/usr/bin/python3 -I -B '+STATE_ROOT+'/state.py before')
            self.copy(self.evidence/'committed-before.json', STATE_ROOT+'/committed-before.json', True)
            self.copy(self.stage/'source.tar', ROOT+'/source.tar')
            self.copy(self.stage/'guest.py', ROOT+'/guest.py')
            self.guest('root-exact-source', 'umask 022\nmkdir -m755 /opt/custody-signedout-source\ntar --no-same-permissions -xf '+ROOT+'/source.tar -C /opt/custody-signedout-source\n')
            code = self.guest('one-real-signedout-prerequisite', '/usr/bin/python3 -I -B '+ROOT+'/guest.py probe', 1050, False)
            self.copy(self.evidence/'result.json', ROOT+'/result.json', True)
            result = json.loads((self.evidence/'result.json').read_text())
            self.receipt['probe_exit'] = code
            self.receipt['guest_result'] = result
            self.save()
            # Dependency refusal stops here. No force-through or replay.
            assert code == 0 and result['passed'] is True, 'signedout prerequisite diagnostic refusal'
            assert [r['name'] for r in result['checks']] == self.doc['expected_checks']
            assert all(r['passed'] is True for r in result['checks'])
            self.receipt['passed'] = True

        def finish_guest(self):
            # Export phase-aware results on BOTH success and refusal. No raw logs,
            # state auth files, CLI auth URL, tokens, headers or journal tails.
            self.guest('native-daemon-stop', '/usr/bin/python3 -I -B '+ROOT+'/guest.py cleanup', 120)
            self.copy(self.evidence/'result.json', ROOT+'/result.json', True)
            self.receipt['guest_result'] = json.loads((self.evidence/'result.json').read_text())
            self.guest('committed-after', '/usr/bin/python3 -I -B '+STATE_ROOT+'/state.py after')
            self.copy(self.evidence/'committed-after.json', STATE_ROOT+'/committed-after.json', True)
            assert (self.evidence/'committed-before.json').read_bytes() == (self.evidence/'committed-after.json').read_bytes()
            self.receipt['committed_metadata_unchanged'] = True
            self.guest('one-poweroff', 'sync\nsystemctl poweroff', 60, False)
            self.proc.wait(timeout=90)

    return CustodyRunner


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage', type=Path, required=True)
    p.add_argument('--manifest-sha256', required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--preflight', action='store_true')
    g.add_argument('--execute', action='store_true')
    a = p.parse_args()
    d, snapshot, report = validate(a.stage, a.manifest_sha256)
    if a.preflight:
        print(json.dumps(report, sort_keys=True))
        return 0
    base = load('custody_vm_transport', a.stage/'storage-runner.py')
    base.native_lock()  # no helper/VPS/ad-hoc invocation can admit execution
    assert report['capacity_admissible'], 'capacity refused before allocation'
    os.umask(0o077)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    r = make_runner(base)(a.stage, {**d, 'product_sha256': d['files']['guest.py']})
    r.receipt.update(scope='signed-out custody prerequisites ONLY', full_acceptance=False,
        owner_identity_positive_proven=False, tailnet_positive_proven=False,
        product_revision=PRODUCT_REV, admission=report)
    r.save()
    def interrupt(sig, frame):
        raise TimeoutError('native signedout deadline')
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
        signal.signal(sig, interrupt)
    signal.alarm(1500)
    try:
        r.perform()
    except BaseException as exc:
        r.receipt['first_error'] = {'type': type(exc).__name__,
            'frames': [{'file':Path(f.filename).name,'function':f.name,'line':f.lineno}
                       for f in traceback.extract_tb(exc.__traceback__)]}
        r.receipt['passed'] = False
        r.receipt['classification'] = 'diagnostic-prerequisite-refusal-not-full-acceptance'
        r.save()
    finally:
        # Give native cleanup its own deadline; cannot erase the first failure.
        signal.alarm(300)
        try:
            if r.proc is not None and r.proc.poll() is None:
                r.finish_guest()
        except BaseException as exc:
            r.receipt['cleanup_errors'].append('guest-cleanup-'+type(exc).__name__)
        finally:
            try:
                r.stop()
            except BaseException as exc:
                r.receipt['cleanup_errors'].append('qemu-stop-'+type(exc).__name__)
        try:
            absence([PRIOR, OLD, a.stage])
            assert len(r.receipt['boots']) <= 1
            assert all(b['stopped'] and not Path('/proc',str(b['pid'])).exists() for b in r.receipt['boots'])
            for n, row in snapshot['disks'].items():
                assert disk_binding(PRIOR/'evidence'/n) == row
            for n, row in snapshot['files'].items():
                assert digest(PRIOR/n) == row['sha256']
                assert stat.S_IMODE((PRIOR/n).lstat().st_mode) == row['mode']
            assert allocated(OLD/'evidence') == json.loads((a.stage/'old-allocation.json').read_text())
            r.receipt['original_disks_and_old_failed_evidence_preserved'] = True
            r.receipt['all_owned_vms_stopped'] = True
            r.capacity()
        except BaseException as exc:
            r.receipt['cleanup_errors'].append('preservation-'+type(exc).__name__)
        result = r.receipt.get('guest_result', {})
        r.receipt['passed'] = bool(r.receipt['passed'] and not r.receipt['cleanup_errors']
            and r.receipt.get('committed_metadata_unchanged')
            and result.get('native_daemon_stopped_before_vm_cleanup'))
        r.receipt['full_acceptance'] = False
        r.save()
        signal.alarm(0)
    print(json.dumps({'passed': r.receipt['passed'], 'full_acceptance': False,
        'review_required': True, 'receipt': str(r.evidence/'receipt.json')}, sort_keys=True))
    return 0 if r.receipt['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
