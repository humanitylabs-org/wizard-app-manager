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

# All sentinels are synthetic and can never authenticate to a live service.
PROOF='fixture-credential-never-valid-live'
CONTROL='fixture-visible-control'
PROBE_HEADERS={'X-Wizard-Owner-Proof':PROOF,'X-Redaction-Control':CONTROL}

def error_headers(event):
    return {k.lower():v for k,v in event.get('request',{}).get('headers',{}).items()}

def redaction_pair(before,after):
    """A missing input/snapshot/control is a failure, never evidence of deletion."""
    for event in [before,after]:
        assert event.get('logger')=='http.log.error' and event.get('status')==502,'not the actual 502 error event'
        assert event.get('request',{}).get('uri')=='/error-probe','wrong error path'
        assert error_headers(event).get('x-redaction-control')==[CONTROL],'nonsecret control missing'
    assert error_headers(before).get('x-wizard-owner-proof')==[PROOF],'unfiltered proof positive missing'
    assert 'x-wizard-owner-proof' not in error_headers(after),'filtered proof key retained'
    assert PROOF not in json.dumps(after),'filtered proof value retained'
    # Only proof deletion may change the original-request header snapshot.
    original=dict(error_headers(before));original.pop('x-wizard-owner-proof')
    assert error_headers(after)==original,'non-proof headers changed'
    for key in ['method','uri','host','proto']:
        assert before['request'].get(key)==after['request'].get(key),'request identity changed '+key
    return True

def fixture(out):
    checks=[];auth=[];upstream=[];proc=None;servers=[];threads=[]
    def start_server(handler):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler);servers.append(server)
        thread=threading.Thread(target=server.serve_forever,daemon=True);threads.append(thread);thread.start()
        return server
    class Auth(BaseHTTPRequestHandler):
        def log_message(self,format,*args):pass
        def do_GET(self):
            auth.append({k.lower():v for k,v in self.headers.items()})
            self.send_response(200 if self.headers.get('X-Private-Peer')=='127.0.0.2' else 403)
            self.send_header('Content-Length','0');self.end_headers()
    server=start_server(Auth)
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self,format,*args):pass
        def do_GET(self):
            upstream.append({k.lower():v for k,v in self.headers.items()})
            self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
    producer=start_server(Upstream)
    s=socket.socket();s.bind(('127.0.0.1',0));port=s.getsockname()[1];s.close()
    marker=out/'RELEASE_SHA';commit=json.loads((ROOT/'carrier/build.json').read_text())['commit'];marker.write_text(commit+'\n')
    route=(ROOT/'metadata.Caddyfile').read_text().replace('__CARRIER__',str(ROOT/'carrier')).replace('__SERVED_SHA_FILE__',str(marker)).replace('__SERVED_SHA__',commit).replace('127.0.0.1:19447','127.0.0.1:'+str(server.server_port))
    (out/'route.caddy').write_text(route)
    candidate=(ROOT/'Caddyfile.candidate').read_text()
    # Reuse exact candidate default logger text, not a second implementation.
    logger=candidate[candidate.index('    log default {'):candidate.index('    admin off')]
    config=out/'fixture.caddy'
    # One config generation: change only the default encoder at reload.
    # /error-probe has explicit CLIENT inputs; /producer-probe separately tests
    # server mutation with a successful upstream, then the same closed upstream.
    site=' admin off\n auto_https off\n}\nhttp://127.0.0.1:'+str(port)+' {\n import '+str(out/'route.caddy')+'\n handle /error-probe {\n reverse_proxy 127.0.0.1:1\n }\n handle /producer-probe {\n route {\n request_header X-Wizard-Owner-Proof '+PROOF+'\n request_header X-Redaction-Control '+CONTROL+'\n reverse_proxy 127.0.0.1:'+str(producer.server_port)+'\n }\n }\n handle /neighbor {\n respond "before" 200\n }\n handle {\n respond "missing" 404\n }\n}\n'
    unfiltered='{\n    log default {\n        format json\n    }\n'+site
    filtered='{\n'+logger+site
    (out/'fixture.unfiltered.caddy').write_text(unfiltered)
    (out/'fixture.filtered.caddy').write_text(filtered)
    config.write_text(unfiltered)
    def req(path=PREFIX+'build.json',method='GET',owner=True,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',port,timeout=5,source_address=('127.0.0.2' if owner else '127.0.0.1',0));c.request(method,path,headers=headers or {});r=c.getresponse();b=r.read();d=(r.status,b,dict(r.getheaders()));c.close();return d
    def check(n,v):checks.append({'name':n,'pass':bool(v)});assert v,n
    result={'accepted':False,'checks':checks,'synthetic':True}
    log=(out/'fixture.log').open('wb')
    # Byte-identical request through the same listener before/after encoder reload.
    probe_wire=('GET /error-probe HTTP/1.1\r\nHost: 127.0.0.1:'+str(port)+'\r\nAccept-Encoding: identity\r\nConnection: close\r\n'+''.join(k+': '+v+'\r\n' for k,v in PROBE_HEADERS.items())+'\r\n').encode('ascii')
    def probe():
        with socket.create_connection(('127.0.0.1',port),timeout=5,source_address=('127.0.0.2',0)) as sock:
            sock.sendall(probe_wire);response=http.client.HTTPResponse(sock);response.begin();response.read();return response.status
    def entries_since(offset):
        log.flush();entries=[]
        for line in (out/'fixture.log').read_bytes()[offset:].splitlines():
            try:entries.append(json.loads(line))
            except ValueError:pass
        return entries
    def error_event(path,offset):
        for _ in range(50):
            events=[x for x in entries_since(offset) if x.get('logger')=='http.log.error' and x.get('request',{}).get('uri')==path]
            if events:
                check('one actual 502 event '+path,len(events)==1 and events[0].get('status')==502)
                return events[0]
            time.sleep(.1)
        raise AssertionError('no actual error record '+path)
    def reload_config(body,label):
        assert proc is not None
        offset=(out/'fixture.log').stat().st_size;old=proc.pid
        config.write_text(body);proc.send_signal(signal.SIGUSR1)
        for _ in range(80):
            assert proc.poll() is None,'fixture exited during reload'
            events=[x for x in entries_since(offset) if x.get('msg')=='successfully reloaded config from file']
            if events:break
            time.sleep(.1)
        else:raise AssertionError('positive fixture reload event missing '+label)
        check('same binary same PID '+label,proc.pid==old and proc.poll() is None)
        return {'same_pid':True,'config_sha256':sha(body.encode()),'positive_event':'successfully reloaded config from file'}
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
        check('encoded query refused',req(PREFIX+'build.json?x=%2F')[0]==400)
        check('encoded child path refused',req(PREFIX+'build.json%2Fother')[0]==404)
        check('auth never receives proof',all('x-wizard-owner-proof' not in a for a in auth))
        # Negative encoder control: the REAL unfiltered error must expose both
        # synthetic inputs, otherwise a later absence cannot establish deletion.
        offset=(out/'fixture.log').stat().st_size
        check('actual unfiltered reverse-proxy 502',probe()==502)
        before=error_event('/error-probe',offset)
        save(out/'error.unfiltered.json',before)
        check('unfiltered client proof positive',error_headers(before).get('x-wizard-owner-proof')==[PROOF])
        check('unfiltered nonsecret control positive',error_headers(before).get('x-redaction-control')==[CONTROL])
        # Independent producer flow. No client proof/control, explicit ordered
        # request_header, actual successful upstream receipt, then same route and
        # config with that owned upstream stopped. Log snapshot is observational.
        check('server-generated proof upstream success',req('/producer-probe')[0]==200)
        check('upstream really received generated proof and control',len(upstream)==1 and upstream[0].get('x-wizard-owner-proof')==PROOF and upstream[0].get('x-redaction-control')==CONTROL)
        save(out/'producer-upstream-receipt.json',{'synthetic':True,'headers':upstream[0],'config_sha256':sha(config.read_bytes()),'pid':proc.pid})
        producer.shutdown();producer.server_close();threads[1].join(timeout=3)
        check('producer upstream stopped',not threads[1].is_alive())
        offset=(out/'fixture.log').stat().st_size
        check('same producer route now returns 502',req('/producer-probe')[0]==502)
        producer_error=error_event('/producer-probe',offset)
        check('producer config generation unchanged',config.read_text()==unfiltered)
        snapshot=error_headers(producer_error)
        proof_seen=snapshot.get('x-wizard-owner-proof')==[PROOF]
        control_seen=snapshot.get('x-redaction-control')==[CONTROL]
        result['producer_flow']={'client_proof_supplied':False,'client_control_supplied':False,'upstream_proof_received':True,'upstream_control_received':True,'same_generation_failure':True,'unfiltered_error_proof_visible':proof_seen,'unfiltered_error_control_visible':control_seen,'snapshot':('mutated-request' if proof_seen and control_seen else 'original-request' if not proof_seen and not control_seen else 'mixed'),'error_sha256':sha(json.dumps(producer_error,sort_keys=True).encode()),'redaction_proof':False}
        save(out/'producer-flow.json',result['producer_flow'])
        check('producer snapshot classification consistent',proof_seen==control_seen)
        result['encoder_reload']=reload_config(filtered,'encoder reload')
        offset=(out/'fixture.log').stat().st_size
        check('actual filtered reverse-proxy 502',probe()==502)
        after=error_event('/error-probe',offset)
        save(out/'error.filtered.json',after)
        check('discriminating runtime redaction pair',redaction_pair(before,after))
        # Feed the real unfiltered event into the filtered oracle: the no-encoder
        # negative must fail, not just a fabricated unit fixture.
        try:redaction_pair(before,before)
        except AssertionError:negative=True
        else:negative=False
        check('actual no-encoder negative rejected',negative)
        result['redaction']={'same_request_bytes_sha256':sha(probe_wire),'byte_identical_client_request':True,'unfiltered_config_sha256':sha(unfiltered.encode()),'filtered_config_sha256':sha(filtered.encode()),'only_encoder_changed':unfiltered.split(' admin off',1)[1]==filtered.split(' admin off',1)[1],'unfiltered_proof_and_control_present':True,'filtered_control_retained':True,'filtered_proof_key_and_value_absent':True,'actual_unfiltered_negative_rejected':negative,'logger_source':'exact Caddyfile.candidate default block'}
        check('only encoder changes between controls',result['redaction']['only_encoder_changed'])
        result['neighbor_reload']=reload_config(filtered.replace('respond "before"','respond "after"'),'neighbor reload')
        check('neighbor change loaded',req('/neighbor')[1]==b'after')
        check('metadata after reload',req()[1]==(ROOT/'carrier/build.json').read_bytes())
        result['accepted']=True
    except Exception as e:result['failure']=type(e).__name__+': '+str(e)
    finally:
        if proc:
            proc.terminate()
            try:proc.wait(timeout=6)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=3)
        for s in servers:s.shutdown();s.server_close()
        for thread in threads:thread.join(timeout=3)
        log.close();result['all_processes_stopped']=(proc is None or proc.poll() is not None) and all(not t.is_alive() for t in threads);result['check_count']=len(checks);result['log_sha256']=sha((out/'fixture.log').read_bytes())
        if not result['all_processes_stopped']:result.update(accepted=False,failure='owned fixture process/thread cleanup incomplete')
        save(out/'fixture-results.json',result)
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
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['candidate','live'],required=True);p.add_argument('--preflight-only',action='store_true');p.add_argument('--browser-only',action='store_true');p.add_argument('--manifest-sha256',required=True);a=p.parse_args()
    m=seal(a.manifest_sha256)
    component=None
    if a.browser_only:
        from inherit_security import verify
        component=verify(ROOT)
    if a.preflight_only:
        print(json.dumps({'sealed':True,'input_count':len(m['inputs']),'runtime_unexecuted':True}));return
    assert str(ROOT).startswith('/srv/work/'),'parent-admitted Forum worker only'
    out=ROOT/('results-'+a.phase);out.mkdir(exist_ok=False)
    tmp=ROOT/('t-'+a.phase);tmp.mkdir();os.environ['TMPDIR']=str(tmp)
    report={'accepted':False,'phase':a.phase,'commit':m['commit'],'manifest_sha256':sha((ROOT/'manifest.json').read_bytes())}
    try:
        if a.phase=='candidate' and not a.browser_only:
            tests=subprocess.run([sys.executable,'-B',str(ROOT/'test_forum_qa.py')],capture_output=True,timeout=30)
            (out/'source-regressions.stdout').write_bytes(tests.stdout);(out/'source-regressions.stderr').write_bytes(tests.stderr)
            assert tests.returncode==0,'synthetic source/oracle regressions failed'
            report['source_regressions']=json.loads(tests.stdout);assert report['source_regressions']['accepted']
            report['fixture']=fixture(out)
        if component:
            report['security_component_inheritance']=component
            report['source_regressions']=component['source_regressions']
            report['fixture']=component['fixture']
            report['security_fixture_rerun']=False
            report['prior_full_receipt_accepted']=False
        report['capture']=capture(out,m,a.phase)
        env={**os.environ,'QA_OUT':str(out),'QA_PHASE':a.phase}
        # Prefer an explicitly pinned parent toolchain; never silently npm install latest.
        dep=ROOT/'playwright-path.txt';assert dep.exists(),'prepared exact Playwright dependency missing'
        tool=json.loads(dep.read_text());pkg=Path(tool['module'])/'package.json'
        assert sha(pkg.read_bytes())==tool['package_sha256'],'Playwright package changed'
        env['QA_PLAYWRIGHT']=tool['module'];env['QA_BROWSER']=tool['browser']
        assert sha(Path(tool['browser']).read_bytes())==tool['browser_sha256'],'Chromium executable changed'
        assert sha(Path(tool['node']).read_bytes())==tool['node_sha256'],'Node executable changed'
        for rel,h in tool['transport_source_sha256'].items():
            assert sha((ROOT/rel).read_bytes())==h,'Playwright private transport changed'
        guard=subprocess.run([tool['node'],str(ROOT/'test_browser_contract.cjs')],capture_output=True,timeout=20)
        (out/'browser-contract.stdout').write_bytes(guard.stdout);(out/'browser-contract.stderr').write_bytes(guard.stderr)
        assert guard.returncode==0,'browser-free guard/catalogue regression failed'
        report['browser_contract']=json.loads(guard.stdout)
        with (out/'browser.stdout').open('wb') as stdout,(out/'browser.stderr').open('wb') as stderr:
            r=subprocess.Popen([tool['node'],str(ROOT/'browser.cjs')],env=env,stdout=stdout,stderr=stderr,start_new_session=True)
            try:r.wait(timeout=360)
            finally:
                try:os.killpg(r.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:r.wait(timeout=5)
                except subprocess.TimeoutExpired:os.killpg(r.pid,signal.SIGKILL);r.wait(timeout=3)
                alive=True
                for _ in range(25):
                    try:os.killpg(r.pid,0)
                    except ProcessLookupError:alive=False;break
                    time.sleep(.1)
                if alive:
                    try:os.killpg(r.pid,signal.SIGKILL)
                    except ProcessLookupError:alive=False
                    # A successful kill syscall is not absence; re-read the target.
                    for _ in range(25):
                        try:os.killpg(r.pid,0)
                        except ProcessLookupError:alive=False;break
                        time.sleep(.1)
                report['browser_process_group_absent']=not alive
        assert r.returncode==0,'browser failed: inspect synthetic result only'
        assert report['browser_process_group_absent'],'browser descendant cleanup not proven'
        browser=json.loads((out/'browser-results.json').read_text());assert browser['accepted'] and browser['browser_disconnected']
        browser['all_browser_processes_stopped']=report['browser_process_group_absent']
        report['browser']=browser;report['accepted']=True
    except Exception as e:report['failure']=type(e).__name__+': '+str(e)
    finally:save(out/'receipt.json',report);print(json.dumps({'accepted':report['accepted'],'phase':a.phase,'receipt':str(out/'receipt.json'),'failure':report.get('failure')}))
    sys.exit(0 if report['accepted'] else 1)
if __name__=='__main__':main()
