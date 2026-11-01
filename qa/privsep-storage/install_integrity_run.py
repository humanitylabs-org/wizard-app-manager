#!/usr/bin/env python3
"""Contract A rerun with a guest-side image integrity gate (QA only; product bytes == 610).

The retained guest base disk bakes the pinned manager image id with an EMPTY stored
/app/main.mjs, and `docker load` / install.sh's `image inspect || pull` keep that copy.
This stage reruns the SAME 610 contract-A flow (real separated root install.sh, two
Service cases, native security checks, exact cleanup) but, before the real install.sh,
measures the pinned manager bytes and, for the exact empty value only, evicts exactly
that id and reloads the hash-bound archive (probe-r3 functions); it fails closed when
still wrong. Measurements are hashes/sizes only. Nothing else in the 610 flow is weakened.

--seal stages metadata; --preflight is read-only; only --execute may boot, and it
requires the durable heavy-worker lease. Diagnostic, not full acceptance.
"""
import argparse
import ast
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
import traceback

STAGES = Path('/srv/work/privsep/storage-contract-a')
SUFFIX = '-install-integrity'
SCHEMA = 'privsep-install-integrity-v1'
BRANCH = 'qa/storage-contract-a-install-integrity'
PRIOR = Path('/srv/work/privsep/storage-zombie-reap/851c2e6c6ec7c905d1a95ac1aad40f9ad41070d7')
OLD = Path('/srv/work/privsep/storage-service-continuation/474414caf8199c1912fd3343abcc796ec0d34973')
SIGNED = Path('/srv/work/privsep/custody-signedout/439e70d4b102e9fe971b8f0200717d99c1aabc83')
P610 = STAGES / '610716410029f2f70fe4233102584c30e9b9022f-source-closure-repair'
P610_MANIFEST = '85b0514354087c0a939ac1943877a748ffe02c2804e4d774a83905ed0808a13e'
P610_RECEIPT = '527e52b79f2fb63d12d2f37c11979ea19670ab21047991ad19e36078925535f2'
P610_FILES = {'backend.json': 'e6c772298b923a3566d0318acd6e57be95764da7e54dd2c954468e3ac733b9cf',
              'binding.json': 'bb4b5cc91e3d927ccb76b035c5061f64d44d1713420552b9efe13e91659fb3bd',
              'custody-static.py': 'fc8666695710f52cf72e724199b93177c6aed94dcf12bd90fdebee49b03475ba',
              'identity.py': '9abed65058ce9ee6e9a2f573eb47a3321af96442e48f38b21c7ff3f31341cffc',
              'inherited.json': 'e495f29e2a0a5b1cdeb4cba6ba3d5a3bc265d77af26ef4c4f5ba58273883f740',
              'service.json': 'd0bb5f073479ce92b2144bf416b8a3c37cfb3efdaa42b24de29b4ef13bff930e',
              'state.py': 'b68217dc4e388e81affd16c47075ff744b1b4f26ab124774b30cb96a4b4e5a3f',
              'storage-runner.py': 'c4dac71e7ea2ce17a54da7e4ace6cdb23de599b98680a3a2cc30fd55896bceb7'}
P610_RUNNER = '5ffaa8854cce576ea7db909c976c7ecae1ff71bb18a232859b45460d91be9cfb'
P610_GUEST = 'f5d610a7570d7267b8f9d91ac4ccbc5fa1d340ecb8f7cb883035d23634acdaf9'
RETAINED = (OLD / 'evidence', SIGNED, PRIOR / 'evidence',
            STAGES / '0ae7388053211f8921e75cac030541b338496869',
            STAGES / '0513fbc4c3870d7e5bd551f7c7cc45fdace34500',
            STAGES / 'e5d76154f5c6dbb4604d13fca32efe1e77bc1502-fixture-repair',
            P610,
            STAGES / '55bff558efdaca1c2be694922d2b5777be43895e-installer-forensics',
            STAGES / 'd102279c61a94b25ebd7aef4c6aa97172b6f702e-installer-forensics',
            STAGES / 'fa42a49b7ec68447fb7caa3c356c24553bf49757-classifier-refinement',
            Path('/srv/work/privsep/storage-zombie-reap/398dc28fa0f39e7fd469e52e0eaa309b8dc7af78/evidence'),
            STAGES / 'f8fd700eb950812d3ce4bd26848f39751bc41a7e-health-capture',
            STAGES / 'bfff7a39c27359c893356a8812e31db810e3174f-manager-start',
            STAGES / 'b452e604f4c9e179bc5cad492ad4beae4d6dfa92-manager-trace',
            STAGES / '195b1e6df6f73858f4a1472605374a1207e5ee57-manager-trace',
            STAGES / 'a6aa893bf5a845f4064447c62c846f9b41d2fe7a-manager-probe',
            STAGES / 'ce40456e37591ad4e4e5165ffbeb91204ee6a187-manager-probe',
            STAGES / '2ace18ed144d019c5fc6643ff7df129b0bf8e0ba-manager-probe')
BACKEND_ARCHIVE = Path('/srv/work/privsep/integrated/20261029T161239Z-595523fb-context-proof/evidence/new-backend-image.tar.gz')
BACKEND_ARCHIVE_SHA = 'a953c4ce3590bc1448bacb0b5b259334a99a608fb0377af425daf87c888d49c0'
BACKEND_ENGINE_ID = 'sha256:94cc09235bd65370eff851207b7ee52335098f078ed4f0a564d38d5aea7a5928'
SERVICE_ARCHIVE = OLD / 'evidence/service-image.tar.gz'
SERVICE_SHA = '7b6e3206023a46c2b35bfd5d853fee005f814c06d6d636ccb1b8c757f323f05c'
TERMINAL = ('completed', 'failed', 'cancelled')
COPIES = {'runner.py': 'qa/privsep-storage/install_integrity_run.py',
          'guest.py': 'qa/privsep-storage/contract_a_guest.py',
          'integrity.py': 'qa/privsep-storage/install_integrity_guest.py',
          'tests.py': 'tests/test_install_integrity.py'}
SEALED = tuple(COPIES) + tuple(P610_FILES) + ('source.tar', 'source-members.json')
QA_PREFIXES = ('qa/privsep-storage/', 'tests/test_health_capture_collector.py', 'tests/test_manager_start.py',
               'tests/test_manager_probe.py', 'tests/test_installer_diagnostic_parser.py',
               'tests/test_install_integrity.py')
RESERVE = 217179869184
PENDING = 60011827200
MAX_NEW = 8589934592
START_FLOOR = RESERVE + PENDING + MAX_NEW          # 285781630976
RUN_FLOOR = RESERVE + PENDING                      # 277191696384
FILE_CAP = 6442450944
ALARM = 10800          # same bound as the 610 install stage; + <=420 s finally stays under the 11400 s job timeout
MONITOR_SECONDS = 2
GATES = ('before-installer-start', 'before-install-sh', 'before-service-cases')
ROOT = '/root/storage-contract-a'
EXPECTED_NATIVE_CHECKS = ['fresh-synthetic-signedout-inputs', 'actual-none-dependency-upstream-metadata',
                          'native-socket-before-dependency', 'same-socket-worker-restart',
                          'forged-owner-origin-action-refusal', 'native-daemon-dependency-restart']


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
    """Read-only whole-tree stat inventory; records mode/uid, never asserts or repairs."""
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
    return {'allocated': {str(p): stat_inventory(p) for p in RETAINED},
            'disks': {n: helpers.disk_binding(PRIOR / 'evidence' / n) for n in ('disk.qcow2', 'secondary.qcow2')},
            'prior_files': {n: digest(PRIOR / 'evidence' / n) for n in ('id_ed25519', 'seed.iso')}}


def _is_integrity_branch(node):
    """True for the `elif sys.argv[1]=='integrity'` branch only."""
    if not isinstance(node, ast.If):
        return False
    t = node.test
    return (isinstance(t, ast.Compare) and len(t.comparators) == 1
            and isinstance(t.comparators[0], ast.Constant) and t.comparators[0].value == 'integrity'
            and ast.unparse(t.left) == 'sys.argv[1]')


class _DropGate(ast.NodeTransformer):
    """Remove exactly the QA additions: the integrity_gate def and calls, the dispatch branch and its
    name in main()'s allow-list. Anything else left over makes the AST differ from 610."""
    def visit_FunctionDef(self, node):
        if node.name == 'integrity_gate':
            return None
        return self.generic_visit(node)

    def visit_Expr(self, node):
        v = node.value
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == 'integrity_gate':
            return None
        return node

    def visit_If(self, node):
        self.generic_visit(node)
        if len(node.orelse) == 1 and _is_integrity_branch(node.orelse[0]):
            node.orelse = node.orelse[0].orelse
        return node

    def visit_Tuple(self, node):
        self.generic_visit(node)
        if any(isinstance(e, ast.Constant) and e.value == 'integrity' for e in node.elts):
            node.elts = [e for e in node.elts if not (isinstance(e, ast.Constant) and e.value == 'integrity')]
        return node


def guest_closure(new_src, old_src):
    """The ONLY guest changes vs 610: integrity_gate(), its two call sites, main()'s 'integrity' dispatch.

    Whole-module AST equality (main included) after removing exactly those pieces, so every
    other function (install, fetched_source_closure, observers, cleanups ...) is identical and
    the 105 inherited focused fixtures stay bound to the same code. Returns the normalized hash."""
    new_tree = ast.parse(new_src)
    defs = [n for n in new_tree.body if isinstance(n, ast.FunctionDef) and n.name == 'integrity_gate']
    assert len(defs) == 1, 'exactly one integrity_gate definition'
    calls = [n for n in ast.walk(new_tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == 'integrity_gate']
    labels = sorted(ast.unparse(c.args[0]) for c in calls)
    assert labels == ["'before-install-sh'", 'sys.argv[2]'], 'unexpected integrity_gate call sites: ' + repr(labels)
    old_dump = ast.dump(_DropGate().visit(ast.parse(old_src)), include_attributes=False)
    new_dump = ast.dump(_DropGate().visit(new_tree), include_attributes=False)
    assert new_dump == old_dump, 'guest changed outside the integrity gate'
    return hashlib.sha256(new_dump.encode()).hexdigest()


def static_identity(stage):
    identity = load('ii_static_identity', stage / 'identity.py')
    assert digest(BACKEND_ARCHIVE) == BACKEND_ARCHIVE_SHA
    backend = json.loads((stage / 'backend.json').read_text())
    assert backend['archive_sha256'] == BACKEND_ARCHIVE_SHA
    with BACKEND_ARCHIVE.open('rb') as f:
        identity.bind_archive(identity.archive_identity(f), backend, tag=backend['manifest'][0]['RepoTags'][0])
    assert digest(SERVICE_ARCHIVE) == SERVICE_SHA
    accepted = json.loads((stage / 'service.json').read_text())
    with SERVICE_ARCHIVE.open('rb') as f:
        assert identity.archive_identity(f) == accepted
    return {'backend_archive_bound': True, 'service_archive_bound': True}


def syntax(stage):
    for n in ('runner.py', 'guest.py', 'integrity.py', 'tests.py', 'identity.py', 'storage-runner.py',
              'custody-static.py', 'state.py'):
        ast.parse((stage / n).read_text())
    with tarfile.open(stage / 'source.tar') as t:
        for n in ('get.sh', 'install.sh', 'scripts/prepare-named-user.sh'):
            subprocess.run(['bash', '-n'], input=t.extractfile(n).read(), check=True, timeout=30)
    with (stage / 'source.tar').open('rb') as f:
        return subprocess.check_output(['git', 'get-tar-commit-id'], stdin=f, timeout=60).decode().strip()


def inherited_610(stage, helpers, rows):
    """Everything the 610 stage bound that does not depend on the QA delta (read-only)."""
    assert digest(P610 / 'manifest.json') == P610_MANIFEST
    m610 = json.loads((P610 / 'manifest.json').read_text())
    for n, h in P610_FILES.items():
        assert digest(stage / n) == h == m610['files'][n] == digest(P610 / n)
    assert digest(P610 / 'runner.py') == P610_RUNNER == m610['files']['runner.py']
    assert digest(P610 / 'guest.py') == P610_GUEST == m610['files']['guest.py']
    assert digest(P610 / 'evidence/receipt.json') == P610_RECEIPT
    old = json.loads((P610 / 'evidence/receipt.json').read_text())
    assert old['passed'] is False and old['focused_composite']['passed'] and old['focused_composite']['inherited_count'] == 105
    assert old['native']['first_error'] == {'phase': 'install', 'type': 'CalledProcessError'}
    assert old['all_boot_pids_absent'] and old['original_disks_and_old_evidence_unchanged'] and not old['cleanup_errors']
    prior_rows = {r['path']: r for r in json.loads((PRIOR / 'backend-inputs.json').read_text())}
    now = {r['path']: r for r in rows}
    assert len(prior_rows) == 622 and all(now[n] == r for n, r in prior_rows.items())
    contract = load('ii_contract_610', P610 / 'runner.py')
    snapshot = json.loads((stage / 'inherited.json').read_text())
    prior = load('ii_old_continuation', OLD / 'continuation.py')
    production = load('ii_old_production', PRIOR / 'runner.py')
    closure = prior.inherited(stage, snapshot, production)
    with tarfile.open(stage / 'source.tar') as t:
        guest_sha = guest_closure(t.extractfile('qa/privsep-storage/contract_a_guest.py').read().decode(),
                                  (P610 / 'guest.py').read_text())
    return m610, contract, snapshot, closure, guest_sha, old


# ------------------------------------------------------------------ seal ---
def seal(stage):
    assert stage.parent == STAGES and stage.name.endswith(SUFFIX) and Path.cwd() == stage
    assert not (stage / 'manifest.json').exists() and not (stage / 'evidence').exists()
    revision = stage.name[:-len(SUFFIX)]; assert re.fullmatch('[0-9a-f]{40}', revision)
    assert {p.name for p in stage.iterdir()} == set(SEALED), 'unexpected staged member set'
    helpers = load('ii_static_helpers', stage / 'custody-static.py')
    rows = helpers.inventory(stage / 'source.tar')
    assert rows == json.loads((stage / 'source-members.json').read_text())
    m610, _, _, _, guest_sha, _ = inherited_610(stage, helpers, rows)
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
    retained = retained_snapshot(helpers)
    put(stage / 'retained.json', retained); (stage / 'retained.json').chmod(0o400)
    for n in SEALED:
        (stage / n).chmod(0o400)
    files = {n: member(stage / n) for n in sorted(SEALED + ('retained.json',))}
    assert all(row['mode'] == 0o400 for row in files.values())
    product = hashlib.sha256(json.dumps(product_rows(rows), sort_keys=True).encode()).hexdigest()
    doc = {'schema': SCHEMA, 'label': 'contract-a-install-integrity', 'revision': revision, 'branch': BRANCH,
           'product_source_revision': '610716410029f2f70fe4233102584c30e9b9022f',
           'product_sha256': product, 'product_rows_equal_610': True, 'qa_delta_from_610': delta,
           'guest_closure_ast_sha256': guest_sha,
           'source_tar_git_archive_sha256': files['source.tar']['sha256'], 'source_members': len(rows),
           'p610_manifest_sha256': P610_MANIFEST, 'p610_receipt_sha256': P610_RECEIPT,
           'backend_engine_id': BACKEND_ENGINE_ID, 'backend_archive_sha256': BACKEND_ARCHIVE_SHA,
           'service_archive_sha256': SERVICE_SHA, 'inherited': P610_FILES,
           'expected_native_checks': EXPECTED_NATIVE_CHECKS,
           'expected_service_cases': list(m610_cases(m610)), 'integrity_gates': list(GATES),
           'inherited_focused_count': 105, 'fresh_pytest_cases': [],
           'reserve_bytes': RESERVE, 'pending_bytes': PENDING, 'max_new_allocation_bytes': MAX_NEW,
           'start_floor_bytes': START_FLOOR, 'run_floor_bytes': RUN_FLOOR, 'alarm_s': ALARM,
           'install_sh_run': True, 'review_required': True, 'full_acceptance': False, 'retries': 0, 'files': files}
    raw = json.dumps(doc, sort_keys=True, indent=2) + '\n'
    (stage / 'manifest.json').write_text(raw); (stage / 'manifest.json').chmod(0o400)
    return hashlib.sha256(raw.encode()).hexdigest()


def m610_cases(m610):
    return m610['expected_service_cases']


# -------------------------------------------------------------- validate ---
def validate(stage, expected, key=None, executing=False):
    assert re.fullmatch('[0-9a-f]{64}', expected) and digest(stage / 'manifest.json') == expected
    d = json.loads((stage / 'manifest.json').read_text())
    assert stage == STAGES / (d['revision'] + SUFFIX) and Path.cwd() == stage
    assert d['schema'] == SCHEMA and d['review_required'] and not d['full_acceptance']
    assert d['retries'] == 0 and d['install_sh_run'] is True and d['fresh_pytest_cases'] == []
    assert (d['reserve_bytes'], d['pending_bytes'], d['max_new_allocation_bytes']) == (RESERVE, PENDING, MAX_NEW)
    assert d['integrity_gates'] == list(GATES) and d['expected_native_checks'] == EXPECTED_NATIVE_CHECKS
    assert not (stage / 'evidence').exists(), 'one-shot stage already entered'
    assert {p.name for p in stage.iterdir()} == set(d['files']) | {'manifest.json'}, 'unsealed stage member'
    assert set(d['files']) == set(SEALED) | {'retained.json'}
    for p in [stage, stage / 'manifest.json']:
        s = p.lstat(); assert not stat.S_ISLNK(s.st_mode) and s.st_uid == os.getuid() and not s.st_mode & 0o022
    for n, row in d['files'].items():
        assert '/' not in n and member(stage / n) == row, 'stage member differs: ' + n
    assert digest(__file__) == d['files']['runner.py']['sha256']
    for n, h in P610_FILES.items():
        assert d['files'][n]['sha256'] == h
    helpers = load('ii_static_helpers', stage / 'custody-static.py')
    rows = helpers.inventory(stage / 'source.tar')
    assert rows == json.loads((stage / 'source-members.json').read_text())
    m610, contract, snapshot, closure, guest_sha, _ = inherited_610(stage, helpers, rows)
    assert guest_sha == d['guest_closure_ast_sha256']
    old = helpers.inventory(P610 / 'source.tar')
    a, b = ({r['path']: r for r in v} for v in (old, rows))
    delta = sorted([n for n in b if n not in a or a[n] != b[n]] + [n for n in a if n not in b])
    assert delta == d['qa_delta_from_610'] and all(n.startswith(QA_PREFIXES) for n in delta)
    assert product_rows(old) == product_rows(rows), 'product bytes differ from 610'
    assert hashlib.sha256(json.dumps(product_rows(rows), sort_keys=True).encode()).hexdigest() == d['product_sha256']
    for n, path in COPIES.items():
        assert b[path]['sha256'] == d['files'][n]['sha256']
    assert syntax(stage) == d['revision'], 'source.tar is not the git archive of the stage revision'
    assert d['backend_archive_sha256'] == BACKEND_ARCHIVE_SHA and d['backend_engine_id'] == BACKEND_ENGINE_ID
    bound = static_identity(stage)
    assert d['expected_service_cases'] == list(contract.CASES)
    retained = json.loads((stage / 'retained.json').read_text())
    assert retained_snapshot(helpers) == retained, 'retained inventories or 851 disks changed'
    for n, row in snapshot['disks'].items():
        assert helpers.disk_binding(PRIOR / 'evidence' / n) == row
    for n, row in snapshot['files'].items():
        assert digest(PRIOR / n) == row['sha256']
    free = shutil.disk_usage(stage).free
    qemu = qemu_running(); refs = spool_refs(stage, key)
    if executing:
        live = [r for r in refs if r['state'] not in TERMINAL]
        refs = [r for r in refs if r['state'] in TERMINAL] + (live if len(live) > 1 else [])
    report = {'revision': d['revision'], 'manifest_sha256': expected, 'members': len(d['files']),
              'source_members': len(rows), 'qa_delta_from_610': delta, 'product_rows_equal_610': True,
              'guest_closure_ast_sha256': guest_sha, 'inherited_focused_pytest': 105, 'fresh_pytest_count': 0,
              'expected_native_checks': d['expected_native_checks'], 'expected_service_cases': d['expected_service_cases'],
              'integrity_gates': d['integrity_gates'], 'inherited_old_closure': sorted(closure),
              'free_bytes': free, 'start_floor_bytes': START_FLOOR, 'run_floor_bytes': RUN_FLOOR,
              'capacity_admissible': free >= START_FLOOR, 'headroom_bytes': free - START_FLOOR,
              'qemu_running': len(qemu), 'spool_references': refs,
              'retained_inventories': len(retained['allocated']), 'disks_bound': sorted(retained['disks']),
              'images_bound': bound, 'source_tar_commit': d['revision'], 'static_only': True, 'submitted': False,
              'tests_executed': False, 'heavy_executed': False,
              'admissible': free >= START_FLOOR and not qemu and not refs}
    return d, helpers, report, contract


# ---------------------------------------------------------------- runner ---
def make_runner(base, helpers, contract):
    Contract = contract.make_runner(base, helpers)
    integrity = load('ii_host_integrity', Path(__file__).resolve().parent / 'integrity.py')

    class IntegrityRunner(Contract):
        abort = None

        def capacity(self):
            amount = tolerant_allocated(self.stage); free = shutil.disk_usage(self.stage).free
            self.receipt['latest_capacity'] = {'allocated_bytes': amount, 'free_bytes': free,
                                               'max_new_bytes': MAX_NEW, 'run_floor_bytes': RUN_FLOOR}
            self.save()
            assert self.abort is None, 'capacity abort: ' + str(self.abort)
            assert amount <= MAX_NEW and free >= RUN_FLOOR, 'stage ceiling or reserve+pending floor violated'

        def monitor(self, stop):
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

        def collector_tests(self):
            # Synthetic fixtures, BEFORE any VM boot; failure aborts the boot.
            rc = Contract.run(self, 'integrity-fixtures', [sys.executable, '-I', '-B', self.stage / 'tests.py'],
                              timeout=300, required=False)
            self.receipt['integrity_fixtures'] = {'exit': rc, 'log_sha256': digest(self.evidence / 'integrity-fixtures.log')}
            self.save(); assert rc == 0, 'integrity fixtures failed'
            self.phase('integrity-fixtures-passed')

        def retain_regressions(self):
            # The 105 focused fixtures are INHERITED from the 610 receipt (product bytes equal 610 and the
            # guest is AST-identical outside the gate). Nothing is re-executed here and none is claimed fresh.
            old = json.loads((P610 / 'evidence/receipt.json').read_text())['focused_composite']
            assert digest(P610 / 'evidence/receipt.json') == P610_RECEIPT and old['passed'] and old['inherited_count'] == 105
            self.receipt['focused_composite'] = {'passed': True, 'fresh_count': 0, 'inherited_count': 105,
                'cases': old['cases'], 'reexecuted': False, 'inherited_from_receipt_sha256': P610_RECEIPT,
                'guest_closure_ast_sha256': self.doc['guest_closure_ast_sha256']}
            self.save()

        def integrity(self, label, tail='', required=True):
            rc = self.guest('integrity-' + label, '/usr/bin/python3 -I -B ' + ROOT + '/guest.py integrity ' + label + tail, 900, False)
            doc = self.download('image-integrity.json', required=False)
            self.receipt['image_integrity'] = doc; self.save()
            if required:
                assert rc == 0, 'integrity gate failed closed: ' + label
                assert doc is not None, 'no integrity export'
                integrity.validate_export(doc, GATES[:len([r for r in doc['gates']])])
            return rc

        def perform(self, production):
            self.collector_tests()
            for n, dst in (('id_ed25519', self.key), ('seed.iso', self.evidence / 'seed.iso')):
                shutil.copyfile(PRIOR / 'evidence' / n, dst); dst.chmod(0o600)
            for n, dst in (('disk.qcow2', self.disk), ('secondary.qcow2', self.secondary)):
                self.run('thin-' + n, ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', PRIOR / 'evidence' / n, dst])
            self.start(1)
            self.phase('booted-fresh-thin-pair')
            self.guest('new-synthetic-marker', 'touch /run/wizard-storage-production-synthetic\numask 077\nmkdir -m700 ' + ROOT + ' ' + contract.STATE_ROOT + '\n')
            for n in ('source.tar', 'source-members.json', 'guest.py', 'identity.py', 'backend.json', 'service.json'):
                self.copy(self.stage / n, ROOT + '/' + n)
            for n in ('state.py', 'binding.json'):
                self.copy(self.stage / n, contract.STATE_ROOT + '/' + n)
            self.guest('committed-before', '/usr/bin/python3 -I -B ' + contract.STATE_ROOT + '/state.py before')
            self.download('committed-before.json', contract.STATE_ROOT + '/committed-before.json')
            self.guest('exact-source', 'umask 022\nmkdir -m755 ' + contract.TREE + '\ntar --no-same-permissions -xf ' + ROOT + '/source.tar -C ' + contract.TREE + '\n')
            self.retain_regressions()
            self.phase('focused-regressions-inherited')
            rc = self.guest('fresh-source-before-images', '/usr/bin/python3 -I -B ' + ROOT + '/guest.py fresh ' + self.doc['revision'], 300, False)
            self.receipt['native'] = self.download('native.json'); self.save(); assert rc == 0
            assert self.receipt['native']['checks'] == [{'name': EXPECTED_NATIVE_CHECKS[0], 'passed': True}]
            self.phase('fresh-source-closure-passed')
            self.guest('docker-real-prerequisite', 'test -S /var/run/docker.sock\ndocker info >/dev/null\n')
            before = {}
            before['backend'] = self.load_image('backend', production.BACKEND_ARCHIVE, 'privsep-wam:595523fb')
            before['service'] = self.load_image('service', OLD / 'evidence/service-image.tar.gz', 'privsep-service:e1ba0d0564f1')
            self.receipt['image_identity_before'] = before; self.save()
            self.phase('images-loaded-attested')
            # Gate 1: exact-evict-and-reload of the damaged baked manager image, BEFORE the 2 h root helper.
            self.integrity(GATES[0])
            gated = {k: self.attest(k, 'post-integrity') for k in ('backend', 'service')}
            assert gated == before, 'image identity changed across the integrity gate'
            self.receipt['image_identity_post_integrity'] = gated; self.save()
            self.phase('integrity-gate-before-installer-passed')
            self.phase('actual-none-root-installer')
            args = [self.doc['revision'], before['backend']['engine_image_id'], before['service']['engine_image_id']]
            # Gate 2 runs INSIDE this step, immediately before the real install.sh executes.
            rc = self.guest('actual-none-install', '/usr/bin/python3 -I -B ' + ROOT + '/guest.py install ' + shlex.join(args), 7200, False)
            self.receipt['native'] = self.download('native.json'); self.save()
            self.receipt['image_integrity'] = self.download('image-integrity.json', required=False); self.save()
            assert rc == 0
            integrity.validate_export(self.receipt['image_integrity'], GATES[:2])
            self.phase('real-install-sh-completed')
            self.copy(PRIOR / 'service-members.json', '/root/storage-production/service-members.json')
            self.integrity(GATES[2], ' verify')     # content still pinned after the installer started the manager
            guest = contract.TREE + '/qa/privsep-storage/production_guest.py'
            for name, command in ((contract.CASES[0], 'installed ' + shlex.join(args[1:])), (contract.CASES[1], 'stale-mapping')):
                self.guest(name, '/usr/bin/python3 -I -B ' + guest + ' ' + command, 300)
                self.receipt.setdefault('actual_cases', []).append({'name': name, 'passed': True}); self.save()
            for n in ('installed', 'stale-mapping'):
                self.download(n + '.json', '/root/storage-production/' + n + '.json')
            self.guest('native-security', '/usr/bin/python3 -I -B ' + ROOT + '/guest.py security', 300)
            self.receipt['native'] = self.download('native.json'); assert self.receipt['native']['passed']; self.save()
            after = {k: self.attest(k, 'after') for k in ('backend', 'service')}
            assert after == before; self.receipt['image_identity_after'] = after; self.save()
            self.guest('committed-after', '/usr/bin/python3 -I -B ' + contract.STATE_ROOT + '/state.py after')
            self.download('committed-after.json', contract.STATE_ROOT + '/committed-after.json')
            assert (self.evidence / 'committed-before.json').read_bytes() == (self.evidence / 'committed-after.json').read_bytes()
            self.receipt['committed_storage_metadata_unchanged'] = True; self.save()
            self.guest('stop-native-services', '/usr/bin/python3 -I -B ' + ROOT + '/guest.py cleanup', 120)
            self.guest('exact-cleanup-with-negative', '/usr/bin/python3 -I -B ' + ROOT + '/guest.py cleanup-containers', 240)
            self.receipt['native'] = self.download('native.json')
            for n in ('container-cleanup.json', 'cleanup-negative.json'):
                self.download(n)
            self.receipt['image_integrity_summary'] = integrity.validate_export(self.receipt['image_integrity'], GATES)
            self.receipt['new_guest_container_absence_proven'] = True
            self.receipt['perform_complete'] = True; self.save()

        def finish(self):
            self.download('native.json', required=False)
            self.download('image-integrity.json', required=False)
            if not self.receipt.get('perform_complete') and 'actual-none-root-installer' in self.receipt['reached_phases']:
                rc = self.guest('bounded-failed-install-cleanup', '/usr/bin/python3 -I -B ' + ROOT + '/guest.py cleanup-failed', 180, False)
                self.download('container-cleanup.json', required=False)
                self.receipt['failed_cleanup_exit'] = rc
                if rc != 0:
                    self.receipt['cleanup_errors'].append('failed-install-exact-cleanup-unproven')
            if self.receipt.get('perform_complete'):      # same as 610: only a completed run powers off cleanly
                self.guest('one-poweroff', 'sync\nsystemctl poweroff', 60, False); self.proc.wait(timeout=90)

    return IntegrityRunner


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
    d, helpers, report, contract = validate(args.stage, args.manifest_sha256, args.key, args.execute)
    if args.preflight:
        print(json.dumps(report, sort_keys=True)); return 0 if report['admissible'] else 1
    base = load('ii_vm_transport', args.stage / 'storage-runner.py'); base.native_lock()
    assert report['admissible'], 'capacity/QEMU/spool admission refused'
    os.umask(0o077); tmp = args.stage / 'tmp'; tmp.mkdir(mode=0o700)
    os.environ['TMPDIR'] = str(tmp); os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    r = make_runner(base, helpers, contract)(args.stage, d)
    r.monitor_state = {}
    r.receipt.update(scope='contract A rerun after guest image integrity gate: six native checks plus two Service cases; '
                           '105 focused fixtures inherited from 610, not re-executed',
                     full_acceptance=False, admission=report, retries=0, label=d['label'],
                     old_474_container_absence='UNPROVEN', owner_identity_positive_proven=False)
    r.save()
    production = load('ii_production', PRIOR / 'runner.py')
    stop = threading.Event(); mon = threading.Thread(target=r.monitor, args=(stop,), daemon=True); mon.start()

    def interrupt(sig, frame):
        raise TimeoutError('bounded contract A native deadline')
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, interrupt)
    signal.alarm(ALARM)
    try:
        r.perform(production)
    except BaseException as e:
        r.receipt['first_error'] = {'type': type(e).__name__, 'frames': [{'file': Path(f.filename).name, 'function': f.name,
                                    'line': f.lineno} for f in traceback.extract_tb(e.__traceback__)]}
        r.receipt['passed'] = False; r.save()
    finally:
        signal.alarm(420)
        try:
            if r.proc is not None and r.proc.poll() is None:
                r.finish()
        except BaseException as e:
            r.receipt['cleanup_errors'].append('guest-finalize-' + type(e).__name__)
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
            r.receipt['original_disks_and_old_evidence_unchanged'] = True
        except BaseException as e:
            r.receipt['cleanup_errors'].append('preservation-' + type(e).__name__)
        try:
            r.capacity()
        except BaseException as e:
            r.receipt['cleanup_errors'].append('capacity-' + type(e).__name__)
        r.receipt['passed'] = bool(r.receipt.get('perform_complete') and r.receipt['first_error'] is None and
                                   not r.receipt['cleanup_errors'] and r.receipt.get('new_guest_container_absence_proven') and
                                   r.receipt.get('all_boot_pids_absent') and r.receipt.get('original_disks_and_retained_unchanged') and
                                   r.receipt.get('image_integrity_summary') is not None)
        r.save(); signal.alarm(0)
    print(json.dumps({'passed': r.receipt['passed'], 'receipt': str(r.evidence / 'receipt.json'), 'full_acceptance': False}))
    return 0 if r.receipt['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
