"""Read-only real-byte post-update replay; never a fresh installed acceptance."""
import ast
import copy
import hashlib
import inspect
import json
from pathlib import Path
import traceback
from types import SimpleNamespace

import manager_seal_installed_path as rig
import manager_seal_update_continuation as update

SOURCE = 'ece1410307f54e6c6b47f4b81088459d624b9dd4'


def prune_source_contract(root):
    checks = {
        'commands/update-app-command.ts': [
            'await this.ensureAppDir(appUrn, form, { pruneContainers: false, persistSubnet: false });',
            'await this.ensureAppDir(appUrn, form, { pruneContainers: false });'],
        'handlers/update-app.handler.ts': ['await this.updateConfigHandler.execute(appUrn, { form: app.config });'],
        'handlers/update-config.handler.ts': ["command: 'generate_env'"],
        'commands/generate-env-command.ts': ['await this.ensureAppDir(appUrn, form);'],
        'commands/command.ts': ['if (options.pruneContainers !== false)',
            '.pruneContainers({ filters: { label: [`runtipi.appurn=${appUrn}`] } })',
            '.catch(() => ({ ContainersDeleted: [], SpaceReclaimed: 0 }))'],
    }
    pins = {}
    for rel, tokens in checks.items():
        rel = 'packages/backend/src/modules/app-lifecycle/'+rel
        raw = (Path(root)/rel).read_bytes(); lines = raw.decode().splitlines()
        matches = {token: [i for i, line in enumerate(lines, 1) if token in line] for token in tokens}
        assert all(matches.values()), 'prune source contract drift'
        pins[rel] = {'sha256': hashlib.sha256(raw).hexdigest(), 'lines': matches}
    return {'source_pins': pins, 'cause': 'successful update handler awaits update-config -> generate_env -> ensureAppDir default prune; SDK refusal caught locally',
            'update_command_itself_disables_prune': True, 'product_changed': False}


def load(fixture):
    fixture = Path(fixture)
    provenance = json.loads((fixture/'SHA256.json').read_bytes())
    for name, pin in provenance.items():
        raw = (fixture/name).read_bytes()
        assert len(raw) == pin['bytes'] and hashlib.sha256(raw).hexdigest() == pin['sha256']
        assert pin['source_path'].startswith('/srv/work/manager-seal-installed-'+SOURCE[:12]+'/')
    receipt = json.loads((fixture/'receipt.json').read_bytes())
    assert receipt['source'] == SOURCE
    rows = [json.loads(line) for line in (fixture/'guard-ledger.jsonl').read_bytes().splitlines()]
    events = json.loads((fixture/'runtime-events.json').read_bytes())
    return receipt, rows, events


def tail(source):
    """Compile ALL actual statements from the failing assignment to run() end."""
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Rig')
    run = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
    start = next(i for i, n in enumerate(run.body) if isinstance(n, ast.Assign)
        and ast.unparse(n.targets[0]) == "self.receipt['guard_accounting']")
    body = run.body[start:]
    assert body[-1].targets[0].slice.value == 'phase'
    return ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), len(body)


def replay(root, fixture=None):
    root = Path(root)
    fixture = Path(fixture) if fixture else root/'scripts/tests/fixtures/manager-seal-update-ece1410307f5'
    historical, rows, events = load(fixture)
    assert len(rows) == 646
    assert historical['guard_ledger_sha256'] == hashlib.sha256((fixture/'guard-ledger.jsonl').read_bytes()).hexdigest()
    assert historical['runtime_events_sha256'] == hashlib.sha256((fixture/'runtime-events.json').read_bytes()).hexdigest()
    creates = [r for r in rows if r['operation'] == 'created' and r.get('kind') == 'container']
    first, second = [[r['id'] for r in creates[i:i+5]] for i in (0, 5)]
    third = historical['accepted_update']['runtime_ids']
    assert set(third) == {r['id'] for r in creates[10:]} and len(set(first+second+third)) == 15
    names = [rig.PROJECT+'_'+n for n in rig.NETS]
    probes = rows[2:11]
    assert len(probes) == 9 and all(r['operation'] == 'deny' for r in probes)
    lower, upper = historical['install_guard_sequence_window']
    # Old rig did not capture PATCH sequence bounds. This is an explicitly
    # derived historical phase boundary, NOT a claimed captured PATCH window:
    # its final restart runtime() inspects each second-cycle ID; seq401 is the
    # last response. The retained ledger ends after terminal update readbacks.
    inspections = [r['sequence']+1 for r in rows if r['operation'] == 'forward'
        and r.get('route') in {'/containers/'+cid+'/json' for cid in second}]
    update_lower, update_upper = max(inspections), len(rows)
    assert (update_lower, update_upper) == (401, 646)
    prefix = rows[:update_lower]
    assert rig.reconcile_guard_accounting(prefix, 9, names, 2, (lower, upper), probes) == historical['first_slice_guard_accounting']
    assert rig.reconcile_runtime_events(events, prefix, set(first+second), 'msi-'+SOURCE[:12]) == historical['first_slice_real_runtime_events']
    kwargs = dict(expected_denials=9, network_names=names, expected_cycles=3,
        install_prune_window=(lower, upper), expected_probe_rows=probes)
    old = (fixture/'old-installed-path.py').read_text()
    fn = next(n for n in ast.parse(old).body if isinstance(n, ast.FunctionDef) and n.name == 'reconcile_guard_accounting')
    ns = dict(vars(rig)); exec(compile(ast.Module(body=[fn], type_ignores=[]), 'retained-ece1410307f5', 'exec'), ns)
    try:
        ns['reconcile_guard_accounting'](rows, **kwargs)
    except AssertionError as error:
        frames = traceback.extract_tb(error.__traceback__)
        assert str(error) == 'expected exactly one install prune refusal' and frames[-1].lineno == 332
        old_failure = {'line': frames[-1].lineno, 'reason': str(error)}
    else:
        raise AssertionError('old accounting unexpectedly accepted')
    assert historical['failure']['frames'][-1]['line'] == 332
    assert historical['failure']['frames'][-2]['line'] == 1013
    old_tail, old_count = tail(old)
    new_tail, new_count = tail((root/'scripts/manager_seal_installed_path.py').read_text())
    # Prove no later statement/expectation was weakened: only the update-window
    # keyword differs across the complete old/new run tails.
    original = copy.deepcopy(new_tail)
    call = original.body[0].value
    call.keywords = [kw for kw in call.keywords if kw.arg != 'update_prune_window']
    assert ast.dump(original, include_attributes=False) == ast.dump(old_tail, include_attributes=False)
    assert new_count == old_count
    projected = SimpleNamespace(receipt=copy.deepcopy(historical), owner='msi-'+SOURCE[:12])
    env = dict(vars(rig), self=projected, creation_rows=rows, probes=probes,
        cfg={'network_names': names}, install_lower=lower, install_upper=upper,
        update_lower=update_lower, update_upper=update_upper, ledger=probes,
        events=events, first=first, second=second, third=third)
    exec(compile(new_tail, 'actual-current-complete-post-update-tail', 'exec'), env)
    result = projected.receipt
    assert result['single_accepted_update_pass'] and result['http_request_ids_distinct']
    assert result['guard_accounting']['update_prune_refusals'] == result['guard_accounting']['install_prune_refusals'] == 1
    assert result['real_runtime_events']['container_create_coverage'] == 15
    assert result['real_runtime_events']['connect_pair_coverage'] == 30
    assert result['guard_accounting']['paired_network_creations'] == 12
    assert result['guard_accounting']['network_discovery_misses'] == 12
    assert [t['action'] for t in historical['transactions']] == ['install', 'start', 'restart', 'update']
    accepted = historical['accepted_update']; seal = accepted['seal_readback']
    assert accepted['terminal_database'] == 'running|2' and accepted['actual_auth_http_readback'] and accepted['effective_snapshot_launch']
    assert seal['installed_version'] == 2 and seal['revision_command'] == update.COMMAND_V2
    assert seal['sha256'] == historical['trusted_update_stage']['sha256']
    assert update.terminal_markers((fixture/'manager.sanitized.log').read_text()) == (True, False)
    # Cleanup already executed in the real run. Audit its saved outcomes rather
    # than mocking daemon calls or creating/removing any runtime object offline.
    controller = [json.loads(line) for line in (fixture/'controller-ledger.jsonl').read_bytes().splitlines()]
    recorded = {(r['kind'], r['id']) for r in controller if 'id' in r}
    assert all((r['kind'], r['id']) in recorded for r in rows if r['operation'] == 'created')
    assert historical['cleanup_errors'] == []
    assert historical['reserve_after_cleanup_bytes'] >= rig.capacity.RESERVE
    cleanup = json.loads((fixture/'cleanup.json').read_bytes())
    assert cleanup['parent_cleanup_residual'] == [] and cleanup['free_bytes'] >= rig.capacity.RESERVE
    assert not cleanup['old_stages_deleted']
    assert historical['rabbit_limit_canary']['exclusive_holder_released']
    assert all(not state.get('OOMKilled') and not state.get('Dead') for state in historical['pre_cleanup_states'].values())
    for key in ('rollback_transaction_pass', 'full_acceptance', 'RUNTIPI_ADMITTED'):
        assert historical[key] is False and result[key] is False
    assert historical['single_accepted_update_pass'] is False
    return {'historical_source': SOURCE, 'old_code_failure': old_failure,
        'full_actual_run_tail_statements': new_count, 'full_tail_pass': True,
        'later_expectations_changed': [], 'guard_accounting': result['guard_accounting'],
        'runtime_events': result['real_runtime_events'], 'cleanup_saved_outcomes_pass': True,
        'controller_recorded_resources': len(recorded), 'derived_historical_update_window': [update_lower, update_upper],
        'historical_patch_window_was_not_captured': True, 'future_patch_window_capture_prepared': True,
        'counterfactual_tail_single_accepted_update_pass': True,
        'historical_receipt_single_accepted_update_pass': False, 'fresh_runtime_executed': False,
        'rollback_transaction_pass': False, 'full_acceptance': False, 'RUNTIPI_ADMITTED': False,
        'prune_product_source': prune_source_contract(root)}


if __name__ == '__main__':
    print(json.dumps(replay(Path(__file__).resolve().parents[1]), indent=2))
