"""Synthetic-only recovery helpers; no Docker/runtime operations at import."""
import hashlib
import json
from pathlib import Path
import re
import selectors
import subprocess
import tarfile
import time

PRIOR_SOURCE = 'c44b1ac6dab07b94a75074169ffcd4a35fd6b87a'
PRIOR_ROOT = Path('/srv/work/manager-seal-installed-'+PRIOR_SOURCE[:12])
PRIOR_MANIFEST_SHA = '0c6ad4931e7e7841262153898d8c38052947dc9829cfac61578a7e1aa09bc4a6'
PRIOR_ARCHIVE_SHA = 'e9d848ad144e5afda0f5b7c28095b08ca23fcf43bf630530f07dc7702d1cb53d'
PRIOR_RECEIPT_SHA = '202efaaadc2ca6cf5a791290ac3639daf66d9d725cb43d403904af8a3af58be9'
# Exact QA/doc-only exceptions. Everything else, including build inputs, must
# have identical Git-export path membership, content and modes before reuse.
QA_DELTA = frozenset({
    'scripts/manager_seal_installed_path.py',
    'scripts/manager_seal_installed_recovery.py',
    'scripts/tests/test_manager_seal_installed_path.py',
    'docs/manager-seal-installed-recovery.md',
})
MANAGER_WORKDIR = '/app'  # Dockerfile final runner, not the QA tooling image.
CLI_ERL_ARGS = '+S 2:2 +SDcpu 1 +SDio 1 +A 4'
CLI_ENV = 'RABBITMQ_CTL_ERL_ARGS='+CLI_ERL_ARGS
DEFAULT_FSIZE = 32*2**20
RABBIT_FSIZE = 64*2**20  # finite broker-role bound; all other roles remain 32MiB
RABBIT_ESCRIPT_SHA = '863b1228a2f437fe17cd2baa924a2b5bd94d943effe2c2c338e91e7e47233df0'
RABBIT_ESCRIPT_BYTES = 7414174


class FatalBrokerCLI(RuntimeError):
    """Deterministic local resource failure, never a readiness retry."""


def audit_packaged_startup(root, options, command, db_password):
    """Audit actual source + actual manager argv; no runtime or image claims.

    Extend the old inventory here, inside the existing four-file QA exception.
    In particular an empty synthetic frontend is NOT the packaged frontend.
    """
    from manager_seal_installed_source_audit import inventory
    root = Path(root)
    backend = root/'packages/backend/src'
    pins = {}
    def source(rel, tokens):
        p = root/rel
        assert p.is_file() and not p.is_symlink(), rel
        raw = p.read_bytes(); text = raw.decode()
        for token in tokens:
            assert token in text, 'startup source drift: '+rel+': '+token
        pins[rel] = digest(raw)
        return text
    dockerfile = source('Dockerfile', ['FROM runner_base AS runner'])
    runner = dockerfile.split('FROM runner_base AS runner', 1)[1]
    assert re.findall(r'^WORKDIR\s+(\S+)\s*$', runner, re.M) == [MANAGER_WORKDIR], 'runner WORKDIR drift'
    copies = {
        '/app/assets': '/app/packages/backend/assets ./assets',
        '/app/assets/migrations': '/app/packages/backend/src/core/database/drizzle ./assets/migrations',
        '/app/assets/translations': '/app/packages/backend/src/modules/i18n/translations ./assets/translations',
        '/app/assets/frontend': '/app/packages/frontend/dist/client ./assets/frontend',
    }
    for copy in copies.values():
        assert 'COPY --from=builder '+copy in runner, 'runner asset COPY drift'
    assert options.count('--workdir') == 1 and options[options.index('--workdir')+1] == MANAGER_WORKDIR, 'manager WORKDIR drift'
    mounts = [options[i+1] for i, token in enumerate(options) if token == '--mount']
    assert len([m for m in mounts if ',dst=/app,' in m and m.endswith(',readonly')]) == 1, 'manager app mount drift'
    env = [options[i+1] for i, token in enumerate(options) if token == '--env']
    assert 'NODE_ENV=production' in env and 'HOME=/tmp/home' in env
    assert 'PATH=/work/bin:/usr/local/bin:/usr/bin:/bin' in env
    assert len(command) == 2 and command[0] == '-c'
    for token in ('mkdir -p /tmp/home /data/state', '/data/state/settings.json',
                  'http://127.0.0.1:8080/store.git', 'python3 /app/store_http.py',
                  'exec /tooling/node --experimental-sqlite /app/main.mjs'):
        assert token in command[1], 'manager startup command drift'
    assert not re.search(r'\bcd\s|\bchdir\b', command[1]), 'shell overrides Docker working directory'
    checks = {
        'common/constants.ts': ["APP_DIR = '/app'", "DATA_DIR = '/data'", "APP_DATA_DIR = '/app-data'"],
        'modules/i18n/i18n.service.ts': ["path.join(process.cwd(), 'assets', 'translations')", 'fs.readdirSync(directory)', "loadPath: path.join(directory, '{{lng}}.json')"],
        'core/database/database.service.ts': ["path.join(appDir, 'assets', 'migrations')", 'migrationsFolder: this.getMigrationsPath()', 'const connectionString =', '${password}', 'connect_timeout=300'],
        'app.module.ts': ["NODE_ENV === 'production'", "rootPath: path.join(APP_DIR, 'assets', 'frontend')"],
        'app.service.ts': ["path.join(appDir, 'assets')", "path.join(assetsFolder, 'traefik', 'traefik.yml')", "path.join(assetsFolder, 'traefik', 'dynamic', 'dynamic.yml')", 'await this.databaseService.migrate()', 'await this.marketplaceService.initialize()', 'await this.copyAssets()', 'await this.generateTlsCertificates'],
        'modules/app-stores/app-store-files-manager.ts': ["path.join(appDir, 'assets', 'default-app-logo.jpg')"],
        'core/cache/cache.service.ts': ["new DatabaseSync('/cache/cache.sqlite')"],
        'common/helpers/env-helpers.ts': ["path.join(DATA_DIR, 'state')", "path.join(DATA_DIR, '.env')", "envMap.set('POSTGRES_HOST', 'runtipi-db')", "dotenv.config({ path: envFilePath, override: true"],
        'core/config/configuration.service.ts': ["path.join(DATA_DIR, '.env')", 'host: env.RABBITMQ_HOST', 'appDir: APP_DIR'],
        'modules/queue/queue.factory.ts': ['hostname: host', 'await this.waitForConnection()', 'confirm: true', 'autoDelete: false, durable: true'],
        'modules/queue/queue.module.ts': ["queueName: 'app-events-queue'", "queueName: 'repo-queue'", "queueName: 'system-events-queue'"],
        'modules/app-stores/repos.helpers.ts': ["path.join(dataDir, 'repos', id)", "execFileSync('git', ['config', '--global'"],
        'main.ts': ['await generateSystemEnvFile()', 'await appService.bootstrap()', 'await app.listen(3000)', "NODE_ENV === 'production'"],
    }
    for rel, tokens in checks.items(): source('packages/backend/src/'+rel, tokens)
    source('scripts/manager_seal_store_http.py', ["project_root = '/app'", "('127.0.0.1', 8080)"])
    # Exhaustive scan of the staged backend source, including any newly added file.
    # cwd has just the production/dev translations branches; no module-relative loader.
    cwd = []; asset_consumers = []
    for p in sorted(backend.rglob('*.ts')):
        if any(part in ('tests', '__tests__') for part in p.relative_to(backend).parts): continue
        for n, line in enumerate(p.read_text().splitlines(), 1):
            row = {'source': str(p.relative_to(backend)), 'line': n, 'text': line.strip()}
            if re.search(r'process\s*\.\s*(?:cwd|chdir)\s*\(|__dirname|import\s*\.\s*meta', line):
                cwd.append(row)
            if not line.lstrip().startswith('//') and re.search(r"['\"]assets['\"]|assetsFolder|migrationsFolder", line):
                asset_consumers.append(row)
    assert {r['source'] for r in asset_consumers} == {
        'app.module.ts', 'app.service.ts', 'core/database/database.service.ts',
        'modules/app-stores/app-store-files-manager.ts', 'modules/i18n/i18n.service.ts'}, 'new packaged asset consumer'
    assert [r['text'] for r in cwd] == [
        "let directory = path.join(process.cwd(), 'assets', 'translations');",
        "directory = path.join(process.cwd(), 'src', 'modules', 'i18n', 'translations');"], 'new cwd/module-relative startup consumer'
    assert db_password and 'POSTGRES_PASSWORD='+db_password in env, 'configured synthetic database credential mismatch'
    inv = inventory(root)
    groups = inv['asset_groups']
    assert all(g['exists'] and g['file_count'] for d, g in groups.items() if d != '/app/assets/frontend')
    base = root/'packages/backend/assets'
    for rel in ('traefik/traefik.yml', 'traefik/dynamic/dynamic.yml', 'default-app-logo.jpg'):
        assert (base/rel).is_file() and (base/rel).stat().st_size, rel
    translations = root/groups['/app/assets/translations']['source']
    for p in translations.glob('*.json'): assert isinstance(json.loads(p.read_bytes()), dict)
    assert (translations/'en.json').is_file()
    migrations = root/groups['/app/assets/migrations']['source']
    journal = json.loads((migrations/'meta/_journal.json').read_bytes())
    assert journal['entries']
    for entry in journal['entries']: assert (migrations/(entry['tag']+'.sql')).is_file(), entry['tag']
    for d in ('/data', '/app-data', '/cache', '/tmp'):
        assert any(options[i+1].startswith(d+':size=') for i, token in enumerate(options) if token == '--tmpfs'), 'missing writable root '+d
    corrected = {d: {'source': g['source'], 'exists_in_source': g['exists'], 'source_files': g['file_count'],
                     'runtime_origin': 'empty synthetic directory; frontend NOT built/copied; UI NOT proven' if d.endswith('/frontend') else 'unchanged actual source files'}
                 for d, g in groups.items()}
    return {'static_audit_pass': True, 'canonical_runner_workdir': MANAGER_WORKDIR,
            'actual_manager_argv_workdir': options[options.index('--workdir')+1],
            'source_pins': pins, 'all_cwd_or_module_relative_consumers': cwd,
            'all_packaged_asset_consumer_lines': asset_consumers,
            'asset_groups': corrected, 'migration_journal_entries': len(journal['entries']),
            'database_uses_configured_password': True,
            'queue_prerequisites': ['internal runtipi-rabbitmq alias and synthetic credentials', 'durable app-events-queue/repo-queue/system-events-queue and real consumers required'],
            'writable_roots': ['/data', '/app-data', '/cache', '/tmp'],
            'bootstrap_prerequisites': ['data env/state/seed/logs', 'migrations and PostgreSQL authentication', 'loopback Git server and git safe.directory HOME', 'traefik template copy and openssl TLS', 'SQLite cache and real queue consumers'],
            'frontend_proven': False, 'runtime_executed': False, 'full_acceptance': False, 'RUNTIPI_ADMITTED': False}


def cli_classification(outcome):
    if (outcome['termination_reason'] is None and outcome['exit_code'] == 153
            and outcome['stdout'] == b'' and outcome['stderr'].strip() == b'File size limit exceeded'):
        return 'fatal_file_size_limit'
    if outcome['exit_code'] == 0 and outcome['termination_reason'] is None:
        return 'success'
    return 'transient_or_unclassified'


def role_fsize(role):
    assert role in ('default', 'rabbit')
    return RABBIT_FSIZE if role == 'rabbit' else DEFAULT_FSIZE


def rabbit_options(net, user, password):
    """One shared exact option producer: canary and installed broker."""
    uid, gid = map(int, user.split(':'))
    options = ['--user', user, '--network', net, '--network-alias', 'runtipi-rabbitmq',
               '--memory', str(2**30), '--memory-swap', str(2**30)]
    for env in ('RABBITMQ_DEFAULT_USER=synthetic', 'RABBITMQ_DEFAULT_PASS='+password,
                'RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS=+S 2:2', 'RABBITMQ_LOGS=-'):
        options += ['--env', env]
    for path, size in (('/var/lib/rabbitmq', 512*2**20), ('/var/log/rabbitmq', 16*2**20), ('/tmp', 16*2**20)):
        options += ['--tmpfs', f'{path}:size={size},uid={uid},gid={gid},mode=0700']
    return options


CLI_OUTPUT_CAP = 8192  # each stream, not an unbounded communicate() buffer
RABBIT_ENV_SHA = '4593a53a99e9115feaeddb272e206b1d39c22fb9532f9c143fc682e78c9d62af'
# Exact function body from the retained pinned-image public source, NOT a
# whitespace-tolerant shell guess. Full-file identity is checked independently.
RABBIT_ESCRIPT_BODY = b'''    escript="${1:?escript must be defined}"
    shift

    maybe_noinput="${1:?maybe_noinput must be defined}"
    shift

    _rmq_env_set_erl_libs

    tmp_erl_flags="-boot $CLEAN_BOOT_FILE $RABBITMQ_CTL_ERL_ARGS -kernel inet_dist_listen_min $RABBITMQ_CTL_DIST_PORT_MIN -kernel inet_dist_listen_max $RABBITMQ_CTL_DIST_PORT_MAX"

    if [ "$maybe_noinput" = 'noinput' ]
    then
        tmp_erl_flags="-noinput $tmp_erl_flags"
    fi

    ERL_FLAGS="$tmp_erl_flags" "$escript" "$@"

    unset tmp_erl_flags'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_rabbit_cli_source(raw):
    """Pinned public bytes and the exact CTL -> escript callback, offline only."""
    assert isinstance(raw, bytes) and digest(raw) == RABBIT_ENV_SHA, 'Rabbit public source hash drift'
    marker = b'\nrun_escript()\n{\n'
    assert raw.count(marker) == 1, 'Rabbit run_escript definition drift'
    body, close, _ = raw.split(marker, 1)[1].partition(b'\n}\n')
    assert close and body == RABBIT_ESCRIPT_BODY, 'Rabbit CTL/escript route drift'
    return {'sha256': digest(raw), 'release_source_sha256': RABBIT_ENV_SHA,
            'release': 'v4.4.0', 'ctl_args_route_verified': True,
            'full_source_pin_verified': True, 'run_escript_body_sha256': digest(body)}


def exact_missing(result, kind, ident):
    """Full single-target Docker diagnostic + exit, not substring stderr."""
    assert kind in ('container', 'network', 'volume')
    assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', ident)
    if result.returncode != 1 or result.stdout.strip() not in (b'', b'[]'):
        return False
    target = re.escape(ident)
    alternatives = {
        'container': [rf'Error: No such object: {target}', rf'Error response from daemon: No such container: {target}'],
        'network': [rf'Error response from daemon: network {target} not found', rf'Error: No such network: {target}'],
        'volume': [rf'Error response from daemon: get {target}: no such volume', rf'Error: No such volume: {target}'],
    }
    text = result.stderr.decode('utf-8', errors='replace').strip()
    return any(re.fullmatch(pattern, text, re.IGNORECASE) for pattern in alternatives[kind])


def broker_argv(cid, user, action):
    """Every Erlang CLI gets the same bounded scheduler pool and RPC timeout."""
    assert re.fullmatch(r'[a-f0-9]{64}', cid)
    assert re.fullmatch(r'[0-9]+:[0-9]+', user)
    commands = {
        'cookie_metadata': ['sh', '-c', 'test -s /var/lib/rabbitmq/.erlang.cookie && test "$(stat -c %u /var/lib/rabbitmq/.erlang.cookie)" = "$(id -u)" && stat -c "%u %g %a %s" /var/lib/rabbitmq/.erlang.cookie'],
        'limit_metadata': ['sh', '-c', 'id -u; id -g; for p in /proc/self/limits /proc/1/limits; do while IFS= read -r line; do case "$line" in "Max file size"*) printf "%s\\n" "$line";; esac; done < "$p"; done'],
        'public_files': ['sh', '-c', 'stat -c %s /opt/rabbitmq/escript/rabbitmqctl /opt/rabbitmq/escript/rabbitmq-diagnostics; sha256sum /opt/rabbitmq/escript/rabbitmqctl /opt/rabbitmq/escript/rabbitmq-diagnostics'],
        'ping': ['rabbitmq-diagnostics', '--timeout', '10', '-q', 'ping'],
        'check_running': ['rabbitmq-diagnostics', '--timeout', '10', '-q', 'check_running'],
        'list_queues': ['rabbitmqctl', '--timeout', '10', '-q', 'list_queues', 'name', 'consumers'],
    }
    return ['docker', 'exec', '--user', user, '--env', CLI_ENV, cid, *commands[action]]


def sanitized_cli(data, secrets=()):
    """Only synthetic broker commands; never retain cookie value OR hash."""
    for secret in secrets:
        data = data.replace(secret.encode(), b'[SYNTHETIC-SECRET-REDACTED]')
    data = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', data)
    rows = []
    sensitive_context = False
    for line in data.splitlines():
        if any(word in line.lower() for word in (b'cookie', b'authorization', b'password', b'secret', b'token')):
            sensitive_context = True
            rows.append(b'[credential-bearing diagnostic withheld]')
        elif sensitive_context and (not line.strip() or line[:1].isspace()):
            rows.append(b'[credential diagnostic continuation withheld]')
        else:
            sensitive_context = False
            rows.append(line)
    return b'\n'.join(rows).decode('utf-8', errors='replace')


def bounded_cli(argv, timeout=15.0):
    """Bound pipe reads while running; cap/timeout never becomes readiness."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    chunks = {'stdout': bytearray(), 'stderr': bytearray()}
    reason = None
    end = time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                remaining = end-time.monotonic()
                if remaining <= 0:
                    reason = 'timeout'; break
                for key, _ in selector.select(min(.25, remaining)):
                    import os
                    data = os.read(key.fd, 4096)
                    if not data:
                        selector.unregister(key.fileobj); continue
                    dest = chunks[key.data]
                    room = CLI_OUTPUT_CAP-len(dest)
                    dest.extend(data[:room])
                    if len(data) > room:
                        reason = 'output_cap'; break
                if reason: break
        if reason: process.kill()
        code = process.wait(timeout=3)
        return {'exit_code': code, 'termination_reason': reason,
                **{key: bytes(value) for key, value in chunks.items()}}
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=3)
        process.stdout.close(); process.stderr.close()


def verify_reuse(prior_raw, current):
    assert digest(prior_raw) == PRIOR_MANIFEST_SHA
    prior = json.loads(prior_raw)
    assert prior['source'] == PRIOR_SOURCE
    old = set(prior['files'])-QA_DELTA; new = set(current['files'])-QA_DELTA
    assert old == new, 'backend reuse path membership drift'
    for rel in old:
        assert prior['files'][rel] == current['files'][rel], 'backend reuse content drift'
        assert prior['modes'][rel] == current['modes'][rel], 'backend reuse mode drift'
    return {'production_source': PRIOR_SOURCE, 'current_qa_source': current['source'],
            'unchanged_export_files': len(old), 'qa_exceptions': sorted(QA_DELTA),
            'backend_rebuilt': False, 'full_non_qa_export_equal': True}


def restore_bundle(archive, dist):
    """Read only four exact retained regular files; never general tar extract."""
    assert archive.is_file() and not archive.is_symlink() and archive.stat().st_size <= 128*2**20
    assert digest(archive.read_bytes()) == PRIOR_ARCHIVE_SHA
    prefix = 'source/packages/backend/dist/'
    names = {'main.js', 'main.js.map', 'cpufeatures-e8x7s2f1.node', 'sshcrypto-a3khcfhj.node'}
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
        assert len(members) <= 100 and sum(m.size for m in members) <= 128*2**20
        seen = set()
        for member in members:
            assert member.name not in seen; seen.add(member.name)
            assert not member.name.startswith('/') and '..' not in Path(member.name).parts
            assert member.isdir() or member.isreg()
            assert not member.linkname and member.sparse is None
        stream = tf.extractfile('build-result.json'); assert stream is not None
        assert tf.getmember('build-result.json').size <= 1024*1024
        metadata = json.loads(stream.read())
        assert metadata['built'] and metadata['source'] == PRIOR_SOURCE
        assert {m.name for m in members if m.name.startswith(prefix)} == {prefix+n for n in names}
        assert set(metadata['bundle']['files']) == names
        blobs = {}
        for name in names:
            member = tf.getmember(prefix+name)
            pin = metadata['bundle']['files'][name]
            assert member.isreg() and member.size == pin['bytes'] <= 64*2**20
            stream = tf.extractfile(member); assert stream is not None
            blob = stream.read()
            assert len(blob) == pin['bytes'] and digest(blob) == pin['sha256']
            blobs[name] = blob
    dist.mkdir(parents=True, exist_ok=False)
    for name, blob in blobs.items():
        with (dist/name).open('xb') as out: out.write(blob)
    return metadata


def validate_limit_metadata(text, user, bound):
    rows = text.splitlines()
    assert len(rows) == 4 and rows[:2] == user.split(':')
    for row in rows[2:]:
        assert row.split() == ['Max', 'file', 'size', str(bound), str(bound), 'bytes']
    return {'uid': int(rows[0]), 'gid': int(rows[1]), 'exec_soft_bytes': bound,
            'exec_hard_bytes': bound, 'pid1_soft_bytes': bound, 'pid1_hard_bytes': bound}


def validate_public_files(text):
    rows = text.splitlines()
    assert rows[:2] == [str(RABBIT_ESCRIPT_BYTES)]*2 and len(rows) == 4
    paths = ['/opt/rabbitmq/escript/rabbitmqctl', '/opt/rabbitmq/escript/rabbitmq-diagnostics']
    for row, path in zip(rows[2:], paths):
        assert row.split() == [RABBIT_ESCRIPT_SHA, path]
    return {path: {'bytes': RABBIT_ESCRIPT_BYTES, 'sha256': RABBIT_ESCRIPT_SHA} for path in paths}
