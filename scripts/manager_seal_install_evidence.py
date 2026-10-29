"""Bounded public-install evidence. No environment, credentials or package bodies.

The stdout transport is sanitized BEFORE export; raw logs stay only on tmpfs.
Every cap is an explicit failure, never a successful truncated diagnostic.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import time

MIB = 1024 ** 2
INSTALL_LOG_CAP = 32 * MIB
ARTIFACT_CAP = 40 * MIB
TOTAL_ARTIFACT_CAP = 64 * MIB
PREFIX = 'EARLY_EVIDENCE='
ANSI = re.compile(rb'\x1b\[[0-?]*[ -/]*[@-~]')
# Match header names, not filename substrings (e.g. cookie-parser is public).
HEADER = re.compile(rb'(?i)(\b(?:set-cookie|cookie|authorization|proxy-authorization|www-authenticate|proxy-authenticate|x-[a-z0-9-]*(?:token|key|auth)[a-z0-9-]*)\s*[:=])[^\r\n]*')
HTTP_HEADER = re.compile(rb'^(\s*(?:\[[^\]\r\n]+\]\s*)?(?:[<>|]\s*)?)([A-Za-z][A-Za-z0-9-]*):[^\r\n]*$')
ERROR = re.compile(rb'(?i)^(?:error\s*[:\[]|warn(?:ing)?\s*:|failed\s+(?:to\s+)?(?:install|link|spawn|open|copy|extract)|(?:E[A-Z0-9_]+|[A-Za-z]+Error):)|\b(?:ENOENT|EACCES|ENOSPC|EIO|EROFS|EMFILE|SIGXFSZ)\b')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(MIB), b''):
            h.update(block)
    return h.hexdigest()


def sanitize_line(line):
    line = ANSI.sub(b'', line)
    line = HEADER.sub(lambda m: m[1] + b' [REDACTED]', line)
    # Bun's verbose HTTP headers are prefixed with < or >, or indented.
    # Retain error/warning diagnostics; redact all other HTTP header values.
    match = HTTP_HEADER.match(line.rstrip(b'\r\n'))
    if match and (match[1] or match[2].lower() in (b'server', b'date', b'content-type', b'content-length', b'location', b'etag', b'cache-control', b'connection', b'vary')) and match[2].lower() not in (b'error', b'warn', b'warning'):
        return match[1] + match[2] + b': [REDACTED]\n'
    return line


def sanitize_log(raw, clean):
    assert raw.stat().st_size <= INSTALL_LOG_CAP + 65536, 'raw log byte cap'
    with raw.open('rb') as src, clean.open('wb') as dst:
        for line in src:
            dst.write(sanitize_line(line))
    assert clean.stat().st_size <= ARTIFACT_CAP, 'sanitized log byte cap'
    return {'raw_sha256': sha(raw), 'raw_bytes': raw.stat().st_size,
            'sanitized_sha256': sha(clean), 'sanitized_bytes': clean.stat().st_size,
            'redaction': 'ANSI removed; sensitive and verbose HTTP header values redacted'}


def error_index(clean):
    # This is an index, not the only retained error evidence. Full sanitized log
    # survives even for an unrecognized diagnostic format or an enormous line.
    rows = []
    with clean.open('rb') as f:
        for number, line in enumerate(f, 1):
            if ERROR.search(line):
                rows.append({'line': number, 'text': line.decode(errors='replace').rstrip()})
    return rows


def write_json(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    assert len(data) <= ARTIFACT_CAP, 'JSON evidence byte cap'
    path.write_bytes(data)


def emit_artifact(path):
    assert path.name in ('dependencies.sanitized.log', 'dependency-comparison.json', 'dependency-errors.json')
    assert path.stat().st_size <= ARTIFACT_CAP, 'export byte cap'
    metadata = {'name': path.name, 'bytes': path.stat().st_size, 'sha256': sha(path)}
    print(PREFIX + json.dumps({'begin': metadata}), flush=True)
    with path.open('rb') as f:
        for index, block in enumerate(iter(lambda: f.read(32768), b'')):
            print(PREFIX + json.dumps({'name': path.name, 'index': index,
                                       'data': base64.b64encode(block).decode('ascii')}), flush=True)
    print(PREFIX + json.dumps({'end': metadata}), flush=True)
    return metadata


def recover_artifacts(log, output):
    """Fail closed on omission, incomplete frames, drift, duplicate or cap hit."""
    expected = {'dependencies.sanitized.log', 'dependency-comparison.json', 'dependency-errors.json'}
    records = {}
    active = None
    total = 0
    with log.open('rb') as src:
        for line in src:
            if not line.startswith(PREFIX.encode()):
                continue
            assert len(line) <= 50000, 'evidence frame byte cap'
            frame = json.loads(line[len(PREFIX):])
            if 'begin' in frame:
                assert active is None
                metadata = frame['begin']
                name = metadata['name']
                assert name in expected and name not in records
                assert 0 <= metadata['bytes'] <= ARTIFACT_CAP
                active = (metadata, bytearray(), 0)
            elif 'end' in frame:
                assert active is not None
                metadata, data, _ = active
                assert metadata == frame['end'] and len(data) == metadata['bytes']
                assert hashlib.sha256(data).hexdigest() == metadata['sha256']
                target = output/metadata['name']
                with target.open('xb') as dst:
                    dst.write(data)
                records[metadata['name']] = metadata
                active = None
            else:
                assert active is not None
                metadata, data, index = active
                assert frame['name'] == metadata['name'] and frame['index'] == index
                block = base64.b64decode(frame['data'], validate=True)
                total += len(block)
                assert total <= TOTAL_ARTIFACT_CAP and len(data)+len(block) <= metadata['bytes']
                data.extend(block)
                active = (metadata, data, index+1)
    assert active is None, 'incomplete evidence transport'
    if records:
        assert set(records) == expected, 'missing dependency evidence'
    return records


def read_lock(path):
    # Bun text lock v1 is JSON with trailing commas. Protect JSON strings before
    # removing commas; a regex over unprotected strings corrupts package values.
    text = re.sub(r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])',
                  lambda m: m[0] if m[0].startswith('"') else '', path.read_text())
    obj = json.loads(text)
    assert obj['lockfileVersion'] == 1
    return obj


def lock_path(key):
    parts = key.split('/')
    names = []
    while parts:
        part = parts.pop(0)
        if part.startswith('@'):
            part += '/' + parts.pop(0)
        assert part not in ('', '.', '..') and '..' not in part.split('/')
        names.append(part)
    return Path('node_modules').joinpath(*('/node_modules/'.join(names).split('/')))


def platform_matches(value, actual):
    if not value:
        return True
    values = value if isinstance(value, list) else [value]
    return '!'+actual not in values and (all(v.startswith('!') for v in values) or actual in values)


def compare_install(checkout):
    lock = read_lock(checkout/'bun.lock')
    workspaces = []
    for rel, expected in lock['workspaces'].items():
        path = checkout/rel/'package.json'
        actual = json.loads(path.read_text()) if path.is_file() else {}
        differences = {field: {'lock': expected.get(field, {}), 'source': actual.get(field, {})}
                       for field in ('name', 'version', 'dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies')
                       if expected.get(field, {}) != actual.get(field, {})}
        # Root version is not serialized by Bun; don't call its absence a drift.
        if rel == '' and 'version' not in expected:
            differences.pop('version', None)
        workspaces.append({'path': rel, 'exists': path.is_file(), 'differences': differences})
    rows = []
    for key, entry in lock['packages'].items():
        target = checkout/lock_path(key)
        resolved = target.resolve()
        assert resolved.is_relative_to(checkout.resolve()), 'package path escapes checkout'
        manifest = target/'package.json'
        actual = {}
        if manifest.is_file():
            assert manifest.stat().st_size <= 2*MIB
            obj = json.loads(manifest.read_text())
            actual = {k: obj[k] for k in ('name', 'version', 'os', 'cpu', 'libc') if k in obj}
        meta = entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}
        applicable = all(platform_matches(meta.get(k), v) for k, v in (('os', 'linux'), ('cpu', 'x64'), ('libc', 'glibc')))
        spec = entry[0]
        expected_name, version = spec.rsplit('@', 1)
        workspace = version.startswith('workspace:')
        state = ('present' if actual else 'missing')
        if actual and not workspace and (actual.get('name') != expected_name or actual.get('version') != version):
            state = 'mismatch'
        bins = meta.get('bin', {})
        if isinstance(bins, str):
            bins = {expected_name.rsplit('/', 1)[-1]: bins}
        bin_status = []
        if actual:
            for name, rel in bins.items():
                binary = target/rel
                assert binary.resolve().is_relative_to(checkout.resolve()), 'bin escapes checkout'
                bin_status.append({'name': name, 'path': rel, 'exists': binary.is_file(),
                                   'executable': os.access(binary, os.X_OK)})
        rows.append({'lock_key': key, 'expected': spec, 'path': str(target.relative_to(checkout)),
                     'symlink': target.is_symlink(), 'actual': actual, 'platform_applicable': applicable,
                     'state': state, 'bins': bin_status})
    # Also enumerate actual installs, including package-local node_modules and
    # unexpected packages. Never follow arbitrary symlinks or read package bodies.
    installed = []
    for rel in ['', *[r for r in lock['workspaces'] if r]]:
        modules = checkout/rel/'node_modules'
        if not modules.is_dir():
            continue
        for parent, dirs, files in os.walk(modules, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != '.cache' and not d.startswith('.'))
            path = Path(parent)
            if 'package.json' not in files or path.parent.name not in ('node_modules',) and not path.parent.name.startswith('@'):
                # Traverse only node_modules/package[/node_modules], not sources.
                if path.name != 'node_modules' and not path.name.startswith('@'):
                    dirs[:] = [d for d in dirs if d == 'node_modules']
                continue
            manifest = path/'package.json'
            assert manifest.stat().st_size <= 2*MIB
            obj = json.loads(manifest.read_text())
            installed.append({'path': str(path.relative_to(checkout)), 'name': obj.get('name'), 'version': obj.get('version')})
            assert len(installed) <= 10000, 'installed inventory count cap'
            dirs[:] = [d for d in dirs if d == 'node_modules']
    mask = os.umask(0o077)
    os.umask(mask)
    return {'lock_sha256_after': sha(checkout/'bun.lock'), 'lock_package_count': len(rows),
            'bootstrap': {'umask': oct(mask), 'uid': os.getuid(), 'gid': os.getgid(),
                          'tools': {name: shutil.which(name) for name in ('node', 'bun', 'git', 'sh', 'bash', 'python3', 'make', 'g++', 'unzip')},
                          'source_modes': {rel: oct((checkout/rel).stat().st_mode & 0o777)
                                           for rel in ('package.json', 'bun.lock', 'bunfig.toml', 'packages/frontend/scripts/postinstall.sh')
                                           if (checkout/rel).is_file()}},
            'workspace_comparison': workspaces, 'packages': rows, 'installed': installed,
            'candidate_missing_or_mismatch': [r['lock_key'] for r in rows if r['platform_applicable'] and r['state'] != 'present'],
            'interpretation': 'Lock placement comparison, not proof every missing row is fatal; platform optional/hoisting may differ. Full sanitized error log is authoritative.'}


def capture_install(argv, cwd, log, timeout):
    """One invocation only; stop explicitly on timeout/cap, retain original exit."""
    deadline = time.monotonic()+timeout
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    assert proc.stdout is not None
    reason = None
    stop_deadline = None
    captured = 0
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    def terminate():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        with log.open('xb') as out:
            while sel.get_map():
                if time.monotonic() >= deadline and reason is None:
                    reason = 'timeout'
                    stop_deadline = time.monotonic()+5
                    terminate()
                for key, _ in sel.select(timeout=0.2):
                    block = os.read(key.fd, 65536)
                    if not block:
                        sel.unregister(key.fileobj)
                        continue
                    # One bounded overflow block is retained to expose cap hits.
                    if captured <= INSTALL_LOG_CAP:
                        out.write(block)
                        captured += len(block)
                    if captured > INSTALL_LOG_CAP and reason is None:
                        reason = 'log-byte-cap'
                        stop_deadline = time.monotonic()+5
                        terminate()
                if stop_deadline is not None and time.monotonic() > stop_deadline:
                    reason = str(reason)+'-pipe-open'
                    break
        code = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            terminate()
            proc.wait(timeout=10)
        sel.close()
        proc.stdout.close()
    return {'exit_code': code, 'termination_reason': reason, 'log_complete': reason is None,
            'log_sha256': sha(log), 'log_bytes': log.stat().st_size}


def dependency_step(checkout, logs, manifest_lock_sha, outcome):
    raw = logs/'dependencies.log'
    # Publish the real exit before any diagnostic processing can itself fail.
    outcome.update(capture_install(['bun', 'install', '--frozen-lockfile', '--verbose'], checkout, raw, 660))
    clean = logs/'dependencies.sanitized.log'
    outcome['log_evidence'] = sanitize_log(raw, clean)
    errors = logs/'dependency-errors.json'
    write_json(errors, {'matches': error_index(clean), 'full_log': clean.name})
    comparison = logs/'dependency-comparison.json'
    try:
        data = compare_install(checkout)
        data['lock_sha256_before'] = manifest_lock_sha
        data['frozen_lock_unchanged'] = data['lock_sha256_after'] == manifest_lock_sha
    except Exception as exc:
        # Preserve the original dependency exit even if inventory encounters a
        # malformed or partially installed manifest; diagnose that separately.
        data = {'comparison_error': type(exc).__name__ + ': ' + str(exc)}
    write_json(comparison, data)
    outcome['comparison_complete'] = 'comparison_error' not in data
    artifacts = [clean, comparison, errors]
    assert sum(p.stat().st_size for p in artifacts) <= TOTAL_ARTIFACT_CAP
    outcome['artifacts'] = {p.name: emit_artifact(p) for p in artifacts}
    return outcome
