"""Adapter and refusal-order regressions; real services remain a separate VM gate."""
import copy
import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from wam import credential_rotation as r
from test_credential_rotation import OLD, case  # reuse protected fixture, not a weaker path policy

NETWORK = 'c' * 64
IMAGE = 'sha256:' + 'd' * 64
ADDRESS = '172.30.0.2'
REMOTE = dict(type='host', database=['all'], user_name=['all'], address='all',
              netmask=None, auth_method='scram-sha-256', error=None)
LOOP = dict(REMOTE, address='127.0.0.1', netmask='255.255.255.255', auth_method='trust')


def installed(case, monkeypatch, *, negative='denied'):
    io = r.DockerServices(case.root)
    backend = dict(OLD, POSTGRES_HOST='runtipi-db', POSTGRES_USERNAME='tipi', POSTGRES_DBNAME='tipi',
                   POSTGRES_PORT='5432', RABBITMQ_HOST='runtipi-queue', RABBITMQ_USERNAME='tipi')
    model = {'services': {'runtipi': {'environment': backend, 'networks': {'default': {}}},
                         'runtipi-db': {'environment': dict(POSTGRES_PASSWORD=OLD['POSTGRES_PASSWORD'], POSTGRES_USER='tipi', POSTGRES_DB='tipi'), 'networks': {'default': {}}},
                         'runtipi-queue': {'environment': dict(RABBITMQ_DEFAULT_PASS=OLD['RABBITMQ_PASSWORD'], RABBITMQ_DEFAULT_USER='tipi')}},
             'networks': {'default': {'name': 'runtipi_default'}}}
    infos = {}
    ids = {s: str(i) * 64 for i, s in enumerate(model['services'], 1)}
    for s in ids:
        infos[s] = {'Config': {'Labels': {
            'com.docker.compose.project': 'runtipi', 'com.docker.compose.service': s,
            'com.docker.compose.project.working_dir': str(case.root),
            'com.docker.compose.project.config_files': ','.join(str(case.root / p) for p in ('docker-compose.yml', 'user-config/tipi-compose.yml'))}},
            'Mounts': [{'Type': 'bind', 'Source': str(case.root / '.env'), 'Destination': '/data/.env'}],
            'Image': IMAGE, 'NetworkSettings': {'Networks': {'runtipi_default': {
                'IPAddress': ADDRESS, 'NetworkID': NETWORK, 'Aliases': ['runtipi-db']}}}}
    network = {'Name': 'runtipi_default', 'Id': NETWORK, 'Driver': 'bridge',
               'Labels': {'com.docker.compose.project': 'runtipi', 'com.docker.compose.network': 'default'},
               'Containers': {ids['runtipi-db']: {}}}
    calls = []
    def command(args, data=None, **kw):
        calls.append((args, data))
        if args[:len(io.compose)] == io.compose:
            tail = args[len(io.compose):]
            assert tail[:2] == ['ps', '-aq'], 'unexpected mutation'
            return SimpleNamespace(stdout=ids[tail[-1]].encode())
        if args[:2] == ['docker', 'inspect']:
            return SimpleNamespace(stdout=json.dumps([infos[next(s for s, cid in ids.items() if cid == args[2])]]).encode())
        if args[:3] == ['docker', 'network', 'inspect']:
            return SimpleNamespace(stdout=json.dumps([network]).encode())
        if 'pg_hba_file_rules' in args[-1]:
            return SimpleNamespace(stdout=json.dumps([LOOP, REMOTE]).encode())
        if args[:2] == ['docker', 'run']:
            if negative == 'timeout': raise r.Refusal('Credential service command unavailable or timed out; resume required')
            if negative == 'inconclusive': return SimpleNamespace(returncode=2, stdout=b'', stderr=b'connection refused')
            if negative == 'allowed': return SimpleNamespace(returncode=0, stdout=('tipi|tipi|' + ADDRESS).encode(), stderr=b'')
            return SimpleNamespace(returncode=2, stdout=b'', stderr=b'FATAL:  password authentication failed for user "tipi"\n')
        pytest.fail('unexpected service action')
    monkeypatch.setattr(io, 'model', lambda: copy.deepcopy(model))
    monkeypatch.setattr(io, 'command', command)
    return io, calls, model, infos, network


@pytest.mark.parametrize('negative,match', [('allowed', 'invalid password'), ('inconclusive', 'inconclusive'), ('timeout', 'timed out')])
def test_negative_control_refuses_before_stop_backup_or_journal(case, monkeypatch, negative, match):
    io, calls, *_ = installed(case, monkeypatch, negative=negative)
    obj = r.Rotation(**{**case.kwargs, 'io': io})
    with pytest.raises(r.Refusal, match=match): obj.rotate()
    assert not obj.path.exists()
    assert not (obj.control / 'before.env').exists()
    assert not (obj.control / 'after.env').exists()
    assert (case.root / '.env').read_bytes() == case.env
    assert case.io.running  # existing sessions/backend were never stopped
    assert all('stop' not in args and 'change_password' not in args and 'ALTER ROLE' not in str(args) for args, _ in calls)


@pytest.mark.parametrize('point', ['journal:prepared', 'effect:db', 'effect:queue', 'effect:env', 'effect:restart', 'journal:verified'])
def test_resume_negative_control_never_restores_or_replays_on_refusal(case, monkeypatch, point):
    def crash(where):
        if where == point: raise InterruptedError
    original = r.Rotation(**case.kwargs, crash=crash)
    with pytest.raises(InterruptedError): original.rotate()
    before = {p.name: p.read_bytes() for p in original.control.iterdir()}
    env = (case.root / '.env').read_bytes()
    state = (case.io.db, case.io.queue, case.io.jwt, case.io.running)
    io, _, _, _, _ = installed(case, monkeypatch, negative='allowed')
    # Effective current secrets can differ on later resumes; preserve stock model
    # validation seam while exercising actual preflight negative control/order.
    monkeypatch.setattr(io, 'match_credentials', lambda *a: None)
    with pytest.raises(r.Refusal, match='invalid password'):
        r.Rotation(**{**case.kwargs, 'io': io}).rotate()
    assert {p.name: p.read_bytes() for p in original.control.iterdir()} == before
    assert (case.root / '.env').read_bytes() == env
    assert (case.io.db, case.io.queue, case.io.jwt, case.io.running) == state
    if point != 'journal:prepared':
        with pytest.raises(r.Refusal, match='revive leaked secrets'):
            r.rollback_allowed(original.control, owner=os.getuid())


@pytest.mark.parametrize('change', ['loopback', 'wrong-network', 'host-mode', 'external-label', 'wrong-alias', 'ambiguous-network'])
def test_network_target_validation_fails_closed(case, monkeypatch, change):
    io, _, model, infos, network = installed(case, monkeypatch)
    io.ids = {s: str(i) * 64 for i, s in enumerate(model['services'], 1)}
    if change == 'loopback': infos['runtipi-db']['NetworkSettings']['Networks']['runtipi_default']['IPAddress'] = '127.0.0.1'
    if change == 'wrong-network': network['Id'] = 'e' * 64
    if change == 'host-mode': model['services']['runtipi']['network_mode'] = 'host'
    if change == 'external-label': network['Labels']['com.docker.compose.project'] = 'other'
    if change == 'wrong-alias': infos['runtipi-db']['NetworkSettings']['Networks']['runtipi_default']['Aliases'] = ['other']
    if change == 'ambiguous-network': model['services']['runtipi']['networks']['other'] = {}
    with pytest.raises(r.Refusal, match='network'): io.resolve_db_target(model, infos)


@pytest.mark.parametrize('rules', [[dict(REMOTE, auth_method='trust')], [dict(REMOTE, address='172.30.0.3')],
    [dict(REMOTE, user_name=['tipi'])], [REMOTE, REMOTE], [], [dict(REMOTE, error='invalid')],
    [dict(REMOTE, type='hostssl')], [dict(LOOP, netmask='0.0.0.0'), REMOTE]])
def test_nonstock_or_inconclusive_hba_is_not_password_proof(rules):
    with pytest.raises(r.Refusal): r.DockerServices.password_hba(rules)


def test_stock_hba_permits_local_trust_but_only_password_remote():
    r.DockerServices.password_hba([dict(type='local'), LOOP, REMOTE])


@pytest.mark.parametrize('rc,out,err,expected', [
    (0, b'tipi|tipi|172.30.0.2\n', b'', True),
    (2, b'', b'FATAL:  password authentication failed for user "tipi"\n', False),
    (2, b'', b'FATAL:  database "tipi" does not exist\n', None),
    (2, b'', b'connection timed out', None), (2, b'', b'no pg_hba.conf entry', None),
    (2, b'', b'FATAL:  password authentication failed for user "other"\n', None),
    (0, b'tipi|other|172.30.0.2\n', b'', None), (0, b'tipi|tipi|127.0.0.1\n', b'', None),
    (0, b'tipi|tipi|172.30.0.99\n', b'', None), (0, b'1\n', b'', None)])
def test_db_probe_classifies_only_actual_denial(case, monkeypatch, rc, out, err, expected):
    io = r.DockerServices(case.root); io.db_target = (NETWORK, IMAGE, ADDRESS)
    seen = []
    def command(args, data, **kw):
        seen.append((args, data, kw)); return SimpleNamespace(returncode=rc, stdout=out, stderr=err)
    monkeypatch.setattr(io, 'command', command)
    if expected is None:
        with pytest.raises(r.Refusal, match='inconclusive'): io.db_auth('synthetic-password')
    else: assert io.db_auth('synthetic-password') is expected
    args, data, kw = seen[0]
    assert args[:2] == ['docker', 'run'] and args[args.index('--network') + 1] == NETWORK
    assert IMAGE in args and '--pull=never' in args and '--rm' in args
    assert '-h runtipi-db -p 5432' in args[-1] and '127.0.0.1' not in args[-1]
    assert 'synthetic-password' not in str(args) and data == b'synthetic-password\n'
    assert kw['ok'] == (0, 2)


def test_real_shell_stdin_password_and_psql_dispatch(case, monkeypatch, tmp_path):
    # Real shell+subprocess, not a mocked success response. No Docker/server used.
    binary = tmp_path / 'psql'
    binary.write_text('#!/bin/sh\n[ "$PGPASSWORD" = "sentinel-not-argv" ] || exit 99\n'
                      '[ "$PGCONNECT_TIMEOUT" = 5 ] || exit 98\n'
                      '[ "$1" = -X ] && [ "$2" = -h ] && [ "$3" = runtipi-db ] || exit 97\n'
                      'printf "tipi|tipi|172.30.0.2\\n"\n')
    binary.chmod(0o700)
    io = r.DockerServices(case.root); io.db_target = (NETWORK, IMAGE, ADDRESS)
    def real_shell(args, data, **kw):
        return subprocess.run(['sh', '-c', args[-1]], input=data, capture_output=True,
                              env={'PATH': str(tmp_path) + ':/usr/bin:/bin'}, timeout=5)
    monkeypatch.setattr(io, 'command', real_shell)
    assert io.db_auth('sentinel-not-argv') is True
    with pytest.raises(r.Refusal, match='inconclusive'): io.db_auth('wrong')


def test_queue_cli_uses_service_identity_and_stdin_arity(case, monkeypatch):
    io = r.DockerServices(case.root); io.ids = {'runtipi-queue': 'b' * 64}
    calls = []
    def command(args, data=None, **kw):
        calls.append((args, data)); return SimpleNamespace(returncode=0)
    monkeypatch.setattr(io, 'command', command)
    io.queue_password('stdin-only'); assert io.queue_auth('stdin-only')
    for args, data in calls:
        assert args[args.index('--user') + 1] == 'rabbitmq'
        if data:
            assert len(args[args.index('rabbitmqctl') + 1:]) == 2
            assert '-q' not in args and data == b'stdin-only\n'
        assert 'stdin-only' not in str(args)


def test_command_timeout_never_becomes_password_denial(case, monkeypatch):
    io = r.DockerServices(case.root); io.db_target = (NETWORK, IMAGE, ADDRESS)
    def timeout(*a, **kw): raise subprocess.TimeoutExpired(['secret-argv-never-render'], 240)
    monkeypatch.setattr(r.subprocess, 'run', timeout)
    with pytest.raises(r.Refusal, match='timed out') as exc: io.db_auth('private-input')
    assert 'private-input' not in str(exc.value) and 'secret-argv' not in str(exc.value)
