#!/usr/bin/env python3
"""Finite metadata-only refresh; prepare and seal, NEVER submit/activate/run fixtures."""
import argparse,hashlib,importlib.util,json,re,subprocess,sys,urllib.request
from datetime import datetime,timezone
from pathlib import Path
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent

def sha(b):return hashlib.sha256(b).hexdigest()
def call(argv,timeout=180):
    r=subprocess.run(argv,capture_output=True,timeout=timeout)
    if r.returncode:
        # Fixed assertion diagnostics only; no arbitrary logs/config/env values.
        diagnostics=[x for x in r.stderr.decode(errors='replace').splitlines() if x.startswith('AssertionError:')]
        raise AssertionError('preparation failed; stderr_sha256='+sha(r.stderr)+'; '+(diagnostics[-1] if diagnostics else 'non-assertion failure, inspect privately'))
    return r.stdout

def save(p,d):p.write_text(json.dumps(d,indent=2)+'\n')
def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--revision',required=True);p.add_argument('--prior',type=Path,required=True)
    a=p.parse_args();root=a.root.resolve();prior=a.prior.resolve()
    assert re.fullmatch(r'r[1-9][0-9]*',a.revision) and not root.exists(),'new revision/root required; never mutate old capsules'
    old_manifest=sha((prior/'manifest.json').read_bytes())
    call([sys.executable,'-B',str(HERE/'prepare.py'),'--root',str(root),'--revision',a.revision,'--reuse',str(prior)])
    specs=json.loads((root/'parent-heavy-specs.json').read_text());worker=specs['candidate']['cwd']
    assert re.fullmatch(r'/srv/work/ab-[0-9a-f]{8}-r[1-9][0-9]*',worker)
    prior_specs=json.loads((prior/'parent-heavy-specs.json').read_text());oldworker=prior_specs['candidate']['cwd']
    assert re.fullmatch(r'/srv/work/ab-[0-9a-f]{8}-r[1-9][0-9]*',oldworker)
    call(['ssh','forum','mkdir',worker])
    call(['scp','-qr',str(root)+'/.','forum:'+worker+'/'])
    dep=json.loads(call(['ssh','forum','python3','-B',worker+'/stage_dependencies.py','--root',worker,'--reuse',oldworker],timeout=240))
    for n in ['deps-closure.json','playwright-path.txt']:
        call(['scp','-q','forum:'+worker+'/'+n,str(root/n)])
    initial=(root/'manifest.json').read_bytes();(root/'manifest.source.json').write_bytes(initial)
    m=json.loads(initial)
    for n in ['deps-closure.json','playwright-path.txt']:m['inputs'][n]=sha((root/n).read_bytes())
    save(root/'manifest.json',m);digest=sha((root/'manifest.json').read_bytes())
    for phase,spec in specs.items():spec['argv']=['python3','-B',worker+'/forum_qa.py','--phase',phase,'--manifest-sha256',digest]
    save(root/'parent-heavy-specs.json',specs)
    call(['scp','-q',str(root/'manifest.json'),str(root/'manifest.source.json'),str(root/'parent-heavy-specs.json'),'forum:'+worker+'/'])
    worker_preflight=json.loads(call(['ssh','forum',*specs['candidate']['argv'],'--preflight-only']))
    activation=json.loads(call([sys.executable,'-B',str(root/'activate.py'),'preflight']))
    # Public metadata only: current input, deployed module, no fixed count constants.
    url='http://127.0.0.1:7790/api/main-compare?commit='+m['commit']
    with urllib.request.urlopen(url,timeout=20) as r:api=json.loads(r.read());status=r.status
    spec=importlib.util.spec_from_file_location('actual_deployed_compare',root/'source/main_compare.py')
    assert spec and spec.loader;mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    expected=mod.compare(m['commit']);fields=['commit','base','behind','own']
    assert all(api.get(k)==expected.get(k) for k in fields) and 'error' not in api,'actual same-input comparison mismatch'
    prov=json.loads((root/'provenance.json').read_text())
    # Recheck locators and every exact byte after all transfer/hash steps.
    call([sys.executable,'-B',str(root/'activate.py'),'preflight'])
    assert sha((prior/'manifest.json').read_bytes())==old_manifest
    result={'prepared':True,'revision':a.revision,'checked_at':datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),'served_commit':m['commit'],'web_tree':m['web_tree'],'tracked_web_blobs_equal':len(prov['web_files']),'refresh':prov['refresh'],'serving_locators':prov['serving_locators'],'manifest_sha256':digest,'worker':worker,'worker_metadata':dep,'sealed_preflight':worker_preflight,'activation_preflight':activation,'current_commit_api_probe':{'url':url,'status':status,'comparison':api,'module_comparison':expected,'same_input_deployed_module_equal':True},'prior_manifest_sha256_unchanged':old_manifest,'no_live_mutation':True,'fixtures_browsers_tests_unexecuted':True,'job_submitted':False}
    save(root/'preflight-summary.json',result);call(['scp','-q',str(root/'preflight-summary.json'),'forum:'+worker+'/'])
    print(json.dumps(result))
if __name__=='__main__':main()
