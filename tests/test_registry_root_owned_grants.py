"""Registry rebuild under daemon-owned mode-0600 state: API, not chmod."""
import ast,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from wam import cli

class RegistryGrantsTests(unittest.TestCase):
 def invoke(self,grants=None,fail=None):
  tree=ast.parse(Path(cli.__file__).read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='write_registry')
  nodes=[]
  for n in fn.body:
   if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='grants_file' for t in n.targets):nodes.append(n)
   if isinstance(n,ast.Try):nodes.append(n);break
  code=compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'actual-registry-grants','exec')
  with tempfile.TemporaryDirectory() as tmp:
   f=Path(tmp)/'state/wizard-grants.json';f.parent.mkdir();f.write_text('{}');rt=Mock();rt.req.return_value=grants
   if fail:rt.req.side_effect=fail
   ns={'c':SimpleNamespace(runtipi_root=Path(tmp)),'rt':rt,'reg':{},'json':json}
   with patch.object(Path,'read_text',side_effect=PermissionError('daemon-owned fixture')):exec(code,ns)
   return ns['reg'],rt
 def test_authenticated_grants_used_without_filesystem_mutation(self):
  grants=[{'kind':'share','from':'provider','to':'consumer','share':'example'}];r,rt=self.invoke(grants);self.assertEqual(r['grants'],grants);rt.req.assert_called_once_with('GET','/apps/wizard/grants')
 def test_unexpected_authenticated_shape_fails_closed(self):
  for value in [{},None,['not-a-grant']]:
   with self.assertRaises(ValueError):self.invoke(value)
 def test_auth_or_endpoint_error_does_not_drop_grants(self):
  with self.assertRaises(RuntimeError):self.invoke(fail=RuntimeError('401 fixture'))
if __name__=='__main__':unittest.main()
