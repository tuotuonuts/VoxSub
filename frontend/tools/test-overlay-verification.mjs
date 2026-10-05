import assert from 'node:assert/strict';
import { importShared, cleanupShared } from './esbuild-ts.mjs';
const {VerifiedOverlaySurface}=await importShared('src/main/overlay-verification.ts',{bundle:true});
const {frameShape}=await importShared('src/shared/overlay-shape.ts',{bundle:true});
const {validOverlayFrame}=await importShared('src/shared/overlay-frame.ts',{bundle:true});
const frame={x:6,y:6,width:848,height:128,radius:16,viewportWidth:860,viewportHeight:140};
const wait=()=>new Promise(r=>setTimeout(r,140));
let count=0;
function fixture(probe=async()=>({regionVerified:true,materialVerified:true,desktop:'not_run'})){
 const calls=[],states=[];let unsafe=0;
 const win={getContentSize:()=>[860,140],webContents:{getZoomFactor:()=>1},setShape:s=>calls.push(['shape',s]),setBackgroundMaterial:m=>calls.push(['material',m])};
 const monitor=new VerifiedOverlaySurface(win,probe,s=>states.push(s),()=>unsafe++);
 return {win,monitor,calls,states,get unsafe(){return unsafe;}};
}
async function check(name,fn){await fn();count++;console.log('PASS '+name);}
try{
 await check('actual bounds, zoom and malformed geometry',()=>{
  assert.equal(validOverlayFrame({...frame,width:Infinity}),false);
  assert.equal(validOverlayFrame({...frame,x:900}),false);
  assert.equal(frameShape(860,140,1.5,frame),null);
  const s=frameShape(1290,210,1.5,frame);assert.equal(Math.min(...s.map(r=>r.x)),9);
  assert.equal(Math.max(...s.map(r=>r.x+r.width)),1281);
 });
 await check('wait for geometry, then readback; desktop remains unverified',async()=>{
  const f=fixture();f.monitor.update(true,null);assert.equal(f.calls.length,0);
  f.monitor.update(true,frame);assert.equal(f.monitor.evidence.active,false);
  assert.deepEqual(f.calls.map(c=>c[0]),['shape','material']);
  await wait();assert.equal(f.monitor.evidence.active,true);assert.equal(f.monitor.evidence.desktopCheck,'not_run');
  const count=f.calls.length;f.monitor.update(true,frame);assert.equal(f.calls.length,count);f.monitor.dispose();
 });
 await check('failed acrylic readback safely removes material retaining clip',async()=>{
  const f=fixture(async(_shape,material)=>({regionVerified:true,materialVerified:!material,desktop:'not_run'}));
  f.monitor.update(true,frame);await wait();assert.equal(f.monitor.evidence.active,false);
  assert.equal(f.monitor.evidence.fallbackReason,'native_verification_failed');assert.equal(f.unsafe,0);
  assert.deepEqual(f.calls.filter(c=>c[0]==='material').map(c=>c[1]),['acrylic','none']);
  assert.ok(f.calls.filter(c=>c[0]==='shape').every(c=>c[1].length>0));
  f.monitor.update(true,frame);await wait();assert.equal(f.monitor.evidence.active,false);f.monitor.dispose();
 });
 await check('unverifiable removal replaces only owner; disposed callbacks do nothing',async()=>{
  const f=fixture(async()=>({regionVerified:false,materialVerified:false,desktop:'not_run'}));
  f.monitor.update(true,frame);await wait();assert.equal(f.unsafe,1);f.monitor.dispose();
  let resolve;const stale=fixture(()=>new Promise(r=>resolve=r));stale.monitor.update(true,frame);await wait();
  stale.monitor.dispose();resolve({regionVerified:false,materialVerified:false});await Promise.resolve();assert.equal(stale.unsafe,0);
 });
 await check('late enabled result cannot override later disabled state',async()=>{
  let resolve;let n=0;const f=fixture(()=>++n===1?new Promise(r=>resolve=r):Promise.resolve({regionVerified:true,materialVerified:true}));
  f.monitor.update(true,frame);await wait();f.monitor.update(false,frame);
  resolve({regionVerified:true,materialVerified:true});await wait();assert.equal(f.monitor.evidence.active,false);f.monitor.dispose();
 });
 await check('explicit retry clears blocked material failure',async()=>{
  let success=false;const f=fixture(async(_s,m)=>({regionVerified:true,materialVerified:!m||success}));
  f.monitor.update(true,frame);await wait();success=true;f.monitor.reset();f.monitor.update(true,frame);await wait();
  assert.equal(f.monitor.evidence.active,true);assert.equal(f.monitor.evidence.fallbackReason,null);f.monitor.dispose();
 });
 await check('native apply failure never reports active',()=>{
  const f=fixture();f.win.setShape=()=>{throw Error('GDI failure');};f.monitor.update(true,frame);
  assert.equal(f.monitor.evidence.active,false);assert.equal(f.unsafe,1);f.monitor.dispose();
 });
 await check('renderer observer coalesces updates, deduplicates frames and disposes listeners',async()=>{
  const {observeOverlayFrame}=await importShared('src/renderer/overlay-frame.ts',{bundle:true});
  const saved=new Map();for(const key of ['window','innerWidth','innerHeight','getComputedStyle','ResizeObserver','requestAnimationFrame','cancelAnimationFrame'])saved.set(key,globalThis[key]);
  try{
   let resize,raf,cancelled=0,disconnected=0;const events=new Map(),sent=[];
   globalThis.window={addEventListener:(k,fn)=>events.set(k,fn),removeEventListener:k=>events.delete(k)};
   globalThis.innerWidth=860;globalThis.innerHeight=140;globalThis.getComputedStyle=()=>({borderTopLeftRadius:'16px'});
   globalThis.ResizeObserver=class{constructor(fn){resize=fn;}observe(){}disconnect(){disconnected++;}};
   globalThis.requestAnimationFrame=fn=>{raf=fn;return 1;};globalThis.cancelAnimationFrame=()=>cancelled++;
   let bounds={...frame};const dispose=observeOverlayFrame({getBoundingClientRect:()=>bounds},f=>sent.push(f));
   resize();raf();assert.equal(sent.length,1);resize();raf();assert.equal(sent.length,1);
   bounds={...bounds,width:800};events.get('resize')();raf();assert.equal(sent.at(-1).width,800);
   dispose();resize();raf();assert.equal(sent.length,2);assert.equal(events.size,0);assert.equal(disconnected,1);assert.equal(cancelled,1);
  }finally{for(const [key,value]of saved)if(value===undefined)delete globalThis[key];else globalThis[key]=value;}
 });
 console.log(`${count} overlay verification checks passed`);
}finally{cleanupShared();}
