"""Disposable rootful Docker upload proof. Run as root with the Agent Service image as argv[1]. Never mounts owner media."""
import subprocess, tempfile, pathlib, shutil, json, sys, os
image=sys.argv[1]
root=pathlib.Path(tempfile.mkdtemp(prefix='wizard-media-drill-', dir=os.environ.get('TMPDIR')))
media=root/'media'; media.mkdir(); media.chmod(0o775)
script=r'''import {createWizardServer,listen} from './server/service.mjs';
const server=createWizardServer({requireAccess:false,agentBridge:false,followThrough:false,archivedSessionReader:()=>[],mySkin:false,mediaRoot:'/media',mediaIndexPath:'/tmp/media-index.json',settingsPath:'/tmp/settings.json'});
try { const {port}=await listen(server); const base='http://127.0.0.1:'+port; const r=await fetch(base+'/wizard/files/upload',{method:'POST',headers:{'content-type':'application/octet-stream','x-wizard-filename':'launch-proof.txt','x-wizard-mime':'text/plain'},body:'synthetic-launch-proof'}); const data=await r.json();
console.log(JSON.stringify({status:r.status, error:data.error})); if(r.status!==201) process.exitCode=2;
else {const f=await fetch(base+'/wizard/files/'+data.file.id+'/content'); const value=await f.text();if(value!=='synthetic-launch-proof') throw Error('readback mismatch');console.log('PASS upload 201 + exact content readback');}
} finally {server.closeAllConnections?.();server.close();}'''
def run(user,group=False):
 cmd=['docker','run','--rm','--network','none','--cap-drop','ALL','--security-opt','no-new-privileges','--user',user,'-v',f'{media}:/media']
 if group:cmd+=['--group-add','1000']
 cmd += ['--entrypoint','node',image,'--input-type=module','-e',script]
 r=subprocess.run(cmd,text=True,capture_output=True,timeout=45)
 print(json.dumps({'user':user,'shared_group':group,'exit':r.returncode,'output':r.stdout,'stderr':r.stderr[-1600:]}))
 return r.returncode
try:
 for user in ['0:0','1001:1001']:
  shutil.rmtree(media);media.mkdir();subprocess.run(['chown','1000:1000',str(media)],check=True);media.chmod(0o775)
  assert run(user)!=0, 'baseline unexpectedly writable'
  assert run(user,True)==0
  # Files restarting repairs only its media root. The supplementary group survives it.
  subprocess.run(['chown','1000:1000',str(media)],check=True);media.chmod(0o775)
  assert run(user,True)==0
 print('PASS rootful and non-root identity, negative baseline, fixed upload/readback, Files root ownership reset')
finally:shutil.rmtree(root)