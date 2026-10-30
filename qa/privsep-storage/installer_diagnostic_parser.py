#!/usr/bin/python3
"""QA-only finite projection. Input bytes NEVER leave the maintenance appliance.

Unknown text is counted/hashed, not evidence of success. No exception messages,
source lines, arbitrary paths, argv, URLs or environment are returned.
"""
import ast
import hashlib
import re

MAX_BYTES = 1048576
MAX_LINES = 8192
MAX_LINE = 8192
MAX_EVENTS = 96
SOURCE_PATHS = ('install.sh', 'scripts/setup-private-access.sh',
                'scripts/protected-agent-storage.py', 'wam/release_pins.py',
                'scripts/prepare-alongside.py', 'qa/privsep-storage/contract_a_guest.py')
CLASSES = ('PermissionError', 'FileNotFoundError', 'OSError', 'ValueError',
           'TypeError', 'KeyError', 'ModuleNotFoundError', 'ImportError',
           'CalledProcessError', 'Refusal', 'JSONDecodeError', 'AssertionError',
           'RuntimeError', 'TimeoutError')
# These are complete static messages from the admitted install.sh <= line 122.
FIXED = {
    'Private access: none selected; leaving Tailscale unchanged.': ('marker', 'private-access-none'),
    'Docker is unavailable to this account. Ask an administrator to run host preparation, then start a new login and retry.': ('failure', 'docker-unavailable-account'),
    'Wizard OS needs Python 3 with PyYAML (apt-get install -y python3-yaml), then run the command again.': ('failure', 'python-yaml-unavailable'),
    "note: approved app host access was not recorded; apps that need it stay off until 'wam host-approvals sync' succeeds": ('warning', 'host-approval-sync-unrecorded'),
}
# External-tool output is NOT a phase oracle. Only this vocabulary is emitted.
DOCKER_REASONS = {
    'permission denied while trying to connect to the Docker daemon socket': 'docker-socket-permission',
    'Cannot connect to the Docker daemon': 'docker-daemon-unreachable',
    'no configuration file provided': 'compose-config-missing',
    'failed to read dockerfile': 'dockerfile-unreadable',
    'dependency failed to start': 'compose-dependency-start-failed',
    'is unhealthy': 'compose-unhealthy',
    'port is already allocated': 'docker-port-allocated',
    'address already in use': 'docker-address-in-use',
    'pull access denied': 'docker-pull-denied',
    'manifest unknown': 'docker-manifest-unknown',
    'no space left on device': 'docker-space-exhausted',
}


def repertoire(sources):
    """Only pinned source bytes; caller must verify the complete exact key set."""
    if set(sources) != set(SOURCE_PATHS):
        raise ValueError('source-repertoire-membership')
    for text in FIXED:
        if text.startswith('Private access:'):
            assert 'Private access: $provider selected; leaving Tailscale unchanged.' in sources['scripts/setup-private-access.sh']
        else:
            assert text in sources['install.sh']
    assert 'if [ "${WIZARD_SEPARATED:-0}" = 1 ]; then exit 0; fi' in sources['install.sh']
    reasons = set()
    frames = {}
    for path in SOURCE_PATHS:
        if not path.endswith('.py'):
            continue
        tree = ast.parse(sources[path])
        frames[path] = {'<module>'} | {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if path == 'scripts/protected-agent-storage.py':
            # Literal Refusal messages only. No f-string/arbitrary exception text.
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'Refusal' and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                    reasons.add(n.args[0].value)
    return {'reasons': sorted(reasons), 'frames': {k: sorted(v) for k, v in frames.items()},
            'source_sha256': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in sources.items()}}


def classify(line, vocab):
    if line in FIXED:
        kind, code = FIXED[line]
        return {'kind': kind, 'code': code}
    if line.startswith('maintenance-needed: '):
        reason = line[len('maintenance-needed: '):]
        if reason in vocab['reasons']:
            return {'kind': 'failure', 'code': 'storage-refusal', 'reason_id': vocab['reasons'].index(reason)}
        if reason in CLASSES:
            return {'kind': 'failure', 'code': 'storage-exception', 'error_class': reason}
    # Do not export full Python messages, even when their class is known.
    qualified = {'subprocess.CalledProcessError': 'CalledProcessError', 'json.decoder.JSONDecodeError': 'JSONDecodeError', '__main__.Refusal': 'Refusal'}
    m = re.fullmatch(r'(' + '|'.join(re.escape(x) for x in (*CLASSES, *qualified)) + r'):.*', line)
    if m:
        row = {'kind': 'failure', 'code': 'python-exception', 'error_class': qualified.get(m[1], m[1])}
        command = re.fullmatch(r"(?:subprocess\.)?CalledProcessError: Command \['(runuser|bash|docker|python3|/usr/bin/python3|mount)'(?:, .*|)\] returned non-zero exit status ([0-9]{1,3})\.", line)
        if command and 1 <= int(command[2]) <= 255:
            row['command'] = command[1].rsplit('/', 1)[-1]
            row['exit'] = int(command[2])
        e = re.match(r'[^:]+: \[Errno ([0-9]{1,3})\](?: |$)', line)
        if e:
            row['errno'] = int(e[1])
        return row
    m = re.fullmatch(r'  File "([^"]{1,256})", line ([0-9]{1,6}), in ([A-Za-z_][A-Za-z_0-9]*|<module>)', line)
    if m:
        for path, functions in vocab['frames'].items():
            if m[1] in ('/var/lib/wizard-os/wizard-app-manager/' + path, '/opt/storage-contract-a-source/' + path) and m[3] in functions:
                return {'kind': 'frame', 'file_id': SOURCE_PATHS.index(path), 'function_id': functions.index(m[3]), 'line': int(m[2])}
    if line == 'Traceback (most recent call last):':
        return {'kind': 'marker', 'code': 'python-traceback'}
    if line.startswith('invalid release pins: '):
        # Message body may contain arbitrary user paths/IDs; only fixed prefix.
        return {'kind': 'failure', 'code': 'release-pins-invalid'}
    if line.startswith(('Error response from daemon:', 'error during connect:', 'Error ', ' Container ', 'failed to ', 'unable to ')):
        for phrase, code in DOCKER_REASONS.items():
            if phrase in line:
                return {'kind': 'failure', 'code': code}
    return None


def project(raw, vocab, *, eof):
    assert type(raw) is bytes and len(raw) <= MAX_BYTES
    unknown = hashlib.sha256()
    events = []
    unknown_count = 0
    first = None
    lines = raw.splitlines(keepends=True)
    partial = bool(raw and not raw.endswith(b'\n') and not eof)
    processed = 0
    recognized = 0
    for i, data in enumerate(lines[:MAX_LINES], 1):
        processed += 1
        complete = not (i == len(lines) and partial)
        line = data.rstrip(b'\r\n')
        row = None
        if complete and len(line) <= MAX_LINE:
            try:
                row = classify(line.decode('utf-8', 'strict'), vocab)
            except UnicodeError:
                pass
        if row is None:
            unknown_count += 1
            unknown.update(len(data).to_bytes(8, 'big') + data)
        else:
            recognized += 1
            row = {'line_number': i, **row}
            if row['kind'] == 'failure' and first is None:
                first = {**row, 'unknown_lines_before': unknown_count}
            if len(events) < MAX_EVENTS:
                events.append(row)
    bounded = not eof or len(lines) > MAX_LINES
    return {'schema': 'installer-private-projection-v1', 'full_acceptance': False,
            'review_required': True, 'bytes_read': len(raw), 'prefix_sha256': hashlib.sha256(raw).hexdigest(),
            'eof': eof, 'bounded_incomplete': bounded, 'processed_lines': processed,
            'unknown_count': unknown_count, 'unknown_sha256': unknown.hexdigest(),
            'unprocessed_line_count': max(0, len(lines) - MAX_LINES),
            'first_recognized_failure': first, 'events': events,
            'events_capped': recognized > MAX_EVENTS,
            'diagnosis': 'unknown' if first is None or bounded else 'recognized-failure-observed'}


def validate_export(value, vocab):
    """Independent host gate: no arbitrary strings or unexpected JSON members."""
    def keys(obj, required, optional=()):
        assert type(obj) is dict and set(required) <= set(obj) <= set(required) | set(optional)

    def integer(n, maximum=2**64):
        assert type(n) is int and 0 <= n <= maximum

    def boolean(n):
        assert type(n) is bool

    def event(row, first=False):
        keys(row, ('line_number', 'kind'), ('code', 'reason_id', 'error_class', 'errno', 'file_id', 'function_id', 'line', 'command', 'exit', 'unknown_lines_before'))
        integer(row['line_number'], MAX_LINES)
        assert row['line_number'] > 0 and row['kind'] in ('marker', 'warning', 'failure', 'frame')
        if first:
            integer(row['unknown_lines_before'], MAX_LINES)
            assert row['kind'] == 'failure'
        else:
            assert 'unknown_lines_before' not in row
        codes = {v[1] for v in FIXED.values()} | set(DOCKER_REASONS.values()) | {'storage-refusal', 'storage-exception', 'python-exception', 'python-traceback', 'release-pins-invalid', 'command-exit'}
        if row['kind'] == 'frame':
            integer(row['file_id'], len(SOURCE_PATHS)-1)
            fs = vocab['frames'][SOURCE_PATHS[row['file_id']]]
            integer(row['function_id'], len(fs)-1)
            integer(row['line'], 999999)
            assert set(row) == {'line_number', 'kind', 'file_id', 'function_id', 'line'}
        else:
            assert row['code'] in codes
            assert not (set(row) & {'file_id', 'function_id', 'line'})
        if 'reason_id' in row:
            integer(row['reason_id'], len(vocab['reasons'])-1)
        if 'error_class' in row:
            assert row['error_class'] in CLASSES
        if 'errno' in row:
            integer(row['errno'], 4095)
        if 'command' in row:
            assert row['command'] in ('runuser', 'bash', 'docker', 'python3', 'mount')
            integer(row['exit'], 255)
        if 'exit' in row:
            integer(row['exit'], 255)

    keys(value, ('schema', 'review_required', 'full_acceptance', 'diagnosis'),
         ('identity', 'projection', 'log_metadata', 'first_error', 'failed_mount_unmounted'))
    assert value['schema'] == 'installer-forensic-guest-v1'
    assert value['review_required'] is True and value['full_acceptance'] is False
    assert value['diagnosis'] in ('unknown', 'recognized-failure-observed')
    if 'identity' in value:
        ident = value['identity']
        keys(ident, ('pid1', 'no_os_daemons', 'no_nic', 'maintenance_root_ro_noload', 'both_devices_ro', 'mnt_namespace_inode', 'pid_namespace_inode'), ('failed_mount_ro_noload',))
        for k, v in ident.items():
            if k.endswith('_inode'):
                integer(v)
            else:
                assert v is True
    if 'first_error' in value:
        error = value['first_error']
        keys(error, ('operation', 'error_class'), ('errno',))
        assert error['operation'] in ('identity', 'mount-log-filesystem', 'read-allowlisted-log')
        assert error['error_class'] in (*CLASSES, 'UnknownError')
        if 'errno' in error:
            integer(error['errno'], 4095)
    if 'failed_mount_unmounted' in value:
        boolean(value['failed_mount_unmounted'])
    if 'log_metadata' in value:
        keys(value['log_metadata'], ('inode', 'size', 'mode', 'mtime_ns', 'ctime_ns'))
        for n in value['log_metadata'].values():
            integer(n)
    if 'projection' in value:
        assert value.get('identity', {}).get('failed_mount_ro_noload') is True
        p = value['projection']
        keys(p, ('schema', 'full_acceptance', 'review_required', 'bytes_read', 'prefix_sha256', 'eof', 'bounded_incomplete', 'processed_lines', 'unknown_count', 'unknown_sha256', 'unprocessed_line_count', 'first_recognized_failure', 'events', 'events_capped', 'diagnosis'))
        assert p['schema'] == 'installer-private-projection-v1'
        assert p['full_acceptance'] is False and p['review_required'] is True
        assert p['diagnosis'] in ('unknown', 'recognized-failure-observed')
        assert value['diagnosis'] == p['diagnosis']
        for k in ('prefix_sha256', 'unknown_sha256'):
            assert type(p[k]) is str and re.fullmatch('[0-9a-f]{64}', p[k])
        for k in ('eof', 'bounded_incomplete', 'events_capped'):
            boolean(p[k])
        for k in ('bytes_read', 'unprocessed_line_count'):
            integer(p[k], MAX_BYTES)
        for k in ('processed_lines', 'unknown_count'):
            integer(p[k], MAX_LINES)
        assert type(p['events']) is list and len(p['events']) <= MAX_EVENTS
        for r in p['events']:
            event(r)
        if p['first_recognized_failure'] is not None:
            event(p['first_recognized_failure'], True)
    return value
