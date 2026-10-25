#!/usr/bin/env python3
"""Light Recorder factory regression using WAM's actual private-data helper.
No builds/pulls/downloads. --helper-js is Bun's Node-target bundle of
packages/backend/src/modules/docker/wizard-private-data.ts. Disposable volumes only.
"""
import argparse,json,os,subprocess,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--helper-js',required=True,type=Path);p.add_argument('--evidence',required=True,type=Path);p.add_argument('--docker',default='docker');a=p.parse_args()
a.evidence.mkdir(parents=True,exist_ok=False)
name='recorder-factory-owner-'+str(os.getpid());volume=name+'-data';D=a.docker

def call(*args,check=True):
    r=subprocess.run([D,*args],capture_output=True,text=True,timeout=45)
    if check:assert r.returncode==0,r.stdout+r.stderr
    return r

def save(label):
    state=json.loads(call('inspect',name).stdout)[0]
    (a.evidence/(label+'-state.json')).write_text(json.dumps(state['State'],indent=2))
    logs=call('logs','--tail','200',name);(a.evidence/(label+'-logs.txt')).write_text(logs.stdout+logs.stderr)
    return state

def start():
    call('run','--pull=never','--detach','--name',name,'--network','none','--user','1000:1000','--read-only',
         '--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit','256','--memory','768m',
         '--tmpfs','/tmp:rw,nosuid,size=128m','-v',volume+':/data',a.image)

def healthy():
    return call('exec',name,'python','-c',"import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3));assert d['ok'] is True",check=False).returncode==0

try:
    call('volume','create',volume)
    # Runtipi's existing chmod gives write permission but not ownership. Recorder's
    # chmod(0700) must still fail on that root-owned directory; no optional secrets.
    call('run','--pull=never','--rm','--user','0','--entrypoint','sh','-v',volume+':/data',a.image,'-c','touch /data/.missing-bind-proof; chown 0:0 /data; chmod 0777 /data')
    start();time.sleep(3);before=save('unprepared')
    assert before['State']['Running'] is False
    text=(a.evidence/'unprepared-logs.txt').read_text();assert 'Operation not permitted' in text and 'os.chmod(root, 0o700)' in text
    call('rm',name)
    script='''import {prepareMainPrivateData} from '/tmp/helper.mjs';
const app={data:'/app-data',compose:{services:{recorder:{user:'1000:1000','x-runtipi':{is_main:true},volumes:['${APP_DATA_DIR}/data:/data']}}}};
await prepareMainPrivateData(app,async(base,relative,create)=>{
 if(base!='/app-data'||relative!=='data'||!create)throw Error('incorrect confinement request');
 return '/app-data/data';
});'''
    call('create','--name',name,'--user','0','--network','none','--entrypoint','node','-v',volume+':/app-data/data',a.image,'--input-type=module','-e',script)
    call('cp',str(a.helper_js),name+':/tmp/helper.mjs');call('start','-a',name)
    init=json.loads(call('inspect',name).stdout)[0];assert init['State']['ExitCode']==0
    call('rm',name);start();deadline=time.monotonic()+30
    while not healthy():
        assert time.monotonic()<deadline,'owned Recorder did not answer health';time.sleep(1)
    initial=save('prepared');time.sleep(15);assert healthy();stable=save('stable')
    assert stable['RestartCount']==0 and stable['State']['StartedAt']==initial['State']['StartedAt']
    assert stable['Config']['User']=='1000:1000' and stable['HostConfig']['ReadonlyRootfs'] is True
    assert stable['HostConfig']['CapDrop']==['ALL']
    ownership=call('exec',name,'python','-c',"import os,stat; s=os.stat('/data'); assert(s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode))==(1000,1000,0o700)")
    call('restart',name);deadline=time.monotonic()+30
    while not healthy():
        assert time.monotonic()<deadline,'restart did not become healthy';time.sleep(1)
    save('restart')
    result={'passed':True,'image':a.image,'unpreparedRootMountReproduced':True,'preparedHealthySeconds':15,
            'restartHealthy':True,'credentialsRequired':False,'appUser':'1000:1000','capabilities':'none','imageBuilds':0,'dataMode':'0700'}
    (a.evidence/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
finally:
    if call('inspect',name,check=False).returncode==0:save('final')
    call('rm','-f',name,check=False);call('volume','rm',volume,check=False)
