/* Generated template + actual shared client/host/appearance, isolated loopback only.
 * The registry/outer host DOM is a TEST HARNESS, not the production shell.
 * Native share outcomes are not asserted here; platform owner owns those tests.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const crypto = require('node:crypto'), os = require('node:os');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const args = Object.fromEntries(process.argv.slice(2).reduce((r,v,i,a)=>{if(v.startsWith('--'))r.push([v.slice(2),a[i+1]]);return r},[]));
const SOURCE = path.resolve(args.source || ''), CORE = path.resolve(args.core || ''), OUT = path.resolve(args.out || 'evidence/browser');
fs.mkdirSync(OUT,{recursive:true});
const checks=[],errors=[],external=[],writes=[];
let browser,server,backend;
const hashes={};
for(const name of ['app-client.js','app-services.js','appearance-host.js']) {
  assert(fs.statSync(path.join(CORE,name)).isFile(), `Missing actual shared resource: ${name}`);
  hashes[name]=crypto.createHash('sha256').update(fs.readFileSync(path.join(CORE,name))).digest('hex');
}
function ok(value,label){assert(value,label);checks.push(label);console.log('PASS',label)}
const harness=`<!doctype html><html data-skin="nebula"><head><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><style>
html {color-scheme:dark;--bg:#05030c;--ink:#f3efff;--ink-2:#b9b0d8;--ink-3:#7d74a3;--violet-2:#b99bff;--line-2:#b9b0d8}
body {margin:0;background:var(--bg);color:var(--ink)} .win{width:100%;height:92vh}iframe{border:0;width:100%;height:100%;color-scheme:inherit}
</style><script>window.WizCore={apps:()=>({APPS:[{id:'builder-qa',url:'/app/'}]})};</script>
<script defer src="/wizard-os-home/core/appearance-host.js"></script><script defer src="/wizard-os-home/core/app-services.js"></script></head>
<body><button id="back">Back</button><div class="device"><div class="win"></div></div>
<script>addEventListener('DOMContentLoaded',()=>{let f=document.createElement('iframe');f.id='app';f.src='/app/';document.querySelector('.win').appendChild(f);WizAppServices.attach(f,{appId:'builder-qa',path:'/app/'});document.querySelector('#back').onclick=()=>window.lastBack=WizAppServices.back(f)});</script></body></html>`;
function file(res,p) {if(!fs.existsSync(p)){res.writeHead(404);return res.end('not found')}const ext=path.extname(p);res.setHeader('Content-Type',{'.js':'application/javascript','.css':'text/css','.html':'text/html'}[ext]||'application/octet-stream');res.end(fs.readFileSync(p))}
function port(){return new Promise(resolve=>{let s=http.createServer();s.listen(0,'127.0.0.1',()=>{let p=s.address().port;s.close(()=>resolve(p))})})}
(async()=>{
  const backendPort=await port();
  backend=spawn(process.env.QA_PYTHON||'python3',[path.join(SOURCE,'app/server.py')],{env:{...process.env,BIND:'127.0.0.1',PORT:String(backendPort),DATA_DIR:path.join(OUT,'task-data'),WIZARD_TRUSTED_HOSTS:`127.0.0.1:${backendPort}`,WIZARD_TRUSTED_ORIGINS:`http://127.0.0.1:${backendPort}`},stdio:['ignore','pipe','pipe']});
  const backendLog=fs.createWriteStream(path.join(OUT,'task-api.log'));backend.stdout.pipe(backendLog);backend.stderr.pipe(backendLog);
  backend.on('error',e=>errors.push(String(e)));
  for(let i=0;i<100;i++){try{let r=await fetch(`http://127.0.0.1:${backendPort}/healthz`);if(r.ok)break}catch{}await new Promise(r=>setTimeout(r,50));if(i===99)throw Error('Generated task API not ready')}
  server=http.createServer((req,res)=>{
    const u=new URL(req.url,'http://localhost');
    if(u.pathname.startsWith('/app/api/')){
      if(req.method!=='GET')writes.push({method:req.method,path:u.pathname,scope:'isolated loopback task API'});
      const q=http.request({host:'127.0.0.1',port:backendPort,path:req.url.slice('/app'.length),method:req.method,headers:{...req.headers,host:`127.0.0.1:${backendPort}`,origin:`http://127.0.0.1:${backendPort}`}},r=>{res.writeHead(r.statusCode,r.headers);r.pipe(res)});q.on('error',e=>{res.writeHead(502);res.end(String(e))});req.pipe(q);return;
    }
    if(req.method!=='GET'){res.writeHead(405);return res.end()}
    if(u.pathname==='/host'){res.setHeader('Content-Type','text/html');return res.end(harness)}
    let p;
    if(u.pathname.startsWith('/wizard-os-home/core/'))p=path.join(CORE,path.basename(u.pathname));
    else if(u.pathname.startsWith('/app/'))p=path.join(SOURCE,'web',u.pathname.slice(5)||'index.html');
    else {res.writeHead(404);return res.end()}
    file(res,p);
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const base=`http://127.0.0.1:${server.address().port}`;
  browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
  for(const width of [393,1440]){
    const ctx=await browser.newContext({viewport:{width,height:900},hasTouch:width===393,isMobile:width===393});
    await ctx.route('**/*',r=>{if(new URL(r.request().url()).origin!==base){external.push(r.request().url());return r.abort()}return r.continue()});
    const page=await ctx.newPage();page.on('pageerror',e=>errors.push(String(e)));
    await page.goto(base+'/host');await page.waitForFunction(()=>document.querySelector('#app')?.contentWindow.WizardApp?.hosted);
    const frame=page.frames().find(f=>f.url().startsWith(base+'/app/'));
    await frame.waitForFunction(()=>WizardApp.theme.get()!==null);
    ok(await frame.evaluate(()=>innerWidth)===width,`${width} real frame layout viewport matches requested device width`);
    await frame.locator('#habit-name').fill('Unsent domain draft');
    await frame.evaluate(()=>window.qaDoc=document);
    for(const light of [false,true]) {
      await page.evaluate(light=>{const r=document.documentElement;r.dataset.skin=light?'fixture-light':'nebula';r.style.colorScheme=light?'light':'dark';for(const [k,v] of Object.entries(light?{bg:'#faf8f3',ink:'#201d28','ink-2':'#52485f','ink-3':'#65596f','violet-2':'#6943a7','line-2':'#d3cbdc'}:{bg:'#05030c',ink:'#f3efff','ink-2':'#b9b0d8','ink-3':'#7d74a3','violet-2':'#b99bff','line-2':'#b9b0d8'}))r.style.setProperty('--'+k,v);WizAppearance.sync()},light);
      await frame.waitForFunction(light=>document.documentElement.dataset.colorScheme===(light?'light':'dark'),light);
      ok(await frame.evaluate(()=>getComputedStyle(document.body).backgroundColor==='rgba(0, 0, 0, 0)'&&getComputedStyle(document.documentElement).backgroundColor==='rgba(0, 0, 0, 0)'),`${width} ${light?'light':'dark'} transparent hosted canvas`);
      ok(await frame.evaluate(()=>{const p=document.createElement('span');p.style.color='var(--wz-text)';document.body.append(p);const expected=getComputedStyle(p).color;p.remove();return getComputedStyle(document.body).color===expected}),`${width} ${light?'light':'dark'} text inherits actual shared semantic token`);
      ok(await frame.evaluate(()=>getComputedStyle(document.querySelector('.wz-head')).display==='none'),`${width} no redundant app-title chrome`);
      ok(await frame.evaluate(()=>document===qaDoc)&&await frame.locator('#habit-name').inputValue()==='Unsent domain draft',`${width} live theme preserves document/draft`);
      ok(await frame.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`${width} no horizontal overflow`);
      await page.evaluate(()=>{document.querySelector('.device').classList.add('immersive');document.querySelector('.win').classList.add('full')});
      await frame.waitForFunction(()=>document.documentElement.dataset.presentation==='immersive');
      ok(await frame.evaluate(()=>getComputedStyle(document.querySelector('.wz-head')).display!=='none'),`${width} useful header allowed fullscreen`);
      await page.screenshot({path:path.join(OUT,`${width}-${light?'light':'dark'}.png`)});
      await page.evaluate(()=>{document.querySelector('.device').classList.remove('immersive');document.querySelector('.win').classList.remove('full')});
      await frame.waitForFunction(()=>document.documentElement.dataset.presentation==='window');
    }
    // Actual domain callbacks against actual core; never a substitute client.
    await frame.evaluate(()=>{
      window.nested=false;
      window.openNested=()=>{nested=true;const panel=document.createElement('section');panel.id='qa-nested';panel.innerHTML='<h2>Record details (QA domain view)</h2><button id="qa-close">Close details</button>';document.querySelector('.wz-scroll').prepend(panel);panel.querySelector('button').onclick=closeNested};
      window.closeNested=()=>{if(!nested)return false;nested=false;document.querySelector('#qa-nested')?.remove();return true};
      WizardApp.back.register({canGoBack:()=>nested,back:closeNested});openNested();
    });
    await frame.locator('#qa-nested').waitFor();
    await page.locator('#back').click();
    ok(await page.evaluate(()=>lastBack==='handled')&&!await frame.evaluate(()=>nested)&&await frame.locator('#qa-nested').count()===0,`${width} nested Back closes visible domain view, app stays open`);
    await frame.evaluate(()=>openNested());await frame.locator('#qa-close').click();
    ok(!await frame.evaluate(()=>nested)&&await frame.locator('#qa-nested').count()===0,`${width} on-screen close matches nested Back`);
    await frame.evaluate(()=>{window.overlay=true;window.token=WizardApp.back.push(()=>{overlay=false;return true})});
    await page.locator('#back').click();
    ok(!await frame.evaluate(()=>overlay),`${width} overlay Back LIFO callback`);
    await page.locator('#back').click();
    ok(page.url()===base+'/host'&&await page.evaluate(()=>lastBack==='atRoot')&&await frame.evaluate(()=>WizardApp.hosted),`${width} root Back keeps app open`);
    ok(await page.evaluate(()=>WizAppServices.update.busy()),`${width} actual unsaved template draft blocks update`);
    await frame.locator('#habit-name').fill('');
    ok(!await page.evaluate(()=>WizAppServices.update.busy()),`${width} explicit draft discard clears update hold`);
    // Exercise generated real service save, not a synthetic API response.
    const savedName='Saved local QA record '+width;
    await frame.locator('#habit-name').fill(savedName);await frame.locator('#add').click();
    await frame.locator('#habits').getByText(savedName,{exact:true}).first().waitFor();
    ok(!await page.evaluate(()=>WizAppServices.update.busy()),`${width} durable save/readback releases template hold`);
    await page.reload();
    const readback=page.frames().find(f=>f.url().startsWith(base+'/app/'));
    await readback.locator('#habits').getByText(savedName,{exact:true}).first().waitFor();
    ok(true,`${width} generated task API reads back saved record after document reload`);
    await ctx.close();
  }
  for(const light of [false,true]){
    const ctx=await browser.newContext({viewport:{width:393,height:900},colorScheme:light?'light':'dark'});
    const page=await ctx.newPage();page.on('pageerror',e=>errors.push(String(e)));
    await page.goto(base+'/app/');await page.waitForFunction(()=>WizardApp?.version===1);
    ok(await page.evaluate(()=>!WizardApp.hosted&&getComputedStyle(document.documentElement).colorScheme===(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark')),`standalone ${light?'light':'dark'} uses system preference without app theme state`);
    ok(await page.evaluate(()=>{const p=document.createElement('span');p.style.color='var(--canvas)';document.body.append(p);const expected=getComputedStyle(p).color;p.remove();return getComputedStyle(document.body).backgroundColor===expected}),`standalone ${light?'light':'dark'} paints system-preference fallback canvas`);
    await page.locator('#habit-name').click();
    await page.goBack({waitUntil:'domcontentloaded'}).catch(()=>{});
    // CloseWatcher/popstate delivery depends on browser; require one Back stay.
    await page.locator('#wizard-app-back-notice').getByText('Press back again to exit',{exact:true}).waitFor();
    ok(page.url().startsWith(base+'/app/')&&await page.evaluate(()=>!!WizardApp),`standalone ${light?'light':'dark'} first root Back warns and keeps app open`);
    await ctx.close();
  }
  ok(errors.length===0,'zero browser runtime errors');ok(external.length===0,'zero external requests/production writes');
  for(const [name,hash] of Object.entries(hashes))ok(crypto.createHash('sha256').update(fs.readFileSync(path.join(CORE,name))).digest('hex')===hash,'shared resource unchanged: '+name);
})().then(()=>finish(0)).catch(e=>{errors.push(String(e.stack||e));console.error(e);return finish(1)});
async function finish(code){
  if(browser)await browser.close();if(server)await new Promise(r=>server.close(r));if(backend)backend.kill();
  const receipt={host:os.hostname(),scope:'generated template + actual core resources, test host registry/DOM, isolated real task API',source:SOURCE,core:CORE,core_sha256:hashes,passed:checks.length,checks,errors,external_requests:external,local_task_writes:writes,exit_code:code};
  fs.writeFileSync(path.join(OUT,'browser.json'),JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify(receipt));process.exitCode=code;
}
