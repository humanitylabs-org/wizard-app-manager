'use strict';
const assert=require('node:assert/strict');
const PUBLIC_METHODS=Object.freeze(['fetch','get','head','post','put','patch','delete']);
const DENIED='ABOUT_QA_DIRECT_API_REQUEST_DENIED';
// Keep the genuine APIRequestContext: Route.fetch uses its private _innerFetch
// and channel. Shadow public methods only; never replace context.request.
function guardRequestContext(ctx){
 const api=ctx.request,inner=api._innerFetch;
 assert.equal(typeof inner,'function','genuine APIRequestContext transport missing');
 for(const name of PUBLIC_METHODS){
  assert.equal(typeof api[name],'function','unexpected API method '+name);
  Object.defineProperty(api,name,{value:()=>{throw Error(DENIED);},writable:false,configurable:false});
 }
 assert.equal(ctx.request,api);assert.equal(api._innerFetch,inner);
 return api;
}
async function proveDirectDenied(ctx,page){
 assert.equal(page.request,ctx.request,'page API escaped context guard');
 for(const name of PUBLIC_METHODS){
  let denied=false;
  try{await ctx.request[name]('https://request-denial.invalid/never-sent');}
  catch(e){assert.equal(e.message,DENIED);denied=true;}
  assert(denied,'direct API method escaped: '+name);
 }
 return [...PUBLIC_METHODS];
}
module.exports={guardRequestContext,proveDirectDenied,PUBLIC_METHODS,DENIED};
