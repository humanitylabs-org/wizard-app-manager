#!/usr/bin/python3 -I
"""DESTRUCTIVE DISPOSABLE QA ONLY. Run by parent admission on a clean forum VM.

Never invokes a live installer/migration. Creates ONLY synthetic credentials.
Requires an empty dedicated Docker daemon, root, digest-pinned preloaded images,
and an explicit acknowledgement. No image build, pull or credential discovery.
"""
import argparse
import hashlib
import io as bytes_io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wam.credential_rotation import DockerServices, Rotation, parse_env, SAFE_ENV
from wam.runtipi import cli_token
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rotation_diagnostics import Diagnostics, assert_no_credentials, teardown, queue_health_contract

DIAG = Diagnostics()


class DiagnosticServices(DockerServices):
    """Test-only subprocess seam; production Rotation and adapter remain unchanged."""
    def command(self, args, data=None, *, ok=(0,)):
        return DIAG.run(args, data=data, env=SAFE_ENV, cwd=self.root, ok=ok, compose=self.compose)

BASE = Path('/var/lib/wizard-rotation-acceptance')
MANAGER, LEGACY = 1992, 1993
OLD = {'JWT_SECRET': 'disposable-before-jwt-not-real',
       'POSTGRES_PASSWORD': 'disposable-before-db-not-real',
       'RABBITMQ_PASSWORD': 'disposable-before-queue-not-real'}
CASES = ('no-crash', 'journal:db_intent', 'effect:db', 'effect:queue',
         'effect:env', 'effect:restart', 'journal:verified')


def command(args, data=None):
    result = DIAG.run(args, data=data, env=SAFE_ENV, timeout=300)
    return result.stdout


def request(io, path, *, token=None, body=None):
    cid = io.command([*io.compose, 'ps', '-q', 'runtipi']).stdout.decode().strip()
    code = ("const fs=require('fs');const x=JSON.parse(fs.readFileSync(0,'utf8'));"
            "fetch('http://127.0.0.1:3000'+x.path,{method:x.body?'POST':'GET',"
            "headers:{'Content-Type':'application/json',...(x.token?{Authorization:'Bearer '+x.token}:{})},"
            "body:x.body?JSON.stringify(x.body):undefined,redirect:'error',signal:AbortSignal.timeout(15000)})"
            ".then(r=>console.log(r.status)).catch(()=>process.exit(1));")
    raw = io.command(['docker', 'exec', '-i', cid, 'node', '-e', code],
                     json.dumps({'path': path, 'token': token, 'body': body}).encode()).stdout
    status = int(raw.strip())
    DIAG.emit('http-status', endpoint=path if path in ('/api/auth/register', '/api/marketplace/all') else 'other', status=status)
    return status


def identity_snapshot(paths):
    return [(str(p), hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_uid,
             p.stat().st_gid, p.stat().st_mode) for p in paths]


def assert_legacy_denied(paths):
    # Real numeric-UID proof in the fixture, no mocked stat/chown or root read.
    def drop():
        os.setgroups([]); os.setgid(LEGACY); os.setuid(LEGACY)
    for p in paths:
        result = subprocess.run(['/usr/bin/python3', '-I', '-c',
                                 'import sys; open(sys.argv[1],"rb").read()', str(p)],
                                preexec_fn=drop, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=SAFE_ENV, timeout=10)
        assert result.returncode != 0, 'Legacy UID could read protected credential state'


def queue_contract(image):
    DIAG.checkpoint('queue-public-image-contract')
    raw = command(['docker', 'image', 'inspect', '--format',
                   '{{json .Config.User}}\n{{json .Config.Entrypoint}}', image]).decode().splitlines()
    assert len(raw) == 2
    user, entrypoints = (json.loads(line) for line in raw)
    cid = command(['docker', 'create', '--name', 'rotation-queue-contract', '--network', 'none',
                   '--entrypoint', '/bin/true', image]).decode().strip()
    primary = None
    try:
        public = {}
        for filename in ('/etc/passwd', '/usr/local/bin/docker-entrypoint.sh'):
            raw = command(['docker', 'cp', cid + ':' + filename, '-'])
            with tarfile.open(fileobj=bytes_io.BytesIO(raw)) as archive:
                members = archive.getmembers()
                assert len(members) == 1 and members[0].isfile() and members[0].size <= 131072
                public[filename] = archive.extractfile(members[0]).read().decode()
        contract = queue_health_contract(user, entrypoints, public['/etc/passwd'],
                                         public['/usr/local/bin/docker-entrypoint.sh'])
        DIAG.emit('queue-public-image-contract', service_uid=contract['uid'], service_gid=contract['gid'],
                  default_root=contract['default_root'], health_uses_service_user=True)
        return contract
    except BaseException as error:
        primary = error
        DIAG.failure(error)
        raise
    finally:
        try: command(['docker', 'rm', '--volumes', cid])
        except Exception as error:
            DIAG.failure(error, cleanup=True)
            if primary is None: raise


def fixture(folder, images, queue):
    root = folder / 'runtipi'; root.mkdir()
    (root / 'user-config').mkdir()
    root.chmod(0o750)
    for name in ('app-data/demo/data', 'data', 'state', 'logs', 'cache', 'repos', 'apps', 'traefik', 'media', 'backups'):
        (root / name).mkdir(parents=True, exist_ok=True)
    app = root / 'app-data/demo/data/payload'; app.write_bytes(b'opaque-disposable-app-data\x00\xff')
    appenv = root / 'app-data/demo/app.env'; appenv.write_bytes(b'APP_OWN_SECRET=preserve-this-disposable-secret\n')
    os.chown(app, LEGACY, LEGACY); app.chmod(0o640)
    os.chown(appenv, MANAGER, MANAGER); appenv.chmod(0o600)
    env = dict(OLD, ROOT_FOLDER_HOST=str(root), INTERNAL_IP='127.0.0.1',
               RABBITMQ_USERNAME='tipi', RABBITMQ_HOST='runtipi-queue',
               POSTGRES_HOST='runtipi-db', POSTGRES_DBNAME='tipi', POSTGRES_USERNAME='tipi', POSTGRES_PORT='5432',
               TIPI_VERSION='disposable-privsep', APPS_REPO_ID='disposable-fixture',
               APPS_REPO_URL='https://example.invalid/no-catalog', DOMAIN='rotation.invalid', LOCAL_DOMAIN='rotation.lan',
               RUNTIPI_APP_DATA_PATH=str(root), RUNTIPI_FORWARD_AUTH_URL='http://runtipi:3000/api/auth/traefik',
               TZ='UTC', DEMO_MODE='false', GUEST_DASHBOARD='false', ALLOW_ERROR_MONITORING='false',
               ALLOW_AUTO_THEMES='false', PERSIST_TRAEFIK_CONFIG='false', ADVANCED_SETTINGS='false',
               EXPERIMENTAL_INSECURE_COOKIE='true', THEME_BASE='gray', THEME_COLOR='blue')
    (root / '.env').write_text('\n'.join(f'{k}={v}' for k, v in env.items()))
    (root / '.env').chmod(0o600)
    services = {
        'runtipi-db': {'image': images['db'], 'environment': {'POSTGRES_USER': 'tipi', 'POSTGRES_DB': 'tipi', 'POSTGRES_PASSWORD': '${POSTGRES_PASSWORD}'},
                       'volumes': [str(root / 'data/postgres') + ':/var/lib/postgresql/data'],
                       'healthcheck': {'test': ['CMD-SHELL', 'pg_isready -U tipi -d tipi'], 'interval': '2s', 'timeout': '2s', 'retries': 60}},
        'runtipi-queue': {'image': images['queue'], 'environment': {'RABBITMQ_DEFAULT_USER': 'tipi', 'RABBITMQ_DEFAULT_PASS': '${RABBITMQ_PASSWORD}'},
                         'volumes': [str(root / 'data/rabbitmq') + ':/var/lib/rabbitmq'],
                         'healthcheck': {'test': queue['health_test'], 'interval': '3s', 'timeout': '10s', 'retries': 60}},
        'runtipi': {'image': images['backend'], 'env_file': ['.env'], 'environment': {'NODE_ENV': 'production'},
                    'depends_on': {'runtipi-db': {'condition': 'service_healthy'}, 'runtipi-queue': {'condition': 'service_healthy'}},
                    'volumes': [str(root) + ':/data', str(root / '.env') + ':/data/.env', str(root / 'cache') + ':/cache',
                                str(root / 'app-data') + ':/app-data', '/var/run/docker.sock:/var/run/docker.sock:ro'],
                    'healthcheck': {'test': ['CMD', 'curl', '-fsS', 'http://127.0.0.1:3000/api/health'], 'interval': '3s', 'timeout': '3s', 'retries': 60}}}
    (root / 'docker-compose.yml').write_text(json.dumps({'services': services}))
    (root / 'user-config/tipi-compose.yml').write_text('{"services":{}}\n')
    for p in (root, root / 'user-config', root / '.env', root / 'docker-compose.yml', root / 'user-config/tipi-compose.yml'):
        os.chown(p, MANAGER, MANAGER)
        if p.suffix == '.yml': p.chmod(0o644)
    (root / 'user-config').chmod(0o755)
    control = folder / 'control'; control.mkdir(mode=0o700)
    return root, control, app, appenv


def main():
    DIAG.checkpoint('authorization')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorize-disposable', action='store_true')
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--diagnostic-first-case', action='store_true',
                        help='Only no-crash; emits review_required, never full acceptance')
    for name in ('backend', 'db', 'queue'): parser.add_argument('--' + name + '-image', required=True)
    args = parser.parse_args()
    if os.getuid() != 0 or not args.authorize_disposable:
        raise RuntimeError('Root + explicit disposable authorization required')
    images = {name: getattr(args, name + '_image') for name in ('backend', 'db', 'queue')}
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', images['backend']) or any(
            not re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}', images[k]) for k in ('db', 'queue')):
        raise RuntimeError('Backend must be an actual local image ID; database and queue must be repository-digest pinned')
    if not re.fullmatch(r'[0-9a-f]{40}', args.source_revision):
        raise RuntimeError('Exact backend source revision required')
    marker = Path('/run/wizard-disposable-rotation')
    if (not marker.is_file() or marker.is_symlink() or marker.stat().st_uid != 0
            or marker.stat().st_mode & 0o777 != 0o600 or marker.read_text().strip() != args.source_revision):
        raise RuntimeError('Fresh guest admission marker bound to this source is required')
    if command(['docker', 'ps', '-aq']).strip() or BASE.exists():
        raise RuntimeError('Refusing nonempty Docker daemon or reused acceptance directory')
    for image in images.values(): command(['docker', 'image', 'inspect', image])
    inspected = json.loads(command(['docker', 'image', 'inspect', images['backend']]))
    assert len(inspected) == 1 and inspected[0]['Id'] == images['backend']
    assert inspected[0]['Config']['Labels']['org.opencontainers.image.revision'] == args.source_revision
    queue = queue_contract(images['queue'])
    BASE.mkdir(mode=0o755)
    receipt = {'schema': 1, 'images': images, 'source_revision': args.source_revision,
               'cases': [], 'runtime': 'real-disposable-docker'}
    points = CASES[:1] if args.diagnostic_first_case else CASES
    for index, point in enumerate(points):
        DIAG.case = point
        DIAG.checkpoint('fixture')
        folder = BASE / str(index); folder.mkdir(mode=0o755)
        io = DiagnosticServices(folder / 'runtipi')
        primary = None
        try:
            root, control, app, appenv = fixture(folder, images, queue)
            DIAG.checkpoint('initial-service-readiness')
            io.command([*io.compose, 'up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '240'])
            DIAG.checkpoint('register-synthetic-operator')
            # Synthetic-only first operator. No existing owner credential is read.
            assert request(io, '/api/auth/register', body={'username': 'owner@rotation.invalid', 'password': 'Disposable-Only-Password-2026!'}) in (200, 201)
            original_bearer = cli_token(OLD['JWT_SECRET'], ttl=3600)
            DIAG.checkpoint('original-bearer')
            assert request(io, '/api/marketplace/all', token=original_bearer) == 200
            before = identity_snapshot((app, appenv))
            def crash(where):
                # Rotation emits fixed source-defined labels, never credential data.
                assert where in ('backup:before.env', 'backup:after.env', 'effect:stop', 'effect:db', 'effect:queue', 'effect:env',
                                 'effect:restart', 'effect:verify') or where in tuple('journal:' + p for p in
                                 ('prepared', 'quiesced', 'db_intent', 'db_rotated', 'queue_intent',
                                  'queue_rotated', 'file_committed', 'restarted', 'verified'))
                DIAG.emit('rotation-checkpoint', checkpoint=where)
                if where == point: raise InterruptedError('injected disposable crash')
            kwargs = dict(root=root, control=control / 'credentials', uid=MANAGER, gid=MANAGER, io=io)
            DIAG.checkpoint('rotate')
            try: Rotation(**kwargs, crash=crash).rotate()
            except InterruptedError:
                assert point != 'no-crash'
            DIAG.checkpoint('resume-or-idempotent-verify')
            result = Rotation(**kwargs, crash=lambda where: DIAG.emit('rotation-checkpoint', checkpoint=where)).rotate()
            DIAG.checkpoint('assert-credentials-and-bearers')
            assert result['phase'] == 'verified'
            new = parse_env((root / '.env').read_bytes())
            assert all(new[k] != OLD[k] for k in OLD)
            assert request(io, '/api/marketplace/all', token=original_bearer) == 401
            assert request(io, '/api/marketplace/all', token=cli_token(new['JWT_SECRET'])) == 200
            DIAG.checkpoint('assert-app-preservation-and-legacy-denial')
            assert identity_snapshot((app, appenv)) == before
            assert_legacy_denied((root / '.env', control / 'credentials/before.env', control / 'credentials/after.env', control / 'credentials/journal.json'))
            for p in (control / 'credentials').iterdir():
                assert p.stat().st_uid == 0 and p.stat().st_mode & 0o777 == 0o600
            # Container logs must not contain any synthetic old/new plaintext key.
            DIAG.checkpoint('assert-no-credential-logs')
            logs = io.command([*io.compose, 'logs', '--no-color']).stdout
            assert_no_credentials(logs, OLD, new)
            receipt['cases'].append({'boundary': point, 'phase': 'verified', 'old_bearer': 401, 'new_bearer': 200,
                                     'old_database_password': 'rejected', 'old_queue_password': 'rejected',
                                     'app_bytes_uid_gid_mode': 'unchanged', 'legacy_uid_protected_read': 'denied'})
            print('PASS disposable boundary ' + point, flush=True)
        except BaseException as error:
            primary = error
            DIAG.failure(error)
            raise
        finally:
            teardown(io, DIAG, primary)
    assert len(receipt['cases']) == len(points)
    name = 'diagnostic-receipt.json' if args.diagnostic_first_case else 'receipt.json'
    if args.diagnostic_first_case:
        receipt.update(status='review_required', full_acceptance=False, diagnostic_only=True)
    (BASE / name).write_text(json.dumps(receipt, indent=2) + '\n')
    print('Receipt: ' + str(BASE / name))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        DIAG.failure(error)
        raise SystemExit(1)
