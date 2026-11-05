#!/usr/bin/env python3
"""Forum metadata/copy-only dependency staging. No browser, fixture, or tests are launched."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path

def sha(b):return hashlib.sha256(b).hexdigest()
def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();root=a.root.resolve()
    assert str(root).startswith('/srv/work/ab-') and root.is_dir()
    assert not (root/'deps-closure.json').exists()
    old=Path('/srv/work/notifications-preservation-547177ad3f39-20261105/node/node_modules')
    assert json.loads((old/'playwright/package.json').read_text())['version']=='1.56.1'
    registry=json.loads((old/'playwright-core/browsers.json').read_text())
    chromium=next(b for b in registry['browsers'] if b['name']=='chromium')
    assert chromium['revision']=='1194' and chromium['browserVersion']=='141.0.7390.37'
    prior=Path('/home/james/.cache/ms-playwright/chromium-'+chromium['revision'])
    assert (prior/'chrome-linux/chrome').is_file() and os.access(prior/'chrome-linux/chrome',os.X_OK)
    (root/'node').mkdir();shutil.copytree(old,root/'node/node_modules',symlinks=True)
    (root/'browser').mkdir();shutil.copytree(prior,root/'browser'/prior.name,symlinks=True)
    module=root/'node/node_modules/playwright';browser=root/'browser'/prior.name/'chrome-linux/chrome'
    ldd=subprocess.run(['ldd',str(browser)],capture_output=True,text=True,timeout=15);assert ldd.returncode==0 and 'not found' not in ldd.stdout
    # Read-only module resolution and syntax checking are not browser execution.
    node=subprocess.run(['node','--check',str(root/'browser.cjs')],capture_output=True,timeout=15);assert node.returncode==0
    resolved=subprocess.check_output(['node','-e','const p=require(process.argv[1]);if(!p.chromium||!p.devices["Pixel 7"])process.exit(1);console.log(require(process.argv[1]+"/package.json").version)',str(module)],timeout=15).decode().strip();assert resolved=='1.56.1'
    closure={str(f.relative_to(root)):sha(f.read_bytes()) for d in [root/'node',root/'browser'] for f in sorted(d.rglob('*')) if f.is_file()}
    (root/'deps-closure.json').write_text(json.dumps(closure,sort_keys=True,indent=2)+'\n')
    evidence={'module':str(module),'package_sha256':sha((module/'package.json').read_bytes()),'browser':str(browser),'browser_sha256':sha(browser.read_bytes()),'version':resolved,'registry':chromium,'dependency_files':len(closure),'ldd_sha256':sha(ldd.stdout.encode()),'no_missing_libraries':True,'copy_only_no_runtime_executed':True}
    (root/'playwright-path.txt').write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps({'metadata_preflight':True,'dependency_files':len(closure),'module_version':resolved,'chromium_revision':chromium['revision'],'no_browser_launched':True}))
if __name__=='__main__':main()
