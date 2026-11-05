#!/usr/bin/env python3
"""Synthetic oracle regressions; --static-only never imports or runs the fixture."""
import ast,copy,io,json,sys,unittest
from pathlib import Path
HERE=Path(__file__).resolve().parent

def static_contract():
    source=(HERE/'forum_qa.py').read_text();tree=ast.parse(source)
    funcs={n.name:n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    fixture=ast.unparse(funcs['fixture'])
    probe=ast.unparse(funcs['probe'])
    pair=ast.unparse(funcs['redaction_pair'])
    checks={
        'explicit_client_proof_and_control':'PROBE_HEADERS.items()' in fixture and "'X-Wizard-Owner-Proof': PROOF" in ast.unparse(tree) and "'X-Redaction-Control': CONTROL" in ast.unparse(tree),
        'identical_wire_used_for_both_requests':'sock.sendall(probe_wire)' in probe and fixture.count('probe() == 502')==2,
        'unfiltered_then_exact_candidate_encoder':"config.write_text(unfiltered)" in fixture and "reload_config(filtered, 'encoder reload')" in fixture and "candidate[candidate.index('    log default {'):candidate.index('    admin off')]" in fixture,
        'real_no_encoder_negative':"redaction_pair(before, before)" in fixture,
        'unfiltered_positive_required':"error_headers(before).get('x-wizard-owner-proof') == [PROOF]" in pair,
        'control_and_event_identity_required':"event.get('status') == 502" in pair and "event.get('logger') == 'http.log.error'" in pair and "get('uri') == '/error-probe'" in pair and "get('x-redaction-control') == [CONTROL]" in pair,
        'independent_producer_receipt_before_failure':fixture.index("req('/producer-probe')[0] == 200") < fixture.index("producer.shutdown()") < fixture.index("req('/producer-probe')[0] == 502") and "producer-upstream-receipt.json" in fixture,
        'metadata_guards_retained':all(x in fixture for x in ['new shell refuses stale carrier','forged non-owner refused','foreign Origin refused','cross-site refused','query refused','encoded child path refused','metadata after reload']),
        'same_pid_positive_reload_and_cleanup':"successfully reloaded config from file" in fixture and 'proc.pid == old' in fixture and 'thread.join(timeout=3)' in fixture,
    }
    assert all(checks.values()),'static contract failed: '+','.join(k for k,v in checks.items() if not v)
    return checks

class RedactionOracle(unittest.TestCase):
    def setUp(self):
        # Explicitly synthetic UNIT data, not a substitute for native Caddy output.
        import forum_qa as qa
        self.qa=qa
        self.before={'logger':'http.log.error','status':502,'request':{'method':'GET','uri':'/error-probe','host':'127.0.0.1:1234','proto':'HTTP/1.1','headers':{'X-Wizard-Owner-Proof':[qa.PROOF],'X-Redaction-Control':[qa.CONTROL],'Accept-Encoding':['identity']}}}
        self.after=copy.deepcopy(self.before);del self.after['request']['headers']['X-Wizard-Owner-Proof']
    def rejected(self,before=None,after=None):
        with self.assertRaises(AssertionError):self.qa.redaction_pair(self.before if before is None else before,self.after if after is None else after)
    def test_valid_pair(self):self.assertTrue(self.qa.redaction_pair(self.before,self.after))
    def test_no_encoder_fails(self):self.rejected(after=self.before)
    def test_absent_server_mutation_not_redaction(self):self.rejected(before=self.after)
    def test_missing_nonsecret_control_fails(self):
        del self.after['request']['headers']['X-Redaction-Control'];self.rejected()
    def test_proof_value_elsewhere_fails(self):self.after['extra']=self.qa.PROOF;self.rejected()
    def test_lowercase_proof_key_fails(self):self.after['request']['headers']['x-wizard-owner-proof']=['other'];self.rejected()
    def test_missing_actual_error_fails(self):self.rejected(after={})
    def test_wrong_status_fails(self):self.after['status']=200;self.rejected()
    def test_wrong_path_fails(self):self.after['request']['uri']='/neighbor';self.rejected()
    def test_nonproof_header_change_fails(self):self.after['request']['headers']['Accept-Encoding']=['gzip'];self.rejected()
    def test_runner_contract(self):self.assertTrue(all(static_contract().values()))

if __name__=='__main__':
    if sys.argv[1:]==['--static-only']:
        print(json.dumps({'static_only':True,'checks':static_contract(),'runtime_unexecuted':True}))
    else:
        assert not sys.argv[1:]
        suite=unittest.defaultTestLoader.loadTestsFromTestCase(RedactionOracle)
        names=[t.id() for t in suite];stream=io.StringIO()
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
        report={'accepted':result.wasSuccessful() and result.testsRun==len(names) and not result.skipped,'test_count':result.testsRun,'expected_names':names,'skipped':len(result.skipped),'failures':len(result.failures),'errors':len(result.errors),'synthetic_unit_only':True}
        print(json.dumps(report));sys.exit(0 if report['accepted'] else 1)
