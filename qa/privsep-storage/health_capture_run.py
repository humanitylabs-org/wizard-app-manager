#!/usr/bin/env python3
"""ONE finite installer-health capture over a fresh thin pair on the original 851 baseline.

Changed reproduction ``current-upstream-sealed-at-run``. Parent-admitted only:
--seal (staging, metadata only), --preflight (read-only), --execute (native,
requires the heavy worker lease). No image build/rebuild, no retries, no 610
guest reuse, no app/Service acceptance continuation, no host Docker. Every QEMU
started is stopped/reaped in finally; original disks and retained stages are
re-hashed/stat-compared afterwards. The health export is schema-validated in
the guest AND again here before it is accepted.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import traceback

STAGES = Path('/srv/work/privsep/storage-contract-a')
SUFFIX = '-health-capture'
PRIOR = Path('/srv/work/privsep/storage-zombie-reap/851c2e6c6ec7c905d1a95ac1aad40f9ad41070d7')
OLD = Path('/srv/work/privsep/storage-service-continuation/474414caf8199c1912fd3343abcc796ec0d34973')
SIGNED = Path('/srv/work/privsep/custody-signedout/439e70d4b102e9fe971b8f0200717d99c1aabc83')
P610 = STAGES / '610716410029f2f70fe4233102584c30e9b9022f-source-closure-repair'
P610_MANIFEST = '85b0514354087c0a939ac1943877a748ffe02c2804e4d774a83905ed0808a13e'
RETAINED = (OLD / 'evidence', SIGNED, PRIOR / 'evidence',
            STAGES / '0ae7388053211f8921e75cac030541b338496869',
            STAGES / '0513fbc4c3870d7e5bd551f7c7cc45fdace34500',
            STAGES / 'e5d76154f5c6dbb4604d13fca32efe1e77bc1502-fixture-repair',
            P610,
            STAGES / '55bff558efdaca1c2be694922d2b5777be43895e-installer-forensics',
            STAGES / 'd102279c61a94b25ebd7aef4c6aa97172b6f702e-installer-forensics',
            STAGES / 'fa42a49b7ec68447fb7caa3c356c24553bf49757-classifier-refinement',
            Path('/srv/work/privsep/storage-zombie-reap/398dc28fa0f39e7fd469e52e0eaa309b8dc7af78/evidence'))
BACKEND_ARCHIVE = Path('/srv/work/privsep/integrated/20261029T161239Z-595523fb-context-proof/evidence/new-backend-image.tar.gz')
BACKEND_ARCHIVE_SHA = 'a953c4ce3590bc1448bacb0b5b259334a99a608fb0377af425daf87c888d49c0'
BACKEND_ID = 'sha256:1c10570477becada8c613a55ff402f12b49d54e74fd9e16fefba678d57ead426'
SERVICE_ARCHIVE = OLD / 'evidence/service-image.tar.gz'
SERVICE_ARCHIVE_SHA = '7b6e3206023a46c2b35bfd5d853fee005f814c06d6d636ccb1b8c757f323f05c'
TERMINAL = ('completed', 'failed', 'cancelled')
INHERITED = {'storage-runner.py': 'c4dac71e7ea2ce17a54da7e4ace6cdb23de599b98680a3a2cc30fd55896bceb7',
             'custody-static.py': 'fc8666695710f52cf72e724199b93177c6aed94dcf12bd90fdebee49b03475ba',
             'identity.py': '9abed65058ce9ee6e9a2f573eb47a3321af96442e48f38b21c7ff3f31341cffc',
             'backend.json': 'e6c772298b923a3566d0318acd6e57be95764da7e54dd2c954468e3ac733b9cf',
             'service.json': 'd0bb5f073479ce92b2144bf416b8a3c37cfb3efdaa42b24de29b4ef13bff930e'}
COPIES = {'runner.py': 'qa/privsep-storage/health_capture_run.py',
          'guest.py': 'qa/privsep-storage/health_capture_guest.py',
          'collector.py': 'qa/privsep-storage/health_capture_collector.py',
          'tests.py': 'tests/test_health_capture_collector.py',
          'identity.py': 'qa/privsep-storage/service_identity.py',
          'custody-static.py': 'qa/privsep-storage/custody_signedout_run.py',
          'storage-runner.py': 'qa/privsep-storage/run.py'}
COMPARATORS = ('comparator-setup.sh', 'comparator-cli.tar.gz', 'comparator-cli-template.yml',
               'comparator-latest-release.json', 'comparator-upstream-journal.json')
SEALED = tuple(COPIES) + ('backend.json', 'service.json', 'source.tar', 'source-members.json',
                          'comparator.json') + COMPARATORS
QA_PREFIXES = ('qa/privsep-storage/', 'tests/test_health_capture_collector.py',
               'tests/test_installer_diagnostic_parser.py')   # QA-only test from the fa42a49 lineage
RESERVE = 217179869184
PENDING = 60011827200
MAX_NEW = 8589934592
START_FLOOR = RESERVE + PENDING + MAX_NEW          # 285781630976
RUN_FLOOR = RESERVE + PENDING                      # 277191696384
FILE_CAP = 6442450944
ROOT = '/root/storage-health-capture'
TREE = '/opt/storage-health-capture-source'
INSTALL_TIMEOUT = 2000
ALARM = 3120          # + <=420s finally (cleanup/poweroff/stop) stays under the 3600s job timeout
MONITOR_SECONDS = 2


def digest(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for raw in iter(lambda: f.read(1048576), b''):
            h.update(raw)
    return h.hexdigest()


def load(name, p):
    spec = importlib.util.spec_from_file_location(name, p); assert spec and spec.loader
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def put(p, v):
    p.write_text(json.dumps(v, sort_keys=True, indent=2) + '\n'); p.chmod(0o600)


def member(p):
    s = p.lstat()
    assert stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and not s.st_mode & 0o022 and s.st_nlink == 1
    return {'sha256': digest(p), 'mode': stat.S_IMODE(s.st_mode), 'bytes': s.st_size}


def qemu_running():
    rows = []
    for p in Path('/proc').iterdir():
        if not p.name.isdecimal():
            continue
        try:
            args = (p / 'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if args and Path(os.fsdecode(args[0])).name.startswith('qemu-system-'):
            rows.append(int(p.name))
    return rows


def spool_refs(stage, key):
    """Heavy spool rows (any state) that already reference this stage/key."""
    import sqlite3
    hits = []
    if not key:
        return [{'db': 'none', 'id': 'missing-key', 'state': 'refused'}]
    for db in (Path('/srv/work/heavy/production/spool/jobs.sqlite3'), Path('/srv/work/heavy/production/spool/light/jobs.sqlite3')):
        if not db.exists():
            continue
        con = sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True)
        try:
            for jid, state, spec in con.execute('select id,state,spec from jobs'):
                if str(stage) in (spec or '') or (key and key in (spec or '')):
                    hits.append({'db': db.parent.name, 'id': jid, 'state': state})
        finally:
            con.close()
    return hits


def tolerant_allocated(root):
    total = 0
    for base, dirs, files in os.walk(root):
        for n in files:
            try:
                total += os.lstat(os.path.join(base, n)).st_blocks * 512
            except FileNotFoundError:
                pass
    return total


def product_rows(rows):
    return [r for r in rows if not r['path'].startswith(QA_PREFIXES)]


def stat_inventory(root):
    """Read-only whole-tree stat inventory. Records (never asserts or repairs) mode/uid:
    some retained forensic stages carry 0664 members and must stay byte/stat-identical."""
    rows = []
    for p in sorted(root.rglob('*')):
        s = p.lstat()
        rows.append({'path': str(p.relative_to(root)), 'kind': 'symlink' if stat.S_ISLNK(s.st_mode) else
                     'dir' if stat.S_ISDIR(s.st_mode) else 'file' if stat.S_ISREG(s.st_mode) else 'other',
                     'mode': stat.S_IMODE(s.st_mode), 'uid': s.st_uid, 'nlink': s.st_nlink, 'dev': s.st_dev,
                     'ino': s.st_ino, 'size': s.st_size, 'blocks': s.st_blocks,
                     'mtime_ns': s.st_mtime_ns, 'ctime_ns': s.st_ctime_ns})
    r = root.lstat()
    return {'root': {'mode': stat.S_IMODE(r.st_mode), 'uid': r.st_uid, 'ino': r.st_ino, 'mtime_ns': r.st_mtime_ns,
                     'ctime_ns': r.st_ctime_ns},
            'allocated_bytes': sum(x['blocks'] * 512 for x in rows if x['kind'] == 'file'), 'files': rows}


def retained_snapshot(helpers):
    """Whole retained inventories + the original 851 disk pair (hash/stat) + boot inputs."""
    return {'allocated': {str(p): stat_inventory(p) for p in RETAINED},
            'disks': {n: helpers.disk_binding(PRIOR / 'evidence' / n) for n in ('disk.qcow2', 'secondary.qcow2')},
            'prior_files': {n: digest(PRIOR / 'evidence' / n) for n in ('id_ed25519', 'seed.iso')}}


def static_identity(stage):
    """Read-only archive binding of both images by hash; never rebuild/load on host."""
    identity = load('health_static_identity', stage / 'identity.py')
    assert digest(BACKEND_ARCHIVE) == BACKEND_ARCHIVE_SHA and digest(SERVICE_ARCHIVE) == SERVICE_ARCHIVE_SHA
    accepted = json.loads((stage / 'service.json').read_text())
    with SERVICE_ARCHIVE.open('rb') as f:
        assert identity.archive_identity(f) == accepted
    backend = json.loads((stage / 'backend.json').read_text())
    assert backend['archive_sha256'] == BACKEND_ARCHIVE_SHA
    with BACKEND_ARCHIVE.open('rb') as f:
        identity.bind_archive(identity.archive_identity(f), backend, tag=backend['manifest'][0]['RepoTags'][0])
    return {'backend_archive_bound': True, 'service_archive_bound': True}


def syntax(stage):
    import ast
    for n in ('runner.py', 'guest.py', 'collector.py', 'tests.py', 'identity.py', 'storage-runner.py', 'custody-static.py'):
        ast.parse((stage / n).read_text())
    with tarfile.open(stage / 'source.tar') as t:
        for n in ('install.sh', 'scripts/prepare-named-user.sh'):
            subprocess.run(['bash', '-n'], input=t.extractfile(n).read(), check=True, timeout=30)
    with (stage / 'source.tar').open('rb') as f:
        return subprocess.check_output(['git', 'get-tar-commit-id'], stdin=f, timeout=60).decode().strip()


# ------------------------------------------------------------------ seal ---
def seal(stage):
    """Staging only: bind inherited inputs, retained inventories and every member."""
    assert stage.parent == STAGES and stage.name.endswith(SUFFIX) and Path.cwd() == stage
    assert not (stage / 'manifest.json').exists() and not (stage / 'evidence').exists()
    revision = stage.name[:-len(SUFFIX)]; assert re.fullmatch('[0-9a-f]{40}', revision)
    assert {p.name for p in stage.iterdir()} == set(SEALED), 'unexpected staged member set'
    assert digest(P610 / 'manifest.json') == P610_MANIFEST
    m610 = json.loads((P610 / 'manifest.json').read_text())
    for n, h in INHERITED.items():
        assert digest(stage / n) == h
        if n in m610['files']:
            assert m610['files'][n] == h
    helpers = load('health_static_helpers', stage / 'custody-static.py')
    rows = helpers.inventory(stage / 'source.tar')
    assert rows == json.loads((stage / 'source-members.json').read_text())
    old = helpers.inventory(P610 / 'source.tar')
    assert digest(P610 / 'source.tar') == m610['files']['source.tar']
    a, b = ({r['path']: r for r in v} for v in (old, rows))
    delta = sorted([n for n in b if n not in a or a[n] != b[n]] + [n for n in a if n not in b])
    assert delta and all(n.startswith(QA_PREFIXES) for n in delta), 'non-QA delta from 610'
    assert product_rows(old) == product_rows(rows)
    for n, path in COPIES.items():
        assert b[path]['sha256'] == digest(stage / n), 'staged copy differs from source: ' + n
    assert syntax(stage) == revision, 'source.tar is not the git archive of the stage revision'
    static_identity(stage)
    comp = json.loads((stage / 'comparator.json').read_text())
    for n in COMPARATORS:
        assert comp['files'][n]['sha256'] == digest(stage / n)
    retained = retained_snapshot(helpers)
    put(stage / 'retained.json', retained); (stage / 'retained.json').chmod(0o400)
    for n in SEALED:
        (stage / n).chmod(0o400)          # final mode BEFORE recording it
    files = {n: member(stage / n) for n in sorted(SEALED + ('retained.json',))}
    assert all(row['mode'] == 0o400 for row in files.values())
    product = hashlib.sha256(json.dumps(product_rows(rows), sort_keys=True).encode()).hexdigest()
    doc = {'schema': 'privsep-health-capture-v1', 'label': 'current-upstream-sealed-at-run',
           'revision': revision, 'branch': 'qa/storage-contract-a-health-capture',
           'product_source_revision': '610716410029f2f70fe4233102584c30e9b9022f',
           'product_lineage': 'f18dd0f -> 0ae73880 (bytes of 61071641)',
           'product_sha256': product, 'product_rows_equal_610': True, 'qa_delta_from_610': delta,
           'source_tar_git_archive_sha256': files['source.tar']['sha256'], 'source_members': len(rows),
           'p610_manifest_sha256': P610_MANIFEST, 'backend_image_id': BACKEND_ID,
           'backend_archive_sha256': BACKEND_ARCHIVE_SHA, 'service_archive_sha256': SERVICE_ARCHIVE_SHA,
           'comparator_sha256': files['comparator.json']['sha256'],
           'migration_allowlist_sha256': comp['migration_allowlist_sha256'],
           'inherited': INHERITED, 'reserve_bytes': RESERVE, 'pending_bytes': PENDING,
           'max_new_allocation_bytes': MAX_NEW, 'start_floor_bytes': START_FLOOR, 'run_floor_bytes': RUN_FLOOR,
           'install_timeout_s': INSTALL_TIMEOUT, 'alarm_s': ALARM, 'review_required': True,
           'full_acceptance': False, 'retries': 0, 'files': files}
    raw = json.dumps(doc, sort_keys=True, indent=2) + '\n'
    (stage / 'manifest.json').write_text(raw); (stage / 'manifest.json').chmod(0o400)
    return hashlib.sha256(raw.encode()).hexdigest()


# -------------------------------------------------------------- validate ---
def validate(stage, expected, key=None, executing=False):
    assert re.fullmatch('[0-9a-f]{64}', expected) and digest(stage / 'manifest.json') == expected
    d = json.loads((stage / 'manifest.json').read_text())
    assert stage == STAGES / (d['revision'] + SUFFIX) and Path.cwd() == stage
    assert d['schema'] == 'privsep-health-capture-v1' and d['review_required'] and not d['full_acceptance']
    assert (d['reserve_bytes'], d['pending_bytes'], d['max_new_allocation_bytes']) == (RESERVE, PENDING, MAX_NEW)
    assert not (stage / 'evidence').exists(), 'one-shot stage already entered'
    assert {p.name for p in stage.iterdir()} == set(d['files']) | {'manifest.json'}, 'unsealed stage member'
    assert set(d['files']) == set(SEALED) | {'retained.json'}
    for p in [stage, stage / 'manifest.json']:
        s = p.lstat(); assert not stat.S_ISLNK(s.st_mode) and s.st_uid == os.getuid() and not s.st_mode & 0o022
    for n, row in d['files'].items():
        assert '/' not in n and member(stage / n) == row, 'stage member differs: ' + n
    assert digest(__file__) == d['files']['runner.py']['sha256']
    for n, h in INHERITED.items():
        assert d['files'][n]['sha256'] == h
    assert digest(P610 / 'manifest.json') == P610_MANIFEST
    helpers = load('health_static_helpers', stage / 'custody-static.py')
    rows = helpers.inventory(stage / 'source.tar')
    assert rows == json.loads((stage / 'source-members.json').read_text())
    old = helpers.inventory(P610 / 'source.tar')
    a, b = ({r['path']: r for r in v} for v in (old, rows))
    delta = sorted([n for n in b if n not in a or a[n] != b[n]] + [n for n in a if n not in b])
    assert delta == d['qa_delta_from_610'] and all(n.startswith(QA_PREFIXES) for n in delta)
    assert product_rows(old) == product_rows(rows), 'product bytes differ from 610'
    assert hashlib.sha256(json.dumps(product_rows(rows), sort_keys=True).encode()).hexdigest() == d['product_sha256']
    for n, path in COPIES.items():
        assert b[path]['sha256'] == d['files'][n]['sha256']
    assert syntax(stage) == d['revision'], 'source.tar is not the git archive of the stage revision'
    assert d['backend_archive_sha256'] == BACKEND_ARCHIVE_SHA and d['service_archive_sha256'] == SERVICE_ARCHIVE_SHA
    bound = static_identity(stage)
    comp = json.loads((stage / 'comparator.json').read_text())
    for n in COMPARATORS:
        assert comp['files'][n]['sha256'] == d['files'][n]['sha256']
    allow = comp['migration_allowlist']
    assert hashlib.sha256(json.dumps(allow, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == \
        comp['migration_allowlist_sha256'] == d['migration_allowlist_sha256']
    assert d['comparator_sha256'] == d['files']['comparator.json']['sha256']
    retained = json.loads((stage / 'retained.json').read_text())
    assert retained_snapshot(helpers) == retained, 'retained inventories or 851 disks changed'
    free = shutil.disk_usage(stage).free
    qemu = qemu_running(); refs = spool_refs(stage, key)
    if executing:   # the native job's own single live row is expected; any terminal row means reuse
        live = [r for r in refs if r['state'] not in TERMINAL]
        refs = [r for r in refs if r['state'] in TERMINAL] + (live if len(live) > 1 else [])
    report = {'revision': d['revision'], 'manifest_sha256': expected, 'members': len(d['files']),
              'source_members': len(rows), 'qa_delta_from_610': delta, 'product_rows_equal_610': True,
              'free_bytes': free, 'start_floor_bytes': START_FLOOR, 'run_floor_bytes': RUN_FLOOR,
              'capacity_admissible': free >= START_FLOOR, 'headroom_bytes': free - START_FLOOR,
              'qemu_running': len(qemu), 'spool_references': refs,
              'retained_inventories': len(retained['allocated']), 'disks_bound': sorted(retained['disks']),
              'migration_allowlist_names': len(allow['names']), 'images_bound': bound, 'source_tar_commit': d['revision'],
              'admissible': free >= START_FLOOR and not qemu and not refs}
    return d, helpers, report


# ---------------------------------------------------------------- runner ---
def make_runner(base, helpers):
    class HealthRunner(base.Runner):
        abort = None

        def capacity(self):
            amount = tolerant_allocated(self.stage); free = shutil.disk_usage(self.stage).free
            self.receipt['latest_capacity'] = {'allocated_bytes': amount, 'free_bytes': free,
                                               'max_new_bytes': MAX_NEW, 'run_floor_bytes': RUN_FLOOR}
            self.save()
            assert self.abort is None, 'capacity abort: ' + str(self.abort)
            assert amount <= MAX_NEW and free >= RUN_FLOOR, 'stage ceiling or reserve+pending floor violated'

        def monitor(self, stop):
            """Periodic statvfs + stage allocation; quiesce (power-cut VM) on violation."""
            peak = 0; low = None
            while not stop.wait(MONITOR_SECONDS):
                amount = tolerant_allocated(self.stage); free = shutil.disk_usage(self.stage).free
                peak = max(peak, amount); low = free if low is None else min(low, free)
                self.monitor_state = {'peak_allocated_bytes': peak, 'min_free_bytes': low}
                if amount > MAX_NEW or free < RUN_FLOOR:
                    self.abort = 'stage-ceiling' if amount > MAX_NEW else 'reserve-floor'
                    proc = self.proc
                    if proc is not None and proc.poll() is None:
                        proc.kill()
                    return

        def start(self, *args, **kw):
            old = resource.getrlimit(resource.RLIMIT_FSIZE); resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_CAP, old[1]))
            try:
                return super().start(*args, **kw)
            finally:
                resource.setrlimit(resource.RLIMIT_FSIZE, old)

        def run(self, *a, **kw):
            self.capacity(); rc = super().run(*a, **kw); self.capacity(); return rc

        def download(self, name, path=None, required=True):
            rc = self.copy(self.evidence / name, path or ROOT + '/' + name, True, required=required)
            if rc == 0 and name.endswith('.json'):
                return json.loads((self.evidence / name).read_text())

        def attest(self, kind, when):
            script = ("import importlib.util,json,subprocess,sys;from pathlib import Path\ns=importlib.util.spec_from_file_location('identity'," +
                      repr(ROOT + '/identity.py') + ");m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\na=json.loads(Path(" +
                      repr(ROOT + '/' + kind + '.json') + ").read_text());tag=" +
                      repr('privsep-wam:595523fb' if kind == 'backend' else 'privsep-service:e1ba0d0564f1') +
                      "\nv=m.attest_loaded(a,tag);Path(" + repr(ROOT + '/' + kind + '-attestation-' + when + '.json') +
                      ").write_text(json.dumps(v,sort_keys=True,indent=2)+'\\n')\n")
            self.guest('attest-' + kind + '-' + when, '/usr/bin/python3 -I -B -c ' + shlex.quote(script), 300)
            return self.download(kind + '-attestation-' + when + '.json')

        def load_image(self, kind, path, tag):
            target = ROOT + '/' + kind + '.tar.gz'; self.copy(path, target)
            script = ("import json,re,subprocess;from pathlib import Path\np=subprocess.run(['docker','load','-i'," + repr(target) +
                      "],capture_output=True,text=True,check=True,timeout=300)\nids=re.findall(r'^Loaded image ID: (sha256:[0-9a-f]{64})$',p.stdout,re.M)\nif ids:\n assert len(ids)==1;locator=ids[0]\nelse:\n assert " +
                      repr(kind) + "=='backend';a=json.loads(Path(" + repr(ROOT + '/backend.json') +
                      ").read_text());tags=a['manifest'][0]['RepoTags'];assert len(tags)==1 and p.stdout.strip()=='Loaded image: '+tags[0];locator=tags[0]\nsubprocess.run(['docker','tag',locator," +
                      repr(tag) + "],check=True,timeout=30)\nPath(" + repr(target) + ").unlink()\n")
            self.guest('load-' + kind, '/usr/bin/python3 -I -B -c ' + shlex.quote(script), 360)
            return self.attest(kind, 'before')

        def collector_tests(self):
            # New synthetic collector-only fixtures, BEFORE any VM boot; abort on failure.
            rc = super().run('collector-fixtures', [sys.executable, '-I', '-B', self.stage / 'tests.py'], timeout=300, required=False)
            self.receipt['collector_fixtures'] = {'exit': rc, 'log_sha256': digest(self.evidence / 'collector-fixtures.log')}
            self.save(); assert rc == 0, 'collector fixtures failed'
            self.phase('collector-fixtures-passed')

        def perform(self):
            self.collector_tests()
            for n, dst in (('id_ed25519', self.key), ('seed.iso', self.evidence / 'seed.iso')):
                shutil.copyfile(PRIOR / 'evidence' / n, dst); dst.chmod(0o600)
            for n, dst in (('disk.qcow2', self.disk), ('secondary.qcow2', self.secondary)):
                self.run('thin-' + n, ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', PRIOR / 'evidence' / n, dst])
            self.start(1)
            self.phase('booted-fresh-thin-pair')
            self.guest('new-synthetic-marker', 'touch /run/wizard-storage-production-synthetic\numask 077\nmkdir -m700 ' + ROOT + '\n')
            for n in ('source.tar', 'source-members.json', 'guest.py', 'collector.py', 'identity.py', 'backend.json',
                      'service.json', 'comparator.json'):
                self.copy(self.stage / n, ROOT + '/' + n)
            self.guest('exact-source', 'umask 022\nmkdir -m755 ' + TREE + '\ntar --no-same-permissions -xf ' + ROOT + '/source.tar -C ' + TREE + '\n')
            rc = self.guest('fresh', '/usr/bin/python3 -I -B ' + ROOT + '/guest.py fresh ' + self.doc['revision'], 300, False)
            self.receipt['native'] = self.download('native.json'); self.save(); assert rc == 0
            self.phase('fresh-source-closure-passed')
            self.guest('docker-real-prerequisite', 'test -S /var/run/docker.sock\ndocker info >/dev/null\n')
            before = {'backend': self.load_image('backend', BACKEND_ARCHIVE, 'privsep-wam:595523fb'),
                      'service': self.load_image('service', SERVICE_ARCHIVE, 'privsep-service:e1ba0d0564f1')}
            assert before['backend']['engine_image_id'] == BACKEND_ID or before['backend']['config_digest'] == BACKEND_ID
            self.receipt['image_identity_before'] = before; self.save()
            self.phase('actual-root-helper-health-capture')
            args = [self.doc['revision'], before['backend']['engine_image_id'], before['service']['engine_image_id']]
            rc = self.guest('health-capture-install', '/usr/bin/python3 -I -B ' + ROOT + '/guest.py install ' + shlex.join(args),
                            INSTALL_TIMEOUT, False)
            self.receipt['install_step_exit'] = rc
            self.receipt['native'] = self.download('native.json', required=False); self.save()
            export = self.download('health-export.json', required=False)
            collector = load('health_host_collector', self.stage / 'collector.py')
            assert export is not None, 'no health export'
            collector.validate_export(export)
            self.receipt['health_export_sha256'] = digest(self.evidence / 'health-export.json')
            self.receipt['diagnostic_result'] = export['result']; self.receipt['diagnostic_reason'] = export['result_reason']
            self.receipt['cause_class'] = export['cause_class']; self.save()
            self.phase('health-export-validated')

        def cleanup(self):
            rc = self.guest('exact-cleanup-with-negative', '/usr/bin/python3 -I -B ' + ROOT + '/guest.py cleanup', 300, False)
            self.receipt['native'] = self.download('native.json', required=False)
            for n in ('container-cleanup.json', 'cleanup-negative.json'):
                self.download(n, required=False)
            final = self.receipt['native'] or {}
            proven = rc == 0 and bool((final.get('exact_cleanup') or {}).get('negative_removed_by_full_id_absent'))
            self.receipt['exact_cleanup_proven'] = proven
            if not proven:
                self.receipt['cleanup_errors'].append('exact-cleanup-unproven')
            self.save()

    return HealthRunner


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage', required=True, type=Path); p.add_argument('--manifest-sha256')
    p.add_argument('--key')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--seal', action='store_true'); g.add_argument('--preflight', action='store_true')
    g.add_argument('--execute', action='store_true')
    args = p.parse_args()
    if args.seal:
        print(seal(args.stage)); return 0
    d, helpers, report = validate(args.stage, args.manifest_sha256, args.key, args.execute)
    if args.preflight:
        print(json.dumps(report, sort_keys=True)); return 0 if report['admissible'] else 1
    base = load('health_vm_transport', args.stage / 'storage-runner.py'); base.native_lock()
    assert report['admissible'], 'capacity/QEMU/spool admission refused'
    os.umask(0o077); tmp = args.stage / 'tmp'; tmp.mkdir(mode=0o700)
    os.environ['TMPDIR'] = str(tmp); os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    r = make_runner(base, helpers)(args.stage, d)
    r.monitor_state = {}
    r.receipt.update(scope='one finite installer-health capture; changed reproduction current-upstream-sealed-at-run',
                     full_acceptance=False, admission=report, retries=0, label='current-upstream-sealed-at-run')
    r.save()
    stop = threading.Event(); mon = threading.Thread(target=r.monitor, args=(stop,), daemon=True); mon.start()

    def interrupt(sig, frame):
        raise TimeoutError('bounded health-capture native deadline')
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, interrupt)
    signal.alarm(ALARM)
    try:
        r.perform()
    except BaseException as e:
        r.receipt['first_error'] = {'type': type(e).__name__, 'frames': [{'file': Path(f.filename).name, 'function': f.name,
                                    'line': f.lineno} for f in traceback.extract_tb(e.__traceback__)]}
        r.save()
    finally:
        signal.alarm(420)
        try:
            if r.proc is not None and r.proc.poll() is None and 'actual-root-helper-health-capture' in r.receipt['reached_phases']:
                r.cleanup()
        except BaseException as e:
            r.receipt['cleanup_errors'].append('guest-cleanup-' + type(e).__name__)
        try:
            if r.proc is not None and r.proc.poll() is None:
                r.guest('one-poweroff', 'sync\nsystemctl poweroff', 60, False); r.proc.wait(timeout=90)
        except BaseException as e:
            r.receipt['cleanup_errors'].append('poweroff-' + type(e).__name__)
        try:
            r.stop()
        except BaseException as e:
            r.receipt['cleanup_errors'].append('stop-' + type(e).__name__)
        stop.set(); mon.join(timeout=10)
        r.receipt['capacity_monitor'] = dict(r.monitor_state, abort=r.abort)
        try:
            helpers.absence([PRIOR, OLD, SIGNED, *RETAINED, args.stage])
            assert len(r.receipt['boots']) <= 1 and all(b['stopped'] and not Path('/proc', str(b['pid'])).exists() for b in r.receipt['boots'])
            assert not qemu_running(), 'a QEMU process survived'
            r.receipt['all_boot_pids_absent'] = True
            assert retained_snapshot(helpers) == json.loads((args.stage / 'retained.json').read_text())
            r.receipt['original_disks_and_retained_unchanged'] = True
        except BaseException as e:
            r.receipt['cleanup_errors'].append('preservation-' + type(e).__name__)
        try:
            r.capacity()
        except BaseException as e:
            r.receipt['cleanup_errors'].append('capacity-' + type(e).__name__)
        r.receipt['passed'] = bool(r.receipt.get('diagnostic_result') and r.receipt['first_error'] is None and
                                   not r.receipt['cleanup_errors'] and r.receipt.get('exact_cleanup_proven') and
                                   r.receipt.get('all_boot_pids_absent'))
        r.save(); signal.alarm(0)
    print(json.dumps({'passed': r.receipt['passed'], 'result': r.receipt.get('diagnostic_result'),
                      'receipt': str(r.evidence / 'receipt.json'), 'full_acceptance': False}))
    return 0 if r.receipt['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
