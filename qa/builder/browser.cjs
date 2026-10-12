const fs=require('fs'),path=require('path');
let pw;try{pw=require('playwright')}catch(e){pw=require(process.env.QA_PLAYWRIGHT_MODULE||'playwright-core')}
const origin=process.argv[2]||'http://127.0.0.1:18799/';
const out=process.env.QA_OUT||path.join(require('os').tmpdir(),'wizard-builder-browser');
(async()=>{
fs.mkdirSync(out,{recursive:true});const browser=await pw.chromium.launch({headless:true});
const context=await browser.newContext({viewport:{width:393,height:852},isMobile:true,hasTouch:true,userAgent:'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131.0.0.0 Mobile Safari/537.36'});
let agentWrites=0;const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
await context.route('**/*',async route=>{const req=route.request();const u=new URL(req.url());if(!['GET','HEAD'].includes(req.method())&&!/^\/api\/habits(?:\/\d+\/today)?$/.test(u.pathname)){agentWrites++;return route.fulfill({status:403,body:'QA refuses non-app writes'});}return route.continue();});
const checks=[];async function ok(name,condition){if(!condition)throw Error(name);checks.push(name);}
await page.goto(origin);await page.locator('#day').filter({hasText:'Today'}).waitFor();await ok('dark mobile view',await page.evaluate(()=>getComputedStyle(document.body).backgroundColor==='rgb(15, 17, 21)'));
const name='Read 10 pages';
const rows=await page.locator('#habits li').count();
await page.fill('#habit-name',name);await page.tap('#add');await page.locator('#status').filter({hasText:'Habit added.'}).waitFor();
await ok('add through actual API',await page.locator('#habits li').count()===rows+1);
let card=page.locator('#habits li').filter({hasText:name});await card.locator('button').tap();await card.locator('button').filter({hasText:'Done · Undo'}).waitFor();await ok('mark persisted',await card.locator('button').getAttribute('aria-pressed')==='true');
await page.screenshot({path:path.join(out,'mobile-marked.png'),fullPage:true});
await card.locator('button').tap();await card.locator('button').filter({hasText:'Mark done'}).waitFor();await ok('undo persisted',await card.locator('button').getAttribute('aria-pressed')==='false');
await page.fill('#habit-name',name);await page.tap('#add');await page.locator('#status.error').filter({hasText:'already exists'}).waitFor();await ok('duplicate error without phantom data',await page.locator('#habits li').count()===rows+1);
await page.evaluate(()=>{window.popCount=0;addEventListener('popstate',()=>window.popCount++);});
const length=await page.evaluate(()=>history.length);
for(let i=0;i<3;i++){const count=await page.evaluate(()=>window.popCount);await page.evaluate(()=>history.back());await page.waitForFunction(n=>window.popCount>n,count);await ok('root Back stays '+i,page.url()===origin);}
await ok('root Back does not grow history',await page.evaluate(()=>history.length)===length);
await page.reload();await page.locator('#habits li').filter({hasText:name}).waitFor();await ok('reload preserves data',await page.locator('#habits li').count()===rows+1);
await page.screenshot({path:path.join(out,'mobile-ready.png'),fullPage:true});
await ok('zero page errors',errors.length===0);await ok('zero real-agent writes',agentWrites===0);
const desktop=await context.newPage();await desktop.setViewportSize({width:1200,height:850});await desktop.goto(origin);await desktop.locator('#day').filter({hasText:'Today'}).waitFor();await desktop.screenshot({path:path.join(out,'desktop.png'),fullPage:true});await ok('desktop view bounded',await desktop.locator('main').evaluate(e=>e.getBoundingClientRect().width<=480));
await browser.close();const result={origin,host:require('os').hostname(),checks,passed:checks.length,errors,agentWrites};fs.writeFileSync(path.join(out,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
})().catch(e=>{console.error(e);process.exit(1)});
