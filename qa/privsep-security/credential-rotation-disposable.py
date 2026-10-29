#!/usr/bin/python3 -I
"""DESTRUCTIVE DISPOSABLE QA ONLY. Run by parent admission on a clean forum VM.

Never invokes a live installer/migration. Creates ONLY synthetic credentials.
Requires an empty dedicated Docker daemon, root, digest-pinned preloaded images,
and an explicit acknowledgement. No image build, pull or credential discovery.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wam.credential_rotation import DockerServices, Rotation, parse_env, SAFE_ENV
from wam.runtipi import cli_token

BASE = Path('/var/lib/wizard-rotation-acceptance')
MANAGER, LEGACY = 1992, 1993
OLD = {'JWT_SECRET': 'disposable-before-jwt-not-real',
       'POSTGRES_PASSWORD': 'disposable-before-db-not-real',
       'RABBITMQ_PASSWORD': 'disposable-before-queue-not-real'}
CASES = ('no-crash', 'journal:db_intent', 'effect:db', 'effect:queue',
         'effect:env', 'effect:restart', 'journal:verified')


def command(args, data=None):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=SAFE_ENV, timeout=300)
    if result.returncode: raise RuntimeError('Disposable command failed; raw output intentionally suppressed')
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
    return int(raw.strip())


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


def fixture(folder, images):
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
                         'healthcheck': {'test': ['CMD', 'rabbitmq-diagnostics', '-q', 'ping'], 'interval': '3s', 'timeout': '10s', 'retries': 60}},
        'runtipi': {'image': images['backend'], 'env_file': ['.env'], 'environment': {'NODE_ENV': 'production'},
                    'depends_on': {'runtipi-db': {'condition': 'service_healthy'}, 'runtipi-queue': {'condition': 'service_healthy'}},
                    'volumes': [str(root) + ':/data', str(root / '.env') + ':/data/.env',
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorize-disposable', action='store_true')
    parser.add_argument('--source-revision', required=True)
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
    BASE.mkdir(mode=0o755)
    receipt = {'schema': 1, 'images': images, 'source_revision': args.source_revision,
               'cases': [], 'runtime': 'real-disposable-docker'}
    for index, point in enumerate(CASES):
        folder = BASE / str(index); folder.mkdir(mode=0o755)
        root, control, app, appenv = fixture(folder, images)
        io = DockerServices(root)
        try:
            io.command([*io.compose, 'up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '240'])
            # Synthetic-only first operator. No existing owner credential is read.
            assert request(io, '/api/auth/register', body={'username': 'owner@rotation.invalid', 'password': 'Disposable-Only-Password-2026!'}) in (200, 201)
            original_bearer = cli_token(OLD['JWT_SECRET'], ttl=3600)
            assert request(io, '/api/marketplace/all', token=original_bearer) == 200
            before = identity_snapshot((app, appenv))
            def crash(where):
                if where == point: raise InterruptedError('injected disposable crash')
            kwargs = dict(root=root, control=control / 'credentials', uid=MANAGER, gid=MANAGER, io=io)
            try: Rotation(**kwargs, crash=crash).rotate()
            except InterruptedError:
                assert point != 'no-crash'
            result = Rotation(**kwargs).rotate()
            assert result['phase'] == 'verified'
            new = parse_env((root / '.env').read_bytes())
            assert all(new[k] != OLD[k] for k in OLD)
            assert request(io, '/api/marketplace/all', token=original_bearer) == 401
            assert request(io, '/api/marketplace/all', token=cli_token(new['JWT_SECRET'])) == 200
            assert identity_snapshot((app, appenv)) == before
            assert_legacy_denied((root / '.env', control / 'credentials/before.env', control / 'credentials/after.env', control / 'credentials/journal.json'))
            for p in (control / 'credentials').iterdir():
                assert p.stat().st_uid == 0 and p.stat().st_mode & 0o777 == 0o600
            # Container logs must not contain any synthetic old/new plaintext key.
            logs = io.command([*io.compose, 'logs', '--no-color']).stdout
            assert all(v.encode() not in logs for v in (*OLD.values(), *new.values()))
            receipt['cases'].append({'boundary': point, 'phase': 'verified', 'old_bearer': 401, 'new_bearer': 200,
                                     'old_database_password': 'rejected', 'old_queue_password': 'rejected',
                                     'app_bytes_uid_gid_mode': 'unchanged', 'legacy_uid_protected_read': 'denied'})
            print('PASS disposable boundary ' + point, flush=True)
        finally:
            io.command([*io.compose, 'down', '--volumes', '--remove-orphans', '--timeout', '30'])
    assert len(receipt['cases']) == len(CASES)
    (BASE / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print('Receipt: ' + str(BASE / 'receipt.json'))


if __name__ == '__main__':
    try: main()
    except Exception:
        print('FAIL disposable rotation acceptance; raw exception suppressed to keep synthetic credentials out of logs', file=sys.stderr)
        raise SystemExit(1)
