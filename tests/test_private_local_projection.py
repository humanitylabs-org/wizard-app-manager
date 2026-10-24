"""Private-local registry projection: real installed rows, synthetic data only."""
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from wam import registry

URN='wizard-tinfoil_private:wizardapps'
class PrivateLocalProjectionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.meta={'w':{'id':'wizard-tinfoil','name':'Tinfoil','private_inference':{'kind':'attested-relay','relay_port':8090},'provides':['private-inference']},'cfg':{},'dir':self.root,'repo':'local:wizard-tinfoil_private','private':True,'commit':'a'*40}
  self.rows=[{'info':{'urn':URN},'app':{'status':'running'}}]
 def build(self,rows=None,meta=None):
  with patch.object(registry,'app_meta',return_value=self.meta if meta is None else meta):
   return registry.build(self.rows if rows is None else rows,'wizardapps','localhost',self.root)
 def test_projects_canonical_capability_and_retains_lifecycle_urn(self):
  for _ in range(2):
   r=self.build();self.assertEqual(len(r['apps']),1);a=r['apps'][0]
   self.assertEqual(a['id'],'wizard-tinfoil');self.assertEqual(a['urn'],URN);self.assertEqual(a['private_inference'],self.meta['w']['private_inference'])
  self.assertEqual(self.rows[0]['info']['urn'],URN)
 def test_absent_row_never_projects_from_files(self):self.assertEqual(self.build(rows=[])['apps'],[])
 def test_provenance_and_capability_are_required(self):
  cases=[('private',False),('repo','https://example.invalid/foreign')]
  for k,v in cases:
   m=copy.deepcopy(self.meta);m[k]=v;self.assertEqual(self.build(meta=m)['apps'],[])
  for k,v in [('id','unrelated'),('private_inference',None),('private_inference',{'kind':'plaintext'})]:
   m=copy.deepcopy(self.meta);m['w'][k]=v;self.assertEqual(self.build(meta=m)['apps'],[])
 def test_foreign_store_and_unknown_suffixes_remain_rejected(self):
  for urn in ['wizard-tinfoil_private:otherstore','wizard-tinfoil_untrusted:wizardapps','../wizard-tinfoil_private:wizardapps']:
   self.assertEqual(self.build(rows=[{'info':{'urn':urn}}])['apps'],[])
 def test_alias_collision_is_fail_closed(self):
  with self.assertRaises(ValueError):self.build(rows=self.rows+[{'info':{'urn':'wizard-tinfoil:wizardapps'}}])
 def test_duplicate_installed_physical_rows_fail(self):
  with self.assertRaises(ValueError):self.build(rows=self.rows*2)
if __name__=='__main__':unittest.main()
