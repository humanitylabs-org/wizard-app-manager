/* Appearance v1 contract against the actual shared client, never an app copy.
 * node qa/builder/theme-client.cjs /approved/core/app-client.js
 * VM fixtures test protocol trust/validation; browser lifecycle is a separate gate.
 */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=process.argv[2];if(!source)throw Error('Pass the approved shared app-client.js path; the per-app theme client was retired.');
let count=0;const ok=(v,m)=>{assert(v,m);count++;console.log('PASS '+m)};
const handlers={},props={},messages=[];
const parent={postMessage:(m,o)=>messages.push([m,o])};
const root={dataset:{app:'fixture'},style:{setProperty:(k,v)=>props[k]=v},setAttribute:()=>{}};
const context={document:{documentElement:root,addEventListener:()=>{}},location:{origin:'https://preview.example',pathname:'/fixture/'},parent,
 CSS:{supports:(type,value)=>typeof value==='string'&&!value.includes('url(')},addEventListener:(t,fn)=>{handlers[t]=fn},removeEventListener:()=>{},
 dispatchEvent:()=>{},CustomEvent:function(t,o){this.type=t;this.detail=o.detail},console,setTimeout:()=>1,clearTimeout:()=>{},navigator:{},history:{state:null,pushState(){},replaceState(){}}};
context.window=context;
vm.runInNewContext(fs.readFileSync(source,'utf8'),context);
ok(context.WizardApp.version===1,'Shared client v1 facade');
ok(!context.WizardApp.hosted,'Frame identity alone never grants host attachment');
ok(context.WizardApp.theme.get()===null,'Untrusted appearance is not a system theme');
ok(messages.some(m=>m[0].type==='wizard:appearance:ready'),'Appearance ready handshake');
ok(messages.every(m=>m[1]==='https://preview.example'),'Exact message target origin');
const tokens=Object.fromEntries(['canvas','base','chrome','overlay','surface','surface-raised','text','text-secondary','text-muted','border','accent','accent-soft','on-accent','reader-surface'].map(k=>[k,k==='canvas'?'transparent':'#201d28']));
const payload={version:1,revision:3,skin:'fixture-light',presentation:'window',colorScheme:'light',tokens};
const event=(a,origin='https://preview.example',source=parent)=>handlers.message({source,origin,data:{type:'wizard:appearance',appearance:a}});
event(payload,'https://untrusted.example');ok(context.WizardApp.theme.get()===null,'Wrong origin ignored');
event(payload,'https://preview.example',{});ok(context.WizardApp.theme.get()===null,'Wrong source ignored');
event({...payload,version:2});ok(context.WizardApp.theme.get()===null,'Unsupported version ignored');
event({...payload,tokens:{...tokens,text:'red; } body { display:none'}});ok(context.WizardApp.theme.get()===null,'CSS injection ignored');
event(payload);ok(root.dataset.skin==='fixture-light'&&root.dataset.presentation==='window'&&props['--wz-text']==='#201d28','Host semantic roles and presentation applied');
ok(messages.at(-1)[0].revision===3&&messages.at(-1)[0].type==='wizard:appearance:ack','Applied revision acknowledged');
event({...payload,revision:2,skin:'grove'});ok(root.dataset.skin==='fixture-light','Stale revision ignored');
ok(!('--wz-font-reading' in props),'Intentional reading typography remains app-owned');
event({...payload,revision:4,presentation:'immersive'});ok(root.dataset.presentation==='immersive','Live fullscreen presentation update');
console.log(JSON.stringify({passed:count,failed:0,scope:'VM fixtures with actual shared client bytes'}));
