"""Mac-only executable supervision overlay; absent manifest leaves WAM unchanged."""
import json,os,re
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
