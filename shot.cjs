'use strict';
// Before/after for wizard-app-manager#764: Settings > About & updates > Apps, Update now on Notes while Money updates.
// QA_WEB=<web dir> QA_OUT=<png path>
const {chromium}=require(process.env.QA_REPO+'/tools/qa-playwright.cjs');
const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(process.env.QA_WEB),origin='https://shots764.test',prefix='/wizard-os-home/';
const type=f=>f.endsWith('.js')||f.endsWith('.mjs')?'text/javascript':f.endsWith('.css')?'text/css':f.endsWith('.html')?'text/html':f.endsWith('.json')?'application/json':'application/octet-stream';
const row=(id,name,v,l,status='running')=>({info:{id,urn:`${id}:wizardapps`,name},app:{status,version:v},metadata:{latestVersion:l}});
const INSTALLED={installed:[row('money','Money',1,2),row('wizard-notes','Notes',12,13),row('wizard-calendar','Calendar',7,7)]};
let queued=[];
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const ctx=await browser.newContext({viewport:{width:1280,height:800},serviceWorkers:'block'});
  await ctx.routeWebSocket('**/*',ws=>ws.close());
  await ctx.addInitScript(()=>{if(window!==window.top)return;for(const[k,val]of Object.entries({'wos.shell':'desktop','wos.coached':'true','wos.tour.v1':'true','wos.welcome.v1':'done','wos.desktop-view.v1':'classic'}))localStorage.setItem(k,val);});
  await ctx.route('**/*',async r=>{
   const q=r.request(),u=new URL(q.url()),p=u.pathname;
   if(u.origin!==origin)return r.abort();
   if(p.endsWith('/store-install/_app-updates')){
    if(q.method()==='POST'){
     const b=JSON.parse(q.postData()||'{}');
     if(process.env.QA_OLD==='1')return r.fulfill({json:{started:true}});
     queued=[{app:b.app,at:Math.floor(Date.now()/1000),name:'Notes'}];
     return r.fulfill({json:{started:false,queued:[b.app],note:'queued behind the update that is running'}});
    }
    return r.fulfill({json:{app_mode:'auto',holds:[],failed:{},last:null,due:[],skipped:[],notes:{},running:[{app:'money',from:1,to:2,to_version:'0.2',step:'updating',started:1,by:'app-updates'}],queued}});
   }
   if(p.endsWith('/store-data/api/apps/installed'))return r.fulfill({json:INSTALLED});
   if(p.endsWith('/store-install/_updates'))return r.fulfill({json:{mode:'auto',chosen:true,installed:'0.3.263',latest:'0.3.263',update_available:false,unpinned:false,checked:true,installing:false}});
   if(!['GET','HEAD'].includes(q.method()))return r.fulfill({status:405,json:{error:'refused by test'}});
   if(p.startsWith(prefix)){const f=path.resolve(root,p.slice(prefix.length)||'index.html');if(f.startsWith(root+path.sep)&&fs.existsSync(f)&&fs.statSync(f).isFile())return r.fulfill({contentType:type(f),body:fs.readFileSync(f)});}
   if(p==='/wizard/v1/ui-preferences')return r.fulfill({json:{launcher:{},migrations:{'launcher-seed-v1':true}}});
   if(p.endsWith('/v1/setup'))return r.fulfill({json:{first_run:false}});
   if(p.endsWith('/v1/apps'))return r.fulfill({json:{apps:[]}});
   return r.fulfill({json:{sessions:[],agents:[],apps:[],connected:false}});
  });
  const pg=await ctx.newPage();
  await pg.goto(origin+prefix+'desktop.html');
  await pg.waitForFunction(()=>window.WosSettings&&window.__wux&&window.WosUpdates);
  await pg.addStyleTag({content:'#wosWelcome{display:none!important}'});
  await pg.waitForTimeout(1500);
  await pg.evaluate(()=>{const h=document.documentElement;const on=()=>{if(!String(h.dataset.tweaks||'').split(' ').includes('wizard/app-updates-list'))h.dataset.tweaks=(h.dataset.tweaks||'')+' wizard/app-updates-list';};on();new MutationObserver(on).observe(h,{attributes:true,attributeFilter:['data-tweaks']});});
  await pg.evaluate(()=>window.__wux.openSettings('updates'));
  await pg.waitForSelector('[data-osu="app-apply:wizard-notes"]',{timeout:20000});
  await pg.click('[data-osu="app-apply:wizard-notes"]');
  await pg.waitForTimeout(2500);
  const sec=await pg.$('[data-osu-apps]');
  await sec.scrollIntoViewIfNeeded();
  await (sec).screenshot({path:process.env.QA_OUT});
  console.log(await pg.evaluate(()=>document.querySelector('[data-osu-app="wizard-notes"]').innerText));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
