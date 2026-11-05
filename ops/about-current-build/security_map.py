#!/usr/bin/env python3
"""Model-blind bounded proof-protocol map. Never print values, environ or raw logs."""
import hashlib,json,re,subprocess
from pathlib import Path

def sha(b):return hashlib.sha256(b).hexdigest()
def inspect(home):
    proof=home/'.config/private-gateway/owner-proof'
    current=(home/'.local/share/wizard-mobile-server/current').resolve()
    index=(current/'server/index.mjs').read_bytes()
    principals=(current/'server/principals.mjs').read_bytes()
    assert b"readFileSync(process.env.WIZARD_OWNER_PROOF_FILE, 'utf8').trim()" in index
    assert b'const ownerProof = typeof options.ownerProof' in principals
    cfg=home/'.config/private-gateway'
    producers=[]
    # Follow only actual Caddy imports; never scan agent homes or print source bodies.
    import glob
    queue=[cfg/'Caddyfile'];seen=set()
    while queue:
        p=queue.pop().resolve()
        if p in seen:continue
        seen.add(p);b=p.read_bytes()
        assert b'PRIVATE KEY-----' not in b and p.suffix not in ['.env','.key','.pem'],'credential file is not Caddy source'
        for token in re.findall(rb'^\s*import\s+([^\s#]+)',b,re.M):
            name=token.decode().strip('"');pattern=Path(name) if name.startswith('/') else p.parent/name
            for q in glob.glob(str(pattern)):
                q=Path(q)
                if q.is_file():
                    assert q.name=='Caddyfile' or q.suffix.lower() in ['.caddyfile','.caddy'],'non-Caddy import requires explicit review'
                    queue.append(q)
        if b'X-Wizard-Owner-Proof' in b:
            producers.append({'path':str(p),'sha256':sha(b),'file_placeholder_count':len(re.findall(rb'\{file\.[^}]*owner-proof\}',b)),'header_name_mentions':b.lower().count(b'x-wizard-owner-proof')})
    b=proof.read_bytes(); assert len(b.strip())>=32
    return {'read_only':True,'proof':{'path':str(proof),'mode':oct(proof.stat().st_mode&511),'sha256':sha(b),'minimum_length_valid':True},'gateway_producer':'request-time file placeholder after forward_auth','producer_sources':producers,'consumer':{'source':str(current.resolve()),'index_sha256':sha(index),'principals_sha256':sha(principals),'read_once_at_startup':True,'captured_constant_time_verifier':True},'safe_in_place_rotation':False,'rotation_blocker':'Consumer captures the proof at startup. File replacement alone denies valid owner requests while leaving the exposed old proof accepted by the running service. No dual-secret allowlist or dynamic-auth patch is authorized.','exact_gate':{'action':'One coordinated model-blind rotation of this scoped generated gateway proof, followed by a drained Agent Service restart (wizard-mobile.service) to retire the captured old verifier. Not executed.','preconditions':['Parent authorizes this explicit core-service lifecycle action; current work and queued admissions drained/fenced by existing lifecycle controller','Seal exact service source/start identity, proof hash, gateway producer sources and all consumer identities before write','Fresh >=48 random-byte token generated only inside protected process; encrypted recovery via existing secret custody, no raw token in command, stdout, journal, model context or QA packet','CAS replace only exact 0600 proof file, reload consumer through existing lifecycle boundary; no owner/master account credential change','Readback hashes only; require real peer owner positive, direct forged/old-proof negative and unchanged neighbor deny checks; automatic scoped compensation on failure'],'not_safe_alternatives':['Rotate only producer file','Accept both old and new credentials','Patch running auth implementation','Restart core while other threads are active']},'recurrence_containment':'Candidate Caddy default runtime JSON encoder deletes the exact proof-header fields before error-log output; admitted fixture must prove a real reverse-proxy 502 log omits the synthetic header while preserving the error event. Every journal probe projects only allowlisted event names/timestamps/hashes.'}
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--home',type=Path,default=Path.home());p.add_argument('--out',type=Path,required=True);a=p.parse_args();d=inspect(a.home);a.out.write_text(json.dumps(d,indent=2)+'\n'); print(json.dumps({'mapped':True,'receipt':str(a.out),'read_once_at_startup':True,'safe_in_place_rotation':False,'values_projected':False}))
