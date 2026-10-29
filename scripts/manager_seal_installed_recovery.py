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
CLI_ERL_ARGS = '+S 2:2 +SDcpu 1 +SDio 1 +A 4'
CLI_ENV = 'RABBITMQ_CTL_ERL_ARGS='+CLI_ERL_ARGS
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
