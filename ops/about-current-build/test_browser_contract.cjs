'use strict';
// Browser-free source-focused regressions. No sockets, browser or owner files.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto'),path=require('node:path');
const {guardRequestContext,proveDirectDenied,PUBLIC_METHODS}=require('./request-guard.cjs');
const source=fs.readFileSync(path.join(__dirname,'browser.cjs'),'utf8');
const checks=[];const check=(name,value)=>{assert(value,name);checks.push(name);};
(async()=>{
 let transports=0;
 const api={_innerFetch:async()=>{transports++;return 'tracked-bytes';}};
 for(const m of PUBLIC_METHODS)api[m]=async()=>{throw Error('unprotected public method');};
 const ctx={request:api},page={request:api};guardRequestContext(ctx);
 check('real request object retained',ctx.request===api);
 await proveDirectDenied(ctx,page);check('seven public methods denied before transport',transports===0);
 check('private route transport retained',await ctx.request._innerFetch()==='tracked-bytes'&&transports===1);
 check('no separate public request context',!source.includes('request.newContext')&&!source.includes("defineProperty(ctx,'request'"));
 check('no unrestricted page escape',!source.includes('route.continue(')&&!source.includes('route.fallback('));
 const start=source.indexOf('const handleRoute=async route=>{'),end=source.indexOf("   await ctx.route('**/*'",start);
 assert(start>=0&&end>start);
 const body=Buffer.from('synthetic unit script');const digest=crypto.createHash('sha256').update(body).digest('hex');
 const row={route_errors:[],refused_network:[],live_static_responses:[],static_scripts:[],synthetic_requests:[],metadata_requests:[],live_metadata_responses:[]};
 let aborts=0,fulfilled=0,flushes=0;
 const env={assert,ORIGIN:'https://wizard.tailc0f369.ts.net',PREFIX:'/wizard-os-home/',row,closing:false,phone:false,capture:{static:{'core/box-source.js':{sha256:digest}}},fixture:{responses:{}},build:{commit:'a'.repeat(40)},hash:b=>crypto.createHash('sha256').update(b).digest('hex'),flush:()=>flushes++,process:{env:{QA_PHASE:'candidate'}},URL};
 vm.createContext(env);vm.runInContext(source.slice(start,end)+'\nthis.handler=handleRoute;',env);
 const req={url:()=>env.ORIGIN+env.PREFIX+'core/box-source.js',method:()=>'GET',resourceType:()=>'script'};
 const route={request:()=>req,fetch:async opts=>{assert.deepEqual(opts,{maxRedirects:0});await api._innerFetch();return {status:()=>200,body:async()=>body};},fulfill:async()=>{fulfilled++;},abort:async()=>{aborts++;}};
 // vm objects have distinct prototypes; inspect explicit option instead.
 route.fetch=async opts=>{assert.equal(opts.maxRedirects,0);await api._innerFetch();return {status:()=>200,body:async()=>body};};
 await env.handler(route);
 check('original tracked route completes behind public guard',fulfilled===1&&row.live_static_responses.length===1&&row.route_errors.length===0);
 route.fetch=async()=>{throw Error('synthetic transport failure');};route.abort=async()=>{aborts++;throw Error('synthetic abort failure');};
 await env.handler(route);
 check('route and abort failures captured without rejection',row.route_errors.length===1&&row.route_errors[0].error.includes('synthetic transport failure')&&row.route_errors[0].abort_error.includes('synthetic abort failure')&&aborts===1&&flushes>=2);
 check('awaited route fulfill failures are caught',source.includes('return await route.fulfill')&&source.includes('return await json('));
 check('real matrix preserved',source.includes('for(const phone of [false,true])')&&source.includes("['nebula','grove']")&&source.includes('text.includes(expectedTitle)&&text.includes(expectedSub)')&&source.includes('row.refused_network.length===0'));
 const {fixture}=require(path.join(process.env.QA_FIXTURE_ROOT||path.join(__dirname,'source'),'qa-proof-fixture.cjs'));
 const get=k=>{const r=fixture.responses['GET '+k];assert(r&&r.status===200,'synthetic read catalogue missing '+k);return JSON.parse(r.body);};
 const bootstrap=get('/wizard/v1/bootstrap');
 check('bootstrap declares synthetic settings/capabilities/endpoints',typeof bootstrap.bootstrap==='number'&&bootstrap.settings&&bootstrap.capabilities&&bootstrap.endpoints);
 for(const key of ['apps','sessions']){const endpoint=bootstrap.endpoints[key];assert.equal(typeof endpoint,'string');get(new URL(endpoint,fixture.base).pathname);}
 check('bootstrap declared read endpoints have responses',true);
 for(const k of ['/wizard/v1/ui-preferences','/wizard/v1/skins','/wizard/v1/apps','/wizard/hermes/api/sessions','/wizard/hermes/api/model/options'])get(k);
 check('startup synthetic read catalogue present',true);
 console.log(JSON.stringify({accepted:true,browser_free:true,synthetic_unit_only:true,count:checks.length,checks}));
})().catch(e=>{console.error(String(e));process.exitCode=1;});
