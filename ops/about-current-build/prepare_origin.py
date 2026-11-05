#!/usr/bin/env python3
"""Seal one immutable native fixture, never a per-live-SHA carrier or activator."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request

HERE=Path(__file__).resolve().parent

def sha(b):return hashlib.sha256(b).hexdigest()
def save(p,d):p.write_text(json.dumps(d,indent=2)+'\n')
def git(repo,*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--source',type=Path,required=True);p.add_argument('--commit',required=True);p.add_argument('--prior',type=Path,required=True);a=p.parse_args()
    import re
    assert re.fullmatch('[0-9a-f]{40}',a.commit)
    assert git(a.source,'rev-parse',a.commit)==a.commit
    assert not a.root.exists(),'never mutate or reuse an evidence packet'
    a.root.mkdir(parents=True);(a.root/'source').mkdir()
    prior=json.loads((a.prior/'manifest.json').read_text())
    for n,h in prior['inputs'].items():assert sha((a.prior/n).read_bytes())==h,'prior sealed input drift '+n
    names=['ORIGIN.md','seal_origin.py','prepare_origin.py','activate_origin.py','activate.py','origin_qa.py','browser.cjs','request-guard.cjs','pixel-proof.cjs','consumer_probe.cjs','test_browser_contract.cjs','stage_dependencies.py','metadata.Caddyfile','test_origin_qa.py']
    for n in names:shutil.copyfile(HERE/n,a.root/n)
    for n in ['caddy-private-gateway','source/main_compare.py','source/qa-proof-fixture.cjs','source/qa-synthetic-fence.cjs']:
        shutil.copyfile(a.prior/n,a.root/n)
    # Re-read the deployed comparison source; no owner API or generated web data.
    wam_pid=int(subprocess.check_output(['systemctl','--user','show','wizard-app-manager-api.service','-p','MainPID','--value'],text=True,timeout=10))
    wam_source=Path('/proc',str(wam_pid),'cwd').resolve(strict=True)
    assert re.fullmatch('[0-9a-f]{40}',wam_source.name)
    blob=subprocess.check_output(['git','-C','/home/telos/workspace/wizard-app-manager','show',wam_source.name+':wam/main_compare.py'],timeout=15)
    assert (wam_source/'wam/main_compare.py').read_bytes()==blob
    (a.root/'source/main_compare.py').write_bytes(blob)
    (a.root/'caddy-private-gateway').chmod(0o755)
    # Preserve the exact loaded default log encoder, not its expanded config.
    text=(a.prior/'Caddyfile.candidate').read_text();logger=text[text.index('    log default {'):text.index('    admin off')]
    live_config=Path('/home/telos/.config/private-gateway/Caddyfile').read_text()
    assert live_config.count('    log default {')==1
    assert live_config[live_config.index('    log default {'):live_config.index('    admin off')]==logger
    (a.root/'source/default-logger.caddy').write_text(logger)
    (a.root/'metadata.live.Caddyfile').write_text((HERE/'metadata.Caddyfile').read_text().replace('__SHELL_STATIC_UPSTREAM__','127.0.0.1:8928'))
    headers=Path('/home/telos/.hermes/state/troubleshooter-ship-20261104/primary-static.conf').read_text()
    (a.root/'source/static-headers.conf').write_text('\n'.join(x for x in headers.splitlines() if x.startswith('add_header '))+'\n')
    paths=['web','.gitattributes','Dockerfile','tools/test_build_stamp_archive.py']
    for name,ref in [('release.tar',a.commit),('raw.tar',a.commit+'^{tree}')]:
        (a.root/'source'/name).write_bytes(subprocess.check_output(['git','-C',str(a.source),'archive',ref,*paths]))
    import io,tarfile
    with tarfile.open(fileobj=io.BytesIO((a.root/'source/release.tar').read_bytes())) as t:
        stream=t.extractfile('web/build.json');assert stream is not None
        body=stream.read()
        assert body==('{"commit":"'+a.commit+'"}\n').encode()
        files={}
        for m in t.getmembers():
            if m.isfile() and m.name.startswith('web/'):
                stream=t.extractfile(m);assert stream is not None
                files[m.name[4:]]={'sha256':sha(stream.read()),'mode':m.mode}
    forbidden={'data.js','summaries.js','ux-version.txt','ux-changes.json','my-release.json'}
    assert forbidden.isdisjoint(files),'generated owner data is not fixture source'
    spec=importlib.util.spec_from_file_location('pinned_compare',a.root/'source/main_compare.py');assert spec and spec.loader
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    raw=[]
    def fetch(url):
        assert url=='https://api.github.com/repos/humanitylabs-org/wizard-ux/compare/'+a.commit+'...main?per_page=1&page=2'
        r=urllib.request.urlopen(urllib.request.Request(url,headers={'Accept':'application/vnd.github+json','User-Agent':'wizard-about-native-fixture'}),timeout=20)
        with r: data=json.loads(r.read(module.MAX_BYTES))
        raw.append(data);return data
    comparison=module.compare(a.commit,fetch=fetch)
    assert 'error' not in comparison and len(raw)==1,comparison
    save(a.root/'source/comparison.json',comparison);save(a.root/'source/comparison-input.json',raw[0])
    base=Path('/home/telos/.local/share/wizard-ux/current').resolve()
    d=json.loads(subprocess.check_output(['docker','inspect','wizard-interface-candidate'],text=True))[0]
    mount=next(m for m in d['Mounts'] if m['Destination']=='/usr/share/nginx/wizard-ux')
    live_sha=(base/'RELEASE_SHA').read_text().strip()
    metadata_closure={name:sha(subprocess.check_output(['git','-C',str(a.source),'show',a.commit+':'+name])) for name in ['.gitattributes','web/build.json']}
    docker=subprocess.check_output(['git','-C',str(a.source),'show',a.commit+':Dockerfile'],text=True)
    metadata_closure['Dockerfile_writer_line']=sha(next(x for x in docker.splitlines() if x.startswith('RUN if ') and 'WIZARD_UX_COMMIT' in x).encode())
    save(a.root/'source/provenance.json',{'fixture_commit':a.commit,'fixture_web_tree':git(a.source,'rev-parse',a.commit+':web'),'static':files,'metadata_source_closure':metadata_closure,'git_archive_commit_authority':True,'source_changes':git(a.source,'diff','--name-only','fc998242940c2e3e8cdb06c9f514e92857340998',a.commit).splitlines(),'live_observation_only':{'current':str(base),'commit':live_sha,'container_id':d['Id'],'read_only_mount':mount['Source'],'mount_rw':mount['RW'],'build_metadata_present':(Path(mount['Source'])/'build.json').exists()},'deployed_main_compare_sha256':sha((a.root/'source/main_compare.py').read_bytes()),'candidate_not_live':True,'all_owner_generated_paths_excluded':True,'eligibility':'sealed metadata source + exact feature/SDK/static hashes; unrelated live HEAD movement is not a fixture invalidation','publisher_boundary':'git archive COMMIT; never a raw tree or copy of the working tree','publisher_caveat':'raw-blob integrity guards must compare the sole export-subst metadata path to canonical generated bytes, not its unsubstituted Git blob; no publisher was modified','log_encoder_sha256':sha(logger.encode()),'legacy_43_security_checks_not_inherited_for_changed_route':True})
    print(json.dumps({'prepared':True,'root':str(a.root),'fixture_commit':a.commit,'static_files':len(files),'comparison':comparison,'native_not_run':True,'live_changes':False}))

if __name__=='__main__':main()
