// Generated app inside the real shell + five accepted skins. Asserts it sits on the window glass like Chats,
// follows live skin changes without losing state, and never steals Back. Writes only to this app's API.
const fs=require('fs'),path=require('path'),os=require('os');const pw=require('../qa-playwright.cjs');
const url=process.argv[2],out=process.env.QA_OUT||path.join(os.tmpdir(),'builder-skin-qa');
const SKINS=['nebula','grove','tide','ember','signal'];
const today='new Date(Date.now()-new Date().getTimezoneOffset()*6e4).toISOString().slice(0,10)';
(async()=>{fs.mkdirSync(out,{recursive:true});const browser=await pw.chromium.launch({headless:true});const checks=[];let blocked=0;
function ok(name,value,detail){if(!value)throw Error(name+': '+JSON.stringify(detail));checks.push(name)}
const clear=v=>v==='rgba(0, 0, 0, 0)'||v==='transparent';
for(const [width,height] of [[393,852],[1200,850]]){
 const c=await browser.newContext({viewport:{width,height},hasTouch:true,isMobile:width<500,deviceScaleFactor:width<500?2:1});
 await c.route('**/*',r=>{const q=r.request(),u=new URL(q.url());if(!['GET','HEAD'].includes(q.method())&&!/^\/wizard-os-home\/demo\/apps\/api\/habits(?:\/\d+\/today)?$/.test(u.pathname)){blocked++;return r.fulfill({status:403,body:'{}'})}r.continue()});
 const p=await c.newPage(),errors=[];p.on('pageerror',e=>errors.push(e.message));
 await p.goto(url);await p.waitForFunction(()=>window.__wos&&window.WizardAppearance&&window.WosSkin);
 const frame=await (async()=>{for(let i=0;i<100;i++){const f=p.frames().find(f=>f.url().includes('/demo/apps/builder.html'));if(f)return f;await p.waitForTimeout(100)}throw Error('No generated app frame')})();
 await frame.waitForFunction(()=>window.WizardTheme?.connected&&document.getElementById('day-short')?.textContent);
 await p.waitForSelector('.frame-wrap.wz-themed');
 const hist0=await p.evaluate(()=>history.length);
 ok('Paper is retired from the shell registry',await p.evaluate(()=>!WosSkin.list.some(s=>s.id==='paper')));
 ok('Paper is absent from the skin controls',await p.locator('.pv-skin[data-skin="paper"]').count()===0);
 ok(width+' hosted app pushes no history into the shell',!(await frame.evaluate(()=>!!(history.state&&history.state.wzGuard))));

 const tag=String(Date.now()).slice(-5);
 for(const name of ['Stretch '+tag,'Read 10 pages '+tag]){
  await frame.fill('#habit-name',name);await frame.click('#add');
  await frame.waitForFunction(n=>[...document.querySelectorAll('#habits b')].some(b=>b.textContent===n),name);
 }
 await frame.locator('#habits .wz-row',{hasText:'Stretch '+tag}).click();
 await frame.waitForFunction(n=>[...document.querySelectorAll('#habits .wz-row')].some(r=>r.textContent.includes(n)&&r.getAttribute('aria-pressed')==='true'),'Stretch '+tag);
 ok(width+' add and mark through the real UI',true);
 // Using the app (typing, adding, marking) must not add history of its own. The shell arms exactly one
 // Back-guard entry (ensureGuard) on first user activation; that is the only growth allowed.
 {const after=await p.evaluate(()=>history.length),guard=await p.evaluate(()=>__wos.back.guardOn);
  ok(width+' app wrote no history (Back stays the shell\'s)',after===hist0+(guard?1:0),{before:hist0,after,guard});}

 await p.locator('.pv-pill').click();await p.locator('.pv-skin[data-skin="grove"]').click();
 await frame.waitForFunction(()=>WizardTheme.current.skin==='grove');await p.locator('.pv-x').click();
 ok(width+' Preview skin menu re-skins the app',true);

 await frame.evaluate(()=>{window.qaDocument=crypto.randomUUID();document.getElementById('habit-name').value='Unsent habit'});
 const stamp=await frame.evaluate(()=>window.qaDocument);
 const data=()=>frame.evaluate(async t=>JSON.stringify((await(await fetch('api/habits?day='+eval(t))).json()).habits),today);
 const beforeData=await data();
 for(const skin of SKINS){
  await p.evaluate(s=>WosSkin.set(s),skin);await frame.waitForFunction(s=>WizardTheme.current.skin===s,skin);
  await p.waitForFunction(()=>WizardAppearance.inspect().every(x=>x.ack));await p.waitForTimeout(150);
  const app=await frame.evaluate(()=>{const a=WizardTheme.current,s=getComputedStyle(document.documentElement);return{skin:a.skin,accent:s.getPropertyValue('--wz-accent').trim()===a.tokens.accent,body:getComputedStyle(document.body).backgroundColor,html:s.backgroundColor,scheme:s.colorScheme,draft:document.getElementById('habit-name').value,stamp:window.qaDocument,head:getComputedStyle(document.querySelector('.wz-head')).display}});
  const host=await p.evaluate(()=>{const f=document.querySelector('.frame-wrap.wz-themed iframe.appframe');const chain=[];let n=f;while(n&&!n.classList?.contains('win')){chain.push([String(n.className||n.tagName),getComputedStyle(n).backgroundColor]);n=n.parentElement}return{chain,win:n&&getComputedStyle(n).backgroundColor,frameScheme:getComputedStyle(f).colorScheme}});
  ok(width+' '+skin+' app paints no canvas, sits on window glass',clear(app.body)&&clear(app.html)&&host.chain.every(([,bg])=>clear(bg))&&host.win&&!clear(host.win)&&host.win!=='rgb(15, 16, 20)',{app,host});
  ok(width+' '+skin+' frame colour scheme matches the app',host.frameScheme.split(' ')[0]===app.scheme.split(' ')[0],{f:host.frameScheme,a:app.scheme});
  ok(width+' '+skin+' roles applied, window title not duplicated',app.accent&&app.head==='none',app);
  ok(width+' '+skin+' draft and document survive',app.draft==='Unsent habit'&&app.stamp===stamp,app);
  if(width<500)await p.screenshot({path:path.join(out,'window-'+skin+'.png')});
 }
 ok(width+' skin changes keep saved data',await data()===beforeData);

 await p.evaluate(()=>WosSkin.set('grove'));await frame.waitForFunction(()=>WizardTheme.current.skin==='grove');
 await frame.evaluate(()=>{const a={...WizardTheme.current,skin:'forged'};window.postMessage({type:'wizard:appearance',appearance:a},location.origin);dispatchEvent(new MessageEvent('message',{origin:'https://untrusted.invalid',source:parent,data:{type:'wizard:appearance',appearance:a}}));dispatchEvent(new MessageEvent('message',{origin:location.origin,source:parent,data:{type:'wizard:appearance',appearance:{...a,revision:0}}}))});
 await p.waitForTimeout(80);ok(width+' forged, foreign and stale messages ignored',await frame.evaluate(()=>WizardTheme.current.skin==='grove'));

 const wid=await p.evaluate(()=>Object.keys(__wos.wins).find(k=>__wos.wins[k].appId==='habit-tracker'));
 for(const skin of ['grove','nebula']){
  await p.evaluate(s=>WosSkin.set(s),skin);await frame.waitForFunction(s=>WizardTheme.current.skin===s,skin);
  await p.evaluate(w=>__wos.setFull(w,true),wid);await frame.waitForFunction(()=>WizardTheme.current.presentation==='immersive');await p.waitForTimeout(250);
  const full=await p.evaluate(()=>getComputedStyle(document.querySelector('.win.full')).backgroundColor);
  const fa=await frame.evaluate(()=>({body:getComputedStyle(document.body).backgroundColor,head:getComputedStyle(document.querySelector('.wz-head')).display}));
  ok(width+' '+skin+' full screen is skin glass with the app title',full!=='rgb(15, 16, 20)'&&!clear(full)&&clear(fa.body)&&fa.head!=='none',{full,fa});
  if(width<500)await p.screenshot({path:path.join(out,'full-'+skin+'.png')});
  await p.evaluate(()=>__wos.onBack());await frame.waitForFunction(()=>WizardTheme.current.presentation==='window');
 }
 ok(width+' Back leaves full screen, shell stays open',await p.locator('#device').count()===1);
 await p.evaluate(()=>__wos.onBack());await p.waitForFunction(()=>__wos.idx===0);
 await p.evaluate(()=>__wos.onBack());ok(width+' root Back stays in shell',await p.evaluate(()=>__wos.idx===0));
 await p.evaluate(()=>__wos.goDesk(1));await frame.waitForFunction(()=>WizardTheme.current.presentation==='window');
 ok(width+' reopened app visible',await frame.locator('#add').isVisible());
 ok(width+' no horizontal overflow',await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 ok(width+' zero page errors',errors.length===0,errors);
 await c.close();
}
{const c=await browser.newContext({viewport:{width:393,height:852},isMobile:true,deviceScaleFactor:2});const p=await c.newPage();
 const base=new URL(url);await p.goto(base.origin+'/wizard-os-home/demo/apps/builder.html');await p.waitForFunction(()=>document.getElementById('day-short')?.textContent);
 ok('standalone paints the skin canvas',await p.evaluate(()=>getComputedStyle(document.body).backgroundColor!=='rgba(0, 0, 0, 0)'&&!WizardTheme.connected));
 const u=p.url();await p.evaluate(()=>history.back());await p.waitForFunction(()=>document.getElementById('status').textContent.includes('Press back again'));
 ok('standalone first Back warns and stays',p.url()===u);await p.screenshot({path:path.join(out,'standalone.png')});await c.close()}
ok('zero real-agent writes',blocked===0);
const result={host:os.hostname(),url,passed:checks.length,checks,realAgentWrites:blocked};fs.writeFileSync(path.join(out,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify({passed:result.passed,realAgentWrites:blocked}));await browser.close();})().catch(e=>{console.error(e);process.exit(1)});
