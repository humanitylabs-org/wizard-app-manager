"""Install same private source bundle on a fresh disposable guest; exercise real API/MCP."""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

from wam.builder import check_app, install_local, prepare_publish, source_hash, install_skill

source=pathlib.Path(sys.argv[1]).resolve()
home=pathlib.Path(sys.argv[2]).resolve()
assert check_app(source, proposal=True)['ok']
rec=install_local(source,home/'state',stage_only=True,run=True,port=18799)
install_skill(home/'state',home/'hermes')
origin=rec['url'].rstrip('/')
def call(path,body=None):
    req=urllib.request.Request(origin+path,None if body is None else json.dumps(body).encode(),{'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=5) as response:
        return json.load(response)
def rpc(tool,args=None):
    return call('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':tool,'arguments':args or {}}})
for i in range(50):
    try:
        health=call('/healthz');break
    except Exception:
        time.sleep(.2)
else:
    raise RuntimeError('App health failed')
assert health['app']=='habit-tracker'
assert call('/api/habits')['habits']==[]
res=rpc('habits_add',{'name':'Drink water'})
assert not res['result']['isError']
h=call('/api/habits')['habits'][0]
assert h['name']=='Drink water'
call('/api/habits/'+str(h['id'])+'/today',{'done':True})
assert rpc('habits_list')['result']['structuredContent']['habits'][0]['done']
rpc('habits_set_done',{'id':h['id'],'done':False})
assert not call('/api/habits')['habits'][0]['done']
assert urllib.request.urlopen(origin+'/').status==200
assert urllib.request.urlopen(origin+'/openapi.json').status==200
# Restart exact local developer project; state must survive.
subprocess.run(['docker','compose','-p',rec['project'],'-f',rec['compose'],'restart'],check=True)
for i in range(50):
    try:
        persisted=call('/api/habits');break
    except Exception:
        time.sleep(.2)
else:
    raise RuntimeError('App did not recover after restart')
assert persisted['habits'][0]['name']=='Drink water' and not persisted['habits'][0]['done']
inspect=json.loads(subprocess.check_output(['docker','inspect',rec['project']+'-habit-tracker-1']))[0]
plan=prepare_publish(source,'humanitylabs-org/habit-tracker')
result={'host':os.uname().nodename,'architecture':os.uname().machine,'source':str(source),
        'source_sha256':source_hash(source),'container_id':inspect['Id'],'image_id':inspect['Image'],
        'app_user':inspect['Config']['User'],'state':'installed_running_verified',
        'checks':['approved+proposal checker','actual local container build/install','API add','MCP list','API mark','MCP undo','actual UI HTTP','OpenAPI served','restart persistence','metadata preserved','system builder outside generated skills','publication prepared without public writes'],
        'api':rec['api'],'view':rec['view'],'public_writes':0,
        'publication':plan['state'],'production_registry_touched':False,
        'inference':'current parent route scaffold; no alternate-model evaluation; no guest LLM invoked',
        'approval_boundary':'private bundle rehearsal, not store publication/second-user catalog install'}
home.mkdir(parents=True,exist_ok=True);(home/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
print('BUILDER_GUEST_RESULT='+json.dumps(result),flush=True)
