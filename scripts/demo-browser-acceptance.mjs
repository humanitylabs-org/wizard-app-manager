// Official Chromium image on demo, using bounded loopback-only host CDP.
// Start with --entrypoint /headless-shell/headless-shell; NEVER the image's default socat entrypoint.
import {readFileSync, writeFileSync} from 'node:fs';
const evidence=process.env.WIZARD_ACCEPTANCE_EVIDENCE_DIR || '/root/wizard-acceptance-20261019';
const base='http://127.0.0.1:19222';
const file=JSON.parse(readFileSync(`${evidence}/http-acceptance.json`,'utf8')).file;
const url=`https://demo.tail6adf1a.ts.net:8796/wizard/files/${encodeURIComponent(file.id)}/content`;
const tab=await (await fetch(`${base}/json/new?${encodeURIComponent(url)}`,{method:'PUT'})).json();
const endpoint=new URL(tab.webSocketDebuggerUrl);endpoint.hostname='127.0.0.1';endpoint.port='19222';
const ws=new WebSocket(endpoint);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});
let counter=0;const pending=new Map();ws.onmessage=e=>{const r=JSON.parse(e.data);if(r.id&&pending.has(r.id)){pending.get(r.id)(r);pending.delete(r.id)}};
const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++counter;pending.set(id,r=>r.error?reject(r.error):resolve(r.result));ws.send(JSON.stringify({id,method,params}));setTimeout(()=>reject(new Error(`CDP timeout ${method}`)),15000).unref()});
await send('Page.enable');
let result;
for(let i=0;i<30;i++){
 result=await send('Runtime.evaluate',{expression:'JSON.stringify({url:location.href,title:document.title,images:[...document.images].map(x=>({src:x.src,complete:x.complete,width:x.naturalWidth,height:x.naturalHeight})),text:document.body?.innerText?.slice(0,300)})',returnByValue:true});
 const data=JSON.parse(result.result.value);if(data.images.some(x=>x.width===32&&x.height===32)){result=data;break}
 await new Promise(r=>setTimeout(r,500));
}
const image=await send('Page.captureScreenshot',{format:'png'});
writeFileSync(`${evidence}/browser-image.png`,Buffer.from(image.data,'base64'));
writeFileSync(`${evidence}/browser-image.json`,JSON.stringify(result,null,2));
console.log(JSON.stringify(result));
ws.close();await fetch(`${base}/json/close/${tab.id}`);
if(!result.images?.some(x=>x.width===32&&x.height===32))process.exitCode=1;
