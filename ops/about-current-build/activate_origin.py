#!/usr/bin/env python3
"""Parent-only narrow metadata-origin activation; no shell/core/container mutation."""
import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import activate as helpers

ROOT=Path(__file__).resolve().parent
HOME=Path.home()
def sha(b):return hashlib.sha256(b).hexdigest()

def inputs():
    m=json.loads((ROOT/'manifest.json').read_text())
    for n,h in m['inputs'].items():assert sha((ROOT/n).read_bytes())==h,'sealed input drift '+n
    return m,json.loads((ROOT/'source/provenance.json').read_text())

def authority(provenance):
    shell=json.loads(subprocess.check_output(['docker','inspect','wizard-interface-candidate'],timeout=15))[0]
    mount=next(m for m in shell['Mounts'] if m['Destination']=='/usr/share/nginx/wizard-ux')
    assert not mount['RW']
    web=Path(mount['Source']).resolve(strict=True)
    # Identity follows the actual fixed bind, never the independently moving current link.
    stamp=(web.parent/'RELEASE_SHA').read_bytes()
    assert re.fullmatch(rb'[0-9a-f]{40}\n',stamp),'invalid canonical release stamp'
    commit=stamp.decode().strip();expected=('{"commit":"'+commit+'"}\n').encode()
    assert (web/'build.json').read_bytes()==expected,'metadata source fix is not published at actual nginx bind'
    source=Path((web.parent/'SOURCE_REPO').read_text().strip())
    for name in ['.gitattributes','web/build.json']:
        blob=subprocess.check_output(['git','-C',str(source),'show',commit+':'+name],timeout=15)
        assert sha(blob)==provenance['metadata_source_closure'][name],'metadata archive writer source changed'
    docker=subprocess.check_output(['git','-C',str(source),'show',commit+':Dockerfile'],timeout=15,text=True)
    line=next(x for x in docker.splitlines() if x.startswith('RUN if ') and 'WIZARD_UX_COMMIT' in x)
    assert sha(line.encode())==provenance['metadata_source_closure']['Dockerfile_writer_line'],'metadata image writer changed'
    # Feature/SDK/skin closure rather than unrelated whole-shell HEAD equality.
    closure={n:d['sha256'] for n,d in provenance['static'].items() if n=='core/box-source.js' or n.startswith(('core/settings','core/appearance','kit/','skins/nebula/','skins/grove/'))}
    assert closure
    for n,h in closure.items():assert sha((web/n).read_bytes())==h,'affected feature/SDK/skin changed: fresh browser evidence required for '+n
    wam_pid=int(helpers.val('wizard-app-manager-api.service','MainPID'))
    compare_source=Path('/proc',str(wam_pid),'cwd').resolve(strict=True)/'wam/main_compare.py'
    compare_hash=sha(compare_source.read_bytes())
    assert compare_hash==provenance['deployed_main_compare_sha256'],'comparison module changed: fresh same-input proof required'
    c=http.client.HTTPConnection('127.0.0.1',8928,timeout=8)
    try:c.request('GET','/wizard-os-home/build.json');r=c.getresponse();assert r.status==200 and r.read()==expected,'actual nginx HTTP metadata differs from mounted source'
    finally:c.close()
    return {'actual_serving_commit':commit,'container_id':shell['Id'],'web':str(web),'metadata_sha256':sha(expected),'closure':closure,'comparison_source_sha256':compare_hash}

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['check','apply']);p.add_argument('--expected-config-sha256',required=True);p.add_argument('--parent-reviewed-receipt-sha256');a=p.parse_args()
    m,provenance=inputs()
    config=HOME/'.config/private-gateway/Caddyfile';lock=HOME/'.local/state/wizard-app-promotions/wam.lock'
    with lock.open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before=config.read_bytes();assert sha(before)==a.expected_config_sha256,'Caddy CAS refusal: rebase only this import; do not reprepare/retest immutable fixture'
        text=before.decode();logger=(ROOT/'source/default-logger.caddy').read_text()
        assert text.count('    log default {')==1
        assert text[text.index('    log default {'):text.index('    admin off')]==logger,'loaded logger must remain exactly once and byte-identical'
        marker='    handle /wizard-os-home/* {';assert text.count(marker)==1
        line='    import '+str(ROOT/'metadata.live.Caddyfile')+'\n\n'
        assert line not in text,'metadata origin import already installed'
        candidate=text.replace(marker,line+marker,1).encode()
        assert candidate.replace(line.encode(),b'',1)==before
        current=authority(provenance)
        service_pid=int(helpers.val('wizard-mobile.service','MainPID'))
        pid=int(helpers.val('private-gateway-https.service','MainPID'))
        binary=HOME/'.local/bin/caddy-private-gateway';binary_hash=sha(binary.read_bytes())
        assert binary_hash==m['inputs']['caddy-private-gateway'],'native-tested Caddy binary differs'
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            target=Path(temp)/'Caddyfile';target.write_bytes(candidate)
            validation=helpers.validate(HOME,target,pid)
        d={'gateway_pid':pid}
        if a.action=='check':
            print(json.dumps({'eligible_preflight':True,'live_unmodified':True,'authority':current,'expected_config_sha256':sha(before),'candidate_config_sha256':sha(candidate),'validation':validation,'live_browser_acceptance_pending':True}));return
        receipt=(ROOT/'results-origin/receipt.json').read_bytes()
        assert a.parent_reviewed_receipt_sha256==sha(receipt),'parent must read receipt + screenshots, then explicitly attest this exact receipt hash'
        result=json.loads(receipt)
        assert result['accepted'] and result['manifest_sha256']==sha((ROOT/'manifest.json').read_bytes())
        assert result['all_native_processes_stopped'] and result['browser_process_group_absent']
        assert len(result['checks'])==result['checks_passed'] and all(x['pass'] for x in result['checks'])
        browser=result['browser'];assert browser['accepted'] and browser['all_browser_processes_stopped'] and browser['source_commit']==provenance['fixture_commit']
        assert {x['id'] for x in browser['cases']}=={'desktop','pixel7','standalone-dark'}
        assert len(browser['cases'])==3 and all(all(c['pass'] for c in x['checks']) for x in browser['cases'])
        assert config.read_bytes()==before and authority(provenance)==current
        assert int(helpers.val('private-gateway-https.service','MainPID'))==pid and sha(binary.read_bytes())==binary_hash
        mode=config.stat().st_mode&0o777
        helpers.atomic(config,candidate,mode)
        try:
            reload=helpers.reload_gateway(d,'origin')
            assert config.read_bytes()==candidate
            assert int(helpers.val('private-gateway-https.service','MainPID'))==pid and sha(binary.read_bytes())==binary_hash
            assert int(helpers.val('wizard-mobile.service','MainPID'))==service_pid
            assert authority(provenance)==current
        except Exception:
            # Never overwrite a concurrent later writer while compensating.
            assert config.read_bytes()==candidate,'compensation CAS refused: later writer changed config'
            helpers.atomic(config,before,mode);helpers.reload_gateway(d,'origin-compensated');raise
        print(json.dumps({'applied_metadata_origin_only':True,'readback_config_sha256':sha(config.read_bytes()),'authority':current,'same_pid_binary':True,'core_unchanged':True,'reload':reload,'fresh_live_render_acceptance_pending':True}))

if __name__=='__main__':main()
