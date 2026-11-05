#!/usr/bin/env python3
"""Public metadata only: real peer TLS, comparison engine, exact served formatter."""
import datetime, hashlib, importlib.util, json, shlex, socket, ssl, subprocess, sys, urllib.request, urllib.error
from pathlib import Path
sys.dont_write_bytecode=True
R=Path(__file__).resolve().parent
sys.path.insert(0,str(R))
from activate_about import CONTROL,SOURCE,SHA,OLD,apps,runtimes,val,sha,check_source,outputs
ORIGIN='https://wizard.tailc0f369.ts.net'
HOST='wizard.tailc0f369.ts.net'
COMMIT='0f03360e91c28656296021d61e9d8c1f08937cdb'
PATH='/wizard-os-home/store-install/_main-compare'
d=json.loads((R/'live-freeze.json').read_text());before,out=outputs(d)

def peer():
 code=r'''import urllib.request,urllib.error,hashlib,json,base64
origin=ORIGIN
path=PATH
commit=COMMIT
cases=[('owner-plain','GET',path+'?commit='+commit,{}),('owner-origin','GET',path+'?commit='+commit,{'Origin':origin}),('foreign-origin','GET',path+'?commit='+commit,{'Origin':'https://foreign.invalid'}),('cross-site','GET',path+'?commit='+commit,{'Sec-Fetch-Site':'cross-site'})]
for method in ['HEAD','POST','PUT','PATCH','DELETE','OPTIONS']:cases.append(('unsafe-'+method,method,path+'?commit='+commit,{}))
for label,q in [('missing',''),('bad','?commit=HEAD'),('extra','?commit='+commit+'&repo=evil/repo'),('duplicate','?commit='+commit+'&commit='+commit),('encoded','?commit=%30'+commit[1:])]:cases.append(('query-'+label,'GET',path+q,{}))
for p in [path+'/',path+'/extra','/wizard-os-home/store-install/_updates','/wizard-os-home/store-install/some-app']:
 cases.append(('neighbor','GET',p,{}))
for p in [path.replace('_main','_%6dain'),path.replace('/_main','/%2f_main'),path.replace('/_main','//_main')]:cases.append(('encoded-path','GET',p+'?commit='+commit,{}))
cases.append(('static','GET','/wizard-os-home/core/box-source.js',{}))
for label,method,p,headers in cases:
 try:r=urllib.request.urlopen(urllib.request.Request(origin+p,method=method,headers={'User-Agent':'WAM-About-readonly-acceptance',**headers}),timeout=30)
 except urllib.error.HTTPError as e:r=e
 b=r.read();row={'case':label,'method':method,'path':p,'status':r.status,'sha256':hashlib.sha256(b).hexdigest(),'tls_verification':True,'no_auth_headers_injected':True,'cache_control':r.headers.get('Cache-Control'),'content_type':r.headers.get('Content-Type')}
 if label in ['owner-plain','owner-origin']:row['response']=json.loads(b)
 if label=='static':row['public_static_base64']=base64.b64encode(b).decode()
 print(json.dumps(row))
'''.replace('origin=ORIGIN','origin='+repr(ORIGIN)).replace('path=PATH','path='+repr(PATH)).replace('commit=COMMIT','commit='+repr(COMMIT))
 raw=subprocess.check_output(['ssh','-o','BatchMode=yes','forum','python3 -c '+shlex.quote(code)],text=True,timeout=180)
 rows=[json.loads(x) for x in raw.splitlines()]
 import base64
 static=next(x for x in rows if x['case']=='static')
 b=base64.b64decode(static.pop('public_static_base64'))
 assert sha(b)==sha(CONTROL['box_source'].read_bytes())
 (R/'served-box-source.js').write_bytes(b)
 return rows

def local_request(port,path,headers=None):
 try:r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:'+str(port)+path,headers=headers or {}),timeout=20)
 except urllib.error.HTTPError as e:r=e
 b=r.read()
 return {'status':r.status,'sha256':sha(b),'response':json.loads(b)}

def denied_gateway(headers):
 # Never impersonate a real Tailscale peer: PROXY source stays loopback/non-owner.
 with socket.create_connection(('127.0.0.1',19445),timeout=10) as s:
  s.sendall(b'PROXY TCP4 127.0.0.1 127.0.0.1 55555 19445\r\n')
  with ssl.create_default_context().wrap_socket(s,server_hostname=HOST) as t:
   h=''.join(k+': '+v+'\r\n' for k,v in headers.items())
   t.sendall(('GET '+PATH+'?commit='+COMMIT+' HTTP/1.1\r\nHost: '+HOST+'\r\nConnection: close\r\n'+h+'\r\n').encode())
   data=b''
   while True:
    b=t.recv(65536)
    if not b: break
    data+=b
 status=int(data.split(b'\r\n',1)[0].split()[1])
 return {'status':status,'tls_verification':True,'proxy_source':'127.0.0.1','forged_headers':bool(headers),'response_sha256':sha(data)}

rows=peer()
owner=[x for x in rows if x['case'].startswith('owner')]
assert all(x['status']==200 and 'application/json' in x['content_type'] and x['cache_control']=='private, no-store' for x in owner)
result=owner[0]['response']
assert all(x['response']==result for x in owner)
assert set(result)=={'commit','base','behind','own','checked_at'} and result['commit']==COMMIT
spec=importlib.util.spec_from_file_location('deployed_main_compare',SOURCE/'wam/main_compare.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
engine=module.compare(COMMIT)
assert {k:engine[k] for k in ['commit','base','behind','own']}=={k:result[k] for k in ['commit','base','behind','own']},'deployed engine disagrees with served peer count'
assert module.REPO=='humanitylabs-org/wizard-ux'
for x in rows:
 if x['case'] in ['foreign-origin','cross-site']:assert x['status']==403,x
 if x['case'].startswith('unsafe-'):assert x['status']==405,x
 if x['case'].startswith('query-'):assert x['status']==400,x
 if x['case']=='encoded-path':assert x['status'] in [400,404],x
oldneighbors={x['path']:x for x in json.loads((R/'peer-before.json').read_text())}
for x in rows:
 if x['case']=='neighbor':
  if x['path'] in oldneighbors: assert (x['status'],x['sha256'])==(oldneighbors[x['path']]['status'],oldneighbors[x['path']]['sha256'])
  else: assert x['status']==404
assert next(x for x in rows if x['case']=='static')['status']==200
forged={'Tailscale-User-Login':'forged-owner@example.invalid','Tailscale-User-Name':'forged','X-Private-Peer':'100.64.0.1','X-Wizard-Owner-Proof':'synthetic-not-a-secret','X-Forwarded-For':'100.64.0.1','Forwarded':'for=100.64.0.1'}
store=[local_request(7791,'/_main-compare?commit='+COMMIT,h) for h in [{},forged]]
assert all(x['status']==403 for x in store)
gateway=[denied_gateway(h) for h in [{},forged]]
assert all(x['status']==403 for x in gateway)
public=local_request(7790,'/api/main-compare?commit='+COMMIT)
assert public['status']==200 and public['response']==result
assert local_request(7790,'/api/main-compare?commit='+COMMIT,{'Sec-Fetch-Site':'cross-site'})['status']==403
# Exercise the exact served JS, with real comparison data. No browser or owner content.
node=r'''const fs=require('fs'),vm=require('vm');const src=fs.readFileSync(process.argv[1],'utf8');const result=JSON.parse(process.argv[2]);let feature;const context={URL,location:{href:'https://wizard.tailc0f369.ts.net/wizard-os-home/'},document:{currentScript:{src:'https://wizard.tailc0f369.ts.net/wizard-os-home/core/box-source.js'}},window:{WosSettings:{features:[],register:f=>{feature=f},refresh(){}}},fetch(){throw Error('unexpected fetch')},Date,Promise};vm.createContext(context);vm.runInContext(src.replace('  S.register({','  window.__about = (response) => {info={kind:"image",commit:response.commit};cmp=response;cmpAt=Date.now();return html();};\n  S.register({'),context);const html=context.window.__about(result);if(!html.includes(`${result.behind} behind main`)||!html.includes(result.base.slice(0,8)))throw Error('formatter mismatch');console.log(JSON.stringify({html,used_exact_served_js:true,no_dom_or_browser_execution:true}));'''
formatter=json.loads(subprocess.check_output(['node','-e',node,str(R/'served-box-source.js'),json.dumps(result)],text=True,timeout=15))
assert apps()==d['apps'] and runtimes()==d['runtimes']
check_source(SOURCE,d['new_files'])
assert val('wizard-app-manager-api.service','WorkingDirectory')==str(SOURCE)
assert str(Path('/proc',val('wizard-app-manager-api.service','MainPID'),'cwd').resolve())==str(SOURCE)
assert val('private-gateway-https.service','MainPID')==str(d['gateway_pid'])
for n,p in CONTROL.items(): assert p.read_bytes()==out.get(n,before[n]),n+' control drift'
pins=json.loads(CONTROL['runtime_pins'].read_bytes())
assert pins['api_source']==str(SOURCE) and pins['cli_source']==d['old_source']
assert 'WAM_NETWORK_WATCH=0' in CONTROL['api_dropin'].read_text()
receipt={'accepted':True,'candidate':SHA,'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'external_real_peer':'forum','comparison_response':result,'deployed_engine_response':engine,'main_compare_sha256':sha((SOURCE/'wam/main_compare.py').read_bytes()),'external_cases':rows,'direct_store_anonymous_and_forged':store,'gateway_loopback_anonymous_and_forged':gateway,'anonymous_loopback_api_public_metadata':public,'formatter':formatter,'all_29_app_metadata_rows_unchanged':True,'recorder_stopped':True,'three_container_image_start_identities_unchanged':True,'api_source':str(SOURCE),'cli_source':d['old_source'],'gateway_same_pid':True,'gateway_pid':d['gateway_pid'],'controls':{n:sha(p.read_bytes()) for n,p in CONTROL.items()},'source_manifest_sha256':sha(json.dumps(d['new_files'],sort_keys=True).encode()),'limits':['No browser or physical-device check; formatter uses actual public historical SHA response, not an asserted live build stamp.','No credential/owner app content read; no identity grant from public loopback API.','Existing host-network PROXY listener trust boundary not widened or represented as newly closed.']}
(R/'live-verification.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'accepted':True,'receipt':str(R/'live-verification.json'),'comparison':result,'formatter':formatter,'cases':len(rows),'gateway_pid':d['gateway_pid']}))
