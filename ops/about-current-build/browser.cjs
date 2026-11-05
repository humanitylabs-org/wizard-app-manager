'use strict';
// Full real shell entrypoints, captured GET bytes. All owner state is synthetic and all sockets are closed.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const ROOT=__dirname,OUT=process.env.QA_OUT,ORIGIN='https://wizard.tailc0f369.ts.net',PREFIX='/wizard-os-home/';
const {chromium,devices}=require(process.env.QA_PLAYWRIGHT);
const {fixture,seed}=require('./source/qa-proof-fixture.cjs');
const {guardRequestContext,proveDirectDenied}=require('./request-guard.cjs');
const {difference,proveOpaque}=require('./pixel-proof.cjs');
fixture.base=ORIGIN;
const capture=JSON.parse(fs.readFileSync(path.join(OUT,'capture.json'))),cmp=JSON.parse(fs.readFileSync(path.join(OUT,'comparison.json'))),build=JSON.parse(fs.readFileSync(path.join(OUT,'build.json')));
assert.equal(build.commit,capture.source_commit);
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const transportOrigin=process.env.QA_TRANSPORT_ORIGIN;
if(transportOrigin){assert.equal(process.env.QA_PHASE,'candidate');assert.match(transportOrigin,/^http:\/\/127\.0\.0\.1:[0-9]+$/);}
const fetchOptions=u=>({maxRedirects:0,...transportOrigin?{url:transportOrigin+u.pathname+u.search}:{}});
// Node-side bounded polling retains the production CSP; no waitForFunction eval.
async function settle(page,predicate,arg){const until=Date.now()+20000;do{if(await page.evaluate(predicate,arg))return;await new Promise(r=>setTimeout(r,50));}while(Date.now()<until);throw Error('bounded real-page predicate did not settle: '+predicate.toString().slice(0,140));}
const expectedTitle='Built on main '+cmp.base.slice(0,8);
const expectedSub=!cmp.behind&&!cmp.own?'Up to date with main':[cmp.behind+' behind main',cmp.own?cmp.own+' own '+(cmp.own===1?'change':'changes'):''].filter(Boolean).join(' · ');
const report={accepted:false,phase:process.env.QA_PHASE,source_commit:build.commit,web_tree:capture.web_tree,actual_comparison:cmp,expected:{title:expectedTitle,subtitle:expectedSub},cases:[],owner_dynamic_network_reads:0,owner_network_writes:0,all_browser_processes_stopped:false};
const flush=()=>fs.writeFileSync(path.join(OUT,'browser-results.json'),JSON.stringify(report,null,2)+'\n');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.QA_BROWSER,args:['--use-angle=swiftshader','--enable-unsafe-swiftshader','--disable-dev-shm-usage']});
 try {
  for(const scenario of [{phone:false},{phone:true},{phone:false,standalone:true}]){
   const {phone,standalone=false}=scenario;
   const row={id:standalone?'standalone-dark':phone?'pixel7':'desktop',pageerrors:[],route_errors:[],refused_network:[],synthetic_requests:[],static_scripts:[],metadata_requests:[],live_metadata_responses:[],live_static_responses:[],shots:[],checks:[]};report.cases.push(row);flush();
   const ctx=await browser.newContext({...phone?devices['Pixel 7']:{viewport:{width:1280,height:1000}},colorScheme:'dark',serviceWorkers:'block'});
   const check=(name,pass,detail)=>{row.checks.push({name,pass:!!pass,detail});flush();assert(pass,name);};
   // APIRequestContext must never bypass the closed page route fence.
   guardRequestContext(ctx);
   let closing=false;const pendingRoutes=new Set();
   await ctx.routeWebSocket('**/*',ws=>ws.close());
   if(!standalone){
    await ctx.addInitScript(seed);
    await ctx.addInitScript(()=>{localStorage.setItem('wos.skin','nebula');localStorage.setItem('wos.welcome.dismissed','1');});
   }else await ctx.addInitScript(()=>{
    // Only the registration host is synthetic; markup/fetch/format are real.
    window.WosSettings={features:[],register(feature){this.features.push(feature);this.refresh();},refresh(){const render=()=>{const root=document.querySelector('#settingsBody');if(root)root.innerHTML=this.features.map(f=>f.html()).join('');};if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',render,{once:true});else render();}};
   });
   const handleRoute=async route=>{
    try{
    const req=route.request(),u=new URL(req.url()),method=req.method(),key=method+' '+u.pathname+u.search;
    const json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(u.origin!==ORIGIN){row.refused_network.push({key,reason:'external origin'});return await route.abort('blockedbyclient');}
    if(standalone&&method==='GET'&&u.pathname===PREFIX+'__about-standalone-dark.html'&&!u.search)return await route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><html data-presentation="standalone"><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="favicon.ico"><link rel="stylesheet" href="kit/wizard-tokens.css"><link rel="stylesheet" href="kit/wizard-ui.css"><link rel="stylesheet" href="core/settings.css"><script defer src="core/box-source.js"></script></head><body><main id="settingsBody" class="st-root"></main></body></html>'});
    // All writes are synthetic, never forwarded; known preference writes may occur during skin changes.
    if(method!=='GET'){
     row.synthetic_requests.push(key);
     const f=fixture.responses[key];if(f)return await route.fulfill({status:f.status,contentType:f.contentType||'application/json',body:f.body});
     if(method==='POST'&&['/wizard/v1/ui-preferences','/wizard/v1/ui-preferences/merge'].includes(u.pathname))return await json({ok:true,preferences:{}});
     row.refused_network.push({key,reason:'uncatalogued synthetic write'});return await json({error:'synthetic_write_refused'},451);
    }
    if(u.pathname===PREFIX+'build.json') {
     row.metadata_requests.push(key);
     const response=await route.fetch(fetchOptions(u)),body=await response.body();
     assert.equal(response.status(),200);assert.deepEqual(JSON.parse(body),build);
     row.live_metadata_responses.push({key,status:response.status(),sha256:hash(body)});
     return await route.fulfill({response,body});
    }
    if(u.pathname===PREFIX+'my-release.json'){
     row.metadata_requests.push(key);const response=await route.fetch(fetchOptions(u));assert.equal(response.status(),404);
     row.live_metadata_responses.push({key,status:404,sha256:hash(await response.body())});return await route.fulfill({response});
    }
    if(u.pathname===PREFIX+'store-install/_main-compare'){
     assert.equal(u.search,'?commit='+build.commit);row.metadata_requests.push(key);
     const response=await route.fetch(fetchOptions(u)),body=await response.body(),actual=JSON.parse(body);assert.equal(response.status(),200);
     for(const k of ['commit','base','behind','own'])assert.equal(actual[k],cmp[k]);
     row.live_metadata_responses.push({key,status:200,sha256:hash(body),commit:actual.commit,behind:actual.behind,own:actual.own});
     return await route.fulfill({response,body});
    }
    // Deliberately no update/channel selection and no live update-policy read.
    if(u.pathname===PREFIX+'store-install/_updates'){row.synthetic_requests.push(key);return await json({error:'unavailable'},404);}
    if(u.pathname===PREFIX+'ux-version.txt')return await route.fulfill({status:200,contentType:'text/plain',body:build.commit+'\n'});
    if(u.pathname===PREFIX+'ux-changes.json')return await json({head:build.commit,changes:[]});
    if(u.pathname===PREFIX+'data.js'||u.pathname===PREFIX+'summaries.js')return await route.fulfill({status:200,contentType:'application/javascript',body:'// synthetic empty generated owner data\n'});
    let rel=u.pathname.startsWith(PREFIX)?u.pathname.slice(PREFIX.length):null;if(rel==='')rel=phone?'index.html':'desktop.html';
    if(rel&&Object.hasOwn(capture.static,rel)){
     const item=capture.static[rel],response=await route.fetch(fetchOptions(u)),body=await response.body();assert.equal(response.status(),200);assert.equal(hash(body),item.sha256);
     row.live_static_responses.push({path:rel,status:200,sha256:hash(body)});
     if(req.resourceType()==='script')row.static_scripts.push({path:rel,sha256:item.sha256});
     return await route.fulfill({response,body});
    }
    const f=fixture.responses[key]||fixture.responses['GET '+u.pathname];
    if(f){row.synthetic_requests.push(key);return await route.fulfill({status:f.status,contentType:f.contentType||'application/json',body:f.body});}
    row.refused_network.push({key,reason:'uncatalogued synthetic read'});return await json({error:'synthetic_read_refused'},451);
    }catch(e){
     row.route_errors.push({url:route.request().url(),error:String(e),teardown:closing});flush();
     try{await route.abort('failed');}catch(abort){row.route_errors.at(-1).abort_error=String(abort);}
     flush();
    }
   };
   await ctx.route('**/*',route=>{
    const task=handleRoute(route);pendingRoutes.add(task);
    return task.finally(()=>pendingRoutes.delete(task));
   });
   const page=await ctx.newPage();page.setDefaultTimeout(20000);page.on('pageerror',e=>{row.pageerrors.push(String(e));flush();});
   const shot=async name=>{const p=path.join(OUT,row.id+'-'+name+'.png');await page.screenshot({path:p,fullPage:false});row.shots.push(p);flush();};
   try{
    check('direct public API denied with real transport preserved',(await proveDirectDenied(ctx,page)).length===7);
    if(standalone){
     await page.goto(ORIGIN+PREFIX+'__about-standalone-dark.html',{waitUntil:'load'});
     await settle(page,({a,b})=>{const e=document.querySelector('[data-box-source]');return e?.querySelector('b')?.textContent===a&&e?.querySelector('small')?.textContent===b;},{a:expectedTitle,b:expectedSub});
     const fallback=await page.evaluate(()=>{const root=document.documentElement,s=getComputedStyle(root),body=getComputedStyle(document.body),r=document.querySelector('[data-box-source]');return {top:parent===window,hosted:!!(window.WizardApp||window.WosSkin||root.hasAttribute('data-wz-hosted')),skin:root.dataset.skin||null,dark:matchMedia('(prefers-color-scheme:dark)').matches,scheme:s.colorScheme,canvas:body.backgroundColor,ink:getComputedStyle(r.querySelector('b')).color,muted:getComputedStyle(r.querySelector('small')).color,text:r.textContent,base:s.getPropertyValue('--wz-base').trim()};});
     check('standalone has no hosting bridge or worn skin',fallback.top&&!fallback.hosted&&fallback.skin===null,fallback);
     check('real dark standalone token fallback',fallback.dark&&fallback.scheme==='dark'&&fallback.canvas!=='rgba(0, 0, 0, 0)'&&fallback.base,fallback);
     const lum=c=>{const rgb=c.match(/[\d.]+/g).slice(0,3).map(Number).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;};
     const canvas=lum(fallback.canvas),contrast=c=>(Math.max(lum(c),canvas)+.05)/(Math.min(lum(c),canvas)+.05);
     check('standalone title and subtitle contrast',contrast(fallback.ink)>=4.5&&contrast(fallback.muted)>=4.5,{title:contrast(fallback.ink),subtitle:contrast(fallback.muted)});
     check('standalone renders actual comparison',fallback.text.includes(expectedTitle)&&fallback.text.includes(expectedSub),fallback.text);
     await shot('fallback');
    }else{
    await page.goto(ORIGIN+PREFIX+(phone?'index.html':'desktop.html')+'?skin=nebula',{waitUntil:'load'});
    await settle(page,()=>!!(window.__wos||window.__wux)&&window.WosSettings&&window.WosSkin&&(!window.WosExperience||!WosExperience.state.busy));
    check('real responsive shell',await page.evaluate(p=>p?!!window.__wos&&!window.__wux:!!window.__wux&&!window.__wos,phone));
    const documentIdentity=await page.evaluate(()=>window.__qaAboutDocument=crypto.randomUUID());
    for(const skin of ['nebula','grove']){
     await page.evaluate(()=>WizCore.deeplink.route('/wizard-os-home/?view=skin'));
     await page.locator('#settingsBody').waitFor({state:'visible'});
     const family=await page.evaluate(s=>WosSkin.familyOf?.(s),skin);
     if(family&&await page.locator('[data-skinfamily="'+family+'"]:visible').count())await page.locator('[data-skinfamily="'+family+'"]:visible').click();
     await page.locator('[data-skinpick="'+skin+'"]:visible').click();
     await settle(page,s=>WosSkin.current===s&&(!window.WosExperience||!WosExperience.state.busy),skin);
     check('live skin change keeps same document '+skin,await page.evaluate(id=>window.__qaAboutDocument===id,documentIdentity));
     const tokens=await page.evaluate(()=>{const s=getComputedStyle(document.documentElement);return Object.fromEntries(['--wz-text','--wz-base','--wz-accent','--wz-panel'].map(k=>[k,s.getPropertyValue(k).trim()]));});
     check('Appearance tokens '+skin,Object.values(tokens).every(Boolean),tokens);
     // Same shell instance, real live skin change, no reload or owner preference writes.
     if(phone)await page.evaluate(()=>__wos.onBack());else await page.keyboard.press('Escape');
     await page.locator('#settingsBody [data-st-page="about"]:visible').click();
     await settle(page,()=>document.querySelector('#settingsBody')?.dataset.settingsPage==='about');
     const card=page.locator('#settingsBody [data-box-source]:visible');await card.waitFor();
     await settle(page,({a,b})=>{const e=document.querySelector('#settingsBody [data-box-source]');return e?.querySelector('b')?.textContent===a&&e?.querySelector('small')?.textContent===b;},{a:expectedTitle,b:expectedSub});
     await card.scrollIntoViewIfNeeded();
     const text=await card.innerText(),bounds=await card.boundingBox();
     check('actual displayed count '+skin,text.includes(expectedTitle)&&text.includes(expectedSub),{text,bounds});
     check('card visible in viewport '+skin,bounds&&bounds.x>=0&&bounds.width>100&&bounds.y>=0&&bounds.y+bounds.height<=(phone?devices['Pixel 7'].viewport.height:1000)+1,bounds);
     await settle(page,()=>[...document.querySelector('#settingsBody').closest('.dsettings,.sheet').getAnimations({subtree:true})].every(a=>a.playState!=='running'||a.effect.getComputedTiming().iterations===Infinity));
     await page.evaluate(()=>document.fonts.ready);
     const panelPaint=await proveOpaque(page,bounds),clip={x:Math.ceil(bounds.x),y:Math.ceil(bounds.y),width:Math.floor(bounds.width)-2,height:Math.floor(bounds.height)-2};
     check('actual Settings panel opacity '+skin,panelPaint.opacity==='1',panelPaint);
     const pair=async label=>{const files=[];for(const state of ['noise','blank']){await page.evaluate(s=>{document.querySelector('#qa-about-underlay').style.visibility=s==='noise'?'visible':'hidden';},state);const file=path.join(OUT,row.id+'-'+skin+'-'+label+'-'+state+'.png');files.push(await page.screenshot({path:file,clip}));row.shots.push(file);}return difference(...files);};
     try{
      const changed=await pair('actual-panel');check('opaque panel decoded pixels '+skin,changed===0,{changed_pixels:changed});
      await page.evaluate(()=>{const body=document.querySelector('#settingsBody'),p=body.closest('.win')||body.closest('.dsettings,.sheet');p.dataset.qaSavedStyle=p.getAttribute('style')||'';p.style.visibility='hidden';});
      const control=await pair('underlay-control');check('underlay positive signal '+skin,control>0,{changed_pixels:control});
     }finally{await page.evaluate(()=>{const body=document.querySelector('#settingsBody'),p=body.closest('.win')||body.closest('.dsettings,.sheet');if('qaSavedStyle' in p.dataset){p.setAttribute('style',p.dataset.qaSavedStyle);delete p.dataset.qaSavedStyle;}document.querySelector('#qa-about-underlay')?.remove();});}
     await shot(skin+'-about');
     if(phone)await page.evaluate(()=>__wos.onBack());else await page.keyboard.press('Escape');
    }
    }
    check('actual box-source script executed',row.static_scripts.some(s=>s.path==='core/box-source.js'&&s.sha256===capture.static['core/box-source.js'].sha256));
    check('guarded original tracked route.fetch transport works',row.live_static_responses.some(s=>s.path==='core/box-source.js'&&s.sha256===capture.static['core/box-source.js'].sha256));
    check('one exact current-build compare request',row.metadata_requests.filter(x=>x==='GET '+PREFIX+'store-install/_main-compare?commit='+build.commit).length===1,row.metadata_requests);
    check('owner-gated current-build comparison 200',row.live_metadata_responses.some(x=>x.commit===build.commit&&x.status===200&&x.behind===cmp.behind&&x.own===cmp.own),row.live_metadata_responses);
    check('actual origin build stamp 200',row.live_metadata_responses.some(x=>x.key==='GET '+PREFIX+'build.json'&&x.status===200));
    check('no missing/forbidden network requests',row.refused_network.length===0,row.refused_network);
    check('zero page errors',row.pageerrors.length===0,row.pageerrors);
    check('zero route exceptions',row.route_errors.length===0,row.route_errors);
   }catch(e){row.failure=String(e);await shot('failure').catch(()=>{});throw e;}finally{
    closing=true;await ctx.close();await Promise.allSettled([...pendingRoutes]);
    // Teardown races are retained, never silently turned into a green row.
    if(row.route_errors.length)row.failure=row.failure||'route exceptions recorded';flush();
   }
  }
  report.accepted=report.cases.length===3&&report.cases.every(c=>!c.failure&&c.checks.every(x=>x.pass));assert(report.accepted);
 }finally{await browser.close();report.browser_disconnected=!browser.isConnected();flush();}
})().catch(e=>{report.failure=String(e);flush();console.error(String(e));process.exitCode=1;});
