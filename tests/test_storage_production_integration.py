"""Small integration closure. Runtime boot/Service acceptance is in the VM rig."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import pytest
from wam import privsep

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def public_mapping(monkeypatch):
    import pwd
    import runpy
    mapping = {'schema': 1, 'uid': 1234, 'gid': 1235, 'state': 'ready',
               'logical': '/home/hermes/.hermes', 'physical': '/var/lib/wizard-agent-homes/1234',
               'identity': [8, 456]}
    calls = []
    def require(account, logical):
        calls.append((account, logical)); return copy.deepcopy(mapping)
    monkeypatch.setattr(pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_gid=1235, pw_name='hermes'))
    monkeypatch.setattr(runpy, 'run_path', lambda path: {'require_mapping': require})
    st = {'agent': {'uid': 1234, 'gid': 1235, 'hermes_home': '/home/hermes/.hermes',
                    'origin': 'http://127.0.0.1:8642'}}
    return st, mapping, calls


def test_public_mapping_is_only_physical_bind_and_keeps_both_container_paths(public_mapping):
    from wam.network import load_yaml
    st, mapping, calls = public_mapping
    doc = load_yaml(privsep.service_override(st, '/var/lib/wizard-os/.wizard-os'))
    service = doc['services']['wizard-agent-service']
    assert service['user'] == '1234:1235'
    assert calls == [('hermes', '/home/hermes/.hermes')]
    mounts = service['volumes']
    assert '/var/lib/wizard-agent-homes/1234:/hermes' in mounts
    assert '/var/lib/wizard-agent-homes/1234:/home/hermes/.hermes' in mounts
    assert not any(v.startswith('/home/hermes/.hermes:') for v in mounts)
    assert service['environment']['HERMES_HOME'] == '/home/hermes/.hermes'
    assert '/home/hermes/.hermes/cache/images' in service['environment']['WIZARD_AGENT_ARTIFACT_ROOTS']
    assert not any('docker.sock' in v for v in mounts)


@pytest.mark.parametrize('bad', ['uid-string', 'uid-overflow', 'gid-mismatch', 'unknown-field', 'identity-bool',
    'schema-bool', 'unsafe-logical', 'mapping-uid', 'mapping-gid', 'mapping-schema', 'mapping-state',
    'mapping-logical', 'mapping-physical'])
def test_bad_public_mapping_or_runtime_identity_is_closed(public_mapping, bad):
    st, mapping, _ = public_mapping
    if bad == 'uid-string': st['agent']['uid'] = '1234'
    elif bad == 'uid-overflow': st['agent']['uid'] = 2**80
    elif bad == 'gid-mismatch': st['agent']['gid'] = 9999
    elif bad == 'unknown-field': mapping['command'] = 'never execute'
    elif bad == 'identity-bool': mapping['identity'][0] = True
    elif bad == 'schema-bool': mapping['schema'] = True
    elif bad == 'unsafe-logical': st['agent']['hermes_home'] += '\n    privileged: true'
    elif bad == 'mapping-uid': mapping['uid'] = 1236
    elif bad == 'mapping-gid': mapping['gid'] = 1236
    elif bad == 'mapping-schema': mapping['schema'] = 2
    elif bad == 'mapping-state': mapping['state'] = 'rollback-unavailable'
    elif bad == 'mapping-logical': mapping['logical'] = '/home/other/.hermes'
    elif bad == 'mapping-physical': mapping['physical'] = '/etc'
    with pytest.raises(ValueError, match='maintenance-needed'): privsep.storage_source(st)


def test_missing_mapping_stops_override_before_reconciliation_or_writes(tmp_path, monkeypatch):
    from wam import alongside
    monkeypatch.setattr(privsep, 'enabled', lambda home: True)
    monkeypatch.setattr(alongside, 'load', lambda home: {'agent': {}})
    def refuse(st): raise ValueError('maintenance-needed: fixture')
    monkeypatch.setattr(privsep, 'storage_source', refuse)
    def forbidden(*a, **kw): raise AssertionError('mutated before preflight')
    monkeypatch.setattr(alongside, 'reconcile_images', forbidden)
    monkeypatch.setattr(alongside, 'save', forbidden)
    with pytest.raises(ValueError, match='maintenance-needed'):
        alongside.write_shell_override(tmp_path, 'wizardapps', 'wizard-agent-service', tmp_path, 'service')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('operation', ['install', 'app_update', 'app_lifecycle'])
def test_service_mutation_preflight_precedes_api_mutation(operation):
    tree = ast.parse((REPO/'wam/cli.py').read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == operation)
    body = ast.unparse(fn)
    assert body.index('privsep.preflight(') < body.index({'install': 'ensure_store(',
        'app_update': 'rt.update(', 'app_lifecycle': 'getattr(rt, verb)'}[operation])


def test_bootstrap_has_fixed_boot_purpose_no_root_remount_or_service_exec():
    text = (REPO/'scripts/storage-maintenance-init.py').read_text()
    tree = ast.parse(text)
    body = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'bootstrap'))
    assert "mount(b'proc', b'/proc', b'proc', 2 | 4 | 8, None)" in body
    assert body.index("product['require_fence']()") < body.index("os.statvfs('/')") < body.index("product['main']()")
    assert not any(isinstance(n, ast.Call) and ast.unparse(n.func) in ('os.execve', 'os.execv', 'subprocess.run') for n in ast.walk(tree))
    assert 'getenv' not in body and 'systemd' not in body


def test_pid1_completion_retirement_is_after_transaction_and_fence():
    tree = ast.parse((REPO/'scripts/protected-agent-storage.py').read_text())
    body = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main'))
    assert body.index("set(request) != {'action', 'agent', 'logical'}") < body.index("Transaction(request['agent']")
    assert body.index("Transaction(request['agent']") < body.index("rename_new(parent, REQUEST.name, parent, 'completed.json')")
    assert "'fixed maintenance exception'" in body
