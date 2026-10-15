"""Real cross-process flock admission for managed one-off transactions."""
import fcntl,json,os,tempfile,unittest
from pathlib import Path
from wam import managed_runtime as m
class Operation(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.before=(m.MANIFEST,m.STATE);m.MANIFEST=self.root/'manifest.json';m.STATE=str(self.root/'state')
  m.MANIFEST.write_text(json.dumps({'image':'sha256:'+'a'*64,'stock':'f97608f178d1ffeca59860195ab7da295f7c8e5f'}))
  Path(m.STATE).mkdir();(Path(m.STATE)/'operations').mkdir();(Path(m.STATE)/'admission.lock').touch()
 def tearDown(self):m.MANIFEST,m.STATE=self.before;self.tmp.cleanup()
 def test_real_owned_lifetime(self):
  with m.operation() as reservation:
   self.assertFalse(reservation.closed)
   pid=os.fork()
   if pid==0:
    reservation.close()
    with open(reservation.name) as reader:
     try:fcntl.flock(reader,fcntl.LOCK_EX|fcntl.LOCK_NB)
     except BlockingIOError:os._exit(0)
     os._exit(1)
   self.assertEqual(os.waitpid(pid,0)[1],0)
  self.assertTrue(reservation.closed);self.assertEqual(list((Path(m.STATE)/'operations').iterdir()),[])
 def test_update_and_recovery_refuse_before_reservation(self):
  for name in ['lease.json','resume.json']:
   p=Path(m.STATE)/name;p.write_text('{}')
   with self.assertRaisesRegex(RuntimeError,'maintenance_active'):
    with m.operation():self.fail('admitted')
   self.assertEqual(list((Path(m.STATE)/'operations').iterdir()),[]);p.unlink()
 def test_unmanaged_unchanged(self):
  m.MANIFEST.unlink()
  with m.operation() as reservation:self.assertIsNone(reservation)
if __name__=='__main__':unittest.main()
