"""Small integration closure. Runtime boot/Service acceptance is in the VM rig."""
import ast
import copy
import hashlib
import importlib.util
import json
import tarfile
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
    assert service['volumes'].tag == '!override'
    mounts = service['volumes'].value
    assert isinstance(mounts, list)
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


@pytest.fixture
def production_constructor(tmp_path):
    """Genuine inherited constructor, real current product bytes; no VM calls.

    This is the complete constructor contract, not a full deployable stage.
    Preflight negatives below must refuse at this same early contract boundary.
    """
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    stage = tmp_path/'stage'; stage.mkdir(mode=0o700)
    for source, target in [('production_run.py', 'runner.py'), ('run.py', 'storage-runner.py')]:
        (stage/target).write_bytes((REPO/'qa/privsep-storage'/source).read_bytes())
    runner = load('production_constructor_fixture', stage/'runner.py')
    base = runner.load_old(stage)
    with tarfile.open(stage/'source.tar', 'w') as archive:
        archive.add(REPO/runner.PRODUCT_PATH, arcname=runner.PRODUCT_PATH, recursive=False)
    rows = base.inventory(stage/'source.tar')
    (stage/'source-members.json').write_text(json.dumps(rows))
    doc = {'revision':'a'*40, 'service_revision':'e1ba0d0564f1e0e3da3540f12f884e67ae17fbdf',
           'product_path':runner.PRODUCT_PATH, 'product_sha256':rows[0]['sha256'],
           'source_members':len(rows), 'expected_pytest_count':1,
           'expected_pytest_cases':['tests/synthetic_constructor.py::test_complete']}
    (stage/'manifest.json').write_text(json.dumps(doc))
    return runner, base, stage, doc


def test_genuine_production_constructor_persists_current_product_identity(production_constructor):
    runner, base, stage, doc = production_constructor
    instance = runner.make_runner(base)(stage, doc)
    receipt = json.loads((stage/'evidence/receipt.json').read_text())
    assert instance.proc is None and receipt['boots'] == [] and receipt['steps'] == []
    assert receipt['product_sha256'] == hashlib.sha256((REPO/runner.PRODUCT_PATH).read_bytes()).hexdigest()
    assert receipt['manifest_sha256'] == runner.digest(stage/'manifest.json')
    assert receipt['service_source_revision'] == doc['service_revision']
    assert receipt['actual_cases'] == [] and receipt['passed'] is False


@pytest.mark.parametrize('field', ['revision', 'product_sha256', 'service_revision',
    'expected_pytest_count', 'expected_pytest_cases', 'product_path', 'source_members'])
def test_constructor_missing_inherited_input_refuses_before_evidence(production_constructor, field):
    runner, base, stage, doc = production_constructor
    del doc[field]
    with pytest.raises(runner.ManifestContractError, match='missing runner manifest fields: '+field):
        runner.make_runner(base)(stage, doc)
    assert not (stage/'evidence').exists()


@pytest.mark.parametrize('bad', ['old-inherited-sha', 'malformed-sha', 'wrong-path', 'archive-mismatch'])
def test_constructor_wrong_product_identity_refuses_before_evidence(production_constructor, bad):
    runner, base, stage, doc = production_constructor
    if bad == 'old-inherited-sha':
        doc['product_sha256'] = '3ac25e1b2cbd79e452b3161920571ce2fd83b8d4084f363ead54a614812c47a7'
    elif bad == 'malformed-sha': doc['product_sha256'] = 'not-a-sha'
    elif bad == 'wrong-path': doc['product_path'] = 'inherited-product.py'
    else:
        doc['product_sha256'] = '0'*64
        rows = json.loads((stage/'source-members.json').read_text())
        rows[0]['sha256'] = doc['product_sha256']
        (stage/'source-members.json').write_text(json.dumps(rows))
    with pytest.raises(runner.ManifestContractError, match='product'):
        runner.make_runner(base)(stage, doc)
    assert not (stage/'evidence').exists()


@pytest.mark.parametrize('bad', ['missing-product', 'missing-inventory', 'wrong-product'])
def test_preflight_contract_negative_precedes_import_probe_or_evidence(production_constructor, monkeypatch, bad):
    runner, _, stage, doc = production_constructor
    if bad == 'missing-product': del doc['product_sha256']
    elif bad == 'missing-inventory': del doc['expected_pytest_cases']
    else: doc['product_sha256'] = '0'*64
    (stage/'manifest.json').write_text(json.dumps(doc))
    def forbidden(*args, **kwargs): raise AssertionError('external work before contract refusal')
    monkeypatch.setattr(runner, 'load_old', forbidden)
    monkeypatch.setattr(runner.subprocess, 'check_output', forbidden)
    with pytest.raises(runner.ManifestContractError):
        runner.validate(stage, runner.digest(stage/'manifest.json'), local=True)
    assert not (stage/'evidence').exists() and not (stage/'tmp').exists()


def test_effective_inherited_manifest_reads_are_explicit(production_constructor):
    runner, _, stage, doc = production_constructor
    assert runner.validate_runner_contract(stage, doc) == {
        'base.__init__':['product_sha256', 'revision'],
        'base.focused_tests':['expected_pytest_cases', 'expected_pytest_count'],
        'production.__init__':['service_revision'],
        'production.execute':['revision'],
        'production.continue_production':['revision', 'service_revision']}
    # The replaced base execute's tree/cut/intervention budgets are NOT consumed
    # by the production proof; do not invent dummy fields for those old oracles.
    original = (stage/'storage-runner.py').read_text()
    changed = original.replace("self.doc['expected_pytest_count']", "self.doc['new_required_field']")
    assert changed != original
    (stage/'storage-runner.py').write_text(changed)
    with pytest.raises(runner.ManifestContractError, match='fields changed'):
        runner.validate_runner_contract(stage, doc)
    assert not (stage/'evidence').exists()


@pytest.fixture
def production_readback(production_constructor, monkeypatch):
    """Real derived class/checkpoint/diagnostics; stub only guest transport."""
    runner, base, stage, doc = production_constructor
    instance = runner.make_runner(base)(stage, doc)
    calls = []
    def configure(outcome):
        calls.clear()
        def copy(local, remote, download=False, *, required=True):
            assert download is True and required is False
            assert remote.startswith('/root/storage-production/') and remote.endswith('.json')
            calls.append(('copy', remote))
            if outcome in ('collected', 'corrupt'):
                local.write_text('{broken' if outcome == 'corrupt' else '{"synthetic":true}')
                return 0
            return 1
        def guest(label, script, *, required=True):
            assert label.startswith('probe-production-') and required is False
            assert 'then exit 0;' in script and 'then exit 3;' in script and 'else exit 4;' in script
            calls.append(('probe', label))
            return {'absent':3, 'transport':0, 'probe':255}[outcome]
        monkeypatch.setattr(instance, 'copy', copy)
        monkeypatch.setattr(instance, 'guest', guest)
        return calls
    return runner, instance, configure


@pytest.mark.parametrize('outcome', ['collected', 'optional-absent', 'required-absent', 'transport', 'probe', 'corrupt'])
def test_genuine_collect_json_checkpoint_and_report_classification(production_readback, outcome):
    runner, instance, configure = production_readback
    calls = configure('absent' if outcome.endswith('-absent') else outcome)
    required = outcome != 'optional-absent'
    if outcome == 'collected':
        assert instance.collect_json('online', required=required) == {'synthetic':True}
    elif outcome == 'optional-absent':
        assert instance.collect_json('online', required=False) is None
    else:
        error = json.JSONDecodeError if outcome == 'corrupt' else AssertionError
        with pytest.raises(error): instance.collect_json('online', required=required)
    row = json.loads((instance.evidence/'receipt.json').read_text())['checkpoints'][-1]
    expected = {'collected':'collected', 'optional-absent':'missing_optional',
                'required-absent':'missing_expected', 'transport':'transport_error',
                'probe':'probe_error', 'corrupt':'collection_error'}
    assert row['name'] == 'guest-readback-online' and row['artifact_name'] == 'online'
    assert row['boot'] == 0 and row['status'] == expected[outcome]
    assert len(calls) == (1 if outcome in ('collected', 'corrupt') else 2)
    if outcome == 'collected':
        assert row['sha256'] == runner.digest(instance.evidence/'0-online.json')
    if outcome == 'corrupt': assert row['error_type'] == 'JSONDecodeError'


@pytest.mark.parametrize('outcome', ['collected', 'absent', 'transport', 'probe', 'corrupt'])
def test_genuine_diagnostics_preserves_first_error_and_optional_absence(production_readback, outcome):
    _, instance, configure = production_readback
    configure(outcome)
    first = {'type':'AssertionError', 'reason':'fixed phase failed: focused-tests'}
    instance.receipt['first_error'] = copy.deepcopy(first)
    instance.receipt['boots'].append({'maintenance':False})
    instance.proc = SimpleNamespace(poll=lambda: None)
    instance.diagnostics()
    saved = json.loads((instance.evidence/'receipt.json').read_text())
    assert saved['first_error'] == first and saved['passed'] is False
    names = ['original-agent', 'online', 'return-normal', 'installed', 'stale-mapping']
    assert [r['name'] for r in saved['checkpoints']] == ['guest-readback-'+n for n in names]
    assert [r['artifact_name'] for r in saved['checkpoints']] == names
    errors = saved['diagnostic_errors']
    if outcome in ('collected', 'absent'):
        assert errors == []
    else:
        assert [r['name'] for r in errors] == names
        assert {r['error_type'] for r in errors} == {'JSONDecodeError' if outcome == 'corrupt' else 'AssertionError'}
    assert saved['cleanup_errors'] == []


@pytest.mark.parametrize('tag', ['!override', '!reset'])
def test_actual_guest_compose_assertions_preserve_override_tag(public_mapping, tag):
    from wam import network
    st, _, _ = public_mapping
    state = '/var/lib/wizard-os/.wizard-os'
    text = privsep.service_override(st, state).replace('volumes: !override', 'volumes: '+tag)
    tree = ast.parse((REPO/'qa/privsep-storage/production_guest.py').read_text())
    installed = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'installed')
    # Execute only the genuine finite Compose observation, before any guest
    # identity, Docker, HTTP or real installed-state access.
    start = next(i for i,n in enumerate(installed.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'doc')
    end = next(i for i,n in enumerate(installed.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'uid')
    code = compile(ast.Module(body=installed.body[start:end], type_ignores=[]), 'guest-compose-observation', 'exec')
    scope = {'network':network, 'privsep':SimpleNamespace(service_override=lambda *args: text),
             'st':st, 'state':state, 'physical':'/var/lib/wizard-agent-homes/1234'}
    if tag == '!override': exec(code, scope)
    else:
        with pytest.raises(AssertionError): exec(code, scope)


@pytest.mark.parametrize('phase', ['validate-manifest-and-inherited-contract', 'private-temporary-directory',
                                  'genuine-production-runner-constructor'])
def test_startup_failure_has_exclusive_bounded_diagnostic_receipt(production_constructor, phase):
    runner, _, stage, _ = production_constructor
    row = runner.startup_failure(stage, phase, RuntimeError('never persist arbitrary exception payload'))
    saved = json.loads((stage/'startup-failure.json').read_text())
    assert saved == row and saved['phase'] == phase and saved['passed'] is False
    assert saved['execution_started'] is False and saved['first_error']['reason'] == 'fixed setup exception'
    assert (stage/'startup-failure.json').stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError): runner.startup_failure(stage, phase, RuntimeError())
    assert json.loads((stage/'startup-failure.json').read_text()) == row
    assert not (stage/'evidence').exists()
