"""Test-only metadata diagnostics. Never serialize exception messages or child output."""
import ast
import builtins
import json
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ('qa/privsep-security/rotation_diagnostics.py',
           'qa/privsep-security/credential-rotation-disposable.py',
           'wam/credential_rotation.py', 'wam/runtipi.py')
OPERATIONS = ('compose-up', 'compose-down', 'compose-config', 'compose-ps', 'compose-stop',
              'compose-logs', 'container-inspect', 'container-exec', 'docker-ps', 'image-inspect',
              'db-password', 'db-auth', 'queue-password', 'queue-revoke', 'queue-auth',
              'http-request', 'container-create', 'container-remove', 'public-image-file-copy', 'other-service-command')
STATUSES = ('created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead')
HEALTH = ('starting', 'healthy', 'unhealthy', 'none')


def exception_metadata(error):
    # Frame metadata must originate in the fixed source closure, not attacker-
    # chosen exception attributes, fake filenames, notes or code object names.
    trusted = {}
    for relative in SOURCES:
        path = ROOT / relative
        tree = ast.parse(path.read_bytes(), filename=relative)
        trusted[str(path)] = (relative, {'<module>', '<lambda>', '<genexpr>', '<listcomp>',
                                       '<dictcomp>', '<setcomp>'} |
                             {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))})
    classes = {v for v in vars(builtins).values() if isinstance(v, type) and issubclass(v, BaseException)}
    from wam.credential_rotation import Refusal
    classes |= {Refusal, subprocess.TimeoutExpired, subprocess.CalledProcessError}
    seen = set()

    def describe(exc, depth=0):
        if id(exc) in seen or depth >= 8:
            return {'truncated': True}
        seen.add(id(exc))
        frames = []
        tb = exc.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            relative, names = trusted.get(code.co_filename, ('<external>', set()))
            frames.append({'filename': relative, 'function': code.co_name if code.co_name in names else '<unknown>',
                           'line': tb.tb_lineno})
            tb = tb.tb_next
        item = {'class': type(exc).__name__ if type(exc) in classes else 'OtherException', 'frames': frames[-24:]}
        for name, nested in (('cause', exc.__cause__), ('context', exc.__context__)):
            if nested is not None:
                item[name] = describe(nested, depth + 1)
        # Built-in ExceptionGroup only: arbitrary properties must not be evaluated.
        if sys.version_info >= (3, 11) and isinstance(exc, BaseExceptionGroup):
            item['exceptions'] = [describe(e, depth + 1) for e in exc.exceptions[:8]]
        return item
    return describe(error)


def operation(args, compose=()):
    if compose and list(args[:len(compose)]) == list(compose):
        action = args[len(compose)] if len(args) > len(compose) else ''
        return 'compose-' + action if 'compose-' + action in OPERATIONS else 'other-service-command'
    if args[:2] == ['docker', 'inspect']: return 'container-inspect'
    if args[:2] == ['docker', 'create']: return 'container-create'
    if args[:2] == ['docker', 'rm']: return 'container-remove'
    if args[:2] == ['docker', 'cp']: return 'public-image-file-copy'
    if args[:2] == ['docker', 'ps']: return 'docker-ps'
    if args[:3] == ['docker', 'image', 'inspect']: return 'image-inspect'
    if args[:2] == ['docker', 'exec']:
        for token, label in (('change_password', 'queue-password'), ('authenticate_user', 'queue-auth'),
                             ('close_all_user_connections', 'queue-revoke'), ('psql', 'db-password'),
                             ('node', 'http-request')):
            if token in args: return label
        return 'container-exec'
    return 'other-service-command'


class Diagnostics:
    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.case = 'setup'
        self.phase = 'authorization'
        self.last_command = None
        self.first_failure = None

    def emit(self, event, **fields):
        print(json.dumps({'event': event, 'case': self.case, 'phase': self.phase, **fields}, sort_keys=True),
              file=self.stream, flush=True)

    def checkpoint(self, phase):
        self.phase = phase
        self.emit('progress')

    def run(self, args, *, data=None, env=None, cwd=None, timeout=240, ok=(0,), compose=()):
        label = operation(args, compose)
        self.last_command = {'operation': label, 'status': 'started'}
        self.emit('command', **self.last_command)
        try:
            result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, cwd=cwd, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            self.last_command = {'operation': label, 'status': 'unavailable-or-timeout'}
            self.emit('command', **self.last_command)
            raise
        self.last_command = {'operation': label, 'status': 'completed', 'returncode': result.returncode}
        self.emit('command', **self.last_command)
        if result.returncode not in ok:
            raise RuntimeError('Disposable service operation failed')
        return result

    def failure(self, error, *, cleanup=False):
        if not cleanup:
            if self.first_failure is not None: return
            self.first_failure = {'case': self.case, 'phase': self.phase,
                                  'error': exception_metadata(error), 'last_command': self.last_command}
        self.emit('cleanup-error' if cleanup else 'failure', error=exception_metadata(error),
                  last_command=self.last_command)


def container_states(io, diag):
    # Query only State (never Config.Env), then project enums/bools/integers.
    for service in ('runtipi', 'runtipi-db', 'runtipi-queue'):
        try:
            ids = io.command([*io.compose, 'ps', '-aq', service]).stdout.decode().split()
            if len(ids) != 1:
                diag.emit('container-state', service=service, present=False)
                continue
            raw = io.command(['docker', 'inspect', '--format', '{{json .State}}', ids[0]]).stdout
            state = json.loads(raw)
            safe = {k: state[k] for k in ('Running', 'Paused', 'Restarting', 'OOMKilled', 'Dead') if type(state.get(k)) is bool}
            if type(state.get('ExitCode')) is int: safe['ExitCode'] = state['ExitCode']
            safe['Status'] = state.get('Status') if state.get('Status') in STATUSES else 'unknown'
            status = state.get('Health', {}).get('Status', 'none')
            safe['Health'] = status if status in HEALTH else 'unknown'
            diag.emit('container-state', service=service, present=True, state=safe)
            logs = io.command([*io.compose, 'logs', '--no-color', '--tail', '80', service]).stdout.lower()
            # Fixed literal predicates only, no matching text or raw log fragments.
            signatures = {'db-auth-rejected': b'password authentication failed',
                          'sqlite-open-failed': b'unable to open database',
                          'invalid-bootstrap-env': b'invalid environment variables',
                          'cookie-file-mentioned': b'cookie file',
                          'schema-version-mentioned': b'schemaversion'}
            diag.emit('container-log-signatures', service=service,
                      signatures=[name for name, marker in signatures.items() if marker in logs])
            if service == 'runtipi-queue' and state.get('Running') is True:
                # Only public numeric UID/GID and cookie stat; NEVER cookie bytes.
                raw = io.command(['docker', 'exec', '--user', 'rabbitmq', ids[0], 'sh', '-c',
                                  'id -u; id -g; stat -c "%u %g %a %s" /var/lib/rabbitmq/.erlang.cookie']).stdout
                numbers = raw.decode().split()
                if len(numbers) == 6 and all(v.isdigit() and len(v) <= 12 for v in numbers):
                    uid, gid, owner, group, mode, size = (int(v) for v in numbers)
                    diag.emit('queue-cookie-metadata', service_uid=uid, service_gid=gid,
                              owner_uid=owner, owner_gid=group, mode=mode, nonempty=size > 0,
                              service_owned=uid == owner and gid == group,
                              private_mode=mode in (400, 600))
                else:
                    diag.emit('queue-cookie-metadata', available=False)
        except Exception as error:
            diag.failure(error, cleanup=True)


def teardown(io, diag, primary=None):
    # Save health before teardown. Cleanup cannot replace the earliest failure.
    container_states(io, diag)
    try:
        io.command([*io.compose, 'down', '--volumes', '--remove-orphans', '--timeout', '30'])
    except Exception as error:
        diag.failure(error, cleanup=True)
        if primary is None: raise


def assert_no_credentials(logs, old, new):
    from wam.credential_rotation import KEYS
    # parse_env already returns ONLY KEYS. Keep all three old AND new secrets.
    assert set(old) == set(new) == set(KEYS)
    assert all(values[key].encode() not in logs for values in (old, new) for key in KEYS)


def queue_health_contract(user, entrypoints, passwd, entrypoint):
    """Public exact-image metadata only; refuse unknown privilege-drop contracts."""
    assert entrypoints in (['docker-entrypoint.sh'], ['/usr/local/bin/docker-entrypoint.sh'])
    matches = [line.split(':') for line in passwd.splitlines() if line.startswith('rabbitmq:')]
    assert len(matches) == 1 and len(matches[0]) == 7
    uid, gid = int(matches[0][2]), int(matches[0][3])
    assert uid > 0 and gid > 0
    launcher = ''
    if user in ('', '0', 'root', '0:0', 'root:root'):
        drops = re.findall(r'\bexec (gosu|su-exec) rabbitmq\b', entrypoint)
        assert len(set(drops)) == 1
        launcher = drops[0] + ' rabbitmq '
    else:
        assert user in ('rabbitmq', 'rabbitmq:rabbitmq', str(uid), f'{uid}:{gid}')
    script = ('set -eu; cookie=/var/lib/rabbitmq/.erlang.cookie; test -s "$cookie"; '
              'meta="$(stat -c %u:%g:%a "$cookie")"; '
              f'case "$meta" in {uid}:{gid}:400|{uid}:{gid}:600) ;; *) exit 1 ;; esac; '
              'exec ' + launcher + 'rabbitmq-diagnostics -q ping')
    return {'uid': uid, 'gid': gid, 'default_root': bool(launcher),
            'health_test': ['CMD-SHELL', script.replace('$', '$$')]}
