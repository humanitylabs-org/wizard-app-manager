#!/usr/bin/env python3
"""Read-only source custody; metadata-only carrier; stage, never execute worker QA."""
import argparse, glob, hashlib, io, json, os, re, shutil, subprocess, tarfile
from pathlib import Path

def sha(b): return hashlib.sha256(b).hexdigest()
def cmd(a): return subprocess.check_output(a,timeout=60).decode().strip()

def served(home):
    """Discover actual serving locators before choosing an immutable baseline."""
    current=home/'.local/share/wizard-ux/current'
    pid=int(cmd(['systemctl','--user','show','wizard-mobile.service','-p','MainPID','--value']))
    env=dict(x.split('=',1) for x in Path('/proc',str(pid),'environ').read_bytes().decode().split('\0') if '=' in x)
    ux=Path(env['WIZARD_UX_DIR']); web=ux.resolve(strict=True)
    shell=json.loads(subprocess.check_output(['docker','inspect','wizard-interface-candidate'],timeout=20))[0]
    static=next(m for m in shell['Mounts'] if m['Destination']=='/usr/share/nginx/wizard-ux')
    assert not static['RW'] and Path(static['Source']).resolve(strict=True)==web,'Service/static served-source split; no mutation'
    release=web.parent
    assert web.name=='web' and current.resolve(strict=True)==release,'current is not the actual served release'
    return release,{'service_env_path':str(ux),'service_resolved_web':str(web),'static_source':static['Source'],'static_destination':static['Destination'],'static_readonly':True,'current_locator':str(current)},pid,shell

def prepare(root,home,revision,reuse=None):
    root.mkdir(parents=True,exist_ok=True)
    assert not (root/'manifest.json').exists(), 'one-shot preparation root'
    current=home/'.local/share/wizard-ux/current'; release,locators,active,mounts=served(home)
    commit=(release/'RELEASE_SHA').read_text().strip()
    assert re.fullmatch('[0-9a-f]{40}',commit)
    repo=Path((release/'SOURCE_REPO').read_text().strip())
    tree=(release/'WEB_TREE').read_text().strip()
    assert cmd(['git','-C',str(repo),'rev-parse',commit+':web'])==tree
    # Immutable served web bytes, not checkout HEAD, establish build provenance.
    archive=subprocess.check_output(['git','-C',str(repo),'archive',commit,'web'],timeout=60)
    files={}; modes={}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for m in tar.getmembers():
            if not m.isfile(): continue
            stream=tar.extractfile(m); assert stream is not None
            b=stream.read(); rel=m.name[4:]
            assert (release/m.name).is_file() and (release/m.name).read_bytes()==b,'immutable source drift '+rel
            files[rel]=sha(b); modes[rel]=m.mode
    assert not (release/'web/build.json').exists() and not (release/'web/my-release.json').exists()
    docker=cmd(['git','-C',str(repo),'show',commit+':Dockerfile'])
    line=next(l[4:] for l in docker.splitlines() if l.startswith('RUN if ') and '> /usr/share/nginx/wizard-ux/build.json;' in l)
    assert line.count('/usr/share/nginx/wizard-ux/build.json')==1
    carrier=root/'carrier'; carrier.mkdir()
    # Execute the existing generator verbatim except its output destination.
    generation=line.replace('/usr/share/nginx/wizard-ux/build.json',str(carrier/'build.json'))
    subprocess.run(['sh','-c',generation],env={**os.environ,'WIZARD_UX_COMMIT':commit},check=True,capture_output=True,timeout=5)
    assert json.loads((carrier/'build.json').read_text())=={'commit':commit}
    here=Path(__file__).resolve().parent
    for n in ['metadata.Caddyfile','prepare.py','activate.py','forum_qa.py','browser.cjs','security_map.py','stage_dependencies.py','refresh.py']:
        shutil.copyfile(here/n,root/n)
    source=root/'source';source.mkdir();(source/'web.tar').write_bytes(archive)
    for n in ['qa-proof-fixture.cjs','qa-synthetic-fence.cjs','qa-playwright.cjs']:
        shutil.copyfile(release/'tools'/n,source/n)
    wam_pid=int(cmd(['systemctl','--user','show','wizard-app-manager-api.service','-p','MainPID','--value']))
    wam_source=Path('/proc',str(wam_pid),'cwd').resolve()
    assert re.fullmatch('[0-9a-f]{40}',wam_source.name),'WAM API source has no immutable commit identity'
    wam_blob=subprocess.check_output(['git','-C',str(home/'workspace/wizard-app-manager'),'show',wam_source.name+':wam/main_compare.py'],timeout=15)
    assert (wam_source/'wam/main_compare.py').read_bytes()==wam_blob,'deployed main_compare differs from immutable git module'
    shutil.copyfile(wam_source/'wam/main_compare.py',source/'main_compare.py')
    stamp=(root/'metadata.Caddyfile').read_text().replace('__CARRIER__',str(carrier)).replace('__SERVED_SHA_FILE__',str(current/'RELEASE_SHA')).replace('__SERVED_SHA__',commit)
    (root/'metadata.live.Caddyfile').write_text(stamp)
    controls={'caddy':home/'.config/private-gateway/Caddyfile','route_map':home/'worktrees/private-portfolio/private-routes.json','adapter':home/'worktrees/private-portfolio/private-route-adapter.mjs'}
    imports={};queue=[controls['caddy']]
    while queue:
        path=queue.pop().resolve()
        if str(path) in imports:continue
        body=path.read_bytes();imports[str(path)]=sha(body)
        for token in re.findall(rb'^\s*import\s+([^\s#]+)',body,re.M):
            name=token.decode().strip('"');pattern=Path(name) if name.startswith('/') else path.parent/name
            for match in glob.glob(str(pattern)):
                p=Path(match)
                if p.is_file():
                    assert p.name=='Caddyfile' or p.suffix.lower() in ['.caddyfile','.caddy'],'unclassified import'
                    queue.append(p)
    before=controls['caddy'].read_text()
    logger='    log default {\n        format filter {\n            wrap json\n            fields {\n                request>headers>X-Wizard-Owner-Proof delete\n                request>headers>x-wizard-owner-proof delete\n            }\n        }\n    }\n'
    assert 'log default {' not in before and before.startswith('{\n')
    marker='    handle /wizard-os-home/* {'
    assert before.count(marker)==1
    candidate=before.replace('{\n','{\n'+logger,1).replace(marker,'    import '+str(root/'metadata.live.Caddyfile')+'\n\n'+marker,1)
    (root/'Caddyfile.before').write_text(before);(root/'Caddyfile.candidate').write_text(candidate)
    # Only placeholders/paths exist in Caddy source. Never project expanded config/environment/logs.
    pid=int(cmd(['systemctl','--user','show','private-gateway-https.service','-p','MainPID','--value']))
    env=dict(x.split('=',1) for x in Path('/proc',str(pid),'environ').read_bytes().decode().split('\0') if '=' in x)
    binary=home/'.local/bin/caddy-private-gateway'
    validation=[]
    for n in ['Caddyfile.before','Caddyfile.candidate']:
        p=subprocess.run([str(binary),'adapt','--config',str(root/n),'--adapter','caddyfile','--validate'],capture_output=True,env=env,timeout=30)
        validation.append({'name':n,'returncode':p.returncode,'stdout_sha256':sha(p.stdout),'stderr_sha256':sha(p.stderr)})
        assert p.returncode==0,'redacted Caddy validation failed '+n
    shutil.copyfile(binary,root/'caddy-private-gateway');os.chmod(root/'caddy-private-gateway',0o755)
    final,final_locators,final_active,final_mounts=served(home)
    assert (final,final_locators,final_active,final_mounts['Id'])==(release,locators,active,mounts['Id']),'served source moved during preparation'
    receipt={'commit':commit,'web_tree':tree,'current':str(release),'source_repo':str(repo),'checkout_head':cmd(['git','-C',str(repo),'rev-parse','HEAD']),'checkout_tracked_dirty':bool(cmd(['git','-C',str(repo),'status','--porcelain','--untracked-files=no'])),'all_tracked_web_equal':True,'web_files':files,'web_modes':modes,'generator':{'source':'Dockerfile','blob':cmd(['git','-C',str(repo),'rev-parse',commit+':Dockerfile']),'line_sha256':sha(line.encode()),'output_sha256':sha((carrier/'build.json').read_bytes())},'validation':validation,'controls':{k:{'path':str(p),'sha256':sha(p.read_bytes()),'mode':p.stat().st_mode&511} for k,p in controls.items()},'gateway_pid':pid,'gateway_binary_sha256':sha(binary.read_bytes()),'shell_container':{k:mounts[k] for k in ['Id','Image']},'service_pid':active,'my_release_unmodified_absent':True,'generated_runtime_extras':'not opened, inventoried for contents, copied, or changed'}
    (root/'provenance.json').write_text(json.dumps(receipt,indent=2)+'\n')
    receipt['wam_module']={'source':str(wam_source),'sha256':sha((source/'main_compare.py').read_bytes()),'api_pid':wam_pid}
    receipt['caddy_imports']=imports
    receipt['serving_locators']=locators
    if reuse:
        old=json.loads((reuse/'provenance.json').read_text())
        prior=old['web_files']
        receipt['refresh']={'prior_capsule':str(reuse),'prior_commit':old['commit'],'unchanged_web_blobs':sum(prior.get(n)==h for n,h in files.items()),'changed_or_added_web_blobs':[n for n,h in files.items() if prior.get(n)!=h],'removed_web_blobs':sorted(set(prior)-set(files)),'prior_capsule_unmodified':True,'full_runtime_acceptance_reused':False}
    assert all(sha(Path(p).read_bytes())==h for p,h in imports.items()),'gateway import drift during preparation'
    assert int(cmd(['systemctl','--user','show','wizard-app-manager-api.service','-p','MainPID','--value']))==wam_pid and Path('/proc',str(wam_pid),'cwd').resolve()==wam_source,'WAM moved during preparation'
    (root/'provenance.json').write_text(json.dumps(receipt,indent=2)+'\n')
    inputs={str(p.relative_to(root)):sha(p.read_bytes()) for p in root.rglob('*') if p.is_file()}
    manifest={'inputs':inputs,'commit':commit,'web_tree':tree,'parent_admission_required':True}
    (root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    worker='/srv/work/ab-'+commit[:8]+'-'+revision
    specs={}
    for phase in ['candidate','live']:
        specs[phase]={'host':'forum','lane':'light','key':'about-build-'+commit[:8]+'-'+phase+'-'+revision,'cwd':worker,'argv':['python3',worker+'/forum_qa.py','--phase',phase],'timeout_seconds':600}
    (root/'parent-heavy-specs.json').write_text(json.dumps(specs,indent=2)+'\n')
    print(json.dumps({'prepared':True,'commit':commit,'web_tree':tree,'all_tracked_web_equal':len(files),'carrier':str(carrier),'worker':worker,'parent_specs':str(root/'parent-heavy-specs.json'),'validation':validation}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--home',type=Path,default=Path.home());p.add_argument('--revision',default='r1');p.add_argument('--reuse',type=Path);a=p.parse_args();assert re.fullmatch('r[1-9][0-9]*',a.revision);prepare(a.root,a.home,a.revision,a.reuse)
