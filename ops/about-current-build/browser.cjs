'use strict';
// Full real shell entrypoints, captured GET bytes. All owner state is synthetic and all sockets are closed.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const ROOT=__dirname,OUT=process.env.QA_OUT,ORIGIN='https://wizard.tailc0f369.ts.net',PREFIX='/wizard-os-home/';
const {chromium,devices}=require(process.env.QA_PLAYWRIGHT);
const {fixture,seed}=require('./source/qa-proof-fixture.cjs');
const {guardRequestContext,proveDirectDenied}=require('./request-guard.cjs');
fixture.base=ORIGIN;
const capture=JSON.parse(fs.readFileSync(path.join(OUT,'capture.json'))),cmp=JSON.parse(fs.readFileSync(path.join(OUT,'comparison.json'))),build=JSON.parse(fs.readFileSync(path.join(OUT,'build.json')));
assert.equal(build.commit,capture.source_commit);
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const expectedTitle='Built on main '+cmp.base.slice(0,8);
const expectedSub=!cmp.behind&&!cmp.own?'Up to date with main':[cmp.behind+' behind main',cmp.own?cmp.own+' own '+(cmp.own===1?'change':'changes'):''].filter(Boolean).join(' · ');
const report={accepted:false,phase:process.env.QA_PHASE,source_commit:build.commit,web_tree:capture.web_tree,actual_comparison:cmp,expected:{title:expectedTitle,subtitle:expectedSub},cases:[],owner_dynamic_network_reads:0,owner_network_writes:0,all_browser_processes_stopped:false};
const flush=()=>fs.writeFileSync(path.join(OUT,'browser-results.json'),JSON.stringify(report,null,2)+'\n');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.QA_BROWSER,args:['--use-angle=swiftshader','--enable-unsafe-swiftshader','--disable-dev-shm-usage']});
 try {
  for(const phone of [false,true]){
   const row={id:phone?'pixel7':'desktop',pageerrors:[],route_errors:[],refused_network:[],synthetic_requests:[],static_scripts:[],metadata_requests:[],live_metadata_responses:[],live_static_responses:[],shots:[],checks:[]};report.cases.push(row);flush();
   const ctx=await browser.newContext({...phone?devices['Pixel 7']:{viewport:{width:1440,height:1000}},serviceWorkers:'block'});
   const check=(name,pass,detail)=>{row.checks.push({name,pass:!!pass,detail});flush();assert(pass,name);};
   // APIRequestContext must never bypass the closed page route fence.
   guardRequestContext(ctx);
   let closing=false;const pendingRoutes=new Set();
   await ctx.routeWebSocket('**/*',ws=>ws.close());
   await ctx.addInitScript(seed);
   await ctx.addInitScript(()=>{localStorage.setItem('wos.skin','nebula');localStorage.setItem('wos.welcome.dismissed','1');});
   const handleRoute=async route=>{
    try{
    const req=route.request(),u=new URL(req.url()),method=req.method(),key=method+' '+u.pathname+u.search;
    const json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(u.origin!==ORIGIN){row.refused_network.push({key,reason:'external origin'});return await route.abort('blockedbyclient');}
    // All writes are synthetic, never forwarded; known preference writes may occur during skin changes.
    if(method!=='GET'){
     row.synthetic_requests.push(key);
     const f=fixture.responses[key];if(f)return await route.fulfill({status:f.status,contentType:f.contentType||'application/json',body:f.body});
     if(method==='POST'&&['/wizard/v1/ui-preferences','/wizard/v1/ui-preferences/merge'].includes(u.pathname))return await json({ok:true,preferences:{}});
     row.refused_network.push({key,reason:'uncatalogued synthetic write'});return await json({error:'synthetic_write_refused'},451);
    }
    if(u.pathname===PREFIX+'build.json') {
     row.metadata_requests.push(key);
     if(process.env.QA_PHASE==='candidate')return await json(build);
     const response=await route.fetch({maxRedirects:0}),body=await response.body();
     assert.equal(response.status(),200);assert.deepEqual(JSON.parse(body),build);
     row.live_metadata_responses.push({key,status:response.status(),sha256:hash(body)});
     return await route.fulfill({response,body});
    }
    if(u.pathname===PREFIX+'my-release.json'){
     row.metadata_requests.push(key);const response=await route.fetch({maxRedirects:0});assert.equal(response.status(),404);
     row.live_metadata_responses.push({key,status:404,sha256:hash(await response.body())});return await route.fulfill({response});
    }
    if(u.pathname===PREFIX+'store-install/_main-compare'){
     assert.equal(u.search,'?commit='+build.commit);row.metadata_requests.push(key);
     const response=await route.fetch({maxRedirects:0}),body=await response.body(),actual=JSON.parse(body);assert.equal(response.status(),200);
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
     const item=capture.static[rel],response=await route.fetch({maxRedirects:0}),body=await response.body();assert.equal(response.status(),200);assert.equal(hash(body),item.sha256);
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
    await page.goto(ORIGIN+PREFIX+(phone?'index.html':'desktop.html')+'?skin=nebula',{waitUntil:'load'});
    await page.waitForFunction(()=>!!(window.__wos||window.__wux)&&window.WosSettings&&window.WosSkin&&(!window.WosExperience||!WosExperience.state.busy));
    check('real responsive shell',await page.evaluate(p=>p?!!window.__wos&&!window.__wux:!!window.__wux&&!window.__wos,phone));
    for(const skin of ['nebula','grove']){
     await page.evaluate(()=>WizCore.deeplink.route('/wizard-os-home/?view=skin'));
     await page.locator('#settingsBody').waitFor({state:'visible'});
     const family=await page.evaluate(s=>WosSkin.familyOf?.(s),skin);
     if(family&&await page.locator('[data-skinfamily="'+family+'"]:visible').count())await page.locator('[data-skinfamily="'+family+'"]:visible').click();
     await page.locator('[data-skinpick="'+skin+'"]:visible').click();
     await page.waitForFunction(s=>WosSkin.current===s&&(!window.WosExperience||!WosExperience.state.busy),skin);
     const tokens=await page.evaluate(()=>{const s=getComputedStyle(document.documentElement);return Object.fromEntries(['--wz-text','--wz-base','--wz-accent','--wz-panel'].map(k=>[k,s.getPropertyValue(k).trim()]));});
     check('Appearance tokens '+skin,Object.values(tokens).every(Boolean),tokens);
     // Same shell instance, real live skin change, no reload or owner preference writes.
     if(phone)await page.evaluate(()=>__wos.onBack());else await page.keyboard.press('Escape');
     await page.locator('#settingsBody [data-st-page="about"]:visible').click();
     await page.waitForFunction(()=>document.querySelector('#settingsBody')?.dataset.settingsPage==='about');
     const card=page.locator('#settingsBody [data-box-source]:visible');await card.waitFor();
     await page.waitForFunction(({a,b})=>{const e=document.querySelector('#settingsBody [data-box-source]');return e?.querySelector('b')?.textContent===a&&e?.querySelector('small')?.textContent===b;},{a:expectedTitle,b:expectedSub});
     await card.scrollIntoViewIfNeeded();
     const text=await card.innerText(),bounds=await card.boundingBox();
     check('actual displayed count '+skin,text.includes(expectedTitle)&&text.includes(expectedSub),{text,bounds});
     check('card visible in viewport '+skin,bounds&&bounds.x>=0&&bounds.width>100&&bounds.y>=0&&bounds.y+bounds.height<=(phone?devices['Pixel 7'].viewport.height:1000)+1,bounds);
     await shot(skin+'-about');
     if(phone)await page.evaluate(()=>__wos.onBack());else await page.keyboard.press('Escape');
    }
    check('actual box-source script executed',row.static_scripts.some(s=>s.path==='core/box-source.js'&&s.sha256===capture.static['core/box-source.js'].sha256));
    check('guarded original tracked route.fetch transport works',row.live_static_responses.some(s=>s.path==='core/box-source.js'&&s.sha256===capture.static['core/box-source.js'].sha256));
    check('one exact current-build compare request',row.metadata_requests.filter(x=>x==='GET '+PREFIX+'store-install/_main-compare?commit='+build.commit).length===1,row.metadata_requests);
    check('real HTTPS current-build comparison 200',row.live_metadata_responses.some(x=>x.commit===build.commit&&x.status===200&&x.behind===cmp.behind&&x.own===cmp.own),row.live_metadata_responses);
    if(process.env.QA_PHASE==='live')check('real HTTPS build stamp 200',row.live_metadata_responses.some(x=>x.key==='GET '+PREFIX+'build.json'&&x.status===200));
    check('no missing/forbidden network requests',row.refused_network.length===0,row.refused_network);
    check('zero page errors',row.pageerrors.length===0,row.pageerrors);
    check('zero route exceptions',row.route_errors.length===0,row.route_errors);
   }catch(e){row.failure=String(e);await shot('failure').catch(()=>{});throw e;}finally{
    closing=true;await ctx.close();await Promise.allSettled([...pendingRoutes]);
    // Teardown races are retained, never silently turned into a green row.
    if(row.route_errors.length)row.failure=row.failure||'route exceptions recorded';flush();
   }
  }
  report.accepted=report.cases.length===2&&report.cases.every(c=>!c.failure&&c.checks.every(x=>x.pass));assert(report.accepted);
 }finally{await browser.close();report.browser_disconnected=!browser.isConnected();flush();}
})().catch(e=>{report.failure=String(e);flush();console.error(String(e));process.exitCode=1;});
