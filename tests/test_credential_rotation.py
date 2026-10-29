"""Bounded synthetic transaction evidence, NOT Docker/Postgres/RabbitMQ proof."""
import hashlib
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from wam import credential_rotation as r
from wam import privilege_migration as migration

OLD = {'JWT_SECRET': 'legacy-jwt-sentinel', 'POSTGRES_PASSWORD': 'legacy-db-sentinel',
       'RABBITMQ_PASSWORD': 'legacy-queue-sentinel'}


class SyntheticServices:
    def __init__(self, root):
        self.root = root
        self.db = OLD['POSTGRES_PASSWORD']; self.queue = OLD['RABBITMQ_PASSWORD']
        self.jwt = OLD['JWT_SECRET']; self.running = True
        self.calls = []; self.model = 'fixed-compose-hash'

    def preflight(self, values, *, authenticate=True):
        self.calls.append('preflight')
        if authenticate:
            self.assert_passwords(values, valid=True)
            assert self.jwt == values['JWT_SECRET']
        return self.model

    def stop(self): self.calls.append('stop'); self.running = False
    def discover(self): self.calls.append('discover')
    def db_password(self, value):
        assert not self.running
        self.calls.append('db'); self.db = value
    def queue_password(self, value):
        assert not self.running
        self.calls.append('queue'); self.queue = value
    def db_auth(self, value): return self.db == value
    def queue_auth(self, value): return self.queue == value
    def assert_passwords(self, values, *, valid):
        assert self.db_auth(values['POSTGRES_PASSWORD']) is valid
        assert self.queue_auth(values['RABBITMQ_PASSWORD']) is valid
    def restart(self, values):
        self.calls.append('restart')
        assert r.parse_env((self.root / '.env').read_bytes()) == values
        self.assert_passwords(values, valid=True)
        self.jwt = values['JWT_SECRET']; self.running = True
    def verify(self, old, new):
        self.calls.append('verify')
        assert self.running
        self.assert_passwords(new, valid=True)
        self.assert_passwords(old, valid=False)
        assert self.jwt == new['JWT_SECRET'] != old['JWT_SECRET']


@pytest.fixture
def case(tmp_path):
    # pytest's /tmp root can be 1777; use the configured private scratch root.
    root = tmp_path / 'system/runtipi'; root.mkdir(parents=True)
    (root / 'user-config').mkdir()
    for name in ('docker-compose.yml', 'user-config/tipi-compose.yml'):
        (root / name).write_text('services: {}\n')
        (root / name).chmod(0o644)
    for folder in (root, root.parent, root / 'user-config'): folder.chmod(0o755)
    env = ('# retain unrelated manager options\n' + ''.join(f'{k}={v}\n' for k, v in OLD.items()) + 'UNCHANGED_MANAGER_OPTION=keep-exact').encode()
    (root / '.env').write_bytes(env); (root / '.env').chmod(0o600)
    parent = tmp_path / 'root-control'; parent.mkdir(mode=0o700)
    app = root / 'app-data/store/app/data/secret'; app.parent.mkdir(parents=True)
    app.write_bytes(b'opaque-app-own-secret\x00\xff'); app.chmod(0o640)
    appenv = root / 'app-data/store/app/app.env'; appenv.write_bytes(b'APP_PASSWORD=unchanged-app-secret\n')
    io = SyntheticServices(root)
    kwargs = dict(root=root, control=parent / 'credentials', uid=os.getuid(), gid=os.getgid(), owner=os.getuid(), io=io)
    return SimpleNamespace(root=root, env=env, io=io, kwargs=kwargs, app=app, appenv=appenv)


def test_rotate_preserves_app_secrets_owner_mode_and_noncredential_bytes(case):
    before = [(p.read_bytes(), p.stat().st_uid, p.stat().st_gid, p.stat().st_mode) for p in (case.app, case.appenv)]
    obj = r.Rotation(**case.kwargs)
    assert obj.rotate()['phase'] == 'verified'
    new = r.parse_env((case.root / '.env').read_bytes())
    assert all(new[k] != OLD[k] for k in r.KEYS)
    text = (case.root / '.env').read_text()
    assert text.startswith('# retain unrelated manager options\n')
    assert text.endswith('UNCHANGED_MANAGER_OPTION=keep-exact')
    assert [(p.read_bytes(), p.stat().st_uid, p.stat().st_gid, p.stat().st_mode) for p in (case.app, case.appenv)] == before
    assert (obj.control / 'before.env').read_bytes() == case.env
    for p in obj.control.iterdir():
        assert p.stat().st_uid == os.getuid() and p.stat().st_mode & 0o777 == 0o600
    assert obj.control.stat().st_mode & 0o777 == 0o700
    journal = obj.path.read_text()
    assert all(v not in journal for v in (*OLD.values(), *new.values()))
    assert (case.root / '.env').stat().st_uid == os.getuid()
    assert (case.root / '.env').stat().st_mode & 0o777 == 0o600
    calls = list(case.io.calls)
    r.Rotation(**case.kwargs).rotate()
    assert case.io.calls[len(calls):] == ['preflight', 'discover', 'verify']


CRASHES = ['backup:before.env', 'backup:after.env', *['journal:' + s for s in r.PHASES],
           'effect:stop', 'effect:db', 'effect:queue', 'effect:env', 'effect:restart', 'effect:verify']


@pytest.mark.parametrize('point', CRASHES)
def test_resume_from_each_durable_and_unacknowledged_side_effect(case, point):
    def crash(where):
        if where == point: raise InterruptedError('synthetic power loss')
    obj = r.Rotation(**case.kwargs, crash=crash)
    with pytest.raises(InterruptedError): obj.rotate()
    # Restart the transaction object from protected bytes, retain external stores.
    restored = r.Rotation(**case.kwargs)
    assert restored.rotate()['phase'] == 'verified'
    case.io.verify(OLD, r.parse_env((case.root / '.env').read_bytes()))
    assert case.app.read_bytes() == b'opaque-app-own-secret\x00\xff'


@pytest.mark.parametrize('point', ['journal:prepared', 'effect:stop', 'journal:quiesced'])
def test_abort_before_password_intent_is_safe(case, point):
    def crash(where):
        if where == point: raise InterruptedError
    with pytest.raises(InterruptedError): r.Rotation(**case.kwargs, crash=crash).rotate()
    assert r.Rotation(**case.kwargs).abort()['phase'] == 'aborted'
    assert (case.root / '.env').read_bytes() == case.env
    case.io.assert_passwords(OLD, valid=True)


@pytest.mark.parametrize('point', ['journal:db_intent', 'effect:db', 'journal:db_rotated',
                                  'journal:queue_intent', 'effect:queue', 'effect:env', 'effect:restart', 'journal:verified'])
def test_abort_forbidden_at_ambiguous_password_boundary(case, point):
    def crash(where):
        if where == point: raise InterruptedError
    with pytest.raises(InterruptedError): r.Rotation(**case.kwargs, crash=crash).rotate()
    with pytest.raises(r.Refusal, match='abort/rollback forbidden'):
        r.Rotation(**case.kwargs).abort()


@pytest.mark.parametrize('target,mode', [('.env', 0o644), ('.', 0o777), ('user-config', 0o775)])
def test_readable_secret_or_writable_ancestor_fails_before_service_call(case, target, mode):
    (case.root / target).chmod(mode)
    with pytest.raises(r.Refusal): r.Rotation(**case.kwargs).rotate()
    assert not case.io.calls


def test_symlink_and_hardlink_refused(case):
    env = case.root / '.env'; env.rename(case.root / 'real.env'); env.symlink_to('real.env')
    with pytest.raises(r.Refusal): r.Rotation(**case.kwargs).rotate()
    env.unlink(); os.link(case.root / 'real.env', env)
    with pytest.raises(r.Refusal): r.Rotation(**case.kwargs).rotate()
    assert not case.io.calls


def test_wrong_uid_refused_even_with_private_leaf(case):
    kwargs = {**case.kwargs, 'uid': os.getuid() + 1}
    with pytest.raises(r.Refusal): r.Rotation(**kwargs).rotate()
    assert not case.io.calls


def test_live_env_or_backup_drift_never_guessed(case):
    def crash(where):
        if where == 'effect:db': raise InterruptedError
    with pytest.raises(InterruptedError): r.Rotation(**case.kwargs, crash=crash).rotate()
    env = case.root / '.env'; env.write_bytes(case.env + b'\nNEW_OPTION=concurrent-edit')
    before = list(case.io.calls)
    with pytest.raises(r.Refusal, match='Live dotenv drift'): r.Rotation(**case.kwargs).rotate()
    assert case.io.calls == before
    env.write_bytes(case.env)
    backup = case.kwargs['control'] / 'after.env'; backup.write_bytes(case.env)
    with pytest.raises(r.Refusal, match='backup drift'): r.Rotation(**case.kwargs).rotate()


def test_compose_drift_rejected_before_stop_on_resume(case):
    def crash(where):
        if where == 'journal:prepared': raise InterruptedError
    with pytest.raises(InterruptedError): r.Rotation(**case.kwargs, crash=crash).rotate()
    case.io.model = 'changed-compose'
    with pytest.raises(r.Refusal, match='Compose drift'): r.Rotation(**case.kwargs).rotate()
    assert 'stop' not in case.io.calls


def test_store_only_file_edit_cannot_verify(case):
    case.io.db_password = lambda value: None
    with pytest.raises(r.Refusal, match='Database password invalidation not proven'):
        r.Rotation(**case.kwargs).rotate()
    assert (case.root / '.env').read_bytes() == case.env
    assert not case.io.running


def test_stdin_adapters_never_put_secrets_in_argv_or_errors(monkeypatch, case):
    calls = []
    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
    monkeypatch.setattr(r.subprocess, 'run', command)
    io = r.DockerServices(case.root); io.ids = {'runtipi-db': 'a' * 64, 'runtipi-queue': 'b' * 64}
    io.db_password(OLD['POSTGRES_PASSWORD']); io.queue_password(OLD['RABBITMQ_PASSWORD'])
    assert all(value not in str(argv) for argv, _ in calls for value in OLD.values())
    assert OLD['POSTGRES_PASSWORD'].encode() not in calls[0][1]['input']
    assert b'SCRAM-SHA-256$' in calls[0][1]['input']
    assert calls[1][1]['input'] == (OLD['RABBITMQ_PASSWORD'] + '\n').encode()
    def failed(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b'', stderr=OLD['JWT_SECRET'].encode())
    monkeypatch.setattr(r.subprocess, 'run', failed)
    with pytest.raises(r.Refusal) as error: io.queue_password(OLD['RABBITMQ_PASSWORD'])
    assert all(value not in str(error.value) for value in OLD.values())


def test_scram_verifier_matches_password_and_uses_fresh_salt():
    import base64
    value = r.scram(OLD['POSTGRES_PASSWORD'])
    algorithm, salt_iters, keys = value.split('$')
    iterations, salt = salt_iters.split(':'); stored, server = keys.split(':')
    salted = hashlib.pbkdf2_hmac('sha256', OLD['POSTGRES_PASSWORD'].encode(), base64.b64decode(salt), int(iterations))
    assert algorithm == 'SCRAM-SHA-256'
    assert base64.b64decode(stored) == hashlib.sha256(hmac.digest(salted, b'Client Key', 'sha256')).digest()
    assert base64.b64decode(server) == hmac.digest(salted, b'Server Key', 'sha256')
    assert value != r.scram(OLD['POSTGRES_PASSWORD'])


@pytest.mark.parametrize('bad', [b'JWT_SECRET=one\nJWT_SECRET=two\n', b'JWT_SECRET=$(command)\n', b'JWT_SECRET="multiline\nvalue"\n'])
def test_ambiguous_dotenv_refused_without_guessing(bad):
    with pytest.raises(r.Refusal): r.parse_env(bad)


def test_cutover_sequence_rotates_only_after_docker_revocation():
    assert migration.STEPS.index('remove_docker') < migration.STEPS.index('credentials') < migration.STEPS.index('complete')
    obj = object.__new__(migration.Migration); obj.doc = {}
    with pytest.raises(ValueError, match='invalidation'): obj.complete()


def test_real_docker_adapter_rejects_old_password_auth_inconclusive(monkeypatch, case):
    io = r.DockerServices(case.root); io.ids = {'runtipi-db': 'a' * 64, 'runtipi-queue': 'b' * 64}
    monkeypatch.setattr(io, 'command', lambda *a, **kw: SimpleNamespace(returncode=2, stdout=b'', stderr=b'connection refused'))
    with pytest.raises(r.Refusal, match='inconclusive'): io.db_auth('synthetic')
    monkeypatch.setattr(io, 'command', lambda *a, **kw: SimpleNamespace(returncode=65, stdout=b'', stderr=b'node down'))
    with pytest.raises(r.Refusal, match='inconclusive'): io.queue_auth('synthetic')


def test_snapshot_rollback_guard_uses_protected_phase_and_runs_before_any_restore(case, monkeypatch):
    def crash(where):
        if where == 'journal:db_intent': raise InterruptedError
    with pytest.raises(InterruptedError): r.Rotation(**case.kwargs, crash=crash).rotate()
    with pytest.raises(r.Refusal, match='revive leaked secrets'):
        r.rollback_allowed(case.kwargs['control'], owner=os.getuid())
    def refuse(*args): raise r.Refusal('mutation guard')
    monkeypatch.setattr(r, 'rollback_allowed', refuse)
    obj = object.__new__(migration.Migration)  # no doc/paths: no restore may begin
    with pytest.raises(r.Refusal, match='mutation guard'): obj.rollback()


def test_entrypoint_does_not_print_child_or_env_error_text(monkeypatch, capsys):
    def fail(argv): raise ValueError(OLD['JWT_SECRET'])
    monkeypatch.setattr(r, 'main', fail)
    assert r.entrypoint([]) == 1
    output = capsys.readouterr()
    assert OLD['JWT_SECRET'] not in output.out + output.err


def test_migration_requires_explicit_owner_consent_before_first_reset(case, monkeypatch):
    real = r.Rotation(**case.kwargs)
    monkeypatch.setattr(r, 'require_revocation', lambda: None)
    monkeypatch.setattr(r, 'Rotation', lambda *args: real)
    obj = object.__new__(migration.Migration); obj.doc = {}
    with pytest.raises(ValueError, match='--authorize-invalidation'): obj.credentials()
    assert not case.io.calls and not real.path.exists()
    obj.authorize_invalidation = True
    obj.credentials()
    assert obj.doc['credentials_verified']


@pytest.mark.parametrize('key,value', [('POSTGRES_HOST', 'outside-db'), ('POSTGRES_USERNAME', 'postgres'),
                                      ('RABBITMQ_HOST', 'outside-queue'), ('RABBITMQ_USERNAME', 'guest')])
def test_adapter_refuses_nonlocal_or_nonstock_backend_targets(case, monkeypatch, key, value):
    backend = dict(OLD, POSTGRES_HOST='runtipi-db', POSTGRES_USERNAME='tipi', POSTGRES_DBNAME='tipi',
                   POSTGRES_PORT='5432', RABBITMQ_HOST='runtipi-queue', RABBITMQ_USERNAME='tipi')
    backend[key] = value
    model = {'services': {'runtipi': {'environment': backend},
                         'runtipi-db': {'environment': {'POSTGRES_PASSWORD': OLD['POSTGRES_PASSWORD'], 'POSTGRES_USER': 'tipi', 'POSTGRES_DB': 'tipi'}},
                         'runtipi-queue': {'environment': {'RABBITMQ_DEFAULT_PASS': OLD['RABBITMQ_PASSWORD'], 'RABBITMQ_DEFAULT_USER': 'tipi'}}}}
    io = r.DockerServices(case.root)
    monkeypatch.setattr(io, 'model', lambda: model)
    monkeypatch.setattr(io, 'command', lambda *args, **kwargs: pytest.fail('container action before target validation'))
    with pytest.raises(r.Refusal, match='targets differ'): io.preflight(OLD)
