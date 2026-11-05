#!/usr/bin/env python3
"""Forum metadata/copy-only dependency staging. No browser, fixture, or tests are launched."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path

def sha(b):return hashlib.sha256(b).hexdigest()
def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--reuse',type=Path);a=p.parse_args();root=a.root.resolve()
    assert str(root).startswith('/srv/work/ab-') and root.is_dir()
    assert not (root/'deps-closure.json').exists()
    prior_root=Path();prior_closure={}
    if a.reuse:
        prior_root=a.reuse.resolve();assert str(prior_root).startswith('/srv/work/ab-') and prior_root!=root
        sealed=json.loads((prior_root/'manifest.json').read_text())
        for n in ['deps-closure.json','playwright-path.txt']:
            assert sha((prior_root/n).read_bytes())==sealed['inputs'][n],'prior dependency metadata drift'
        prior_closure=json.loads((prior_root/'deps-closure.json').read_text())
        for n,h in prior_closure.items():
            assert n.startswith(('node/','browser/')) and '..' not in Path(n).parts
            assert sha((prior_root/n).read_bytes())==h,'prior dependency byte drift '+n
        prior_tool=json.loads((prior_root/'playwright-path.txt').read_text())
        old=prior_root/'node/node_modules'
    else:old=Path('/srv/work/notifications-preservation-547177ad3f39-20261105/node/node_modules')
    assert json.loads((old/'playwright/package.json').read_text())['version']=='1.56.1'
    registry=json.loads((old/'playwright-core/browsers.json').read_text())
    chromium=next(b for b in registry['browsers'] if b['name']=='chromium')
    assert chromium['revision']=='1194' and chromium['browserVersion']=='141.0.7390.37'
    prior=(prior_root/'browser'/('chromium-'+chromium['revision'])) if a.reuse else Path('/home/james/.cache/ms-playwright/chromium-'+chromium['revision'])
    assert (prior/'chrome-linux/chrome').is_file() and os.access(prior/'chrome-linux/chrome',os.X_OK)
    # Reflink/copy only: never install, download, or hardlink mutable prior evidence.
    (root/'node').mkdir();subprocess.run(['cp','-a','--reflink=auto',str(old),str(root/'node/node_modules')],check=True,timeout=120)
    (root/'browser').mkdir();subprocess.run(['cp','-a','--reflink=auto',str(prior),str(root/'browser'/prior.name)],check=True,timeout=120)
    module=root/'node/node_modules/playwright';browser=root/'browser'/prior.name/'chrome-linux/chrome'
    ldd=subprocess.run(['ldd',str(browser)],capture_output=True,text=True,timeout=15);assert ldd.returncode==0 and 'not found' not in ldd.stdout
    # Read-only module resolution and syntax checking are not browser execution.
    node=subprocess.run(['node','--check',str(root/'browser.cjs')],capture_output=True,timeout=15);assert node.returncode==0
    resolved=subprocess.check_output(['node','-e','const p=require(process.argv[1]);if(!p.chromium||!p.devices["Pixel 7"])process.exit(1);console.log(require(process.argv[1]+"/package.json").version)',str(module)],timeout=15).decode().strip();assert resolved=='1.56.1'
    closure={str(f.relative_to(root)):sha(f.read_bytes()) for d in [root/'node',root/'browser'] for f in sorted(d.rglob('*')) if f.is_file()}
    if a.reuse:assert closure==prior_closure,'copied dependency closure changed'
    (root/'deps-closure.json').write_text(json.dumps(closure,sort_keys=True,indent=2)+'\n')
    evidence={'module':str(module),'package_sha256':sha((module/'package.json').read_bytes()),'browser':str(browser),'browser_sha256':sha(browser.read_bytes()),'version':resolved,'registry':chromium,'dependency_files':len(closure),'ldd_sha256':sha(ldd.stdout.encode()),'no_missing_libraries':True,'copy_only_no_runtime_executed':True}
    transport=root/'node/node_modules/playwright-core/lib/client'
    fetch=(transport/'fetch.js').read_text();inner=fetch[fetch.index('async _innerFetch'):fetch.index('async storageState',fetch.index('async _innerFetch'))]
    network=(transport/'network.js').read_text()
    assert 'this._channel.fetch({' in inner and 'this.fetch(' not in inner
    assert 'this._context.request._innerFetch({ request: this.request()' in network
    evidence['transport_source_sha256']={str((transport/n).relative_to(root)):sha((transport/n).read_bytes()) for n in ['fetch.js','network.js']}
    node=Path(shutil.which('node')).resolve(strict=True)
    evidence['node']=str(node);evidence['node_sha256']=sha(node.read_bytes())
    evidence['node_version']=subprocess.check_output([str(node),'--version'],text=True,timeout=10).strip()
    (root/'playwright-path.txt').write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps({'metadata_preflight':True,'dependency_files':len(closure),'module_version':resolved,'chromium_revision':chromium['revision'],'no_browser_launched':True}))
if __name__=='__main__':main()
