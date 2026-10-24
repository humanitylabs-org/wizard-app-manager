import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from wam import fresh_agent,managed_runtime

class ManagedOverlayTests(unittest.TestCase):
 def test_owned_marker_and_idempotence(self):
  cfg={'image':'sha256:'+'1'*64}
  with tempfile.TemporaryDirectory() as root,patch.object(managed_runtime,'configuration',return_value=cfg):
   compose=managed_runtime.render(fresh_agent.AGENT_OVERLAY.format(engine=fresh_agent.ENGINE,os_home=fresh_agent.CONTAINER_OS_HOME,agent_ssh=''),fresh_agent.ENGINE)
   self.assertTrue(compose.startswith(fresh_agent.MARK))
   self.assertEqual(managed_runtime.render(compose,fresh_agent.ENGINE),compose)
   self.assertTrue(fresh_agent._write_overlay(Path(root),'test','agent',compose,{}))
   self.assertFalse(fresh_agent._write_overlay(Path(root),'test','agent',compose,{}))
 def test_unowned_overlay_is_not_adopted(self):
  with tempfile.TemporaryDirectory() as root:
   p=Path(root)/'user-config/test/agent/docker-compose.yml';p.parent.mkdir(parents=True);p.write_text('services: {}\n')
   with self.assertRaises(fresh_agent.FreshError):fresh_agent._write_overlay(Path(root),'test','agent','# Written by wam\nservices: {}\n',{})
 def test_absent_manifest_leaves_stock_unchanged(self):
  with patch.object(managed_runtime,'configuration',return_value=None):self.assertEqual(managed_runtime.render('services: {}\n','engine'),'services: {}\n')
