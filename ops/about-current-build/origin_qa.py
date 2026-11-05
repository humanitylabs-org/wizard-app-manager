#!/usr/bin/env python3
"""Parent-admitted immutable native nginx + Caddy acceptance, never a live publisher."""
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time

ROOT=Path(__file__).resolve().parent
IMAGE='sha256:88f52f3d58a505b7e1ad96ca6969478517bcccb58418e75a54223271a64da7df'
PREFIX='/wizard-os-home/'
ORIGIN='https://wizard.tailc0f369.ts.net'
PROOF='synthetic-native-origin-proof-never-valid-live'

def sha(b):return hashlib.sha256(b).hexdigest()
def save(p,d):p.write_text(json.dumps(d,indent=2)+'\n')
def module(path):
    spec=importlib.util.spec_from_file_location('sealed_comparison',path);assert spec and spec.loader
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

def seal(expected):
    assert sha((ROOT/'manifest.json').read_bytes())==expected,'parent manifest mismatch'
    m=json.loads((ROOT/'manifest.json').read_text())
    for n,h in m['inputs'].items():assert sha((ROOT/n).read_bytes())==h,'sealed input drift '+n
    for n,h in json.loads((ROOT/'deps-closure.json').read_text()).items():assert sha((ROOT/n).read_bytes())==h,'dependency drift '+n
    return m

def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def unpack(data,target):
    target.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data)) as t:t.extractall(target,filter='data')

def request(port,path=PREFIX+'build.json',method='GET',owner=True,headers=None):
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=8,source_address=('127.0.0.1' if owner else '127.0.0.2',0))
    try:
        c.request(method,path,headers=headers or {});r=c.getresponse();return r.status,r.read(),{k.lower():v for k,v in r.getheaders()}
    finally:c.close()

def stop_group(proc):
    try:os.killpg(proc.pid,signal.SIGTERM)
    except ProcessLookupError:pass
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=3)
    for attempt in range(50):
        try:os.killpg(proc.pid,0)
        except ProcessLookupError:return True
        if attempt==24:
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:return True
        time.sleep(.1)
    return False

class NativeOrigin:
    def __init__(self,out,report):
        self.out=out;self.report=report;self.containers=[];self.servers=[];self.threads=[];self.nginx=None;self.proc=None
        self.nginx_port=free_port();self.port=free_port();self.auth_calls=[];self.comparison_calls=[]
        self.candidate=out/'candidate';unpack((ROOT/'source/release.tar').read_bytes(),self.candidate)
        self.provenance=json.loads((ROOT/'source/provenance.json').read_text());self.commit=self.provenance['fixture_commit']
        self.comparison=json.loads((ROOT/'source/comparison.json').read_text())
        raw=json.loads((ROOT/'source/comparison-input.json').read_text());mod=module(ROOT/'source/main_compare.py')
        expected_url='https://api.github.com/repos/humanitylabs-org/wizard-ux/compare/'+self.commit+'...main?per_page=1&page=2'
        def fetch(url):assert url==expected_url;return raw
        replay=mod.compare(self.commit,fetch=fetch)
        self.check('same-input deployed module comparison',all(self.comparison[k]==replay[k] for k in ['commit','base','behind','own']))
        save(out/'comparison.json',self.comparison)
        config='pid /tmp/nginx.pid;\nerror_log stderr warn;\nevents {}\nhttp {\ninclude /etc/nginx/mime.types;\ndefault_type application/octet-stream;\naccess_log off;\nclient_body_temp_path /tmp/client_temp;\nproxy_temp_path /tmp/proxy_temp;\nfastcgi_temp_path /tmp/fastcgi_temp;\nuwsgi_temp_path /tmp/uwsgi_temp;\nscgi_temp_path /tmp/scgi_temp;\nserver {listen 8080; root /srv;\n'+(ROOT/'source/static-headers.conf').read_text()+'\nlocation /wizard-os-home/ {try_files $uri =404; limit_except GET {deny all;}}}\n}\n'
        self.nginx_config=out/'nginx.conf';self.nginx_config.write_text(config)
        self.nginx_config.chmod(0o644)
        self.log=(out/'native-caddy.log').open('wb')

    def check(self,name,passed,detail=None):
        row={'name':name,'pass':bool(passed)}
        if detail is not None:row['detail']=detail
        self.report.setdefault('checks',[]).append(row);save(self.out/'native-results.json',self.report)
        assert passed,name

    def server(self,handler):
        s=ThreadingHTTPServer(('127.0.0.1',0),handler);t=threading.Thread(target=s.serve_forever,daemon=True);t.start();self.servers.append(s);self.threads.append(t);return s.server_port

    def bind(self,release):
        # Mirror production's fixed read-only web bind, not a dynamic current link.
        if self.nginx:
            subprocess.run(['docker','stop','--time','3',self.nginx],check=True,capture_output=True,timeout=15)
        name='ab-origin-'+sha(str(self.out).encode())[:10]+'-'+str(len(self.containers))
        self.containers.append(name)
        p=subprocess.run(['docker','run','--detach','--pull=never','--name',name,'--label','wizard.qa=about-origin','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--user','101:101','--tmpfs','/tmp:rw,noexec,nosuid,size=16m','--publish','127.0.0.1:'+str(self.nginx_port)+':8080','--mount','type=bind,src='+str(release/'web')+',dst=/srv/wizard-os-home,readonly','--mount','type=bind,src='+str(self.nginx_config)+',dst=/fixture/nginx.conf,readonly','--entrypoint','nginx',IMAGE,'-c','/fixture/nginx.conf','-g','daemon off;'],capture_output=True,text=True,timeout=30)
        assert p.returncode==0,p.stderr
        self.nginx=name
        d=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
        mount=next(m for m in d['Mounts'] if m['Destination']=='/srv/wizard-os-home')
        self.check('actual nginx fixed read-only mount '+name,mount['Source']==str(release/'web') and mount['RW'] is False)
        for _ in range(60):
            try:
                if request(self.nginx_port,PREFIX+'core/box-source.js')[0]==200:break
            except OSError:pass
            time.sleep(.1)
        else:raise AssertionError('native nginx startup timeout')

    def start(self):
        outer=self
        class Auth(BaseHTTPRequestHandler):
            def log_message(self,format,*args):pass
            def do_GET(self):
                outer.auth_calls.append({k.lower():v for k,v in self.headers.items()});self.send_response(200 if self.headers.get('X-Private-Peer')=='127.0.0.1' else 403);self.send_header('Content-Length','0');self.end_headers()
        auth_port=self.server(Auth)
        class Compare(BaseHTTPRequestHandler):
            def log_message(self,format,*args):pass
            def do_GET(self):
                good=self.headers.get('X-Wizard-Owner-Proof')==PROOF
                status=200 if good and self.path==PREFIX+'store-install/_main-compare?commit='+outer.commit else 403
                outer.comparison_calls.append({'path':self.path,'authorized':good})
                body=json.dumps(outer.comparison).encode() if status==200 else b'{}'
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        self.compare_port=self.server(Compare)
        route=(ROOT/'metadata.Caddyfile').read_text().replace('__SHELL_STATIC_UPSTREAM__','127.0.0.1:'+str(self.nginx_port)).replace('127.0.0.1:19447','127.0.0.1:'+str(auth_port))
        self.route=self.out/'metadata.fixture.caddy';self.route.write_text(route)
        prefix=route[route.index('    route {')+len('    route {'):route.index('        @badBuildMethod')]
        compare='handle '+PREFIX+'store-install/_main-compare {\nroute {\n'+prefix+'\n@bad not method GET\nrespond @bad 405\nrequest_header X-Wizard-Owner-Proof '+PROOF+'\nreverse_proxy 127.0.0.1:'+str(self.compare_port)+'\n}\n}\n'
        site='admin off\nauto_https off\n}\nhttp://127.0.0.1:'+str(self.port)+' {\nimport '+str(self.route)+'\n'+compare+'handle /error-probe {\nreverse_proxy 127.0.0.1:1\n}\nhandle /neighbor {\nrespond "before" 200\n}\nhandle {\nreverse_proxy 127.0.0.1:'+str(self.nginx_port)+'\n}\n}\n'
        self.unfiltered='{\nlog default {\nformat json\n}\n'+site
        self.filtered='{\n'+(ROOT/'source/default-logger.caddy').read_text()+site
        self.config=self.out/'native.caddy';self.config.write_text(self.unfiltered)
        for text,name in [(self.unfiltered,'unfiltered'),(self.filtered,'filtered')]:
            q=self.out/(name+'.caddy');q.write_text(text)
            result=subprocess.run([str(ROOT/'caddy-private-gateway'),'validate','--config',str(q),'--adapter','caddyfile'],capture_output=True,timeout=20)
            (self.out/(name+'-validate.log')).write_bytes(result.stderr+result.stdout)
            self.check('native Caddy validates '+name,result.returncode==0)
        self.bind(self.candidate)
        self.proc=subprocess.Popen([str(ROOT/'caddy-private-gateway'),'run','--config',str(self.config),'--adapter','caddyfile'],env={**os.environ,'XDG_CONFIG_HOME':str(self.out/'xdg'),'XDG_DATA_HOME':str(self.out/'data')},stdout=self.log,stderr=subprocess.STDOUT)
        for _ in range(80):
            assert self.proc.poll() is None,'native Caddy exited'
            try:
                if request(self.port,'/neighbor')[0]==200:return
            except OSError:pass
            time.sleep(.1)
        raise AssertionError('native Caddy startup timeout')

    def events(self,offset,path):
        self.log.flush()
        for _ in range(50):
            events=[]
            for line in (self.out/'native-caddy.log').read_bytes()[offset:].splitlines():
                try:d=json.loads(line)
                except ValueError:continue
                if d.get('logger')=='http.log.error' and d.get('request',{}).get('uri')==path:events.append(d)
            if events:
                assert len(events)==1 and events[0]['status']==502;return events[0]
            time.sleep(.1)
        raise AssertionError('actual error event missing')

    def check_guards_and_logger(self):
        assert self.proc is not None
        status,body,headers=request(self.port)
        self.check('actual nginx archived commit',status==200 and body==('{"commit":"'+self.commit+'"}\n').encode())
        self.check('private no-store JSON without validators',headers.get('content-type')=='application/json' and headers.get('cache-control')=='private, no-store' and 'etag' not in headers and 'last-modified' not in headers)
        for method in ['POST','PUT','PATCH','DELETE','HEAD','OPTIONS']:
            self.check('owner wrong method '+method,request(self.port,method=method)[0]==405)
        self.check('anonymous denied',request(self.port,owner=False)[0]==403)
        for headers in [{'X-Wizard-Owner-Proof':PROOF},{'Tailscale-User-Login':'forged@example.invalid','Tailscale-User-Name':'forged','X-Private-Peer':'127.0.0.1'}]:
            self.check('forged client authority denied '+str(len(self.report['checks'])),request(self.port,owner=False,headers=headers)[0]==403)
        for path,headers,expected in [(PREFIX+'build.json',{'Origin':'https://foreign.invalid'},403),(PREFIX+'build.json',{'Sec-Fetch-Site':'cross-site'},403),(PREFIX+'build.json?x=1',{},400)]:
            self.check('guard '+str(headers)+path,request(self.port,path,headers=headers)[0]==expected)
        for path in [PREFIX+'build.json/x',PREFIX+'build.jsonx',PREFIX+'%2e%2e/build.json']:
            self.check('neighbor not delegated '+path,request(self.port,path)[0] not in [200,201])
        self.check('neighbor unchanged',request(self.port,'/neighbor')[:2]==(200,b'before'))
        self.check('direct metadata backend nonowner denied',request(self.compare_port,PREFIX+'store-install/_main-compare?commit='+self.commit)[0]==403)
        self.check('owner compare exact snapshot',json.loads(request(self.port,PREFIX+'store-install/_main-compare?commit='+self.commit)[1])==self.comparison)
        self.check('auth strips client proof and identity',all('x-wizard-owner-proof' not in h and 'tailscale-user-login' not in h and 'tailscale-user-name' not in h for h in self.auth_calls))
        probe={'X-Wizard-Owner-Proof':PROOF,'X-Redaction-Control':'synthetic-control'}
        offset=(self.out/'native-caddy.log').stat().st_size
        self.check('unfiltered positive failure',request(self.port,'/error-probe',headers=probe)[0]==502)
        before=self.events(offset,'/error-probe');bh={k.lower():v for k,v in before['request']['headers'].items()}
        self.check('unfiltered positive proof sentinel',bh.get('x-wizard-owner-proof')==[PROOF])
        offset=(self.out/'native-caddy.log').stat().st_size;pid=self.proc.pid
        self.config.write_text(self.filtered);self.proc.send_signal(signal.SIGUSR1)
        for _ in range(80):
            assert self.proc.poll() is None
            positive=False
            for line in (self.out/'native-caddy.log').read_bytes()[offset:].splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                if event.get('msg') in {'successfully reloaded config from file','config reloaded','Reload succeeded','reload complete','reloaded config','successfully loaded new config'}:positive=True
            if positive:break
            time.sleep(.1)
        else:raise AssertionError('positive same-process reload missing')
        self.check('default filter reloaded same PID',self.proc.pid==pid and self.proc.poll() is None)
        offset=(self.out/'native-caddy.log').stat().st_size
        self.check('filtered positive failure',request(self.port,'/error-probe',headers=probe)[0]==502)
        after=self.events(offset,'/error-probe');ah={k.lower():v for k,v in after['request']['headers'].items()}
        self.check('runtime proof key and value deleted','x-wizard-owner-proof' not in ah and PROOF not in json.dumps(after))
        bh.pop('x-wizard-owner-proof')
        self.check('nonproof error headers preserved',ah==bh and ah.get('x-redaction-control')==['synthetic-control'])
        self.check('one loaded default encoder',self.filtered.count('log default {')==1)
        self.check('metadata unaffected by encoder reload',request(self.port)[:2]==(status,body))

    def atomic_releases(self,tool):
        assert self.proc is not None
        repo=self.out/'synthetic-repo';unpack((ROOT/'source/raw.tar').read_bytes(),repo)
        def git(*args):return subprocess.check_output(['git','-c','core.hooksPath=/dev/null','-C',str(repo),*args],text=True).strip()
        git('init','--quiet');git('config','user.name','Synthetic Native QA');git('config','user.email','synthetic@example.invalid')
        def release(label):
            (repo/'web/qa-stamp-sentinel.txt').write_text(label+'\n');git('add','.');git('commit','--quiet','-m','synthetic native '+label)
            commit=git('rev-parse','HEAD');temp=self.out/('release-'+label+'.preparing');unpack(subprocess.check_output(['git','-C',str(repo),'archive',commit]),temp)
            (temp/'RELEASE_SHA').write_text(commit+'\n')
            # Durable preparation precedes visible release rename and bind.
            for name in ['web/build.json','web/qa-stamp-sentinel.txt','RELEASE_SHA']:
                with (temp/name).open('rb') as f:os.fsync(f.fileno())
            for directory in [temp/'web',temp]:
                fd=os.open(directory,os.O_DIRECTORY);os.fsync(fd);os.close(fd)
            target=self.out/('release-'+label);os.replace(temp,target)
            fd=os.open(self.out,os.O_DIRECTORY);os.fsync(fd);os.close(fd)
            return target,commit
        a,ca=release('A');b,cb=release('B');current=self.out/'current';current.symlink_to(a)
        self.bind(a);pid=self.proc.pid
        self.check('release A stamp and actual asset agree',json.loads(request(self.port)[1])['commit']==ca and request(self.port,PREFIX+'qa-stamp-sentinel.txt')[1]==b'A\n')
        nxt=self.out/'next';nxt.symlink_to(b);os.replace(nxt,current)
        self.check('fixed nginx bind cannot follow unrelated current',current.resolve()==b and json.loads(request(self.port)[1])['commit']==ca and request(self.port,PREFIX+'qa-stamp-sentinel.txt')[1]==b'A\n')
        self.bind(b)
        self.check('release B stamp and asset agree without Caddy reload',json.loads(request(self.port)[1])['commit']==cb and request(self.port,PREFIX+'qa-stamp-sentinel.txt')[1]==b'B\n' and self.proc.pid==pid)
        invalids=[None,'', '{"commit":"$Format:%H$"}', '{"commit":"'+('A'*40)+'"}', '{"commit":"'+('a'*39)+'"}', '{"commit":"'+('a'*41)+'"}', 'not JSON']
        invalid_report=[]
        for i,value in enumerate(invalids):
            target=self.out/('invalid-'+str(i));shutil.copytree(b,target)
            f=target/'web/build.json'
            if value is None:f.unlink()
            else:f.write_text(value)
            self.bind(target);status,body,_=request(self.port)
            result=subprocess.run([tool['node'],str(ROOT/'consumer_probe.cjs'),str(b/'web/core/box-source.js')],input=json.dumps({'status':status,'body':body.decode()}).encode(),capture_output=True,timeout=15)
            self.check('actual consumer rejects origin invalid/missing '+str(i),result.returncode==0,{'status':status,'missing':value is None})
            invalid_report.append(json.loads(result.stdout))
            self.check('anonymous denial precedes missing/malformed metadata '+str(i),request(self.port,owner=False)[0]==403)
        save(self.out/'invalid-consumer-results.json',invalid_report)
        self.bind(self.candidate)
        self.report['atomic_fixture']={'A':ca,'B':cb,'real_synthetic_git_commits':True,'current_not_metadata_authority':True,'caddy_pid_unchanged':self.proc.pid==pid,'invalid_http_200_rejected_by_actual_consumer_not_server_json_parser':True}

    def capture(self):
        static={}
        for rel,expected in self.provenance['static'].items():
            status,body,_=request(self.port,PREFIX+rel)
            self.check('tracked native bytes '+rel,status==200 and sha(body)==expected['sha256'])
            static[rel]={'sha256':sha(body),'status':status}
        status,body,_=request(self.port)
        self.check('fixture source stamp still matches archive',status==200 and json.loads(body)['commit']==self.commit)
        (self.out/'build.json').write_bytes(body)
        save(self.out/'capture.json',{'source_commit':self.commit,'web_tree':self.provenance['fixture_web_tree'],'static':static,'immutable_native_fixture':True,'live_source_capture':False,'owner_dynamic_reads':False})

    def close(self):
        if self.proc:
            self.proc.terminate()
            try:self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=5)
        for name in self.containers:
            subprocess.run(['docker','rm','--force',name],capture_output=True,check=True,timeout=15)
            p=subprocess.run(['docker','inspect',name],capture_output=True,timeout=10)
            assert p.returncode!=0,'fixture container still exists'
        for s in self.servers:s.shutdown();s.server_close()
        for t in self.threads:t.join(timeout=5);assert not t.is_alive()
        self.log.close();self.report['all_native_processes_stopped']=True


def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest-sha256',required=True);p.add_argument('--preflight-only',action='store_true');a=p.parse_args();m=seal(a.manifest_sha256)
    if a.preflight_only:print(json.dumps({'sealed':True,'input_count':len(m['inputs']),'native_unrun':True}));return
    assert str(ROOT).startswith('/srv/work/ab-'),'Forum parent admission only'
    out=ROOT/'results-origin';out.mkdir(exist_ok=False);tmp=ROOT/'runtime-tmp';tmp.mkdir();os.environ['TMPDIR']=str(tmp)
    os.environ.update({'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'})
    report={'accepted':False,'phase':'candidate-immutable-origin','manifest_sha256':a.manifest_sha256,'live_modified':False,'live_acceptance':False,'legacy_43_checks_inherited':False,'checks':[]};fixture=None
    try:
        tool=json.loads((ROOT/'playwright-path.txt').read_text())
        for key in ['node','browser']:
            assert sha(Path(tool[key]).read_bytes())==tool[key+'_sha256']
        for argv,name in [([tool['node'],str(ROOT/'test_browser_contract.cjs')],'browser-contract'),([sys.executable,'-B',str(ROOT/'test_origin_qa.py')],'origin-unit')]:
            result=subprocess.run(argv,capture_output=True,timeout=40);(out/(name+'.stdout')).write_bytes(result.stdout);(out/(name+'.stderr')).write_bytes(result.stderr);assert result.returncode==0,name+' failed'
        fixture=NativeOrigin(out,report)
        result=subprocess.run([sys.executable,'-B',str(fixture.candidate/'tools/test_build_stamp_archive.py')],capture_output=True,timeout=30);(out/'archive-regressions.log').write_bytes(result.stderr+result.stdout);assert result.returncode==0,'archive/Dockerfile regressions failed'
        fixture.start();fixture.check_guards_and_logger();fixture.atomic_releases(tool);fixture.capture()
        env={**os.environ,'QA_OUT':str(out),'QA_PHASE':'candidate','QA_TRANSPORT_ORIGIN':'http://127.0.0.1:'+str(fixture.port),'QA_PLAYWRIGHT':tool['module'],'QA_BROWSER':tool['browser']}
        with (out/'browser.stdout').open('wb') as stdout,(out/'browser.stderr').open('wb') as stderr:
            proc=subprocess.Popen([tool['node'],str(ROOT/'browser.cjs')],env=env,stdout=stdout,stderr=stderr,start_new_session=True)
            try:proc.wait(timeout=360)
            finally:report['browser_process_group_absent']=stop_group(proc)
        assert proc.returncode==0,'native browser failed; inspect retained cases/screenshots'
        assert report['browser_process_group_absent'],'browser process-group cleanup unproven'
        browser=json.loads((out/'browser-results.json').read_text());assert browser['accepted'] and browser['browser_disconnected']
        browser['all_browser_processes_stopped']=True;save(out/'browser-results.json',browser);report['browser']=browser
        report['checks_passed']=len(report['checks']);report['accepted']=all(c['pass'] for c in report['checks'])
    except Exception as e:report['failure']=type(e).__name__+': '+str(e)
    finally:
        if fixture:
            try:fixture.close()
            except Exception as e:report['accepted']=False;report['cleanup_failure']=str(e)
        save(out/'receipt.json',report);print(json.dumps({'accepted':report['accepted'],'receipt':str(out/'receipt.json'),'failure':report.get('failure'),'live_acceptance':False}))
    sys.exit(0 if report['accepted'] else 1)

if __name__=='__main__':main()
