// Real containing-shell and packaged app journey; writes restricted to this developer app.
const fs=require('fs'),path=require('path'),os=require('os');let pw;try{pw=require('playwright')}catch{pw=require('playwright-core')}
const url=process.argv[2],out=process.env.QA_OUT||path.join(os.tmpdir(),'builder-skin-qa');
(async()=>{fs.mkdirSync(out,{recursive:true});const browser=await pw.chromium.launch({headless:true});const checks=[];let blocked=0;
function ok(name,value,detail){if(!value)throw Error(name+': '+JSON.stringify(detail));checks.push(name)}
for(const [width,height] of [[393,852],[1200,850]]){
 const c=await browser.newContext({viewport:{width,height},hasTouch:true,isMobile:width<500});
 await c.route('**/*',r=>{const q=r.request(),u=new URL(q.url());if(!['GET','HEAD'].includes(q.method())&&!/^\/wizard-os-home\/demo\/apps\/api\/habits(?:\/\d+\/today)?$/.test(u.pathname)){blocked++;return r.fulfill({status:403,body:'{}'})}r.continue()});
 const p=await c.newPage(),errors=[];p.on('pageerror',e=>errors.push(e.message));await p.goto(url);await p.waitForFunction(()=>window.__wos&&window.WizardAppearance&&window.WosSkin);
 const frame=p.frames().find(f=>f.url().includes('/demo/apps/builder.html'))||await new Promise(async(resolve,reject)=>{const timer=setTimeout(()=>reject(Error('No generated app frame')),10000);p.on('framenavigated',f=>{if(f.url().includes('/demo/apps/builder.html')){clearTimeout(timer);resolve(f)}})});
 await frame.waitForFunction(()=>window.WizardTheme?.connected&&document.querySelector('#day')?.textContent.includes('Today'));
 const enteredURL=p.url();
 await p.locator('.pv-pill').click();await p.locator('.pv-skin[data-skin="paper"]').click();await frame.waitForFunction(()=>WizardTheme.current.skin==='paper');await p.locator('.pv-x').click();
 ok(width+' delivered skin menu changes the real app',await frame.evaluate(()=>WizardTheme.current.skin==='paper'));
 ok(width+' app opens already inside shell',await p.locator('iframe.appframe').count()===1);
 await frame.evaluate(()=>{window.qaDocument=crypto.randomUUID();window.qaInitialDocument=window.qaDocument;document.getElementById('habit-name').value='An unsent habit';const spacer=document.createElement('div');spacer.id='qa-spacer';spacer.style.height='1000px';document.body.append(spacer);window.scrollTo(0,120)});
 const stamp=await frame.evaluate(()=>window.qaDocument),scroll=await frame.evaluate(()=>scrollY),beforeData=await frame.evaluate(async()=>JSON.stringify(await(await fetch('api/habits')).json()));
 for(const skin of ['nebula','grove','paper','tide','ember','signal']){
  await p.evaluate(s=>WosSkin.set(s),skin);await frame.waitForFunction(s=>WizardTheme.current.skin===s,skin);
  await p.waitForFunction(()=>WizardAppearance.inspect().every(x=>x.ack));
  const same=await frame.evaluate(()=>{const a=WizardTheme.current,s=getComputedStyle(document.documentElement),body=getComputedStyle(document.body),button=getComputedStyle(document.querySelector('#add'));return {skin:a.skin,scheme:body.colorScheme,accent:s.getPropertyValue('--wz-accent').trim()===a.tokens.accent,text:s.getPropertyValue('--wz-text').trim()===a.tokens.text,draft:document.getElementById('habit-name').value,stamp:window.qaDocument,scroll:scrollY,canvas:body.backgroundColor,radius:getComputedStyle(document.querySelector('input')).borderRadius}});
  ok(width+' '+skin+' inherits live semantic roles and ack',same.accent&&same.text&&same.skin===skin&&same.canvas==='rgba(0, 0, 0, 0)'&&same.radius==='14px',same);
  ok(width+' '+skin+' preserves draft, document and scroll',same.draft==='An unsent habit'&&same.stamp===stamp&&Math.abs(same.scroll-scroll)<2,same);
 }
 ok(width+' skin changes preserve persisted data',await frame.evaluate(async()=>JSON.stringify(await(await fetch('api/habits')).json()))===beforeData);
 await frame.evaluate(()=>{document.getElementById('qa-spacer').remove();window.scrollTo(0,0)});
 await p.evaluate(()=>WosSkin.set('paper'));await frame.waitForFunction(()=>WizardTheme.current.skin==='paper');
 await frame.evaluate(()=>{const a={...WizardTheme.current,skin:'forged'};window.postMessage({type:'wizard:appearance',appearance:a},location.origin);dispatchEvent(new MessageEvent('message',{origin:'https://untrusted.invalid',source:parent,data:{type:'wizard:appearance',appearance:a}}));dispatchEvent(new MessageEvent('message',{origin:location.origin,source:parent,data:{type:'wizard:appearance',appearance:{...a,revision:0}}}));});
 await p.waitForTimeout(70);ok(width+' wrong source/origin and stale messages ignored',await frame.evaluate(()=>WizardTheme.current.skin==='paper'));
 await p.screenshot({path:path.join(out,'paper-'+width+'.png'),fullPage:true});
 const wid=await p.evaluate(()=>Object.keys(__wos.wins).find(k=>__wos.wins[k].appId==='habit-tracker'));
 await p.evaluate(wid=>__wos.setFull(wid,true),wid);await frame.waitForFunction(()=>WizardTheme.current.presentation==='immersive');
 ok(width+' fullscreen uses opaque skin canvas',await frame.evaluate(()=>getComputedStyle(document.body).backgroundColor!=='rgba(0, 0, 0, 0)'&&document.getElementById('habit-name').value==='An unsent habit'));
 await p.screenshot({path:path.join(out,'paper-full-'+width+'.png'),fullPage:true});
 await p.evaluate(()=>__wos.onBack());await frame.waitForFunction(()=>WizardTheme.current.presentation==='window');ok(width+' nested Back keeps shell open',p.url()===enteredURL&&await p.locator('#device').count()===1);
 await p.evaluate(()=>__wos.onBack());await p.waitForFunction(()=>__wos.idx===0);ok(width+' Back reaches shell home',await p.locator('#device').count()===1);
 await p.evaluate(()=>__wos.onBack());ok(width+' root Back stays in shell',p.url()===enteredURL&&await p.evaluate(()=>__wos.idx===0));
 await p.evaluate(()=>__wos.goDesk(1));await frame.waitForFunction(()=>WizardTheme.current.skin==='paper');
 ok(width+' reopened app is visibly focused',await p.evaluate(()=>__wos.wins[__wos.desks[__wos.idx].focus]?.appId==='habit-tracker')&&await frame.locator('#add').isVisible());
 await p.evaluate(()=>WosSkin.set('grove'));await frame.waitForFunction(()=>WizardTheme.current.skin==='grove');
 await p.screenshot({path:path.join(out,'grove-'+width+'.png'),fullPage:true});
 ok(width+' shell has no overflow',await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 ok(width+' zero page errors',errors.length===0,errors);
 await c.close();
}
const result={host:os.hostname(),url,passed:checks.length,checks,realAgentWrites:blocked,mode:'actual packaged app in copied real shell/skin host, same-origin adapter'};ok('zero real-agent writes',blocked===0);result.passed=checks.length;fs.writeFileSync(path.join(out,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));await browser.close();})().catch(e=>{console.error(e);process.exit(1)});
