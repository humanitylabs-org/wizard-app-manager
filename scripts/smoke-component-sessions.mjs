// Compatibility smoke using official component source, no mounted Hermes DB or live data.
// node scripts/smoke-component-sessions.mjs /path/to/wizard-agent-service
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {resolve,join} from 'node:path';
import {pathToFileURL} from 'node:url';
const serviceModule=pathToFileURL(resolve(process.argv[2],'server/service.mjs'));
const root=mkdtempSync(join(tmpdir(),'wizard-no-history-'));
const cwd=process.cwd();process.chdir(root);
const {createWizardServer,listen}=await import(serviceModule);
const at=Date.now()/1000, id='wizard_synthetic_alongside_contract';
const row={id,source:'api_server',title:'Synthetic alongside test',message_count:2,parent_session_id:null,
 hidden:false,archived:false,pinned:false,started_at:at,last_active:at};
let writes=0;
const upstream=createServer((req,res)=>{
 res.setHeader('Content-Type','application/json');
 if(req.method!=='GET'){writes++;res.writeHead(405);res.end('{}');return;}
 const u=new URL(req.url,'http://fixture.invalid');
 if(u.pathname==='/api/sessions')return res.end(JSON.stringify({data:Number(u.searchParams.get('offset')||0)?[]:[row],has_more:false}));
 if(u.pathname.endsWith('/messages'))return res.end(JSON.stringify({data:[{id:1,role:'user',content:'Synthetic hello',timestamp:at},
 {id:2,role:'assistant',content:'Synthetic answer',timestamp:at+1,display_kind:'final',finish_reason:'stop'}],has_more:false}));
 if(u.pathname==='/api/sessions/'+id)return res.end(JSON.stringify(row));
 res.end('{}');
});
let service;
try{
 const u=await listen(upstream);
 service=createWizardServer({upstreamOrigin:`http://127.0.0.1:${u.port}`,hermesKey:'synthetic-only',requireAccess:false,
 agents:false,agentBridge:false,followThrough:false,joinedDelegation:'off',runQueue:false,wizardOsHome:root,
 hermesStatePath:'',helmDbPath:'',canonicalSessionListing:true});
 const a=await listen(service), base=`http://127.0.0.1:${a.port}/wizard/hermes/api/sessions`;
 for(const query of ['', '?view=list&window=archive-50']){
  const r=await fetch(base+query),j=await r.json();assert.equal(r.status,200,JSON.stringify(j));
  assert.equal(j.data.length,1);assert.equal(j.data[0].id,id);
  if(query){assert.equal(j.list_version,1);assert(!('latest_message' in j.data[0]));}
 }
 assert.equal(writes,0);
 console.log(JSON.stringify({sessions:'PASS',legacy:1,compact:1,history_mount:false,upstream_writes:writes}));
}finally{
 for(const s of [service,upstream].filter(Boolean))await new Promise(r=>s.close(r));
 process.chdir(cwd);rmSync(root,{recursive:true,force:true});
}
