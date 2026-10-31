"""Retained-engine replay is real bytes; mocked transactions are unit tests only."""
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manager_seal_installed_path as rig
import manager_seal_update_continuation as update

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent/'fixtures/manager-seal-accepted-c8f08cf36f7f'
PINS = {'ledger.jsonl': 'a6eebb315d91bbfcae3b21fb983cbaadf76feab37a6fa8bffab2cf2f8679fb38',
        'receipt.json': '38bff50dbcc952be0af05bf63979b4b8f9de60ad5457c9ea14fc00d2ec2e6dd6',
        'events.json': '21d32ccbf114c4dc5c45c9ac495d0e3947625be1cc6bb9f8517ee7c48ec44ffb',
        'cleanup.json': '47216d8be86db77b54916e07e579d21105d55f74a8016ec5eab3d5840ce25f83'}


class AcceptedBytes(unittest.TestCase):
    def setUp(self):
        for name, sha in PINS.items():
            self.assertEqual(hashlib.sha256((FIXTURE/name).read_bytes()).hexdigest(), sha)
        self.receipt = json.loads((FIXTURE/'receipt.json').read_bytes())
        self.rows = [json.loads(line) for line in (FIXTURE/'ledger.jsonl').read_bytes().splitlines()]
        self.events = json.loads((FIXTURE/'events.json').read_bytes())
        self.probes = [r for r in self.rows if r['operation'] == 'deny' and r.get('reason') != 'prune_refused']
        self.names = [rig.PROJECT+'_'+n for n in rig.NETS]

    def account(self, rows=None, cycles=2):
        return rig.reconcile_guard_accounting(self.rows if rows is None else rows, 9, self.names,
            expected_cycles=cycles, install_prune_window=self.receipt['install_guard_sequence_window'],
            expected_probe_rows=self.probes)

    def test_retained_first_slice_still_exactly_accepted(self):
        self.assertEqual(self.account(), self.receipt['guard_accounting'])
        result = rig.reconcile_runtime_events(self.events, self.rows,
            set(self.receipt['first_runtime_ids']+self.receipt['restarted_runtime_ids']), 'msi-c8f08cf36f7f')
        self.assertEqual(result, self.receipt['real_runtime_events'])
        self.assertTrue(self.receipt['first_installed_synthetic_path_pass'])
        self.assertFalse(self.receipt['single_accepted_update_pass'])
        self.assertEqual(self.receipt['cleanup_errors'], [])
        self.assertEqual(json.loads((FIXTURE/'cleanup.json').read_bytes())['parent_cleanup_residual'], [])

    def test_first_slice_is_not_three_cycles(self):
        with self.assertRaises(AssertionError): self.account(cycles=3)

    def test_explicit_third_network_cycle_accounting_only_not_engine_proof(self):
        rows = copy.deepcopy(self.rows)
        # Deliberately fabricated unit input, never stored or claimed as engine evidence.
        cycle = [r for r in self.rows if r.get('kind') == 'network' and r['operation'] in ('discovery-miss', 'creation-intent', 'created')][:12]
        self.assertEqual(len(cycle), 12)
        for row in copy.deepcopy(cycle):
            row['sequence'] = len(rows)+1; rows.append(row)
        self.assertEqual(self.account(rows, 3)['paired_network_creations'], 12)
        with self.assertRaises(AssertionError): self.account(rows, 2)

    def test_extra_refusal_update_is_never_exempted(self):
        rows = copy.deepcopy(self.rows)
        rows.append({'sequence':len(rows)+1,'operation':'deny','reason':'rule_refused'})
        with self.assertRaises(AssertionError): self.account(rows)

    def test_second_prune_not_allowed_for_update(self):
        rows = copy.deepcopy(self.rows)
        row = copy.deepcopy(next(r for r in rows if r.get('reason') == 'prune_refused'))
        row['sequence'] = len(rows)+1; rows.append(row)
        with self.assertRaises(AssertionError): self.account(rows)

    def test_event_pair_missing_fails(self):
        events = copy.deepcopy(self.events)
        events = [e for e in events if not (e['Type']=='network' and e.get('Action')=='connect')]
        with self.assertRaises(AssertionError):
            rig.reconcile_runtime_events(events, self.rows,
                set(self.receipt['first_runtime_ids']+self.receipt['restarted_runtime_ids']), 'msi-c8f08cf36f7f')


class UpdateSequence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name); self.previous = [format(i, '064x') for i in range(1,6)]
        self.ids = [format(i, '064x') for i in range(10,15)]
        self.log = self.out/'manager.sanitized.log'; self.log.write_text('prior startup log\n')
        self.request = '968a12a8-ea68-41e1-a2fd-85186cc1c9a2'
        self.stage_result = {'urn':update.URN, 'sha256':'b'*64, 'previous_sha256':'a'*64}
        self.readback = {'sha256':'b'*64,'previous_sha256':'a'*64,'installed_version':2,'revision_command':update.COMMAND_V2}
        self.rig = Mock(out=self.out, manager='owned-manager', receipt={'trusted_update_stage': self.stage_result})
        self.rig.sql.return_value = 'running|2'
        self.rig.owned_fixture_ids.return_value = self.ids
        self.rig.runtime.return_value = self.ids
        self.rig.exec.return_value = json.dumps(self.readback)
        self.rig.http.side_effect = [(202, {'requestId':self.request}), (200, {'app':{'status':'running','version':2}})]
        def wait(label, predicate, seconds):
            with self.log.open('a') as f:
                f.write('Running docker compose with args --project-name agent-synthetic_migrated -f /data/state/mandatory-seal-launches/launch-AbCd12/effective.json up --detach --force-recreate --remove-orphans\n')
            self.assertTrue(predicate())
        self.rig.wait.side_effect = wait

    def test_one_patch_then_auth_db_policy_and_runtime_readbacks(self):
        self.assertEqual(update.transaction(self.rig, self.previous), self.ids)
        self.assertEqual(self.rig.http.call_args_list[0].args,
            ('PATCH','/api/app-lifecycle/'+update.URN+'/update',{'performBackup':False}))
        self.assertEqual([c.args[0] for c in self.rig.http.call_args_list], ['PATCH','GET'])
        self.rig.runtime.assert_called_once_with(update.COMMAND_V2)
        self.assertEqual(self.rig.receipt['request_ids'], [self.request])
        self.assertTrue(self.rig.receipt['accepted_update']['effective_snapshot_launch'])
        self.assertNotIn('single_accepted_update_pass', self.rig.receipt)  # set only after event+ledger audit

    def test_rejection_does_not_retry(self):
        self.rig.http.side_effect = [(403, {'message':'refused'})]
        with self.assertRaises(AssertionError): update.transaction(self.rig, self.previous)
        self.assertEqual(self.rig.http.call_count, 1); self.rig.wait.assert_not_called()

    def test_transport_ambiguous_does_not_retry(self):
        self.rig.http.side_effect = RuntimeError('ambiguous')
        with self.assertRaises(RuntimeError): update.transaction(self.rig, self.previous)
        self.assertEqual(self.rig.http.call_count, 1); self.rig.wait.assert_not_called()

    def test_wait_timeout_never_retries_patch(self):
        self.rig.wait.side_effect = RuntimeError('terminal-update deadline')
        with self.assertRaisesRegex(RuntimeError, 'deadline'): update.transaction(self.rig, self.previous)
        self.assertEqual(self.rig.http.call_count, 1); self.rig.runtime.assert_not_called()

    def test_invalid_request_id_does_not_poll_or_retry(self):
        self.rig.http.side_effect = [(202, {'requestId':'invalid'})]
        with self.assertRaises(ValueError): update.transaction(self.rig, self.previous)
        self.assertEqual(self.rig.http.call_count, 1); self.rig.wait.assert_not_called()

    def test_terminal_recovered_old_revision_aborts_early(self):
        self.rig.sql.return_value = 'running|1'
        with self.assertRaisesRegex(RuntimeError, 'terminal update failure'): update.transaction(self.rig, self.previous)
        self.assertEqual([c.args[0] for c in self.rig.http.call_args_list], ['PATCH','GET'])
        self.assertTrue((self.out/'terminal-update-failure.json').exists()); self.rig.runtime.assert_not_called()

    def test_failure_log_aborts_even_if_db_claims_running_v2(self):
        def fail(*args):
            with self.log.open('a') as f: f.write('Failed to update app '+update.URN+': synthetic\n')
            return args[1]()
        self.rig.wait.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'terminal update failure'): update.transaction(self.rig, self.previous)
        self.rig.runtime.assert_not_called()

    def test_nonterminal_updating_polls_only_reads(self):
        self.rig.sql.return_value = 'updating|1'
        self.assertFalse(update.ready(self.rig, set(self.previous), 0, self.request))
        self.rig.http.assert_not_called(); self.rig.owned_fixture_ids.assert_not_called()

    def test_new_version_old_runtime_not_ready(self):
        self.rig.owned_fixture_ids.return_value = self.previous
        self.assertFalse(update.ready(self.rig, set(self.previous), 0, self.request))
        self.rig.http.assert_not_called()

    def test_unexpected_database_state_fails_closed(self):
        self.rig.sql.return_value = 'running|3'
        with self.assertRaises(AssertionError): update.ready(self.rig, set(self.previous), 0, self.request)

    def test_http_old_version_fails(self):
        self.rig.http.side_effect = [(202, {'requestId':self.request}), (200, {'app':{'status':'running','version':1}})]
        with self.assertRaises(AssertionError): update.transaction(self.rig, self.previous)
        self.rig.runtime.assert_not_called()

    def test_old_seal_readback_fails(self):
        self.rig.exec.return_value = json.dumps(dict(self.readback,sha256='a'*64))
        with self.assertRaises(AssertionError): update.transaction(self.rig, self.previous)

    def test_effective_path_required_not_optional_user_config(self):
        self.rig.wait.side_effect = lambda *a: (self.log.write_text('Running docker compose with args -f /data/user-config/evil.json up --detach --force-recreate --remove-orphans\n'), a[1]())
        with self.assertRaises(AssertionError): update.transaction(self.rig, self.previous)

    def test_trusted_review_stage_publish_order_and_cas(self):
        self.rig.exec.side_effect = [json.dumps({'reviewed':True,'mode':'candidate','expected_sha256':'a'*64}),
            json.dumps(self.stage_result),json.dumps({'sha256':'b'*64,'previous_sha256':'a'*64})]
        self.assertEqual(update.stage(self.rig,'10.0.0.0/24'), self.stage_result)
        calls = [c.args[1] for c in self.rig.exec.call_args_list]
        self.assertEqual([c[2] for c in calls], ['candidate','stage','publish'])
        self.assertEqual(calls[1][-1], 'a'*64)
        self.assertNotIn('user-config', json.dumps(calls)); self.rig.http.assert_not_called()

    def test_failed_stage_never_publishes_or_patches(self):
        self.rig.exec.side_effect = [json.dumps({'reviewed':True,'mode':'candidate','expected_sha256':'a'*64}),RuntimeError('refused')]
        with self.assertRaises(RuntimeError): update.stage(self.rig,'10.0.0.0/24')
        self.assertEqual(self.rig.exec.call_count,2); self.rig.http.assert_not_called()

    def test_wrong_previous_sha_never_publishes(self):
        self.rig.exec.side_effect = [json.dumps({'reviewed':True,'mode':'candidate','expected_sha256':'a'*64}),
            json.dumps(dict(self.stage_result, previous_sha256='c'*64))]
        with self.assertRaises(AssertionError): update.stage(self.rig,'10.0.0.0/24')
        self.assertEqual(self.rig.exec.call_count,2)


class SourceAndCapture(unittest.TestCase):
    def test_reuse_count_recomputed_when_existing_review_helper_becomes_qa(self):
        prior = {'files': {'scripts/manager_seal_installed_review.ts':'qa', 'packages/backend/main.ts':'product'}}
        exceptions = {'scripts/manager_seal_installed_review.ts', 'scripts/new-qa.py'}
        reuse = {'unchanged_export_files':1, 'qa_exceptions':sorted(exceptions),
                 'full_non_qa_export_equal':True, 'backend_rebuilt':False}
        self.assertEqual(update.assert_reuse_closure(reuse, prior, exceptions),1)
        with self.assertRaisesRegex(AssertionError, 'baseline count drift'):
            update.assert_reuse_closure(dict(reuse,unchanged_export_files=2),prior,exceptions)

    def test_reuse_wildcard_and_product_exceptions_refused(self):
        for exception in ('scripts/*.py', 'packages/backend/main.ts'):
            with self.assertRaisesRegex(AssertionError, 'QA exception scope widened'):
                update.assert_reuse_closure({}, {'files':{}}, {exception})

    def test_source_pinned_missing_anchor_transition_and_grant_fixture(self):
        result = update.source_contract(ROOT)
        self.assertFalse(result['rollback_transaction_prepared'])
        self.assertFalse(result['grant_revocation_prepared'])
        self.assertEqual(result['source_pins']['packages/backend/src/modules/docker/mandatory-seal.ts']['lines']['if (prior.previous) throw fail();'],[144])
        self.assertEqual(len(result['source_pins']),9)
        build = inspect.getsource(rig.inside_build)
        self.assertNotIn('wizard.md', build)
        self.assertIn("if (!consumer) throw new Error('Consumer not installed');", (ROOT/'packages/backend/src/modules/docker/wizard-storage.ts').read_text())

    def test_patch_response_is_saved_before_status_assertion(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r = rig.Rig.__new__(rig.Rig); r.out=Path(tmp); r.receipt={}; r.secrets=['secret']; r.manager='owned-manager'
            r.exec=Mock(return_value=json.dumps({'status':403,'cookie':None,'body_text':'{"message":"secret"}',
                'captured_bytes':20,'body_truncated':False,'transport_error':None}))
            with self.assertRaises(AssertionError): r.http('PATCH','/api/app-lifecycle/'+update.URN+'/update', {'performBackup':False})
            saved=(r.out/'http-responses.sanitized.json').read_text()
            self.assertNotIn('secret',saved); self.assertIn('403',saved); self.assertIn('PATCH',saved)
            self.assertEqual(r.exec.call_count,1)

    def test_real_http_code_supplies_origin_for_patch(self):
        self.assertIn("['POST','PUT','PATCH','DELETE'].includes(method)", rig.recovery.HTTP_CODE)
        self.assertIn("redirect:'error'",rig.recovery.HTTP_CODE)

    def test_live_stream_spans_single_update_and_flags_wait_for_reconciliation(self):
        source=inspect.getsource(rig.Rig.run)
        self.assertLess(source.index("self.transaction('restart'"),source.index('update_continuation.stage('))
        self.assertLess(source.index('update_continuation.transaction('),source.index('stream.drain('))
        self.assertLess(source.index("self.receipt['real_runtime_events']"),source.index("self.receipt['single_accepted_update_pass'] = True"))
        self.assertIn('expected_cycles=3',source)
        self.assertIn('== 15',source)


if __name__ == '__main__': unittest.main()
