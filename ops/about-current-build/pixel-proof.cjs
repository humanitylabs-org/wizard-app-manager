'use strict';
// Decode Chromium RGB/RGBA PNG scanlines; compare pixels, never PNG encodings.
const assert=require('node:assert/strict'),zlib=require('node:zlib');
function png(buffer){
 assert.equal(buffer.subarray(0,8).toString('hex'),'89504e470d0a1a0a');
 let width,height,bpp,parts=[];
 for(let p=8;p<buffer.length;){const n=buffer.readUInt32BE(p),type=buffer.toString('ascii',p+4,p+8),data=buffer.subarray(p+8,p+8+n);p+=n+12;
  if(type==='IHDR'){width=data.readUInt32BE(0);height=data.readUInt32BE(4);assert.equal(data[8],8);assert([2,6].includes(data[9]));bpp=data[9]===6?4:3;assert.equal(data[12],0);}
  if(type==='IDAT')parts.push(data);
 }
 const stride=width*bpp,raw=zlib.inflateSync(Buffer.concat(parts)),pixels=Buffer.alloc(stride*height);
 assert.equal(raw.length,(stride+1)*height);
 const paeth=(a,b,c)=>{const p=a+b-c,pa=Math.abs(p-a),pb=Math.abs(p-b),pc=Math.abs(p-c);return pa<=pb&&pa<=pc?a:pb<=pc?b:c;};
 for(let y=0;y<height;y++){const filter=raw[y*(stride+1)];assert(filter<=4);for(let x=0;x<stride;x++){const a=x>=bpp?pixels[y*stride+x-bpp]:0,b=y?pixels[(y-1)*stride+x]:0,c=y&&x>=bpp?pixels[(y-1)*stride+x-bpp]:0;pixels[y*stride+x]=(raw[y*(stride+1)+1+x]+[0,a,b,Math.floor((a+b)/2),paeth(a,b,c)][filter])&255;}}
 return {width,height,bpp,pixels};
}
function difference(a,b){a=png(a);b=png(b);assert.deepEqual([a.width,a.height,a.bpp],[b.width,b.height,b.bpp]);let count=0;for(let i=0;i<a.pixels.length;i+=a.bpp)if(!a.pixels.subarray(i,i+a.bpp).equals(b.pixels.subarray(i,i+a.bpp)))count++;return count;}
async function proveOpaque(page,clip){
 return await page.evaluate(()=>{const body=document.querySelector('#settingsBody'),panel=body.closest('.win')||body.closest('.dsettings,.sheet');if(!panel)throw Error('actual Settings panel not found');const style=getComputedStyle(panel),z=Number(style.zIndex);if(!Number.isFinite(z))throw Error('panel stacking not numeric');const noise=document.createElement('div');noise.id='qa-about-underlay';noise.style.cssText=`position:fixed;inset:0;z-index:${z-1};pointer-events:none;overflow:hidden;background:#fff;color:#000;font:bold 28px/1 monospace`;noise.textContent='LOUD UNDERLAY 123456789 '.repeat(5000);panel.before(noise);return {z,background:style.backgroundColor,opacity:style.opacity,backdrop:style.backdropFilter};});
}
module.exports={png,difference,proveOpaque};
