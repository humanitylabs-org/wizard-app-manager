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
MAX_CSI = 32
# Only bounded presentation SGR/erase-line CSI, never OSC, cursor motion or C0.
CSI = re.compile(r'\x1b\[[0-9;]{0,32}[mK]')
SGR = r'(?:\x1b\[[0-9;]{0,32}m)*'
COMMANDS = ('runuser', 'bash', 'sh', 'docker', 'python3', 'mount', 'mkdir',
            'cp', 'mv', 'chmod', 'chown', 'mktemp', 'curl', 'sudo', 'git', 'cd')
SOURCE_PATHS = ('install.sh', 'scripts/setup-private-access.sh',
                'scripts/protected-agent-storage.py', 'wam/release_pins.py',
                'scripts/prepare-alongside.py', 'qa/privsep-storage/contract_a_guest.py')
CLASSES = ('PermissionError', 'FileNotFoundError', 'OSError', 'ValueError',
           'TypeError', 'KeyError', 'ModuleNotFoundError', 'ImportError',
           'CalledProcessError', 'Refusal', 'JSONDecodeError', 'AssertionError',
           'RuntimeError', 'TimeoutError')
# Complete static messages from install.sh <=122 and its private-access helper.
FIXED = {
    'Private access: none selected; leaving Tailscale unchanged.': ('marker', 'private-access-none'),
    'Private access: cloudflare selected; leaving Tailscale unchanged.': ('marker', 'private-access-cloudflare'),
    'Installing Tailscale (private access)': ('marker', 'private-access-install-start'),
    'Tailscale sign-in link:': ('marker', 'private-access-signin-prompt'),
    'Private access: HTTPS certificates are on; continuing.': ('marker', 'private-access-certificates-ready'),
    'Private access: Tailscale connected (existing settings preserved).': ('marker', 'private-access-connected'),
    'Private access: Tailscale preinstalled, not connected yet. Later: sudo tailscale up': ('marker', 'private-access-not-connected'),
    'Docker is unavailable to this account. Ask an administrator to run host preparation, then start a new login and retry.': ('failure', 'docker-unavailable-account'),
    'Wizard OS needs Python 3 with PyYAML (apt-get install -y python3-yaml), then run the command again.': ('failure', 'python-yaml-unavailable'),
    "note: approved app host access was not recorded; apps that need it stay off until 'wam host-approvals sync' succeeds": ('warning', 'host-approval-sync-unrecorded'),
}
# External-tool output is NOT a phase oracle. Only this vocabulary is emitted.
DOCKER_REASONS = {
    # More specific bind/mount errors precede generic errno phrases.
    'bind source path does not exist': 'docker-bind-source-missing',
    'invalid mount config for type': 'docker-mount-config-invalid',
    'invalid bind mount spec': 'docker-bind-spec-invalid',
    'invalid mount path': 'docker-mount-path-invalid',
    'invalid volume specification': 'docker-volume-spec-invalid',
    'is not a shared mount': 'docker-mount-not-shared',
    'not a shared or slave mount': 'docker-mount-not-shared-or-slave',
    'invalid propagation mode': 'docker-propagation-invalid',
    'error while creating mount source path': 'docker-mount-source-create-failed',
    'error mounting': 'docker-mount-failed',
    'mounts denied': 'docker-mount-denied',
    'mount denied': 'docker-mount-denied',
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
    'read-only file system': 'docker-filesystem-readonly',
    'permission denied': 'docker-permission-denied',
    'operation not permitted': 'docker-operation-not-permitted',
    'no such file or directory': 'docker-no-such-file',
    'not a directory': 'docker-not-directory',
    'device or resource busy': 'docker-resource-busy',
    'too many open files': 'docker-open-files-exhausted',
    'cannot allocate memory': 'docker-memory-unavailable',
    'out of memory': 'docker-memory-exhausted',
    'resource temporarily unavailable': 'docker-resource-unavailable',
    'connection refused': 'docker-connection-refused',
    'context deadline exceeded': 'docker-deadline-exceeded',
    'OCI runtime create failed': 'docker-oci-create-failed',
    'failed to create shim task': 'docker-shim-create-failed',
}
SHELL_REASONS = {
    'Permission denied': 'shell-permission-denied',
    'Operation not permitted': 'shell-operation-not-permitted',
    'Read-only file system': 'shell-filesystem-readonly',
    'No such file or directory': 'shell-no-such-file',
    'Not a directory': 'shell-not-directory',
    'Is a directory': 'shell-is-directory',
    'File exists': 'shell-file-exists',
    'No space left on device': 'shell-space-exhausted',
    'Too many open files': 'shell-open-files-exhausted',
    'Cannot allocate memory': 'shell-memory-unavailable',
    'Resource temporarily unavailable': 'shell-resource-unavailable',
    'command not found': 'shell-command-not-found',
    'not found': 'shell-not-found',
}
DOCKER_PREFIX = re.compile(
    r'^(?:docker: |)(?:Error response from daemon:|error during connect:|'
    r'Error(?::| )|ERROR(?::| )|error:|failed to |unable to |'
    r'dependency failed to start:|container |service |[ ]*Container )')
SHELL_PREFIX = re.compile(r'^(?:(?:/[^\s:]{1,256}/)?(?:' +
    '|'.join(COMMANDS) + r')|(?:/[^\s:]{1,256}/)?(?:install\.sh|setup-private-access\.sh)):' )


def normalized(line):
    """Presentation-only, bounded; reject all unsupported control sequences."""
    if line.count('\x1b') > MAX_CSI:
        return None
    line = CSI.sub('', line)
    if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in line):
        return None
    return line


def repertoire(sources):
    """Only pinned source bytes; caller must verify the complete exact key set."""
    if set(sources) != set(SOURCE_PATHS):
        raise ValueError('source-repertoire-membership')
    for text, (_, code) in FIXED.items():
        if code in ('private-access-none', 'private-access-cloudflare'):
            assert 'Private access: $provider selected; leaving Tailscale unchanged.' in sources['scripts/setup-private-access.sh']
        elif code.startswith('private-access-'):
            assert text in sources['scripts/setup-private-access.sh']
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
            'source_line_counts': {k: len(v.splitlines()) for k, v in sources.items()},
            'source_sha256': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in sources.items()}}


def classify(line, vocab):
    original = line
    line = normalized(line)
    if line is None:
        return None
    if line in FIXED:
        # Fixed phase/warning messages cannot be assembled by removing embedded
        # controls, erase-line or arbitrary text. Only outer SGR decoration.
        if not re.fullmatch(SGR + re.escape(line) + SGR, original):
            return None
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
            if (m[1] in ('/var/lib/wizard-os/wizard-app-manager/' + path, '/opt/storage-contract-a-source/' + path)
                    and m[3] in functions and 1 <= int(m[2]) <= vocab['source_line_counts'][path]):
                return {'kind': 'frame', 'file_id': SOURCE_PATHS.index(path), 'function_id': functions.index(m[3]), 'line': int(m[2])}
    if line == 'Traceback (most recent call last):':
        return {'kind': 'marker', 'code': 'python-traceback'}
    if line.startswith('invalid release pins: '):
        # Message body may contain arbitrary user paths/IDs; only fixed prefix.
        return {'kind': 'failure', 'code': 'release-pins-invalid'}
    if DOCKER_PREFIX.match(line):
        matches = list(dict.fromkeys(code for phrase, code in DOCKER_REASONS.items()
                                    if phrase.lower() in line.lower()))
        if matches:
            return {'kind': 'failure', 'code': matches[0], 'matched_phrase_ids': matches}
        m = re.fullmatch(r'(?:Error: |ERROR: |error: |)?service .{1,512} didn\'t complete successfully: exit ([0-9]{1,3})', line)
        if m and 1 <= int(m[1]) <= 255:
            return {'kind': 'failure', 'code': 'compose-service-exit', 'exit': int(m[1])}
    if SHELL_PREFIX.match(line):
        for phrase, code in SHELL_REASONS.items():
            if line.endswith(': ' + phrase):
                return {'kind': 'failure', 'code': code}
        m = re.fullmatch(r'.{1,512}: (?:exit status|exit code|exited with status) ([0-9]{1,3})', line)
        if m and 1 <= int(m[1]) <= 255:
            return {'kind': 'failure', 'code': 'shell-command-exit', 'exit': int(m[1])}
        m = re.fullmatch(r'.{1,512}: \[Errno ([0-9]{1,4})\](?: .*)?', line)
        if m and 1 <= int(m[1]) <= 4095:
            return {'kind': 'failure', 'code': 'shell-errno', 'errno': int(m[1])}
    return None


def project(raw, vocab, *, eof):
    assert type(raw) is bytes and len(raw) <= MAX_BYTES
    unknown = hashlib.sha256()
    events = []
    unknown_count = 0
    first = None
    # CR, CRLF and LF delimit records; NEVER overwrite/discard earlier progress
    # text to mimic a terminal. Line numbers/counts refer to these raw records.
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
        keys(row, ('line_number', 'kind'), ('code', 'reason_id', 'error_class', 'errno', 'file_id', 'function_id', 'line', 'command', 'exit', 'unknown_lines_before', 'matched_phrase_ids'))
        integer(row['line_number'], MAX_LINES)
        assert row['line_number'] > 0 and row['kind'] in ('marker', 'warning', 'failure', 'frame')
        if first:
            integer(row['unknown_lines_before'], MAX_LINES)
            assert row['kind'] == 'failure'
        else:
            assert 'unknown_lines_before' not in row
        fixed_kinds = {v[1]: v[0] for v in FIXED.values()}
        codes = set(fixed_kinds) | set(DOCKER_REASONS.values()) | set(SHELL_REASONS.values()) | {'storage-refusal', 'storage-exception', 'python-exception', 'python-traceback', 'release-pins-invalid', 'compose-service-exit', 'shell-command-exit', 'shell-errno'}
        if row['kind'] == 'frame':
            integer(row['file_id'], len(SOURCE_PATHS)-1)
            path = SOURCE_PATHS[row['file_id']]
            fs = vocab['frames'][path]
            integer(row['function_id'], len(fs)-1)
            integer(row['line'], vocab['source_line_counts'][path])
            assert row['line'] > 0
            assert set(row) == {'line_number', 'kind', 'file_id', 'function_id', 'line'}
        else:
            code = row['code']
            assert code in codes
            assert row['kind'] == fixed_kinds.get(code, 'marker' if code == 'python-traceback' else 'failure')
            required = {'line_number', 'kind', 'code'} | ({'unknown_lines_before'} if first else set())
            extra = {'storage-refusal': {'reason_id'}, 'storage-exception': {'error_class'},
                     'python-exception': {'error_class', 'errno', 'command', 'exit'},
                     'shell-errno': {'errno'}, 'shell-command-exit': {'exit'},
                     'compose-service-exit': {'exit'}}.get(code, set())
            if code in DOCKER_REASONS.values():
                extra = {'matched_phrase_ids'}
                ids = row['matched_phrase_ids']
                assert type(ids) is list and 1 <= len(ids) <= len(DOCKER_REASONS)
                assert all(type(x) is str and x in DOCKER_REASONS.values() for x in ids)
                assert len(set(ids)) == len(ids) and ids[0] == code
            assert required <= set(row) <= required | extra
            for field in extra - ({'errno', 'command', 'exit'} if code == 'python-exception' else set()):
                assert field in row
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
