#!/usr/bin/env python3
"""Browser-free tests of refresh custody only; security verifiers stay unchanged."""
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import refresh

class CarrySecurity(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.prior=self.base/'prior';self.root=self.base/'new'
        self.prior.mkdir();self.root.mkdir();(self.prior/'inherited/r10').mkdir(parents=True)
        self.payload=self.prior/'inherited/r10/receipt.json';self.payload.write_text('synthetic historical failure\n')
        (self.prior/'security-inheritance.json').write_text('{}\n')
        inputs={str(p.relative_to(self.prior)):refresh.sha(p.read_bytes()) for p in self.prior.rglob('*') if p.is_file()}
        (self.prior/'manifest.json').write_text(json.dumps({'inputs':inputs}))
        self.pin='a'*64
        self.evidence={'prior_aggregate_accepted':False,'current_ui_inherited':False,'prior_receipt_sha256':self.pin,'fixture':{'check_count':43},'source_regressions':{'test_count':11}}
    def run_carry(self,**kwargs):
        with patch('inherit_security.verify',**kwargs) as verify:
            result=refresh.carry_security(self.root,self.prior,self.pin)
            self.assertEqual([call.args[0] for call in verify.call_args_list],[self.prior,self.root])
        return result
    def test_identical_chain_preserves_failed_aggregate_and_no_ui(self):
        result=self.run_carry(return_value=self.evidence)
        self.assertTrue(result['inheritance_verified'])
        self.assertIs(result['prior_aggregate_accepted'],False)
        self.assertIs(result['current_ui_inherited'],False)
        self.assertEqual((self.root/'inherited/r10/receipt.json').read_bytes(),self.payload.read_bytes())
        self.assertFalse((self.root/'results-candidate').exists())
    def test_wrong_parent_receipt_pin_rejected_before_copy(self):
        wrong={**self.evidence,'prior_receipt_sha256':'b'*64}
        with self.assertRaisesRegex(AssertionError,'parent pin'):
            self.run_carry(return_value=wrong)
        self.assertEqual(list(self.root.iterdir()),[])
    def test_prior_input_mutation_rejected_before_verifier(self):
        self.payload.write_text('mutated\n')
        with patch('inherit_security.verify') as verify:
            with self.assertRaisesRegex(AssertionError,'prior capsule input drift'):
                refresh.carry_security(self.root,self.prior,self.pin)
            verify.assert_not_called()
        self.assertEqual(list(self.root.iterdir()),[])
    def test_changed_destination_evidence_rejected(self):
        changed=copy.deepcopy(self.evidence);changed['current_ui_inherited']=True
        with self.assertRaisesRegex(AssertionError,'components differ'):
            self.run_carry(side_effect=[self.evidence,changed])

if __name__=='__main__':unittest.main()
