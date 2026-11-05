#!/usr/bin/env python3
"""Parent-admitted Forum QA: exact Caddy fixture, then GET-captured real shell replay."""
import argparse,concurrent.futures,hashlib,http.client,io,json,mimetypes,os,signal,socket,subprocess,sys,tarfile,threading,time,urllib.request,urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
ROOT=Path(__file__).resolve().parent
ORIGIN='https://wizard.tailc0f369.ts.net';PREFIX='/wizard-os-home/'
def sha(b):return hashlib.sha256(b).hexdigest()
def save(p,d):p.write_text(json.dumps(d,indent=2)+'\n')
def seal(expected):
    assert sha((ROOT/'manifest.json').read_bytes())==expected,'manifest digest differs from parent admission'
    m=json.loads((ROOT/'manifest.json').read_text())
    for n,h in m['inputs'].items():assert sha((ROOT/n).read_bytes())==h,'sealed input mismatch '+n
    if (ROOT/'deps-closure.json').exists():
        for n,h in json.loads((ROOT/'deps-closure.json').read_text()).items():
            assert sha((ROOT/n).read_bytes())==h,'dependency drift '+n
    return m

def fixture(out):
    checks=[];auth=[];proc=None;servers=[]
    class Auth(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            auth.append({k.lower():v for k,v in self.headers.items()})
            self.send_response(200 if self.headers.get('X-Private-Peer')=='127.0.0.2' else 403)
            self.send_header('Content-Length','0');self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Auth);servers.append(server)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    s=socket.socket();s.bind(('127.0.0.1',0));port=s.getsockname()[1];s.close()
    marker=out/'RELEASE_SHA';commit=json.loads((ROOT/'carrier/build.json').read_text())['commit'];marker.write_text(commit+'\n')
    route=(ROOT/'metadata.Caddyfile').read_text().replace('__CARRIER__',str(ROOT/'carrier')).replace('__SERVED_SHA_FILE__',str(marker)).replace('__SERVED_SHA__',commit).replace('127.0.0.1:19447','127.0.0.1:'+str(server.server_port))
    (out/'route.caddy').write_text(route)
    candidate=(ROOT/'Caddyfile.candidate').read_text()
    # Reuse exact candidate default logger text, not a second implementation.
    logger=candidate[candidate.index('    log default {'):candidate.index('    admin off')]
    config=out/'fixture.caddy'
    config.write_text('{\n'+logger+' admin off\n auto_https off\n}\nhttp://127.0.0.1:'+str(port)+' {\n import '+str(out/'route.caddy')+'\n handle /error-probe {\n request_header X-Wizard-Owner-Proof fixture-credential-never-valid-live\n reverse_proxy 127.0.0.1:1\n }\n handle /neighbor {\n respond "before" 200\n }\n handle {\n respond "missing" 404\n }\n}\n')
    def req(path=PREFIX+'build.json',method='GET',owner=True,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',port,timeout=5,source_address=('127.0.0.2' if owner else '127.0.0.1',0));c.request(method,path,headers=headers or {});r=c.getresponse();b=r.read();d=(r.status,b,dict(r.getheaders()));c.close();return d
    def check(n,v):checks.append({'name':n,'pass':bool(v)});assert v,n
    result={'accepted':False,'checks':checks,'synthetic':True}
    log=(out/'fixture.log').open('wb')
    try:
        proc=subprocess.Popen([str(ROOT/'caddy-private-gateway'),'run','--config',str(config),'--adapter','caddyfile'],env={**os.environ,'XDG_CONFIG_HOME':str(out/'xdg'),'XDG_DATA_HOME':str(out/'data')},stdout=log,stderr=subprocess.STDOUT)
        for _ in range(80):
            assert proc.poll() is None,'fixture startup failed'
            try:
                if req('/neighbor')[0]==200:break
            except OSError:pass
            time.sleep(.1)
        else:raise AssertionError('fixture startup timeout')
        check('same exact generated build bytes',req()[0:2]==(200,(ROOT/'carrier/build.json').read_bytes()))
        check('private no-store JSON',req()[2].get('Content-Type')=='application/json' and req()[2].get('Cache-Control')=='private, no-store')
        marker.write_text('f'*40+'\n')
        check('new shell refuses stale carrier',req()[0]==503)
        marker.write_text(commit+'\n')
        check('exact shell marker restores current stamp',req()[0]==200)
        check('plain non-owner refused',req(owner=False)[0]==403)
        forged={'X-Wizard-Owner-Proof':'fixture-forgery','X-Private-Peer':'127.0.0.2','Tailscale-User-Login':'fixture@example.invalid'}
        check('forged non-owner refused',req(owner=False,headers=forged)[0]==403)
        check('owner accepts canonical Origin',req(headers={'Origin':ORIGIN})[0]==200)
        check('foreign Origin refused',req(headers={'Origin':'https://foreign.invalid'})[0]==403)
        check('cross-site refused',req(headers={'Sec-Fetch-Site':'cross-site'})[0]==403)
        for method in ['HEAD','POST','PUT','PATCH','DELETE','OPTIONS']:
            check('unsafe '+method,req(method=method)[0]==405)
        check('query refused',req(PREFIX+'build.json?x=1')[0]==400)
        for path in [PREFIX+'build.json/',PREFIX+'build.json/other',PREFIX+'my-release.json',PREFIX+'other.json']:
            check('neighbor '+path,req(path)[0]==404)
        check('auth never receives proof',all('x-wizard-owner-proof' not in a for a in auth))
        check('actual reverse-proxy error emitted',req('/error-probe')[0]==502)
        time.sleep(.3);log.flush();data=(out/'fixture.log').read_bytes()
        entries=[]
        for l in data.splitlines():
            try:entries.append(json.loads(l))
            except ValueError:pass
        errors=[x for x in entries if x.get('logger')=='http.log.error']
        check('runtime error event retained',bool(errors))
        check('runtime error proof field deleted',all(not any(k.lower()=='x-wizard-owner-proof' for k in x.get('request',{}).get('headers',{})) for x in errors))
        # Caddy adaptation messages can include config input, so inspect only actual error records for the canary.
        check('runtime error proof value absent',all('fixture-credential-never-valid-live' not in json.dumps(x) for x in errors))
        old=proc.pid;config.write_text(config.read_text().replace('respond "before"','respond "after"'));proc.send_signal(signal.SIGUSR1)
        for _ in range(80):
            if req('/neighbor')[1]==b'after':break
            time.sleep(.1)
        else:raise AssertionError('fixture reload timeout')
        check('same binary same PID reload',proc.pid==old and proc.poll() is None)
        check('metadata after reload',req()[1]==(ROOT/'carrier/build.json').read_bytes())
        result['accepted']=True
    except Exception as e:result['failure']=type(e).__name__+': '+str(e)
    finally:
        if proc:
            proc.terminate()
            try:proc.wait(timeout=6)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=3)
        for s in servers:s.shutdown();s.server_close()
        log.close();result['all_processes_stopped']=proc is None or proc.poll() is not None;result['check_count']=len(checks);result['log_sha256']=sha((out/'fixture.log').read_bytes());save(out/'fixture-results.json',result)
    assert result['accepted'],result.get('failure')
    return result

def capture(out,m,phase):
    receipt=json.loads((ROOT/'provenance.json').read_text());web=out/'captured';web.mkdir()
    captured={};lock=threading.Lock()
    def get(path):
        assert path.startswith(PREFIX) and not any(x in path for x in ['..','%','\\'])
        request=urllib.request.Request(ORIGIN+path,headers={'Cache-Control':'no-cache','User-Agent':'Wizard-About-static-custody'})
        try:r=urllib.request.urlopen(request,timeout=25)
        except urllib.error.HTTPError as e:r=e
        assert r.geturl()==ORIGIN+path,'redirect refused'
        b=r.read();return r.status,b,{k:r.headers.get(k) for k in ['Content-Type','Cache-Control','Content-Security-Policy']}
    def one(k,h):
        status,b,headers=get(PREFIX+k)
        assert status==200 and b and sha(b)==h,'actual served static mismatch '+k
        p=web/k;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
        with lock:captured[k]={'sha256':h,'status':status,'headers':headers}
    # Public tracked bytes only. Never GET generated owner history/data or arbitrary APIs.
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(one,k,h) for k,h in receipt['web_files'].items() if k not in ['data.js','summaries.js','ux-changes.json','ux-version.txt']]
        for f in concurrent.futures.as_completed(futures):f.result()
    status,b,headers=get(PREFIX+'build.json')
    assert status==(404 if phase=='candidate' else 200),'build route state differs from phase'
    if phase=='live':assert b==(ROOT/'carrier/build.json').read_bytes()
    build={'status':status,'sha256':sha(b),'headers':headers,'replay':'candidate carrier' if phase=='candidate' else 'actual served metadata'}
    if phase=='candidate':b=(ROOT/'carrier/build.json').read_bytes()
    (out/'build.json').write_bytes(b)
    s,body,h=get(PREFIX+'my-release.json');assert s==404,'My release unexpectedly selected';mine={'status':s,'sha256':sha(body)}
    # Exact current served immutable SHA, never the previous historical demonstration SHA.
    status,b,h=get(PREFIX+'store-install/_main-compare?commit='+m['commit'])
    assert status==200
    comparison=json.loads(b);assert comparison['commit']==m['commit'] and set(comparison)=={'commit','base','behind','own','checked_at'}
    assert all(isinstance(comparison[k],int) and comparison[k]>=0 for k in ['behind','own'])
    (out/'comparison.json').write_bytes(b)
    # The exact deployed module and same full commit/public query; no fixed count fixture.
    import importlib.util
    spec=importlib.util.spec_from_file_location('deployed_main_compare',ROOT/'source/main_compare.py');assert spec and spec.loader
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    expected=module.compare(m['commit'])
    save(out/'module-comparison.json',expected)
    assert {k:comparison[k] for k in ['commit','base','behind','own']}=={k:expected.get(k) for k in ['commit','base','behind','own']},'public main/cache changed between identical-input reads; preserve evidence, do not hardcode a count'
    result={'static':captured,'build':build,'my_release':mine,'comparison':comparison,'module_comparison':expected,'same_input_module_equal':True,'source_commit':m['commit'],'web_tree':m['web_tree'],'all_public_tracked_assets':len(captured),'tls_verified':True,'credentials_injected':False,'owner_dynamic_reads':False}
    save(out/'capture.json',result);return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['candidate','live'],required=True);p.add_argument('--preflight-only',action='store_true');p.add_argument('--manifest-sha256',required=True);a=p.parse_args()
    m=seal(a.manifest_sha256)
    if a.preflight_only:
        print(json.dumps({'sealed':True,'input_count':len(m['inputs']),'runtime_unexecuted':True}));return
    assert str(ROOT).startswith('/srv/work/'),'parent-admitted Forum worker only'
    out=ROOT/('results-'+a.phase);out.mkdir(exist_ok=False)
    tmp=ROOT/('t-'+a.phase);tmp.mkdir();os.environ['TMPDIR']=str(tmp)
    report={'accepted':False,'phase':a.phase,'commit':m['commit'],'manifest_sha256':sha((ROOT/'manifest.json').read_bytes())}
    try:
        if a.phase=='candidate':report['fixture']=fixture(out)
        report['capture']=capture(out,m,a.phase)
        env={**os.environ,'QA_OUT':str(out),'QA_PHASE':a.phase}
        # Prefer an explicitly pinned parent toolchain; never silently npm install latest.
        dep=ROOT/'playwright-path.txt';assert dep.exists(),'prepared exact Playwright dependency missing'
        tool=json.loads(dep.read_text());pkg=Path(tool['module'])/'package.json'
        assert sha(pkg.read_bytes())==tool['package_sha256'],'Playwright package changed'
        env['QA_PLAYWRIGHT']=tool['module'];env['QA_BROWSER']=tool['browser']
        assert sha(Path(tool['browser']).read_bytes())==tool['browser_sha256'],'Chromium executable changed'
        r=subprocess.run(['node',str(ROOT/'browser.cjs')],env=env,capture_output=True,timeout=360)
        (out/'browser.stdout').write_bytes(r.stdout);(out/'browser.stderr').write_bytes(r.stderr)
        assert r.returncode==0,'browser failed: inspect synthetic result only'
        browser=json.loads((out/'browser-results.json').read_text());assert browser['accepted'] and browser['all_browser_processes_stopped']
        report['browser']=browser;report['accepted']=True
    except Exception as e:report['failure']=type(e).__name__+': '+str(e)
    finally:save(out/'receipt.json',report);print(json.dumps({'accepted':report['accepted'],'phase':a.phase,'receipt':str(out/'receipt.json'),'failure':report.get('failure')}))
    sys.exit(0 if report['accepted'] else 1)
if __name__=='__main__':main()
