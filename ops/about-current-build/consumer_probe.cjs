'use strict';
// Exercise the exact tracked consumer against bytes returned by native origin.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8')),source=fs.readFileSync(process.argv[2],'utf8');
const requests=[];let feature,html='';
const S={features:[],register(f){feature=f;this.features.push(f);},refresh(){html=feature.html();}};
const env={window:{WosSettings:S},document:{currentScript:{src:'https://wizard.test/wizard-os-home/core/box-source.js'}},location:{href:'https://wizard.test/wizard-os-home/'},URL,Date,fetch:async (url,options)=>{requests.push({url,cache:options.cache});assert.equal(options.cache,'no-store');if(url.endsWith('/my-release.json'))return {ok:false};if(url.endsWith('/build.json'))return {ok:input.status===200,json:async()=>JSON.parse(input.body)};throw Error('invalid/missing stamp must not request comparison');}};
vm.runInNewContext(source,env);html=feature.html();
(async()=>{for(let i=0;i<5;i++)await new Promise(r=>setImmediate(r));html=feature.html();assert(html.includes('Main commit unknown'));assert(!html.includes('Built on main'));assert.equal(requests.filter(r=>r.url.includes('_main-compare')).length,0);console.log(JSON.stringify({accepted:true,invalid_stamp_rejected:true,displayed_html:html,requests}));})().catch(e=>{console.error(String(e));process.exitCode=1;});
