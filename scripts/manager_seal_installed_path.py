#!/usr/bin/env python3
"""First installed synthetic manager slice; ONLY parent may submit on forum.

No image pulls/builds, no live data, no bootstrap patches. --inside-build is a
bounded child container phase, never a second queue submission. The raw Docker
socket is held solely by this controller and the separately confined guard.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import traceback

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import manager_seal_installed_capacity as capacity
from manager_seal_forward_guard import UnixHTTP
import manager_seal_forward_guard as forward_guard
from manager_seal_install_evidence import dependency_step, sanitize_line, capture_install, sanitize_log
from manager_seal_bundle import validate_bundle
import manager_seal_installed_recovery as recovery

GIB = 2**30
MIB = 2**20
PG = capacity.IMAGES[0]
RABBIT = capacity.IMAGES[1]
DONOR = capacity.IMAGES[2]
BUILD = 'sha256:04133e3c92bb23638cec40f6696efecee9427c41c25305bc34d9629775b48d11'
PYTHON = 'sha256:ae4e8b7c575fabea88a029b6cae757f342ca2525e07f7c22272d5105c346b35e'
NODE = Path('/srv/work/heavy/incognito-20261027/tooling/node')
NODE_SHA = 'e6ec2c188d83d813f81f2de8aea084d74dce603ac1abedd0a30ad941b10087b2'
DOCKER_SHA = '32b607f4bef9c7d0a5e717ed812590bfc5253a9dbabcd7210f03c5316dcfa44f'
COMPOSE_SHA = 'bd5835ccbbf06a42dcb5294c65e34a4634b34447afb9ed6fc7adf18a000e0f99'
IMAGES = [PG, RABBIT, DONOR, BUILD, PYTHON]
MAIN = 'agent-synthetic'
URN = MAIN+':migrated'
PROJECT = MAIN+'_migrated'
SERVICES = [MAIN, MAIN+'-tinfoil', MAIN+'-search', MAIN+'-key', MAIN+'-edge']
NETS = [MAIN+'-'+s for s in ('seal', 'provider', 'keyed', 'edge-uplink')]
# A conservative allocation envelope, not lifetime writes. No cached layers
# counted as new. Current stage is already in free/retained at runtime.
FOOTPRINT = {'source_stage': 64*MIB, 'tool_copies': 100*MIB, 'sanitized_logs': 64*MIB,
             'new_artifacts': 128*MIB, 'control_and_ledger': 32*MIB,
             'daemon_overlay_and_metadata_estimate': 256*MIB, 'margin': 380*MIB}
# Max simultaneous hard memory caps, including Docker shmem within containers.
MEMORY = {'build': 6*GIB, 'manager': GIB, 'postgres': GIB, 'rabbit': GIB,
          'guard': 256*MIB, 'fixture': 5*128*MIB, 'controller': GIB, 'launcher_headroom': 256*MIB,
          'sentinels': 2*32*MIB, 'tmpfs_keeper': 32*MIB}
TMPFS = {'work': 5*GIB, 'fixture': 64*MIB, 'postgres_data': 512*MIB,
         'postgres_run': 16*MIB, 'postgres_tmp': 16*MIB, 'rabbit_data': 512*MIB,
         'rabbit_logs': 16*MIB, 'rabbit_tmp': 16*MIB, 'manager_data': 256*MIB,
         'manager_appdata': 64*MIB, 'manager_cache': 32*MIB, 'manager_tmp': 64*MIB,
         'guard_tmp': 16*MIB, 'controller_tmp': 16*MIB, 'shmem': 12*64*MIB}
assert sum(FOOTPRINT.values()) == GIB == capacity.ALLOWANCE
assert sum(MEMORY.values()) <= 12*GIB
assert sum(TMPFS.values()) <= 8*GIB


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest(root, expected_source, expected_sha):
    data = (root/'SOURCE.json').read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected_sha
    value = json.loads(data); assert value['source'] == expected_source
    for rel, digest in value['files'].items():
        p = root/rel
        assert p.is_file() and not p.is_symlink() and sha(p) == digest, 'source pin drift'
        assert p.stat().st_mode & 0o777 == value['modes'][rel]
    return value


def build_command(early, root, out, owner, uid, gid, source, manifest_sha, work, fixturepath):
    cmd = early.create_command(root, owner+'-build', uid, gid, source, manifest_sha)
    idx = cmd.index('--tmpfs'); del cmd[idx:idx+2]
    idx = cmd.index('--memory'); cmd[idx+1] = str(6*GIB)
    idx = cmd.index('--memory-swap'); cmd[idx+1] = str(6*GIB)
    cmd = [v.replace(str(root/'manager-seal-early-results/bootstrap-tmp'), str(out/'bootstrap-tmp')) for v in cmd]
    idx = cmd.index('--workdir'); cmd[idx:idx] = ['--label', 'wizard.qa.manager-seal='+owner, '--mount', f'type=volume,src={work},dst=/work,volume-nocopy',
        '--mount', 'type=bind,src=/usr/bin/docker,dst=/tooling/docker,readonly',
        '--mount', f'type=bind,src={out}/compose,dst=/tooling/compose,readonly']
    idx = cmd.index('/source/scripts/manager-seal-early.py')
    cmd[idx:] = ['/source/scripts/manager_seal_installed_path.py', '--inside-build', '--source', source,
                 '--manifest-sha256', manifest_sha, '--fixture-root', fixturepath, '--owner', owner]
    return cmd


def fixture(root, owner, uid, gid):
    labels = {'wizard.private-network-policy': 'tinfoil-sealed-v1', 'wizard.qa.manager-seal': owner}
    mapping = {MAIN: ['seal'], MAIN+'-tinfoil': ['provider', 'keyed'], MAIN+'-search': ['provider', 'keyed'],
               MAIN+'-key': ['seal', 'keyed'], MAIN+'-edge': ['seal', 'provider', 'edge-uplink']}
    doc = {'services': {}, 'networks': {}}
    for service, nets in mapping.items():
        doc['services'][service] = {'image': PYTHON, 'user': f'{uid}:{gid}', 'container_name': owner+'-'+service,
            'entrypoint': ['python3', '-c'], 'command': ['import time; time.sleep(1800)'],
            'restart': 'no', 'pull_policy': 'never', 'read_only': True, 'mem_limit': '128m',
            'memswap_limit': '128m', 'pids_limit': 32, 'cpus': .5, 'cap_drop': ['ALL'],
            'security_opt': ['no-new-privileges:true'], 'logging': {'driver': 'none'},
            'networks': {MAIN+'-'+n: {} for n in nets}, 'labels': labels.copy()}
    doc['services'][MAIN]['volumes'] = [root+'/home:/home/synthetic:rw']
    doc['services'][MAIN]['environment'] = {'PUBLIC_KEY': 'wizard-sealed-provider-key-held-by-injector'}
    doc['services'][MAIN+'-key']['volumes'] = [root+'/key:/run/provider-key/key:ro']
    for network in NETS:
        internal = not network.endswith('edge-uplink')
        doc['networks'][network] = {'internal': internal, 'enable_ipv6': False, 'labels': labels.copy()}
        if internal: doc['networks'][network]['driver_opts'] = {'com.docker.network.bridge.inhibit_ipv4': 'true'}
    recipe = json.loads(json.dumps(doc)); recipe['x-runtipi'] = {'schema_version': 2}
    recipe['services'][MAIN]['x-runtipi'] = {'is_main': True}
    return doc, recipe


def manager_invocation(uid, gid, net, work, workpath, fixturevol, fixturepath, shared, out, source, dbpw, queuepw):
    """Actual installed manager argv, also used by the offline source audit."""
    return (['--workdir', recovery.MANAGER_WORKDIR, '--user', f'{uid}:{gid}', '--network', net,
            '--memory', str(GIB), '--memory-swap', str(GIB),
            '--mount', f'type=volume,src={work},dst=/work,readonly,volume-nocopy',
            '--mount', f'type=bind,src={workpath}/runtime,dst=/app,readonly',
            '--mount', f'type=volume,src={fixturevol},dst={fixturepath},readonly,volume-nocopy',
            '--mount', f'type=bind,src={shared}/docker.sock,dst=/var/run/docker.sock',
            '--mount', f'type=bind,src={NODE},dst=/tooling/node,readonly',
            '--mount', f'type=bind,src=/usr/bin/docker,dst=/tooling/docker,readonly',
            '--mount', f'type=bind,src={out}/compose,dst=/tooling/compose,readonly',
            *sum((['--tmpfs', f'{p}:size={n},uid={uid},gid={gid},mode=0700'] for p, n in [('/data', 256*MIB), ('/app-data', 64*MIB), ('/cache', 32*MIB), ('/tmp', 64*MIB)]), []),
            '--env', 'NODE_ENV=production', '--env', 'LOG_LEVEL=info', '--env', 'TIPI_VERSION=synthetic-'+source[:12],
            '--env', 'DOCKER_HOST=unix:///var/run/docker.sock', '--env', 'DOCKER_CONFIG=/tmp/docker-config', '--env', 'HOME=/tmp/home',
            '--env', 'PATH=/work/bin:/usr/local/bin:/usr/bin:/bin', '--env', 'ROOT_FOLDER_HOST='+fixturepath,
            '--env', 'POSTGRES_PASSWORD='+dbpw, '--env', 'RABBITMQ_HOST=runtipi-rabbitmq', '--env', 'RABBITMQ_USERNAME=synthetic',
            '--env', 'RABBITMQ_PASSWORD='+queuepw, '--entrypoint=sh'],
            ['-c', 'umask 077; mkdir -p /tmp/home /data/state; printf \'%s\' \'{"appsRepoUrl":"http://127.0.0.1:8080/store.git","allowErrorMonitoring":false,"allowAutoThemes":false,"logLevel":"info"}\' > /data/state/settings.json; python3 /app/store_http.py >/tmp/store.log 2>&1 & exec /tooling/node --experimental-sqlite /app/main.mjs'])


def inside_build(args):
    root = Path('/source'); pins = manifest(root, args.source, args.manifest_sha256)
    spec = importlib.util.spec_from_file_location('old', root/'scripts/manager-seal-docker.py')
    old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
    Path('/work/logs').mkdir()
    result = {'phase': 'bootstrap', 'steps': {}, 'built': False, 'source': args.source}
    def step(label, argv, cwd=Path('/work/source'), timeout=300, **kw):
        result['phase'] = label
        path = Path('/work/logs', label+'.log')
        outcome = capture_install(list(map(str, argv)), cwd, path, timeout)
        result['steps'][label] = outcome
        assert outcome['exit_code'] == 0 and outcome['log_complete'], 'build step failed'
        return 0
    try:
        checkout = old.bootstrap(root, pins, step)
        deps = {}; result['steps']['dependencies'] = deps
        dependency_step(checkout, Path('/work/logs'), pins['files']['bun.lock'], deps)
        assert deps['exit_code'] == 0 and deps['log_complete'] and deps['comparison_complete']
        step('common-build', ['bun', 'run', 'build'], cwd=checkout/'packages/common')
        inherited = None
        if args.continue_saved:
            # Dependencies/compiled CLIs were on deleted tmpfs, NOT retained.
            # Rehydrate their frozen prerequisites; never rebuild the backend.
            inherited = recovery.restore_bundle(Path('/saved/build-artifacts.tar.gz'), checkout/'packages/backend/dist')
            result['inherited_backend'] = {'source': recovery.PRIOR_SOURCE, 'archive_sha256': recovery.PRIOR_ARCHIVE_SHA,
                'original_steps': inherited['steps'], 'backend_rebuilt': False}
        else:
            step('backend-bundle', ['bun', 'run', 'bundle'], cwd=checkout/'packages/backend')
        result['bundle'] = validate_bundle(checkout/'packages/backend/dist', root, installed=checkout)
        if inherited is not None:
            assert result['bundle']['files'] == inherited['bundle']['files']
        source_map = json.loads((checkout/'packages/backend/dist/main.js.map').read_text())
        embedded = {}
        for rel in ('app.service.ts', 'main.ts', 'modules/app-lifecycle/handlers/install-app.handler.ts',
                    'modules/docker/docker.service.ts', 'modules/docker/docker.module.ts',
                    'modules/docker/mandatory-seal.ts', 'modules/docker/wizard-storage.ts'):
            key = '../src/'+rel; assert source_map['sources'].count(key) == 1
            content = source_map['sourcesContent'][source_map['sources'].index(key)]
            assert content == (root/'packages/backend/src'/rel).read_text(), 'new bundle embedded source drift'
            embedded[key] = hashlib.sha256(content.encode()).hexdigest()
        result['embedded_production_sources'] = embedded
        runtime = Path('/work/runtime'); runtime.mkdir()
        for p in (checkout/'packages/backend/dist').iterdir(): shutil.copy2(p, runtime/p.name)
        (runtime/'main.js').rename(runtime/'main.mjs')
        (runtime/'package.json').write_text('{"type":"module"}\n')
        (runtime/'node_modules').symlink_to(checkout/'packages/backend/node_modules')
        shutil.copytree(checkout/'packages/backend/assets', runtime/'assets')
        shutil.copytree(checkout/'packages/backend/src/core/database/drizzle', runtime/'assets/migrations')
        shutil.copytree(checkout/'packages/backend/src/modules/i18n/translations', runtime/'assets/translations')
        (runtime/'assets/frontend').mkdir(exist_ok=True)
        shutil.copy2(checkout/'scripts/manager_seal_store_http.py', runtime/'store_http.py')
        # Resolve QA imports through the backend's genuine frozen dependencies.
        helper = checkout/'scripts/manager_seal_installed_review.ts'
        text = helper.read_text().replace("from 'yaml'", "from '../packages/backend/node_modules/yaml'").replace("from 'arktype'", "from '../packages/backend/node_modules/arktype'")
        helper.write_text(text)
        step('review-cli-build', ['bun', 'build', '--target=node', '--format=esm', '--tsconfig-override', 'packages/backend/tsconfig.json',
             './scripts/manager_seal_installed_review.ts', '--outfile='+str(runtime/'review.mjs')])
        step('trusted-cli-build', ['bun', 'build', '--target=node', '--format=esm', '--tsconfig-override', 'packages/backend/tsconfig.json',
             './packages/backend/src/cli/enroll-mandatory-seal.ts', '--outfile='+str(runtime/'enroll.mjs')])
        compiled, recipe = fixture(args.fixture_root, args.owner, os.getuid(), os.getgid())
        (runtime/'compiled.json').write_text(json.dumps(compiled))
        (runtime/'recipe.json').write_text(json.dumps(recipe))
        store = runtime/'store'; app = store/'apps'/MAIN
        (app/'data').mkdir(parents=True); (app/'metadata').mkdir()
        config = {'id': MAIN, 'name': 'Synthetic sealed fixture', 'port': 18080, 'https': False, 'author': 'QA',
                  'no_gui': True, 'available': True, 'exposable': False, 'dynamic_config': True,
                  'source': 'https://example.invalid/synthetic', 'version': '1.0.0', 'categories': ['utilities'],
                  'description': 'Synthetic only', 'short_desc': 'Synthetic only', 'website': 'https://example.invalid',
                  'supported_architectures': ['amd64'], 'created_at': 0, 'updated_at': 0, 'deprecated': False,
                  'tipi_version': 1, 'force_expose': False, 'generate_vapid_keys': False, 'form_fields': [], 'force_pull': False}
        (app/'config.json').write_text(json.dumps(config)); (app/'docker-compose.yml').write_text(json.dumps(recipe))
        (app/'metadata/description.md').write_text('PUBLIC synthetic fixture.\n')
        for label, cmd, cwd in [
            ('store-init', ['git', 'init', '-b', 'main'], store), ('store-add', ['git', 'add', '.'], store),
            ('store-commit', ['git', '-c', 'user.name=QA', '-c', 'user.email=qa@example.invalid', 'commit', '-m', 'Synthetic fixture'], store),
            ('store-bare', ['git', 'clone', '--bare', str(store), str(runtime/'store.git')], runtime),
            ('store-repack', ['git', 'repack', '-ad'], runtime/'store.git')]: step(label, cmd, cwd=cwd)
        result['recipe_sha256'] = sha(runtime/'recipe.json'); result['compiled_sha256'] = sha(runtime/'compiled.json')
        result['assets'] = {str(p.relative_to(runtime)): sha(p) for p in (runtime/'assets').rglob('*') if p.is_file()}
        if inherited is not None:
            assert result['assets'] == inherited['assets'], 'unchanged runtime assets drift'
        result['built'] = True
    finally:
        (Path('/work')/'build-result.json').write_text(json.dumps(result, indent=2)+'\n')
        # Always preserve the newly built bundle if a later packaging phase fails.
        diagnostic_paths = []
        for path in sorted(Path('/work/logs').glob('*.log')):
            if path.name.endswith('.sanitized.log'): continue
            clean = path.with_name(path.stem+'.sanitized.log')
            if not clean.exists(): sanitize_log(path, clean)
            diagnostic_paths.append(clean)
        diagnostic_paths.extend(Path('/work/logs').glob('dependency-*.json'))
        assert sum(p.stat().st_size for p in diagnostic_paths) <= 64*MIB
        with tarfile.open('/work/build-artifacts.tar.gz', 'w:gz') as archive:
            for p in [Path('/work/build-result.json'), Path('/work/source/packages/backend/dist'), *diagnostic_paths]:
                if p.exists(): archive.add(p, arcname=str(p.relative_to('/work')))
        assert Path('/work/build-artifacts.tar.gz').stat().st_size <= 128*MIB


def copy_owned_file(cid, remote, target, cap):
    """Stream exactly one regular file; reject size/link metadata BEFORE write."""
    process = subprocess.Popen(['docker', 'cp', cid+':'+remote, '-'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timer = threading.Timer(45, process.kill); timer.daemon = True; timer.start()
    try:
        assert process.stdout is not None
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            member = archive.next()
            assert member is not None and member.isreg() and not member.linkname and member.sparse is None
            assert member.name in (Path(remote).name, './'+Path(remote).name) and 0 <= member.size <= cap
            stream = archive.extractfile(member); assert stream is not None
            with Path(target).open('xb') as out:
                left = member.size
                while left:
                    data = stream.read(min(left, MIB)); assert data
                    out.write(data); left -= len(data)
            assert archive.next() is None
        process.stdout.close()
        error = process.stderr.read(65537); assert len(error) <= 65536
        assert process.wait(timeout=15) == 0
    finally:
        timer.cancel()
        if process.poll() is None: process.kill(); process.wait(timeout=10)
        for stream in (process.stdout, process.stderr):
            if stream: stream.close()


def reconcile_runtime_events(events, creation_rows, container_ids, owner):
    containers = {r['id']: r['name'] for r in creation_rows if r.get('operation') == 'created' and r.get('kind') == 'container'}
    networks = {r['id']: r['name'] for r in creation_rows if r.get('operation') == 'created' and r.get('kind') == 'network'}
    mapping = {MAIN: ['seal'], MAIN+'-tinfoil': ['provider', 'keyed'], MAIN+'-search': ['provider', 'keyed'],
               MAIN+'-key': ['seal', 'keyed'], MAIN+'-edge': ['seal', 'provider', 'edge-uplink']}
    expected = set()
    for cid in container_ids:
        service = containers[cid].removeprefix(owner+'-')
        assert service in mapping
        expected.update((cid, PROJECT+'_'+MAIN+'-'+n) for n in mapping[service])
    observed = set(); created = set()
    for e in events:
        actor = e.get('Actor') or {}; attrs = actor.get('Attributes') or {}
        if e.get('Type') == 'container' and e.get('Action') == 'create' and actor.get('ID') in container_ids:
            created.add(actor['ID'])
        if e.get('Type') == 'network' and e.get('Action') == 'connect' and attrs.get('container') in container_ids:
            pair = (attrs['container'], networks.get(actor.get('ID')))
            assert pair in expected, 'unexpected transient attachment'
            observed.add(pair)
    assert created == set(container_ids), 'incomplete real create window'
    assert observed == expected, 'incomplete real network attachment window'
    return {'container_create_coverage': len(created), 'connect_pair_coverage': len(observed),
            'unexpected_transient_attachments': 0, 'finite_real_engine_event_window_complete': True}


def reconcile_creation_intents(rows, owner):
    """Metadata-only exact-new-name lookup after ambiguous real daemon IO."""
    found = []
    for row in rows:
        if row.get('operation') != 'creation-intent': continue
        kind, name = row.get('object_kind', row['kind']), row['name']
        assert kind in ('container', 'network', 'volume')
        import re
        assert re.fullmatch(r'[a-zA-Z0-9_-]{1,160}', name)
        base = ['docker', 'ps', '-aq', '--no-trunc'] if kind == 'container' else ['docker', kind, 'ls', '-q'] + ([] if kind == 'volume' else ['--no-trunc'])
        result = subprocess.run(base + ['--filter', 'name=^'+('/' if kind == 'container' else '')+name+'$',
                                '--filter', 'label=wizard.qa.manager-seal='+owner], capture_output=True, timeout=10)
        assert result.returncode == 0 and len(result.stdout) <= 4096
        values = result.stdout.decode().splitlines(); assert len(values) <= 1
        assert all(v == name if kind == 'volume' else re.fullmatch(r'[0-9a-f]{64}', v) for v in values)
        found.extend({'kind': kind, 'id': ident, 'name': name} for ident in values)
    return found


def reconcile_guard_accounting(rows, expected_denials, network_names, expected_cycles=1,
                               install_prune_window=None, expected_probe_rows=None):
    """Refusals remain exact; discovery must pair once with a later creation.

    No blanket exclusion of 403/404 or arbitrary names. Each fresh/recreated
    network needs one typed local miss, then its creation intent and real ID.
    """
    denies = [r for r in rows if r['operation'] == 'deny']
    prunes = [r for r in denies if r.get('reason') == 'prune_refused']
    probes = [r for r in denies if r.get('reason') != 'prune_refused']
    assert len(probes) == expected_denials, 'unexpected guard refusal'
    assert not any(r['operation'] == 'forward' and r.get('route') == '/containers/prune'
                   for r in rows), 'prune forwarded'
    if expected_probe_rows is not None:
        assert probes == expected_probe_rows, 'probe refusal identity drift'
    if install_prune_window is None:
        assert not prunes, 'prune outside install'
    else:
        assert len(prunes) == 1, 'expected exactly one install prune refusal'
        prune = prunes[0]
        assert set(prune) == {'sequence', 'operation', 'reason', 'route', 'filters'}
        assert prune['route'] == '/containers/prune'
        assert prune['filters'] == {'label': {'runtipi.appurn='+URN: True}}
        # Equality alone admits integer 1 as True. Retain the closed wire type.
        assert type(prune['filters']['label']['runtipi.appurn='+URN]) is bool
        sequences = [r['sequence'] for r in rows]
        assert sequences == list(range(1, len(rows)+1)), 'ledger sequence drift'
        activated = [r['sequence'] for r in rows if r['operation'] == 'activated']
        assert len(activated) == 1
        first_network = min(r['sequence'] for r in rows
                            if r['operation'] == 'creation-intent' and r.get('kind') == 'network')
        lower, upper = install_prune_window
        assert activated[0] <= lower < prune['sequence'] <= upper < first_network, 'prune outside install window'
        assert all(r['sequence'] < activated[0] for r in probes), 'probe after activation'
    assert type(expected_cycles) is int and expected_cycles > 0
    pending = {}; misses = 0; created = 0; counts = dict.fromkeys(network_names, 0)
    for row in rows:
        op = row['operation']
        if op == 'discovery-miss':
            name = row['name']
            assert row.get('kind') == 'network' and type(row.get('status')) is int and row['status'] == 404
            assert name in network_names and name not in pending, 'unexpected/duplicate discovery'
            pending[name] = 'miss'; misses += 1
        elif op == 'creation-intent' and row.get('kind') == 'network':
            name = row['name']; assert pending.get(name) == 'miss', 'network creation without discovery'
            pending[name] = 'intent'
        elif op == 'created' and row.get('kind') == 'network':
            name = row['name']; assert pending.pop(name, None) == 'intent', 'unpaired network creation'
            created += 1; counts[name] += 1
    assert not pending and misses == created and created > 0, 'incomplete discovery accounting'
    assert all(count == expected_cycles for count in counts.values()), 'unexpected discovery/create count'
    return {'expected_refusals': expected_denials+len(prunes), 'probe_refusals': len(probes),
            'install_prune_refusals': len(prunes), 'start_restart_prune_refusals': 0,
            'network_discovery_misses': misses, 'paired_network_creations': created}


def network_filter_canary(output, raw_socket='/var/run/docker.sock'):
    """Queued-controller ONLY: two zero-authority reads, through real Handler.

    No caller-supplied targets/authority. The legacy branch recreates ONLY the
    demonstrated mixed encoding with an all-zero full ID. It cannot enumerate
    foreign objects, activate, create or mutate anything. No CLI exposure.
    """
    import tempfile
    import urllib.parse
    name = PROJECT+'_'+NETS[0]
    incoming = '/networks?'+urllib.parse.urlencode({'filters': json.dumps({'name': {name: True}}, separators=(',', ':'))})
    cfg = {'containers': {}, 'networks': {}, 'images': [], 'network_names': [name]}
    report = {'passed': False, 'installed_proof': False, 'zero_authority': True,
              'same_incoming_query': incoming, 'runs': []}
    class CanaryPolicy(forward_guard.Policy):
        def authorize(self, method, target, data=b''):
            forward_guard.require(method == 'GET' and target == incoming and not data)
            op, target, data, body = super().authorize(method, target, data)
            if self.legacy:
                path, query = self.path(target)
                filters = forward_guard.decode(self.path(incoming)[1]['filters'])
                # Exact previous rewrite: preserve SDK map, inject array.
                filters['id'] = ['0'*64]
                query['filters'] = json.dumps(filters, separators=(',', ':'))
                target = path+'?'+urllib.parse.urlencode(query)
            self.forwarded_target = target
            return op, target, data, body
    try:
        with tempfile.TemporaryDirectory(prefix='msnf-', dir=os.environ.get('TMPDIR')) as tmp:
            for legacy in (True, False):
                rows = []; policy = CanaryPolicy(cfg, rows.append); policy.legacy = legacy
                socket_path = str(Path(tmp)/('old.sock' if legacy else 'new.sock'))
                assert len(os.fsencode(socket_path)) < 104
                server = forward_guard.Server(socket_path, raw_socket, policy)
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
                thread.start(); c = UnixHTTP(socket_path, timeout=10)
                try:
                    c.request('GET', incoming); response = c.getresponse()
                    data = response.read(8193); assert len(data) <= 8192
                    result = json.loads(data)
                    # Only fixed public decoder error / empty list can persist.
                    outcome = ('invalid_filter' if result == {'message': 'invalid filter'}
                               else 'empty_list' if result == [] else 'withheld_unexpected_response')
                    row = {'variant': 'previous_mixed' if legacy else 'canonical', 'status': response.status,
                           'outcome': outcome, 'forwarded_target': policy.forwarded_target,
                           'forward_count': sum(r['operation'] == 'forward' for r in rows),
                           'upstream_empty': next((r['upstream_empty'] for r in rows
                               if r['operation'] == 'zero-authority-network-result'), None)}
                    report['runs'].append(row)
                    assert row['forward_count'] == 1
                    assert legacy or row['upstream_empty'] is True, 'engine list was not empty before guard filtering'
                    assert (response.status, outcome) == ((400, 'invalid_filter') if legacy else (200, 'empty_list'))
                finally:
                    c.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)
                    assert not thread.is_alive()
        report['passed'] = True
        return report
    finally:
        raw = (json.dumps(report, sort_keys=True, indent=2)+'\n').encode()
        assert len(raw) <= 16384
        with Path(output).open('xb') as stream: stream.write(raw)


class Rig:
    def __init__(self, root, args):
        self.root = root; self.args = args; self.owner = 'msi-'+args.source[:12]
        self.out = root/'installed-results'; self.out.mkdir(mode=0o700)
        self.control = root/'guard-control'; self.control.mkdir(mode=0o700)
        self.shared = root/'guard-shared'; self.shared.mkdir(mode=0o700)
        self.ledger = self.out/'controller-ledger.jsonl'; self.ledger.touch(mode=0o600)
        self.ids = []; self.volumes = []; self.networks = []; self.processes = []; self.errors = []
        self.log_bytes = 0; self.log_lock = threading.Lock(); self.log_failure = None
        self.secrets = []
        self.receipt = {'source': args.source, 'manifest_sha256': args.manifest_sha256, 'phase': 'admission',
                        'first_installed_synthetic_path_pass': False, 'full_acceptance': False, 'RUNTIPI_ADMITTED': False,
                        'single_accepted_update_pass': False, 'successive_accepted_updates_pass': False,
                        'rollback_transaction_pass': False, 'official_image_pass': False,
                        'memory_caps': MEMORY, 'tmpfs_caps': TMPFS, 'allocation_envelope': FOOTPRINT,
                        'smaller_envelope_assessment': capacity.smaller_envelope_assessment(FOOTPRINT)}
        self.deadline = time.monotonic()+2500

    def checkpoint(self):
        assert time.monotonic() < self.deadline, 'overall deadline'
        assert self.log_failure is None, 'log cap or capture failure'
        rows = capacity.inventory(Path('/srv/work'), time.monotonic()+12)
        retained = sum(r['allocated_bytes'] for r in rows)
        own = sum(r['allocated_bytes'] for r in rows if r['path'] == str(self.root))
        # Allocate at least one filesystem block; conservative daemon metadata
        # remains unallocated until cleanup, never inferred from a free-space delta.
        assert own <= GIB-FOOTPRINT['daemon_overlay_and_metadata_estimate']
        free = shutil.disk_usage('/srv/work').free
        decision = capacity.admission(retained, free, already_allocated=own)
        assert decision['admitted'], 'capacity admission refused'
        self.receipt['capacity_latest'] = dict(decision, free_bytes=free, retained_allocated_bytes=retained,
                                               current_stage_allocated_bytes=own)

    def record(self, kind, ident, name=None):
        with self.ledger.open('a') as out: out.write(json.dumps({'kind': kind, 'id': ident, 'name': name})+'\n')
        (self.ids if kind == 'container' else self.networks if kind == 'network' else self.volumes).append(ident)

    def creation_intent(self, kind, name):
        row = {'operation': 'creation-intent', 'kind': 'resource-intent', 'object_kind': kind, 'name': name}
        with self.ledger.open('a') as out:
            out.write(json.dumps(row)+'\n'); out.flush(); os.fsync(out.fileno())

    def cmd(self, label, argv, timeout=30, input=None):
        self.receipt['operation'] = label
        result = subprocess.run(list(map(str, argv)), input=input, capture_output=True, timeout=timeout)
        assert len(result.stdout)+len(result.stderr) <= 4*MIB, 'command output cap'
        if result.returncode:
            self.receipt['command_failure'] = {'label': label, 'exit_code': result.returncode}
            raise RuntimeError('command failed')
        return result.stdout.decode().strip()

    def create(self, name, image, options, command, role='default', baseline=False):
        self.checkpoint()
        assert len(self.ids) < 24
        # Write intent first. On ambiguity there is no retry; cleanup reconciles
        # only these exact fresh names plus the unique ownership label.
        with self.ledger.open('a') as out: out.write(json.dumps({'kind': 'intent', 'name': name})+'\n')
        assert not baseline or (role == 'rabbit' and image == RABBIT and name.endswith('-canary-negative'))
        assert role != 'rabbit' or image == RABBIT
        limit = recovery.DEFAULT_FSIZE if baseline else recovery.role_fsize(role)
        cid = self.cmd('create-'+name, ['docker', 'create', '--pull=never', '--name', name,
            '--label', 'wizard.qa.manager-seal='+self.owner, '--read-only', '--log-driver=none',
            '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--pids-limit=256', '--cpus=2',
            '--shm-size=67108864', '--ulimit', 'core=0:0', '--ulimit', f'fsize={limit}:{limit}',
            *options, image, *command])
        assert len(cid) == 64; self.record('container', cid, name); return cid

    def start(self, cid, label):
        path = self.out/(label+'.sanitized.log')
        process = subprocess.Popen(['docker', 'start', '--attach', cid], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        def capture():
            try:
                with path.open('xb') as out:
                    while block := process.stdout.readline(65537):
                        assert len(block) <= 65536, 'log line cap'
                        with self.log_lock:
                            self.log_bytes += len(block); assert self.log_bytes <= 64*MIB
                        clean = sanitize_line(block)
                        for secret in self.secrets:
                            clean = clean.replace(secret.encode(), b'[SYNTHETIC-SECRET-REDACTED]')
                        # Never export any credential-bearing header or session field.
                        if any(t in clean.lower() for t in (b'cookie', b'authorization', b'password', b'jwt_secret', b'sessionid')):
                            clean = b'[credential-bearing log line withheld]\n'
                        out.write(clean); out.flush()
            except Exception:
                self.log_failure = True
        thread = threading.Thread(target=capture, daemon=True); thread.start()
        self.processes.append((process, thread)); return process

    def exec(self, cid, command, user=None, timeout=30, input=None):
        return self.cmd('owned-exec', ['docker', 'exec', *(['-i'] if input is not None else []),
                       *(['--user', user] if user else []), cid, *command], timeout, input)

    def state(self, cid):
        value = json.loads(self.cmd('owned-state', ['docker', 'inspect', '--format', '{{json .State}}', cid]))
        return {k: value[k] for k in ('Status', 'Running', 'ExitCode', 'OOMKilled', 'Dead')}

    def broker_cli(self, cid, user, action):
        self.receipt['operation'] = 'rabbit-'+action
        outcome = recovery.bounded_cli(recovery.broker_argv(cid, user, action))
        classification = recovery.cli_classification(outcome)
        ok = classification == 'success'
        row = {'action': action, 'classification': classification, 'exit_code': outcome['exit_code'], 'termination_reason': outcome['termination_reason'],
               'user': user, 'explicit_cli_env': recovery.CLI_ENV,
               'rpc_timeout_seconds': 10 if action in ('ping', 'check_running', 'list_queues') else None,
               'stdout_bytes_captured': len(outcome['stdout']), 'stderr_bytes_captured': len(outcome['stderr']),
               'stdout': recovery.sanitized_cli(outcome['stdout'], self.secrets),
               'stderr': recovery.sanitized_cli(outcome['stderr'], self.secrets)}
        # Keep first + latest failure per exact command, not 240 repeated copies.
        evidence = self.receipt.setdefault('rabbit_cli', {})
        slot = evidence.setdefault(action, {'attempts': 0, 'failures': 0})
        slot['attempts'] += 1
        if not ok:
            slot['failures'] += 1
            slot.setdefault('first_failure', row); slot['latest_failure'] = row
        else: slot['success'] = {k: v for k, v in row.items() if k not in ('stdout', 'stderr')}
        raw = (json.dumps(evidence, indent=2)+'\n').encode()
        assert len(raw) <= 256*1024
        (self.out/'rabbit-cli.sanitized.json').write_bytes(raw)
        if classification == 'fatal_file_size_limit':
            raise recovery.FatalBrokerCLI('synthetic broker file size limit; no readiness retry')
        if not ok: raise RuntimeError('synthetic broker CLI failed; see bounded diagnostic')
        return outcome['stdout'].decode().strip()

    def rabbit_ready(self, cid, user):
        if not self.state(cid)['Running']: raise RuntimeError('broker exited')
        try:
            # Fixed stat/test only: no cookie value or hash is ever read/exported.
            metadata = self.broker_cli(cid, user, 'cookie_metadata')
            values = metadata.split(); assert len(values) == 4 and all(v.isdigit() for v in values)
            assert values[0] == user.split(':')[0]
            self.receipt['rabbit_cookie_metadata'] = dict(zip(('uid', 'gid', 'mode', 'bytes'), values))
            self.broker_cli(cid, user, 'ping')
            self.broker_cli(cid, user, 'check_running')
            return True
        except recovery.FatalBrokerCLI: raise
        except RuntimeError: return False

    def rabbit_contract(self, cid, user, net, bound):
        # Allowlisted synthetic metadata projection, never Config.Env/cookies.
        fields = {'image': '.Image', 'user': '.Config.User', 'readonly': '.HostConfig.ReadonlyRootfs',
                  'memory': '.HostConfig.Memory', 'memory_swap': '.HostConfig.MemorySwap',
                  'tmpfs': '.HostConfig.Tmpfs', 'ulimits': '.HostConfig.Ulimits',
                  'pids': '.HostConfig.PidsLimit', 'cpus': '.HostConfig.NanoCpus',
                  'shmem': '.HostConfig.ShmSize', 'network': '.HostConfig.NetworkMode',
                  'caps': '.HostConfig.CapDrop', 'security': '.HostConfig.SecurityOpt'}
        template = '{'+','.join(json.dumps(k)+':{{json '+v+'}}' for k, v in fields.items())+'}'
        value = json.loads(self.cmd('owned-rabbit-contract', ['docker', 'inspect', '--format', template, cid]))
        assert value['image'] == RABBIT and value['user'] == user and value['readonly'] is True
        assert value['memory'] == value['memory_swap'] == GIB
        assert value['pids'] == 256 and value['cpus'] == 2000000000 and value['shmem'] == 64*MIB
        assert value['network'] == net and value['caps'] == ['ALL']
        assert value['security'] in (['no-new-privileges'], ['no-new-privileges:true']), 'no-new-privileges absent'
        uid, gid = map(int, user.split(':'))
        assert value['tmpfs'] == {p: f'size={size},uid={uid},gid={gid},mode=0700' for p, size in
            (('/var/lib/rabbitmq', 512*MIB), ('/var/log/rabbitmq', 16*MIB), ('/tmp', 16*MIB))}
        limits = {r['Name']: (r['Soft'], r['Hard']) for r in value['ulimits']}
        assert limits['core'] == (0, 0) and limits['fsize'] == (bound, bound)
        return value

    def rabbit_limit_canary(self, net, user, password):
        """Sequential fresh tmpfs brokers; exact SIGXFSZ negative, real RPC positive.

        No per-command RLIMIT shim, strace, extra image or capability. The only
        resource-policy change between the two is broker-role RLIMIT_FSIZE.
        """
        proof = {'public_image': RABBIT, 'user': user, 'passed': False,
                 'original_specific_file_proven': False, 'runs': []}
        self.receipt['rabbit_limit_canary'] = proof
        import fcntl
        # One-use stage + kernel-held exclusive holder, retained through the
        # installed phase. No extra holder container or overlapping brokers.
        self.canary_holder = os.open(self.root/'rabbit-canary-exclusive.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fcntl.flock(self.canary_holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proof['exclusive_holder_acquired'] = True
        options = recovery.rabbit_options(net, user, password)
        for negative in (True, False):
            label = 'canary-negative' if negative else 'canary-positive'
            bound = recovery.DEFAULT_FSIZE if negative else recovery.RABBIT_FSIZE
            row = {'kind': label, 'fsize_soft_bytes': bound, 'fsize_hard_bytes': bound}
            proof['runs'].append(row)
            # Each run has the same common create policy, UID, network, mounts,
            # env and command as the installed broker. Names/cookies are fresh.
            cid = self.create(self.owner+'-'+label, RABBIT, options, [], role='rabbit', baseline=negative)
            row['container_id'] = cid
            try:
                row['runtime_contract'] = self.rabbit_contract(cid, user, net, bound)
                self.start(cid, label)
                def booted():
                    state = self.state(cid)
                    if state.get('Status') in ('exited', 'dead') or state.get('OOMKilled'):
                        raise RuntimeError('canary broker exited')
                    if not state['Running']: return False  # docker start attach may still be scheduling
                    return 'Server startup complete; 3 plugins started.' in (self.out/(label+'.sanitized.log')).read_text()
                self.wait(label+'-server-started', booted, 120)
                row['server_started'] = True
                row['limit_metadata'] = recovery.validate_limit_metadata(self.broker_cli(cid, user, 'limit_metadata'), user, bound)
                row['public_files'] = recovery.validate_public_files(self.broker_cli(cid, user, 'public_files'))
                # Isolate exact action evidence per canary; never mix readiness
                # attempts from later installed RPC into the A/B proof.
                saved = self.receipt.pop('rabbit_cli', {})
                try:
                    if negative:
                        cookie = self.broker_cli(cid, user, 'cookie_metadata')
                        values = cookie.split()
                        assert len(values) == 4 and all(v.isdigit() for v in values) and values[0] == user.split(':')[0]
                        try: self.broker_cli(cid, user, 'ping')
                        except recovery.FatalBrokerCLI: pass
                        else: raise AssertionError('original precise file-size-limit failure not reproduced')
                        evidence = self.receipt['rabbit_cli']['ping']
                        assert evidence['attempts'] == evidence['failures'] == 1
                        assert evidence['first_failure']['classification'] == 'fatal_file_size_limit'
                        row['precise_negative'] = True
                    else:
                        self.wait(label+'-rpc', lambda: self.rabbit_ready(cid, user), 30)
                        assert all(self.receipt['rabbit_cli'][action].get('success') for action in ('cookie_metadata', 'ping', 'check_running'))
                        row['real_ping_and_check_running_pass'] = True
                    row['cli'] = self.receipt['rabbit_cli']
                finally:
                    row['cli'] = self.receipt.get('rabbit_cli', {})
                    self.receipt['rabbit_cli'] = saved
                row['state_after_rpc'] = self.state(cid)
                assert row['state_after_rpc']['Running'] and not row['state_after_rpc']['OOMKilled']
            finally:
                self.cmd('canary-owned-remove', ['docker', 'rm', '-f', cid])
                check = subprocess.run(['docker', 'inspect', '--format', '{{.Id}}', cid], capture_output=True, timeout=10)
                assert recovery.exact_missing(check, 'container', cid), 'canary cleanup not verified'
                self.ids.remove(cid)
                row['removed_verified'] = True
                (self.out/'rabbit-limit-canary.json').write_text(json.dumps(proof, indent=2)+'\n')
        contracts = []
        for row in proof['runs']:
            contract = dict(row['runtime_contract'])
            contract['ulimits'] = [v for v in contract['ulimits'] if v['Name'] != 'fsize']
            contracts.append(contract)
        assert contracts[0] == contracts[1], 'canary policy changed beyond fsize'
        proof['passed'] = all(row['removed_verified'] for row in proof['runs'])
        proof['same_option_producer_as_installed'] = True
        proof['causal_limit_ab_proven'] = True
        (self.out/'rabbit-limit-canary.json').write_text(json.dumps(proof, indent=2)+'\n')
        assert proof['passed']

    def wait(self, label, predicate, timeout=120):
        self.receipt['phase'] = label
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            self.checkpoint()
            if predicate(): return
            time.sleep(.5)
        raise TimeoutError('readiness timeout')

    def request(self, method, path, body=None, timeout=15):
        c = UnixHTTP(str(self.shared/'docker.sock'), timeout=timeout)
        try:
            c.request(method, path, body=json.dumps(body) if body is not None else None)
            r = c.getresponse(); data = r.read(16*MIB+1); assert len(data) <= 16*MIB
            return r.status, json.loads(data) if data else None
        finally: c.close()

    def events(self, since, until):
        c = UnixHTTP(str(self.shared/'docker.sock'), timeout=20)
        try:
            c.request('GET', f'/events?since={since}&until={until}')
            r = c.getresponse(); assert r.status == 200
            data = r.read(16*MIB+1); assert len(data) <= 16*MIB
            return [json.loads(line) for line in data.splitlines()]
        finally: c.close()

    def run(self):
        self.checkpoint()
        if self.args.continue_saved:
            prior_raw = (recovery.PRIOR_ROOT/'SOURCE.json').read_bytes()
            self.receipt['backend_reuse'] = recovery.verify_reuse(prior_raw, json.loads((self.root/'SOURCE.json').read_bytes()))
            prior_receipt = recovery.PRIOR_ROOT/'installed-results/receipt.json'
            assert sha(prior_receipt) == recovery.PRIOR_RECEIPT_SHA
            assert sha(recovery.PRIOR_ROOT/'installed-results/build-artifacts.tar.gz') == recovery.PRIOR_ARCHIVE_SHA
            # Exercise compatibility under the actual cached controller Python,
            # not only the staging machine's newer interpreter. Offline only.
            preflight_log = self.out/'offline-recovery-preflight.log'
            preflight = capture_install([sys.executable, '-B', '-m', 'unittest', 'discover', '-s',
                'scripts/tests', '-p', 'test_manager_seal_installed_path.py', '-v'], self.root, preflight_log, 30)
            preflight['sanitized'] = sanitize_log(preflight_log, self.out/'offline-recovery-preflight.sanitized.log')
            self.receipt['offline_recovery_preflight'] = preflight
            assert preflight['exit_code'] == 0 and preflight['log_complete']
            filter_log = self.out/'offline-network-filter-preflight.log'
            filter_preflight = capture_install([sys.executable, '-B', '-m', 'unittest', 'discover', '-s',
                'scripts/tests', '-p', 'test_manager_seal_network_filters.py', '-v'], self.root, filter_log, 60)
            filter_preflight['sanitized'] = sanitize_log(filter_log, self.out/'offline-network-filter-preflight.sanitized.log')
            self.receipt['offline_network_filter_preflight'] = filter_preflight
            assert filter_preflight['exit_code'] == 0 and filter_preflight['log_complete']
        assert os.uname().machine == 'x86_64' and os.getuid() != 0
        assert sha(NODE) == NODE_SHA and sha('/usr/bin/docker') == DOCKER_SHA
        assert self.cmd('daemon-root', ['docker', 'info', '--format', '{{.DockerRootDir}}']) == '/var/lib/docker'
        assert int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))*1024 >= 17*GIB
        inventory = []
        for image in IMAGES:
            data = json.loads(self.cmd('cached-image', ['docker', 'image', 'inspect', image]))[0]
            assert data['Id'] == image and data['Architecture'] == 'amd64' and data['Os'] == 'linux'
            inventory.append({'id': image, 'bytes': data['Size'], 'digests': data.get('RepoDigests', []),
                              'volumes': sorted((data['Config'].get('Volumes') or {}).keys())})
        self.receipt['cached_images'] = inventory
        assert next(v for v in inventory if v['id'] == PG)['volumes'] == ['/var/lib/postgresql/data']
        assert next(v for v in inventory if v['id'] == RABBIT)['volumes'] == ['/var/lib/rabbitmq']
        for image in (BUILD, PYTHON, DONOR):
            assert not next(v for v in inventory if v['id'] == image)['volumes']
        self.receipt['phase'] = 'real-daemon-network-filter-prebootstrap'
        self.receipt['network_filter_canary'] = network_filter_canary(self.out/'network-filter-canary.json')
        uid, gid = os.getuid(), os.getgid()
        self.receipt['phase'] = 'cached-public-tool-and-service-identity'
        donor = self.create(self.owner+'-donor', DONOR, ['--network=none', '--memory=33554432', '--memory-swap=33554432'], [])
        copy_owned_file(donor, '/usr/local/bin/docker-compose', self.out/'compose', 100*MIB)
        assert (self.out/'compose').stat().st_size <= 100*MIB and sha(self.out/'compose') == COMPOSE_SHA
        (self.out/'compose').chmod(0o755)
        # Actual pinned producer -> unchanged forwarding code -> counting Unix
        # protocol peer. NO real daemon in this phase; fail before build/Nest.
        self.receipt['phase'] = 'producer-protocol-prebootstrap'
        self.cmd('producer-protocol', [sys.executable, '-B', self.root/'scripts/manager_seal_producer_witness.py',
            '--compose', self.out/'compose', '--output', self.out/'producer-witness.json'], timeout=90)
        witness = json.loads((self.out/'producer-witness.json').read_text())
        assert witness['protocol_pass'] is True and witness['installed_proof'] is False
        self.receipt['producer_protocol_witness'] = {'sha256': sha(self.out/'producer-witness.json'), **witness['summary']}
        if self.args.producer_readiness_only:
            self.receipt['producer_readiness_pass'] = True
            self.receipt['phase'] = 'producer-protocol-only-complete-not-installed-proof'
            return
        identities = {}
        for image, role in ((PG, 'postgres'), (RABBIT, 'rabbitmq')):
            c = self.create(self.owner+'-'+role+'-metadata', image,
                ['--network=none', '--memory=33554432', '--memory-swap=33554432',
                 *sum((['--tmpfs', p+':size=4096'] for p in next(v for v in inventory if v['id'] == image)['volumes']), [])], [])
            copy_owned_file(c, '/etc/passwd', self.out/(role+'-passwd'), 65536)
            passwd = (self.out/(role+'-passwd')).read_text().splitlines()
            row = next(r.split(':') for r in passwd if r.startswith(role+':'))
            identities[role] = (int(row[2]), int(row[3])); assert identities[role][0] != 0
            copy_owned_file(c, '/usr/local/bin/docker-entrypoint.sh', self.out/(role+'-entrypoint.sh'), MIB)
            self.receipt.setdefault('public_entrypoints', {})[role] = sha(self.out/(role+'-entrypoint.sh'))
            if role == 'rabbitmq':
                # Public pinned image PATH/HOME identifies this sbin directory.
                # No new helper: copy static public source from existing donor.
                copy_owned_file(c, '/opt/rabbitmq/sbin/rabbitmq-env', self.out/'rabbitmq-env', 65536)
                validated = recovery.validate_rabbit_cli_source((self.out/'rabbitmq-env').read_bytes())
                self.receipt['rabbit_cli_source'] = {'public_image': RABBIT,
                    'path': '/opt/rabbitmq/sbin/rabbitmq-env', **validated,
                    'original_exit153_cause_proven': False}
            self.cmd('remove-metadata-donor', ['docker', 'rm', c]); self.ids.remove(c)
        self.cmd('remove-tool-donor', ['docker', 'rm', donor]); self.ids.remove(donor)
        # Exclusive single queued controller; one disposable broker at a time.
        # Retain this exact network/password for the later installed broker.
        self.creation_intent('network', self.owner+'-infra')
        net = self.cmd('infra-network-create', ['docker', 'network', 'create', '--internal', '--label', 'wizard.qa.manager-seal='+self.owner, self.owner+'-infra'])
        self.record('network', net)
        dbpw = os.urandom(24).hex(); queuepw = os.urandom(24).hex(); self.secrets.extend([dbpw, queuepw])
        self.rabbit_limit_canary(net, ':'.join(map(str, identities['rabbitmq'])), queuepw)
        assert self.receipt['rabbit_limit_canary']['passed'], 'pre-build Rabbit canary required'
        volumes = {}
        for role, size in (('work', 5*GIB), ('fixture', 64*MIB)):
            name = self.owner+'-'+role
            self.creation_intent('volume', name)
            self.cmd('volume-create', ['docker', 'volume', 'create', '--driver=local', '--label', 'wizard.qa.manager-seal='+self.owner,
                '--opt', 'type=tmpfs', '--opt', 'device=tmpfs', '--opt', f'o=size={size},uid={uid},gid={gid},mode=0700,nosuid,nodev', name])
            self.record('volume', name)
            mount = self.cmd('owned-volume-mount', ['docker', 'volume', 'inspect', '--format', '{{.Mountpoint}}', name])
            assert mount == '/var/lib/docker/volumes/'+name+'/_data'
            volumes[role] = (name, mount)
        work, workpath = volumes['work']; fixturevol, fixturepath = volumes['fixture']
        # local-driver tmpfs disappears when its final active mount is released.
        # Keep BOTH volumes mounted across build/init process exit, until cleanup.
        keeper = self.create(self.owner+'-keeper', PYTHON, ['--user', f'{uid}:{gid}', '--network=none',
            '--memory=33554432', '--memory-swap=33554432',
            '--mount', f'type=volume,src={work},dst=/work,volume-nocopy',
            '--mount', f'type=volume,src={fixturevol},dst={fixturepath},volume-nocopy', '--entrypoint=python3'],
            ['-c', 'import time; time.sleep(2700)'])
        self.start(keeper, 'tmpfs-keeper'); self.keeper = keeper
        self.wait('tmpfs-keeper-mounted', lambda: self.state(keeper)['Running'], 10)
        self.receipt['synthetic_roots'] = {'fixture': fixturepath, 'work': workpath}
        # Existing bootstrap and Bun alias, but no Docker authority in build.
        spec = importlib.util.spec_from_file_location('early', self.root/'scripts/manager-seal-early.py')
        early = importlib.util.module_from_spec(spec); spec.loader.exec_module(early)
        early.prepare_bunx_tmp_alias(self.out, uid)
        cmd = build_command(early, self.root, self.out, self.owner, uid, gid, self.args.source,
                            self.args.manifest_sha256, work, fixturepath)
        if self.args.continue_saved:
            idx = cmd.index('--workdir')
            cmd[idx:idx] = ['--mount', f'type=bind,src={recovery.PRIOR_ROOT}/installed-results/build-artifacts.tar.gz,dst=/saved/build-artifacts.tar.gz,readonly']
            cmd.append('--continue-saved')
        self.creation_intent('container', self.owner+'-build')
        builder = self.cmd('build-create', cmd); self.record('container', builder)
        process = self.start(builder, 'build')
        self.wait('saved-backend-runtime-assembly' if self.args.continue_saved else 'new-backend-bundle-build', lambda: process.poll() is not None, 1000)
        for name in ('build-result.json', 'build-artifacts.tar.gz'):
            copy_owned_file(builder, '/work/'+name, self.out/name, MIB if name.endswith('.json') else 128*MIB)
        result = json.loads((self.out/'build-result.json').read_text()); self.receipt['build'] = result
        assert (self.out/'build-artifacts.tar.gz').stat().st_size <= 128*MIB
        self.receipt['continuation_bundle_archive_sha256' if self.args.continue_saved else 'new_bundle_archive_sha256'] = sha(self.out/'build-artifacts.tar.gz')
        assert process.returncode == 0 and result['built']
        assert not self.state(builder)['OOMKilled']
        # Fixture initializer has no raw/proxy socket and no network.
        init = self.create(self.owner+'-init', PYTHON, ['--user', f'{uid}:{gid}', '--network=none', '--memory=33554432', '--memory-swap=33554432',
            '--mount', f'type=volume,src={fixturevol},dst={fixturepath},volume-nocopy', '--entrypoint=python3'],
            ['-c', f"from pathlib import Path; p=Path({fixturepath!r}); (p/'home').mkdir(mode=0o700); (p/'home/history.txt').write_text('Synthetic continuity only.\\n'); (p/'key').write_text('synthetic-not-a-provider-credential\\n'); (p/'key').chmod(0o600)"])
        self.cmd('fixture-init', ['docker', 'start', '--attach', init]); assert self.state(init)['ExitCode'] == 0
        # Positive owned object and unrelated-but-task-owned sentinel: never
        # started, no mounts, no private probes, full IDs captured by controller.
        probes = []; event_since = int(time.time())-1
        for suffix in ('positive', 'unrelated'):
            probes.append(self.create(self.owner+'-'+suffix, PYTHON, ['--network=none', '--user', f'{uid}:{gid}', '--memory=33554432', '--memory-swap=33554432'], ['python3', '-c', 'pass']))
        positive, unrelated = probes
        cfg = {'owner': self.owner, 'project': PROJECT, 'app_urn': URN, 'services': SERVICES, 'user': f'{uid}:{gid}',
               'images': [PYTHON], 'containers': {positive: self.owner+'-positive'}, 'networks': {},
               'container_names': [self.owner+'-'+s for s in SERVICES],
               'network_names': [PROJECT+'_'+n for n in NETS], 'uplink': PROJECT+'_'+MAIN+'-edge-uplink',
               'binds': {fixturepath+'/home': {'target': '/home/synthetic', 'mode': 'rw'},
                         fixturepath+'/key': {'target': '/run/provider-key/key', 'mode': 'ro'}},
               'activation': os.urandom(32).hex(), 'event_floor': event_since}
        (self.control/'config.json').write_text(json.dumps(cfg))
        guard = self.create(self.owner+'-guard', PYTHON, ['--user', f'{uid}:{gid}', '--group-add', str(Path('/var/run/docker.sock').stat().st_gid),
            '--network=none', '--memory=268435456', '--memory-swap=268435456', '--tmpfs', '/tmp:size=16777216',
            '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
            '--mount', f'type=bind,src={self.root}/scripts/manager_seal_forward_guard.py,dst=/guard.py,readonly',
            '--mount', f'type=bind,src={self.control},dst=/control,readonly',
            '--mount', f'type=bind,src={self.shared},dst=/shared',
            '--mount', f'type=volume,src={fixturevol},dst={fixturepath},readonly,volume-nocopy', '--entrypoint=python3'],
            ['/guard.py', '--config', '/control/config.json', '--socket', '/shared/docker.sock', '--ledger', '/shared/guard-ledger.jsonl', '--activate-file', '/control/activate'])
        self.guard = guard
        self.start(guard, 'guard')
        self.wait('guard-ready', lambda: (self.shared/'docker.sock').is_socket(), 20)
        # Runtime tests MUST precede infrastructure and manager bootstrap.
        self.receipt['phase'] = 'real-engine-guard-negatives'
        positives = self.request('GET', '/containers/'+positive+'/json')
        assert positives[0] == 200 and positives[1]['Id'] == positive
        probes = [('GET', '/containers/'+unrelated+'/json', None), ('GET', '/containers/'+unrelated+'/logs', None),
                  ('POST', '/containers/'+unrelated+'/exec', {'Cmd': ['true']}), ('POST', '/containers/'+unrelated+'/start', None),
                  ('DELETE', '/containers/'+unrelated, None), ('POST', '/networks/prune', {}),
                  ('POST', '/images/create?fromImage=python', None), ('POST', '/build', None),
                  ('POST', '/containers/create?name='+cfg['container_names'][0], {'Image': PYTHON, 'HostConfig': {'Binds': ['/:/host:ro'], 'Privileged': True}})]
        offset = (self.shared/'guard-ledger.jsonl').stat().st_size
        for method, path, body in probes: assert self.request(method, path, body)[0] == 403
        assert self.state(unrelated)['Status'] == 'created'
        ledger = [json.loads(l) for l in (self.shared/'guard-ledger.jsonl').read_text()[offset:].splitlines()]
        assert len([r for r in ledger if r['operation'] == 'deny']) == len(probes)
        assert not any(r['operation'] == 'forward' for r in ledger)
        listed = self.request('GET', '/containers/json?all=1')[1]
        assert {v['Id'] for v in listed} == {positive}
        observed_events = self.events(event_since, int(time.time())+1)
        assert any(e.get('Action') == 'create' and e['Actor']['ID'] == positive for e in observed_events)
        assert not any(e['Actor']['ID'] == unrelated for e in observed_events)
        self.receipt['guard_runtime_proof'] = {'positive_owned_inspect': True, 'unrelated_sentinel_denials': len(probes),
            'owned_event_readiness_marker': True, 'unrelated_events_filtered': True,
            'denied_requests_forwarded': 0, 'unrelated_unchanged': True, 'owned_list_only': True,
            'completed_before_manager_create': True}
        assert self.request('DELETE', '/containers/'+positive)[0] < 300; self.ids.remove(positive)
        (self.control/'activate').write_text(cfg['activation'])
        self.wait('guard-activation', lambda: any(json.loads(l)['operation'] == 'activated' for l in (self.shared/'guard-ledger.jsonl').read_text().splitlines()), 5)
        assert self.receipt['rabbit_limit_canary']['passed']
        infra = {}
        for role, image, service, env, aliases, mounts in [
            ('postgres', PG, 'postgres', ['POSTGRES_DB=tipi', 'POSTGRES_USER=tipi', 'POSTGRES_PASSWORD='+dbpw], ['runtipi-db'],
             [('/var/lib/postgresql/data', 512*MIB), ('/var/run/postgresql', 16*MIB), ('/tmp', 16*MIB)]),
            ('rabbit', RABBIT, 'rabbitmq', ['RABBITMQ_DEFAULT_USER=synthetic', 'RABBITMQ_DEFAULT_PASS='+queuepw,
             'RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS=+S 2:2', 'RABBITMQ_LOGS=-'], ['runtipi-rabbitmq'],
             [('/var/lib/rabbitmq', 512*MIB), ('/var/log/rabbitmq', 16*MIB), ('/tmp', 16*MIB)])]:
            suid, sgid = identities[service]
            options = ['--user', f'{suid}:{sgid}', '--network', net, '--network-alias', aliases[0], '--memory', str(GIB), '--memory-swap', str(GIB)]
            for k in env: options += ['--env', k]
            for path, size in mounts: options += ['--tmpfs', f'{path}:size={size},uid={suid},gid={sgid},mode=0700']
            if role == 'rabbit': options = recovery.rabbit_options(net, f'{suid}:{sgid}', queuepw)
            infra[role] = self.create(self.owner+'-'+role, image, options, [], role='rabbit' if role == 'rabbit' else 'default')
            if role == 'rabbit':
                contract = self.rabbit_contract(infra[role], f'{suid}:{sgid}', net, recovery.RABBIT_FSIZE)
                assert contract == self.receipt['rabbit_limit_canary']['runs'][1]['runtime_contract'], 'installed broker/canary policy drift'
                self.receipt['installed_rabbit_contract_matches_canary'] = True
            self.start(infra[role], role)
        def dbready():
            if not self.state(infra['postgres'])['Running']: raise RuntimeError('database exited')
            try:
                return 'accepting connections' in self.exec(infra['postgres'], ['pg_isready', '-U', 'tipi', '-d', 'tipi'])
            except RuntimeError: return False
        self.wait('postgres-ready', dbready, 90)
        self.wait('rabbit-ready', lambda: self.rabbit_ready(infra['rabbit'], ':'.join(map(str, identities['rabbitmq']))), 120)
        options, command = manager_invocation(uid, gid, net, work, workpath, fixturevol, fixturepath,
            self.shared, self.out, self.args.source, dbpw, queuepw)
        self.receipt['packaged_startup_source_audit'] = recovery.audit_packaged_startup(self.root, options, command, dbpw)
        manager = self.create(self.owner+'-manager', BUILD, options, command)
        self.manager = manager; self.db = infra['postgres']; self.guard = guard
        config = json.loads(self.cmd('owned-manager-confinement', ['docker', 'inspect', manager]))[0]
        assert [m['Source'] for m in config['Mounts'] if m['Destination'] == '/var/run/docker.sock'] == [str(self.shared/'docker.sock')]
        assert config['Config']['Env'].count('DOCKER_HOST=unix:///var/run/docker.sock') == 1
        assert config['Config']['WorkingDir'] == recovery.MANAGER_WORKDIR
        assert config['HostConfig']['ReadonlyRootfs'] and config['HostConfig']['MemorySwap'] == GIB
        self.receipt['manager_confinement'] = {'dockerode_socket_and_compose_host': 'proxy-only',
            'raw_socket_mounted': False, 'readonly_root': True, 'no_host_namespaces_or_privilege': True,
            'working_directory': config['Config']['WorkingDir']}
        self.start(manager, 'manager')
        self.wait('actual-nest-auth-http-ready', lambda: self.http('GET', '/api/apps/installed', authenticated=False, allow_notready=True)[0] in (401, 403), 120)
        self.exec(manager, ['/tooling/node', '/app/review.mjs', 'schema'])
        # Bootstrap catches errors itself: require its end-of-bootstrap files,
        # migrated DB, Git repository and real queue, not just an open HTTP port.
        self.exec(manager, ['sh', '-c', 'test -s /data/traefik/tls/cert.pem && test -s /data/traefik/tls/key.pem && test -f /data/traefik/tls/tipi.lan.txt && test -d /data/repos/migrated/.git && test -s /cache/cache.sqlite'])
        queue_rows = self.broker_cli(infra['rabbit'], ':'.join(map(str, identities['rabbitmq'])), 'list_queues')
        queue_counts = {line.split()[0]: int(line.split()[1]) for line in queue_rows.splitlines()
                        if len(line.split()) == 2 and line.split()[1].isdigit()}
        assert all(queue_counts.get(name, 0) > 0 for name in ('app-events-queue', 'repo-queue', 'system-events-queue'))
        self.receipt['real_queue_consumers'] = {name: queue_counts[name] for name in ('app-events-queue', 'repo-queue', 'system-events-queue')}
        self.receipt['full_bootstrap_assets_tls_git_cache'] = True
        denied = self.http('POST', '/api/app-lifecycle/'+URN+'/install', {'skipRun': True}, authenticated=False)[0]
        assert denied in (401, 403)
        register_password = os.urandom(24).hex(); self.secrets.append(register_password)
        self.http('POST', '/api/auth/register', {'username': 'synthetic@example.invalid', 'password': register_password}, authenticated=False)
        self.receipt['auth'] = {'actual_registration': True, 'unauthenticated_lifecycle_denied': True}
        assert self.http('GET', '/api/apps/installed')[0] == 200, 'registered session readback failed'
        self.receipt['auth']['actual_session_readback'] = True
        self.receipt['install_http_source_contract'] = recovery.audit_install_http_contract(self.root)
        self.receipt['phase'] = 'http-install-skipRun'
        assert self.sql("select count(*) from app where app_name='agent-synthetic' and app_store_slug='migrated';") == '0'
        assert not self.owned_fixture_ids(), 'URN collision before first POST'
        self.receipt['new_install_urn_absent_before_post'] = True
        form = {'skipRun': True, 'skipPull': True, 'skipEnv': False, 'forcePull': False, 'openPort': False,
                'exposed': False, 'exposedLocal': False, 'enableAuth': False, 'isVisibleOnGuestDashboard': False}
        install_lower = len((self.shared/'guard-ledger.jsonl').read_text().splitlines())
        self.transaction('install', form, 'stopped')
        install_upper = len((self.shared/'guard-ledger.jsonl').read_text().splitlines())
        self.receipt['install_guard_sequence_window'] = [install_lower, install_upper]
        assert not self.owned_fixture_ids(), 'pre-enrollment container creation'
        assert not any(json.loads(l).get('kind') == 'container' and json.loads(l)['operation'] == 'created' for l in (self.shared/'guard-ledger.jsonl').read_text().splitlines())
        subnet = self.sql("select subnet from app where app_name='agent-synthetic' and app_store_slug='migrated';")
        self.exec(manager, ['/tooling/node', '/app/review.mjs', 'review', subnet])
        self.exec(manager, ['/tooling/node', '/app/enroll.mjs', 'enroll', '/data', URN, '/app/compiled.json', '/data/reviewed-catalogue.json'])
        self.receipt['trusted_reviewed_pair_enrolled'] = True
        # Hostile optional source is purely synthetic, never an enrollment input.
        self.exec(manager, ['python3', '-c', "from pathlib import Path; p=Path('/data/user-config/migrated/agent-synthetic'); p.mkdir(parents=True, exist_ok=True); (p/'docker-compose.yml').write_text('services: {agent-synthetic: {privileged: true, network_mode: host}}\\n')"])
        runtime_since = int(time.time())-1
        self.transaction('start', None, 'running'); first = self.runtime()
        self.transaction('restart', None, 'running'); second = self.runtime()
        events = self.events(runtime_since, int(time.time())+1)
        creation_rows = [json.loads(l) for l in (self.shared/'guard-ledger.jsonl').read_text().splitlines()]
        self.receipt['guard_accounting'] = reconcile_guard_accounting(creation_rows, len(probes), cfg['network_names'],
            expected_cycles=2, install_prune_window=(install_lower, install_upper),
            expected_probe_rows=[r for r in ledger if r['operation'] == 'deny'])
        assert len([r for r in creation_rows if r.get('operation') == 'created' and r.get('kind') == 'container']) == 10
        self.receipt['real_runtime_events'] = reconcile_runtime_events(events, creation_rows, set(first+second), self.owner)
        (self.out/'runtime-events.json').write_text(json.dumps(events, indent=2)+'\n')
        self.receipt['runtime_events_sha256'] = sha(self.out/'runtime-events.json')
        assert set(first) != set(second) and not set(first) & set(second), 'restart did not really recreate'
        self.receipt['http_request_ids_distinct'] = len(set(self.receipt['request_ids'])) == 3
        assert self.receipt['http_request_ids_distinct']
        self.receipt['first_runtime_ids'] = first; self.receipt['restarted_runtime_ids'] = second
        self.receipt['first_installed_synthetic_path_pass'] = True
        self.receipt['phase'] = 'completed-first-installed-slice'

    def capture_http(self, method, path, authenticated, value):
        # Only this fixed synthetic endpoint set; no request bodies or headers.
        assert (method, path) in {('GET', '/api/apps/installed'), ('GET', '/api/apps/'+URN),
            ('POST', '/api/auth/register'), *(('POST', '/api/app-lifecycle/'+URN+'/'+a) for a in ('install', 'start', 'restart'))}
        clean, parse_error = recovery.sanitized_http_body(value.get('body_text', ''), self.secrets)
        row = {'status': value.get('status', 0), 'body': clean, 'parse_error': parse_error,
               'captured_bytes': value.get('captured_bytes', 0), 'body_truncated': value.get('body_truncated', False),
               'transport_error': value.get('transport_error'), 'authenticated_request': authenticated}
        assert row['transport_error'] in (None, 'timeout', 'transport_or_read_error', 'owned_exec_failed', 'invalid_envelope')
        key = method+' '+path+(' authenticated' if authenticated else ' unauthenticated')
        records = self.receipt.setdefault('http_responses', {})
        entry = records.setdefault(key, {'attempts': 0, 'first': row})
        entry['attempts'] += 1; entry['latest'] = row
        assert len(records) <= 16
        raw = (json.dumps(records, sort_keys=True, indent=2)+'\n').encode()
        assert len(raw) <= 256*1024, 'sanitized HTTP evidence cap'
        with (self.out/'http-responses.sanitized.json').open('wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        return clean, parse_error

    def http(self, method, path, body=None, authenticated=True, allow_notready=False):
        # Exactly one real fetch per call. No timeout/rejection/parse retries.
        try:
            output = self.exec(self.manager, ['/tooling/node', '--input-type=module', '-e', recovery.HTTP_CODE,
                json.dumps([method, path, body, getattr(self, 'cookie', None) if authenticated else None])], timeout=15)
        except (RuntimeError, subprocess.TimeoutExpired):
            self.capture_http(method, path, authenticated, {'transport_error': 'owned_exec_failed'})
            if allow_notready:
                if not self.state(self.manager)['Running']: raise RuntimeError('manager exited')
                return 0, None
            raise
        try: value = json.loads(output)
        except ValueError:
            self.capture_http(method, path, authenticated, {'transport_error': 'invalid_envelope'})
            raise RuntimeError('HTTP response envelope invalid') from None
        if value['cookie'] and path == '/api/auth/register':
            self.cookie = next((c.strip().split(';')[0] for c in value['cookie'].split(',') if c.strip().startswith('runtipi-sid=')), None)
            if self.cookie:
                self.secrets.extend((self.cookie, self.cookie.split('=', 1)[1]))
        # Persist status/body/parse/transport diagnostics BEFORE ANY assertion.
        clean, parse_error = self.capture_http(method, path, authenticated, value)
        if value['transport_error']:
            if allow_notready:
                if not self.state(self.manager)['Running']: raise RuntimeError('manager exited')
                return 0, None
            raise RuntimeError('HTTP transport failed; saved sanitized response')
        if method != 'GET' and not allow_notready and (authenticated or path == '/api/auth/register'):
            assert 200 <= value['status'] < 300, 'HTTP transaction rejected; saved sanitized response'
        assert not value['body_truncated'] and parse_error is None, 'HTTP body incomplete; saved sanitized response'
        if path == '/api/auth/register': assert getattr(self, 'cookie', None), 'registration cookie missing'
        return value['status'], json.loads(value['body_text'])

    def sql(self, query):
        return self.exec(self.db, ['psql', '-U', 'tipi', '-d', 'tipi', '-Atc', query])

    def transaction_ready(self, action, terminal, previous, log_offset, request_id):
        db = self.sql("select status from app where app_name='agent-synthetic' and app_store_slug='migrated';")
        # Start failure is 'stopped', NOT a mythical 'failed' enum (actual
        # start-app.handler.ts). A restart preflight can retain 'running'.
        log = self.out/'manager.sanitized.log'
        failed_log = False
        if log.exists():
            with log.open('rb') as stream:
                stream.seek(log_offset)
                failed_log = ('App '+URN+' '+action+' failed:').encode() in stream.read(64*MIB+1)
        stable = {'', 'stopped', 'running', 'missing'}
        if failed_log or (db in stable and db != terminal):
            # Read-only diagnostics, before abort. http() saves sanitized exact
            # manager body/status independently. There is NEVER a second POST.
            failure = {'action': action, 'request_id': request_id, 'database_status': db,
                       'manager_http_status': None, 'owned_fixture_ids': None,
                       'synthetic_failure_log_seen': failed_log, 'read_only': True}
            self.receipt['terminal_failure'] = failure
            try:
                http_status, _ = self.http('GET', '/api/apps/'+URN)
                failure['manager_http_status'] = http_status
                ids = self.owned_fixture_ids()
                assert all(forward_guard.ID.fullmatch(i) for i in ids) and len(ids) <= 5
                failure['owned_fixture_ids'] = ids
            finally:
                # Also retain the DB state if the diagnostic GET fails; its
                # bounded HTTP transport/error evidence was saved by http().
                raw = (json.dumps(failure, indent=2)+'\n').encode(); assert len(raw) <= 16384
                (self.out/'terminal-failure.json').write_bytes(raw)
            raise RuntimeError('terminal lifecycle failure; saved read-only app and manager evidence')
        return db == terminal and (action != 'restart' or
            (len(ids := self.owned_fixture_ids()) == 5 and not previous.intersection(ids)))

    def transaction(self, action, body, terminal):
        # EXACTLY one POST. Any timeout is ambiguous and stops the job; never
        # retry install (existing app takes a different launch path).
        previous = set(self.owned_fixture_ids()) if action == 'restart' else set()
        log = self.out/'manager.sanitized.log'
        log_offset = log.stat().st_size if log.exists() else 0
        status, value = self.http('POST', '/api/app-lifecycle/'+URN+'/'+action, body)
        import uuid
        request_id = value['requestId']; assert str(uuid.UUID(request_id)) == request_id
        self.receipt.setdefault('request_ids', []).append(request_id)
        self.wait('terminal-'+action,
                  lambda: self.transaction_ready(action, terminal, previous, log_offset, request_id), 180)
        status, value = self.http('GET', '/api/apps/'+URN)
        assert status == 200
        db = self.sql("select status||'|'||version||'|'||coalesce(subnet,'') from app where app_name='agent-synthetic' and app_store_slug='migrated';")
        assert db.startswith(terminal+'|1|')
        self.receipt.setdefault('transactions', []).append({'action': action, 'request_id': request_id, 'terminal_database': db,
                                                         'actual_auth_http_readback': True})

    def owned_fixture_ids(self):
        value = self.request('GET', '/containers/json?all=1')[1]
        return [r['Id'] for r in value if (r.get('Labels') or {}).get('com.docker.compose.project') == PROJECT]

    def runtime(self):
        ids = self.owned_fixture_ids(); assert len(ids) == 5
        seen = set()
        for cid in ids:
            status, value = self.request('GET', '/containers/'+cid+'/json'); assert status == 200 and value['State']['Running']
            s = value['Config']['Labels']['com.docker.compose.service']; seen.add(s)
            assert value['Image'] == PYTHON and value['Config']['Cmd'] == ['import time; time.sleep(1800)']
            assert value['Config']['Entrypoint'] == ['python3', '-c']
            mapping = {MAIN: ['seal'], MAIN+'-tinfoil': ['provider', 'keyed'], MAIN+'-search': ['provider', 'keyed'],
                       MAIN+'-key': ['seal', 'keyed'], MAIN+'-edge': ['seal', 'provider', 'edge-uplink']}
            assert set(value['NetworkSettings']['Networks']) == {PROJECT+'_'+MAIN+'-'+n for n in mapping[s]}
            assert not value['HostConfig']['Privileged']
        assert seen == set(SERVICES)
        self.exec(self.manager, ['python3', '-c', f"from pathlib import Path; import hashlib,json,os; p=Path({self.receipt['synthetic_roots']['fixture']!r}); assert (p/'home/history.txt').read_text()=='Synthetic continuity only.\\n'; assert (p/'key').read_text()=='synthetic-not-a-provider-credential\\n'; policy=Path('/data/state/mandatory-seals')/hashlib.sha256({URN!r}.encode()).hexdigest()/'policy.json'; assert policy.stat().st_mode&0o777==0o600; assert policy.stat().st_uid==os.getuid(); assert json.loads(policy.read_text())['schema']==2"])
        return ids

    def cleanup(self):
        # Reconcile create intents once, not a resubmission after ambiguous IO.
        # Exact caller-owned names + unique label, no broad host inventory.
        try:
            # Fence creators FIRST; only then consume the immutable creation ledger.
            for attr in ('manager', 'guard'):
                cid = getattr(self, attr, None)
                if cid:
                    subprocess.run(['docker', 'stop', '--time=3', cid], capture_output=True, timeout=15)
                    assert not self.state(cid)['Running'], 'creator fence failed'
            rows = [json.loads(l) for l in self.ledger.read_text().splitlines()]
            for name in {r['name'] for r in rows if r['kind'] == 'intent'}:
                found = self.cmd('reconcile-owned-intent', ['docker', 'ps', '-aq', '--no-trunc', '--filter', 'name=^/'+name+'$', '--filter', 'label=wizard.qa.manager-seal='+self.owner])
                for cid in found.splitlines():
                    if cid not in self.ids: self.record('container', cid, name)
            guardledger = self.shared/'guard-ledger.jsonl'
            guardrows = []
            if guardledger.exists():
                guardrows = list(map(json.loads, guardledger.read_text().splitlines()))
                for row in guardrows:
                    if row['operation'] == 'created':
                        target = self.ids if row['kind'] == 'container' else self.networks
                        if row['id'] not in target: self.record(row['kind'], row['id'], row.get('name'))
            for row in reconcile_creation_intents(rows+guardrows, self.owner):
                target = self.ids if row['kind'] == 'container' else self.networks if row['kind'] == 'network' else self.volumes
                if row['id'] not in target: self.record(row['kind'], row['id'], row['name'])
        except Exception: self.errors.append('ownership_reconciliation_failed')
        # Manager first, guard next; no actor can create while cleanup consumes
        # the append-only ledger. Never inspect/log/exec any unrelated ID.
        for cid in list(reversed(self.ids)):
            try: self.cmd('owned-container-remove', ['docker', 'rm', '-f', cid])
            except Exception:
                # A manager-removed historical ID is acceptable ONLY on exact
                # inspect returning missing. Other errors remain cleanup failures.
                r = subprocess.run(['docker', 'inspect', '--format', '{{.Id}}', cid], capture_output=True, timeout=10)
                if not recovery.exact_missing(r, 'container', cid): self.errors.append('container_remove_failed')
        for process, thread in self.processes:
            try: process.wait(timeout=15); thread.join(timeout=2); assert not thread.is_alive()
            except Exception: self.errors.append('attach_process_not_reaped')
        for kind, values in [('network', self.networks), ('volume', self.volumes)]:
            for ident in reversed(values):
                r = subprocess.run(['docker', kind, 'rm', ident], capture_output=True, timeout=15)
                # Removal stderr is not an absence proof. Read exact metadata.
                check = subprocess.run(['docker', kind, 'inspect', ident], capture_output=True, timeout=10)
                if not recovery.exact_missing(check, kind, ident): self.errors.append(kind+'_remove_failed')
        self.receipt['cleanup_errors'] = self.errors
        if hasattr(self, 'canary_holder'):
            os.close(self.canary_holder)
            self.receipt.setdefault('rabbit_limit_canary', {})['exclusive_holder_released'] = True
        self.receipt['reserve_after_cleanup_bytes'] = shutil.disk_usage('/srv/work').free
        if self.receipt['reserve_after_cleanup_bytes'] < capacity.RESERVE: self.errors.append('reserve_breached')
        self.receipt['phase_at_exit'] = self.receipt['phase']
        self.receipt['guard_ledger_sha256'] = sha(self.shared/'guard-ledger.jsonl') if (self.shared/'guard-ledger.jsonl').exists() else None
        if self.errors: self.receipt['first_installed_synthetic_path_pass'] = False
        (self.out/'receipt.json').write_text(json.dumps(self.receipt, indent=2)+'\n')


def launch_controller(root, args):
    """Host entry: one admitted controller cgroup, no second heavy submission."""
    uid, gid = os.getuid(), os.getgid()
    rows = capacity.inventory(Path('/srv/work'), time.monotonic()+12)
    own = sum(r['allocated_bytes'] for r in rows if r['path'] == str(root))
    decision = capacity.admission(sum(r['allocated_bytes'] for r in rows), shutil.disk_usage('/srv/work').free, already_allocated=own)
    assert decision['admitted'] and own <= 64*MIB
    assert sha('/usr/bin/docker') == DOCKER_SHA
    name = 'msi-'+args.source[:12]+('-producer' if args.producer_readiness_only else '')+'-controller'
    label = 'wizard.qa.manager-seal.controller='+args.source
    # Cached only. A missing image is a blocker, never an implicit acquisition.
    probe = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', BUILD], capture_output=True, timeout=10)
    assert probe.returncode == 0 and probe.stdout.decode().strip() == BUILD
    assert not (root/'controller-intent.json').exists()
    (root/'controller-intent.json').write_text(json.dumps({'name': name, 'label': label, 'source': args.source})+'\n')
    cid = None
    try:
        command = ['docker', 'create', '--pull=never', '--name', name, '--label', label,
                   '--user', f'{uid}:{gid}', '--group-add', str(Path('/var/run/docker.sock').stat().st_gid),
                   '--read-only', '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                   '--memory', str(GIB), '--memory-swap', str(GIB), '--pids-limit=256', '--cpus=2',
                   '--shm-size=67108864', '--log-driver=none', '--ulimit', 'core=0:0',
                   '--tmpfs', f'/tmp:size=16777216,uid={uid},gid={gid},mode=0700',
                   '--mount', 'type=bind,src=/srv/work,dst=/srv/work,readonly',
                   '--mount', f'type=bind,src={root},dst={root}',
                   '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
                   '--mount', 'type=bind,src=/usr/bin/docker,dst=/usr/bin/docker,readonly',
                   '--env', 'HOME=/tmp', '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'TMPDIR=/tmp',
                   '--env', 'DOCKER_CONFIG=/tmp/docker-config', '--workdir', str(root), '--entrypoint=python3', BUILD,
                   'scripts/manager_seal_installed_path.py', '--inside-controller', '--source', args.source,
                   '--manifest-sha256', args.manifest_sha256]
        if args.producer_readiness_only: command.append('--producer-readiness-only')
        if args.continue_saved: command.append('--continue-saved')
        result = subprocess.run(command, capture_output=True, timeout=30)
        assert result.returncode == 0
        cid = result.stdout.decode().strip(); assert len(cid) == 64
        (root/'controller-id.json').write_text(json.dumps({'id': cid, 'name': name})+'\n')
        process = subprocess.Popen(['docker', 'start', '--attach', cid], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # The controller emits only fixed receipt metadata. Bound outer output
        # separately from per-service sanitized logs.
        total = 0; deadline = time.monotonic()+2600
        import selectors
        selector = selectors.DefaultSelector(); selector.register(process.stdout, selectors.EVENT_READ)
        with (root/'controller-output.log').open('xb') as out:
            while selector.get_map():
                assert time.monotonic() < deadline
                for key, _ in selector.select(.5):
                    block = os.read(key.fd, 65536)
                    if not block: selector.unregister(key.fileobj); continue
                    total += len(block); assert total <= 16*MIB; out.write(block)
        code = process.wait(timeout=10)
        print(json.dumps({'controller_exit': code, 'receipt': str(root/'installed-results/receipt.json')}))
        return code
    finally:
        # Resolve an ambiguous CREATE once by the exact pre-recorded new name;
        # NEVER create/run a second time after timeout.
        if cid is None:
            r = subprocess.run(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'name=^/'+name+'$', '--filter', 'label='+label], capture_output=True, timeout=10)
            found = r.stdout.decode().splitlines(); assert len(found) <= 1
            cid = found[0] if found else None
        if cid:
            subprocess.run(['docker', 'rm', '-f', cid], capture_output=True, timeout=30)
            r = subprocess.run(['docker', 'inspect', '--format', '{{.Id}}', cid], capture_output=True, timeout=10)
            assert recovery.exact_missing(r, 'container', cid)
        emergency_cleanup(root, args.source)


def emergency_cleanup(root, source):
    """Independent parent-owned cleanup even if the controller was killed/OOM."""
    ledger = root/'installed-results/controller-ledger.jsonl'
    guardledger = root/'guard-shared/guard-ledger.jsonl'
    if not ledger.exists(): return
    owner = 'msi-'+source[:12]
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    ids = {r['id'] for r in rows if r['kind'] == 'container'}
    # Resolve ambiguous controller CREATEs before fencing every controller-owned
    # process. In particular, no manager/guard can append after the next read.
    for name in {r['name'] for r in rows if r['kind'] == 'intent'}:
        r = subprocess.run(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'name=^/'+name+'$',
                            '--filter', 'label=wizard.qa.manager-seal='+owner], capture_output=True, timeout=10)
        assert r.returncode == 0; ids.update(r.stdout.decode().splitlines())
    for ident in sorted(ids):
        subprocess.run(['docker', 'rm', '-f', ident], capture_output=True, timeout=30)
        check = subprocess.run(['docker', 'inspect', '--format', '{{.Id}}', ident], capture_output=True, timeout=10)
        assert recovery.exact_missing(check, 'container', ident), 'creator fence failed'
    networks = {r['id'] for r in rows if r['kind'] == 'network'}
    volumes = {r['id'] for r in rows if r['kind'] == 'volume'}
    guardrows = []
    if guardledger.exists():
        guardrows = list(map(json.loads, guardledger.read_text().splitlines()))
        for row in guardrows:
            if row['operation'] == 'created':
                (ids if row['kind'] == 'container' else networks).add(row['id'])
    reconciled = reconcile_creation_intents(rows+guardrows, owner)
    for row in reconciled:
        (ids if row['kind'] == 'container' else networks if row['kind'] == 'network' else volumes).add(row['id'])
    residual = []
    for kind, values in [('container', ids), ('network', networks), ('volume', volumes)]:
        for ident in sorted(values):
            subprocess.run(['docker', *(['rm', '-f'] if kind == 'container' else [kind, 'rm']), ident], capture_output=True, timeout=30)
            argv = ['docker', *(['inspect'] if kind == 'container' else [kind, 'inspect']), ident]
            r = subprocess.run(argv, capture_output=True, timeout=10)
            if not recovery.exact_missing(r, kind, ident): residual.append({'kind': kind, 'id': ident})
    record = {'parent_cleanup_residual': residual, 'free_bytes': shutil.disk_usage('/srv/work').free,
              'reserve_bytes': capacity.RESERVE, 'old_stages_deleted': False}
    (root/'parent-cleanup.json').write_text(json.dumps(record, indent=2)+'\n')
    assert not residual and record['free_bytes'] >= capacity.RESERVE


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True); p.add_argument('--manifest-sha256', required=True)
    p.add_argument('--inside-build', action='store_true'); p.add_argument('--inside-controller', action='store_true'); p.add_argument('--fixture-root'); p.add_argument('--owner')
    p.add_argument('--producer-readiness-only', action='store_true')
    p.add_argument('--continue-saved', action='store_true')
    args = p.parse_args()
    if args.inside_build: inside_build(args); return
    root = Path.cwd().resolve()
    assert root.parent == Path('/srv/work') and root.name == 'manager-seal-installed-'+args.source[:12]+('-producer' if args.producer_readiness_only else '')
    manifest(root, args.source, args.manifest_sha256)
    if not args.inside_controller:
        raise SystemExit(launch_controller(root, args))
    assert Path('/sys/fs/cgroup/memory.max').read_text().strip() == str(GIB)
    assert Path('/sys/fs/cgroup/memory.swap.max').read_text().strip() == '0'
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    rig = Rig(root, args)
    try:
        rig.run()
    except Exception as exc:
        rig.receipt['failure'] = {'type': type(exc).__name__, 'frames': [
            {'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
            for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).name == Path(__file__).name][-8:]}
        rig.receipt['first_installed_synthetic_path_pass'] = False
    finally:
        # State enums are captured before removal; never raw .Config/.Env/logs.
        rig.receipt['pre_cleanup_states'] = {}
        for cid in rig.ids:
            try: rig.receipt['pre_cleanup_states'][cid] = rig.state(cid)
            except Exception: rig.receipt['pre_cleanup_states'][cid] = {'unavailable': True}
        rig.cleanup()
    print(json.dumps({'receipt': str(rig.out/'receipt.json'), 'sha256': sha(rig.out/'receipt.json'),
                      'first_installed_synthetic_path_pass': rig.receipt['first_installed_synthetic_path_pass'], 'full_acceptance': False}))
    passed = rig.receipt.get('producer_readiness_pass') if args.producer_readiness_only else rig.receipt['first_installed_synthetic_path_pass']
    raise SystemExit(0 if passed and 'failure' not in rig.receipt and not rig.errors else 1)


if __name__ == '__main__': main()
