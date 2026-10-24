"""Mac-only executable supervision overlay; absent manifest leaves WAM unchanged."""
import contextlib,fcntl,json,os,re,uuid
from pathlib import Path
import yaml
from . import registry
MANIFEST=Path('/opt/wizard/managed-runtime.json')
STATE='/var/lib/wizard/managed'
def configuration():
 if not MANIFEST.exists():return None
 st=MANIFEST.lstat()
 if MANIFEST.is_symlink() or st.st_uid!=0 or st.st_mode&0o022:raise RuntimeError('unsafe managed-runtime manifest')
 data=json.loads(MANIFEST.read_text())
 if not re.fullmatch(r'sha256:[a-f0-9]{64}',data.get('image','')):raise RuntimeError('unknown managed runtime image')
 if data.get('stock')!='f97608f178d1ffeca59860195ab7da295f7c8e5f':raise RuntimeError('unknown stock runtime contract')
 return data

@contextlib.contextmanager
def operation():
 """Own an actual flock for the entire import/undo, not an env assertion.
 Admission closes atomically against the supervisor's operation inventory.
 """
 if not configuration():
  yield None;return
 root=Path(STATE);reservation=None;path=None
 try:
  with (root/'admission.lock').open('r+') as gate:
   fcntl.flock(gate,fcntl.LOCK_EX)
   if (root/'lease.json').exists() or (root/'resume.json').exists():raise RuntimeError('maintenance_active')
   path=root/'operations'/str(uuid.uuid4())
   reservation=path.open('x+');fcntl.flock(reservation,fcntl.LOCK_EX)
  yield reservation
 finally:
  if reservation and path is not None:
   # Unlink while locked; a supervisor that already opened it still sees busy
   # until close. On a crash the kernel releases ownership automatically.
   path.unlink(missing_ok=True);reservation.close()

def render(compose,engine):
 """Transform the owned template before its atomic ownership-checked write.
 Preserve its ownership marker and compare the final output for idempotence.
 """
 cfg=configuration()
 if not cfg:return compose
 data=yaml.safe_load(compose)
 service=data.setdefault('services',{}).setdefault(engine,{})
 service.update({'image':cfg['image'],'pull_policy':'never'})
 mounts=service.setdefault('volumes',[]);mount=STATE+':/wizard-managed-state'
 if mount not in mounts:mounts.append(mount)
 comments='\n'.join(line for line in compose.splitlines() if line.startswith('#'))
 return comments+'\n'+yaml.safe_dump(data,sort_keys=False)
