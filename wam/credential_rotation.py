"""Owner-only, forward-recoverable invalidation of the three leaked manager secrets.

No app files are opened. SQL/RabbitMQ mutations are real, not env-only resets.
The caller must first revoke legacy Docker membership AND live process groups.
Never restore the migration snapshot after the first password-change intent.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import subprocess
import tempfile
import time


from .runtipi import cli_token

ROOT = Path('/var/lib/wizard-os/runtipi')
CONTROL = Path('/var/lib/wizard-os-migration/credentials')
KEYS = ('JWT_SECRET', 'POSTGRES_PASSWORD', 'RABBITMQ_PASSWORD')
PHASES = ('prepared', 'quiesced', 'db_intent', 'db_rotated', 'queue_intent',
          'queue_rotated', 'file_committed', 'restarted', 'verified')
SAFE_ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'HOME': '/root', 'LANG': 'C.UTF-8'}


class Refusal(RuntimeError):
    """Only fixed, credential-free messages may escape this module."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def protected(path, owners, *, private=False, directory=False):
    """Check each actual ancestor, not only a private leaf under a writable parent."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Refusal('Absolute canonical protected paths required')
    for p in reversed((path, *path.parents)):
        s = p.lstat()
        allowed = owners if p == path else {0, *owners}
        if stat.S_ISLNK(s.st_mode) or s.st_uid not in allowed or s.st_mode & 0o022:
            raise Refusal('Unsafe credential ownership, symlink or writable ancestor')
        if p != path and not stat.S_ISDIR(s.st_mode):
            raise Refusal('Credential ancestor is not a directory')
    s = path.lstat()
    if directory != stat.S_ISDIR(s.st_mode) or (not directory and not stat.S_ISREG(s.st_mode)):
        raise Refusal('Unexpected credential path type')
    if not directory and s.st_nlink != 1:
        raise Refusal('Hard-linked credential file refused')
    if private and s.st_mode & 0o077:
        raise Refusal('Credential state must be private')


def atomic(path, data, uid, gid):
    """Unique no-follow temp, correct owner/mode before publication, durable rename."""
    fd, name = tempfile.mkstemp(prefix='.rotation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600)
            os.fchown(f.fileno(), uid, gid)
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
        d = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        if os.path.exists(name): os.unlink(name)


def parse_env(data):
    """Deliberately bounded dotenv grammar; unknown *other* lines stay byte-exact."""
    found = {}
    for line in data.decode('utf-8').splitlines():
        match = re.match(r'^\s*(?:export\s+)?(' + '|'.join(KEYS) + r')\s*=(.*)$', line)
        if not match: continue
        key, value = match.groups()
        value = value.strip()
        if value[:1] in ('\"', "'") and value[-1:] == value[:1]: value = value[1:-1]
        if key in found or not re.fullmatch(r'[A-Za-z0-9_./+=$:-]{1,512}', value):
            raise Refusal('Ambiguous manager credential dotenv; owner mapping required')
        found[key] = value
    if set(found) != set(KEYS):
        raise Refusal('All three existing manager credential entries are required')
    return found


def replace_env(data, values):
    return b''.join((m.group(1) + b'=' + values[m.group(1)].encode() + (b'\n' if line.endswith(b'\n') else b'') if
                    (m := re.match(rb'^\s*(?:export\s+)?(' + b'|'.join(k.encode() for k in KEYS) + rb')\s*=', line))
                    else line) for line in data.splitlines(keepends=True))


def scram(password):
    """Postgres 14 SCRAM verifier; never send plaintext in ALTER ROLE/logged SQL."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 4096)
    stored = hashlib.sha256(hmac.digest(salted, b'Client Key', 'sha256')).digest()
    server = hmac.digest(salted, b'Server Key', 'sha256')
    b64 = lambda v: base64.b64encode(v).decode()
    return f'SCRAM-SHA-256$4096:{b64(salt)}${b64(stored)}:{b64(server)}'


class DockerServices:
    """Only the known stock tipi role/user and three installed Compose services.

    Unsupported credentials/layouts fail before stop/reset, rather than guessing.
    All child output is captured and discarded on error; stdin carries secrets.
    """
    def __init__(self, root):
        self.root = Path(root)
        self.compose = ['docker', 'compose', '--project-directory', str(root),
                        '--env-file', str(root / '.env'), '-p', 'runtipi',
                        '-f', str(root / 'docker-compose.yml'),
                        '-f', str(root / 'user-config/tipi-compose.yml')]
        self.ids = {}

    def command(self, args, data=None, *, ok=(0,)):
        try:
            r = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=SAFE_ENV, cwd=self.root, timeout=240)
        except (OSError, subprocess.TimeoutExpired):
            raise Refusal('Credential service command unavailable or timed out; resume required') from None
        if r.returncode not in ok:
            raise Refusal('Credential service command failed; output suppressed; resume required')
        return r

    def model(self):
        try: return json.loads(self.command([*self.compose, 'config', '--format', 'json']).stdout)
        except (ValueError, KeyError): raise Refusal('Unsupported installed Compose model') from None

    def preflight(self, old, *, authenticate=True):
        model = self.model()
        svc = model.get('services', {})
        if any(k not in svc for k in ('runtipi', 'runtipi-db', 'runtipi-queue')):
            raise Refusal('Stock backend, Postgres and RabbitMQ services required')
        self.match_credentials(model, old)
        if svc['runtipi-db']['environment'].get('POSTGRES_USER') != 'tipi' or svc['runtipi-db']['environment'].get('POSTGRES_DB') != 'tipi':
            raise Refusal('Nonstock database role requires owner mapping')
        if svc['runtipi-queue']['environment'].get('RABBITMQ_DEFAULT_USER') != 'tipi':
            raise Refusal('Nonstock queue user requires owner mapping')
        backend = svc['runtipi'].get('environment', {})
        targets = {'POSTGRES_HOST': 'runtipi-db', 'POSTGRES_USERNAME': 'tipi',
                   'POSTGRES_DBNAME': 'tipi', 'POSTGRES_PORT': '5432',
                   'RABBITMQ_HOST': 'runtipi-queue', 'RABBITMQ_USERNAME': 'tipi'}
        if any(str(backend.get(k, '')) != v for k, v in targets.items()):
            raise Refusal('Backend credential targets differ from local stock services; owner mapping required')
        for service in ('runtipi', 'runtipi-db', 'runtipi-queue'):
            ids = self.command([*self.compose, 'ps', '-aq', service]).stdout.decode().split()
            if len(ids) != 1 or not re.fullmatch('[0-9a-f]{12,64}', ids[0]):
                raise Refusal('Exactly one existing container per credential service required')
            info = json.loads(self.command(['docker', 'inspect', ids[0]]).stdout)[0]
            labels = info['Config'].get('Labels', {})
            expected = ','.join(str(self.root / p) for p in ('docker-compose.yml', 'user-config/tipi-compose.yml'))
            if (labels.get('com.docker.compose.project') != 'runtipi' or
                labels.get('com.docker.compose.service') != service or
                labels.get('com.docker.compose.project.working_dir') != str(self.root) or
                labels.get('com.docker.compose.project.config_files') != expected or
                labels.get('com.docker.compose.project.environment_file', str(self.root / '.env')) != str(self.root / '.env')):
                raise Refusal('Container Compose invocation differs; owner mapping required')
            if service == 'runtipi' and not any(m.get('Type') == 'bind' and m.get('Source') == str(self.root / '.env') and m.get('Destination') == '/data/.env' for m in info['Mounts']):
                raise Refusal('Backend is not using the protected installed dotenv')
            self.ids[service] = ids[0]
        # Actual authentication, not merely a config/health/readiness check.
        if authenticate: self.assert_passwords(old, valid=True)
        if authenticate and self.bearer(old['JWT_SECRET']) != 200:
            raise Refusal('Existing operator bearer must work before rotation')
        # Pin all effective noncredential config, not just the names of services.
        for s in svc.values():
            for key in (*KEYS, 'RABBITMQ_DEFAULT_PASS'):
                if key in s.get('environment', {}): s['environment'][key] = '<rotated>'
        return digest(json.dumps(model, sort_keys=True).encode())

    @staticmethod
    def match_credentials(model, values):
        try:
            svc = model['services']
            expected = [('runtipi-db', 'POSTGRES_PASSWORD', 'POSTGRES_PASSWORD'),
                        ('runtipi-queue', 'RABBITMQ_DEFAULT_PASS', 'RABBITMQ_PASSWORD')]
            expected += [('runtipi', k, k) for k in KEYS]
            if any(svc[s]['environment'].get(k) != values[v] for s, k, v in expected):
                raise Refusal('Compose overrides manager secrets; owner mapping required')
        except (KeyError, TypeError): raise Refusal('Incomplete installed credential environment') from None

    def discover(self):
        # Resume uses labels again; preflight auth cannot assume the old passwords.
        for s in ('runtipi-db', 'runtipi-queue'):
            ids = self.command([*self.compose, 'ps', '-q', s]).stdout.decode().split()
            if len(ids) != 1 or not re.fullmatch('[0-9a-f]{12,64}', ids[0]):
                raise Refusal('Credential service missing; owner must restore service before resume')
            self.ids[s] = ids[0]

    def stop(self):
        self.command([*self.compose, 'stop', '--timeout', '60', 'runtipi'])
        ids = self.command([*self.compose, 'ps', '-q', 'runtipi']).stdout.strip()
        if ids: raise Refusal('Backend did not stop; no credential changes admitted')

    def db_password(self, password):
        # Suppress statement/duration/error logging before issuing even the verifier.
        sql = ("SET log_statement = 'none'; SET log_min_duration_statement = -1; "
               "SET log_min_duration_sample = -1; SET log_min_error_statement = 'panic';\n"
               f"ALTER ROLE tipi PASSWORD '{scram(password)}';\n"
               "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'tipi' AND pid <> pg_backend_pid();\n")
        self.command(['docker', 'exec', '-i', self.ids['runtipi-db'], 'psql', '-X',
                      '-U', 'tipi', '-d', 'tipi', '-v', 'ON_ERROR_STOP=1'], sql.encode())

    def queue_password(self, password):
        self.command(['docker', 'exec', '-i', self.ids['runtipi-queue'], 'rabbitmqctl',
                      '-q', 'change_password', 'tipi'], (password + '\n').encode())
        # Revoke preauthenticated AMQP connections, not just future logins.
        self.command(['docker', 'exec', self.ids['runtipi-queue'], 'rabbitmqctl',
                      '-q', 'close_all_user_connections', 'tipi', 'Owner credential invalidation'])

    def db_auth(self, password):
        # A shell builtin reads stdin: no password appears in exec argv/environment
        # on the host, shell history, command diagnostics, or SQL statement logs.
        script = 'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -X -h 127.0.0.1 -U tipi -d tipi -w -Atqc "SELECT 1"'
        r = self.command(['docker', 'exec', '-i', self.ids['runtipi-db'], 'sh', '-c', script],
                         (password + '\n').encode(), ok=(0, 2))
        if r.returncode == 0 and r.stdout.strip() == b'1': return True
        if r.returncode == 2 and b'password authentication failed' in r.stderr: return False
        raise Refusal('Database authentication probe inconclusive')

    def queue_auth(self, password):
        r = self.command(['docker', 'exec', '-i', self.ids['runtipi-queue'], 'rabbitmqctl',
                          '-q', 'authenticate_user', 'tipi'], (password + '\n').encode(), ok=(0, 65))
        if r.returncode == 0: return True
        if r.returncode == 65 and b'failed to authenticate user' in r.stderr: return False
        raise Refusal('Queue authentication probe inconclusive')

    def assert_passwords(self, values, *, valid):
        if self.db_auth(values['POSTGRES_PASSWORD']) != valid or self.queue_auth(values['RABBITMQ_PASSWORD']) != valid:
            raise Refusal('Credential authentication assertion failed')

    def restart(self, values):
        self.match_credentials(self.model(), values)
        # Never recreate DB/queue (RabbitMQ can have nonpersistent queued messages).
        self.command([*self.compose, 'up', '-d', '--no-deps', '--force-recreate',
                      '--wait', '--wait-timeout', '180', 'runtipi'])

    def bearer(self, secret):
        # Execute in the backend container: no host network/port assumption, no
        # redirects, no secret argv. The target is a fixed protected GET endpoint.
        ids = self.command([*self.compose, 'ps', '-q', 'runtipi']).stdout.decode().split()
        if len(ids) != 1: raise Refusal('Backend unavailable for bearer verification')
        code = ("const fs=require('fs'); const token=fs.readFileSync(0,'utf8').trim(); "
                "fetch('http://127.0.0.1:3000/api/marketplace/all', "
                "{headers:{Authorization:'Bearer '+token},redirect:'error',signal:AbortSignal.timeout(15000)})"
                ".then(r=>console.log(r.status)).catch(()=>process.exit(1));")
        r = self.command(['docker', 'exec', '-i', ids[0], 'node', '-e', code], (cli_token(secret) + '\n').encode())
        try: return int(r.stdout.strip())
        except ValueError: raise Refusal('Bearer verification returned no status') from None

    def verify(self, old, new):
        self.assert_passwords(new, valid=True)
        self.assert_passwords(old, valid=False)
        if self.bearer(new['JWT_SECRET']) != 200 or self.bearer(old['JWT_SECRET']) != 401:
            raise Refusal('New operator bearer acceptance / old bearer rejection not proven')


class Rotation:
    def __init__(self, root=ROOT, control=CONTROL, *, uid=None, gid=None, owner=0, io=None, crash=None):
        system = pwd.getpwnam('wizard-system') if uid is None else None
        self.uid, self.gid = (system.pw_uid, system.pw_gid) if system else (uid, gid)
        self.owner = owner
        self.root, self.control = Path(root), Path(control)
        self.io = io or DockerServices(self.root)
        self.crash = crash or (lambda point: None)
        self.path = self.control / 'journal.json'
        self.doc: dict = {}

    def check_paths(self):
        protected(self.root, {self.owner, self.uid}, directory=True)
        protected(self.root / '.env', {self.owner, self.uid}, private=True)
        if (self.root / '.env').stat().st_uid != self.uid:
            raise Refusal('Manager must own its dotenv')
        protected(self.control.parent, {self.owner}, private=True, directory=True)
        if not self.control.exists(): self.control.mkdir(mode=0o700)
        protected(self.control, {self.owner}, private=True, directory=True)
        for name in ('journal.json', 'before.env', 'after.env', 'lock'):
            p = self.control / name
            if p.exists() or p.is_symlink(): protected(p, {self.owner}, private=True)
        # Composition sources are management metadata, never app payload.
        for name in ('docker-compose.yml', 'user-config/tipi-compose.yml'):
            protected(self.root / name, {self.owner, self.uid})

    def load(self):
        self.doc = json.loads(self.path.read_bytes()) if self.path.exists() else {}
        if self.doc and (self.doc.get('schema') != 1 or self.doc.get('root') != str(self.root) or
                         self.doc.get('phase') not in (*PHASES, 'aborted')):
            raise Refusal('Unsupported credential journal; manual recovery required')
        return self.doc

    def save(self, phase):
        self.doc['phase'] = phase
        atomic(self.path, (json.dumps(self.doc, sort_keys=True, indent=2) + '\n').encode(), self.owner, os.getgid())
        self.crash('journal:' + phase)

    def backup(self, name, data):
        atomic(self.control / name, data, self.owner, os.getgid())
        self.crash('backup:' + name)

    def prepare(self):
        before = (self.root / '.env').read_bytes()
        old = parse_env(before)
        model = self.io.preflight(old)
        new = {k: secrets.token_hex(32) for k in KEYS}
        after = replace_env(before, {k.encode(): v for k, v in new.items()})
        # No journal => no side effect could have begun. Orphaned preparations are
        # replaced only while the live env is the original and preflight succeeds.
        self.backup('before.env', before)
        self.backup('after.env', after)
        self.doc = {'schema': 1, 'root': str(self.root), 'before': digest(before),
                    'after': digest(after), 'model': model, 'created': int(time.time())}
        self.save('prepared')

    def values(self):
        before, after = ((self.control / n).read_bytes() for n in ('before.env', 'after.env'))
        if digest(before) != self.doc['before'] or digest(after) != self.doc['after']:
            raise Refusal('Credential backup drift; manual recovery required')
        if digest((self.root / '.env').read_bytes()) not in (self.doc['before'], self.doc['after']):
            raise Refusal('Live dotenv drift; stop writers and resolve manually before resume')
        return before, after, parse_env(before), parse_env(after)

    def rotate(self):
        self.check_paths()
        fd = os.open(self.control / 'lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.load()
            if self.doc and self.doc['phase'] == 'aborted':
                raise Refusal('Aborted preparation retained; archive it before a new authorization')
            if not self.doc: self.prepare()
            before, after, old, new = self.values()
            current = parse_env((self.root / '.env').read_bytes())
            if self.io.preflight(current, authenticate=False) != self.doc['model']:
                raise Refusal('Effective Compose drift; owner review required before resume')
            if self.doc['phase'] == 'verified':
                self.io.discover(); self.io.verify(old, new)
                return self.doc
            # Every resume first stops the backend, even after a lost restart ack.
            self.io.stop(); self.crash('effect:stop')
            self.io.discover()
            if self.doc['phase'] == 'prepared': self.save('quiesced')
            # Replay both idempotent mutations even after a saved acknowledgement:
            # an external queue restart can lose its unpersisted user database.
            self.save('db_intent')  # durable ambiguity boundary, BEFORE DB write
            self.io.db_password(new['POSTGRES_PASSWORD']); self.crash('effect:db')
            if not self.io.db_auth(new['POSTGRES_PASSWORD']) or self.io.db_auth(old['POSTGRES_PASSWORD']):
                raise Refusal('Database password invalidation not proven; forward resume required')
            self.save('db_rotated')
            self.save('queue_intent')
            self.io.queue_password(new['RABBITMQ_PASSWORD']); self.crash('effect:queue')
            if not self.io.queue_auth(new['RABBITMQ_PASSWORD']) or self.io.queue_auth(old['RABBITMQ_PASSWORD']):
                raise Refusal('Queue password invalidation not proven; forward resume required')
            self.save('queue_rotated')
            # Always republish on resume: restart may have regenerated dotenv.
            atomic(self.root / '.env', after, self.uid, self.gid); self.crash('effect:env')
            self.save('file_committed')
            self.io.restart(new); self.crash('effect:restart')
            self.save('restarted')
            self.io.verify(old, new); self.crash('effect:verify')
            self.save('verified')
            return self.doc

    def abort(self):
        self.check_paths()
        fd = os.open(self.control / 'lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.load()
            if not self.doc: return {'phase': 'unprepared'}
            if self.doc['phase'] not in ('prepared', 'quiesced'):
                raise Refusal('Password mutation may have occurred: abort/rollback forbidden; forward resume required')
            before, _, old, _ = self.values()
            if digest((self.root / '.env').read_bytes()) != digest(before):
                raise Refusal('Cannot abort a changed live dotenv')
            self.io.discover(); self.io.assert_passwords(old, valid=True)
            self.io.restart(old)
            self.save('aborted')
            return self.doc


def rollback_allowed(control=CONTROL, *, owner=0):
    path = Path(control) / 'journal.json'
    if not path.exists(): return
    protected(path, {owner}, private=True)
    phase = json.loads(path.read_bytes()).get('phase')
    if phase not in ('prepared', 'quiesced', 'aborted'):
        raise Refusal('Legacy credential mutation started: snapshot rollback would revive leaked secrets; forward recovery required')


def require_revocation():
    """Root migration evidence plus actual account/live-process group readback."""
    migration = CONTROL.parent / 'journal.json'
    protected(migration, {0}, private=True)
    doc = json.loads(migration.read_bytes())
    if 'remove_docker' not in doc.get('done', []):
        raise Refusal('Revoke legacy Docker membership and live process credentials before rotation')
    agent = pwd.getpwnam(doc['agent'])
    import grp
    docker = grp.getgrnam('docker').gr_gid
    if docker in os.getgrouplist(agent.pw_name, agent.pw_gid):
        raise Refusal('Legacy account still has Docker access')
    for p in Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try:
            fields = dict(line.split(':', 1) for line in (p / 'status').read_text().splitlines() if ':' in line)
        except (FileNotFoundError, ProcessLookupError): continue
        uids = [int(v) for v in fields.get('Uid', '').split()]
        if agent.pw_uid in uids and docker in [int(v) for v in fields.get('Groups', '').split()]:
            raise Refusal('A live legacy process retains Docker access; terminate it before rotation')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('rotate', 'abort', 'status'))
    parser.add_argument('--authorize-invalidation', action='store_true')
    args = parser.parse_args(argv)
    if os.getuid() != 0: raise Refusal('Only the owner root migration CLI can rotate credentials')
    rotation = Rotation()
    if args.action == 'status':
        rotation.check_paths(); value = rotation.load() or {'phase': 'unprepared'}
    else:
        if not args.authorize_invalidation:
            raise Refusal('Explicit --authorize-invalidation required; this signs out backend sessions')
        require_revocation()
        value = rotation.rotate() if args.action == 'rotate' else rotation.abort()
    print(json.dumps(value, sort_keys=True, indent=2))
    return 0


def entrypoint(argv=None):
    try: return main(argv)
    except (Refusal, OSError, ValueError, KeyError):
        # No traceback / raw subprocess, dotenv or JSON errors can leak a value.
        print('Credential transaction stopped. Inspect its protected phase; use forward resume or the documented manual recovery.', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(entrypoint())
