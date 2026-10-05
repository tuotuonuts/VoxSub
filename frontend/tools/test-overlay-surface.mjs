import assert from 'node:assert/strict';
import fs from 'node:fs';
import {importShared,cleanupShared} from './esbuild-ts.mjs';
const [{overlayShape,OVERLAY_FRAME_INSET,OVERLAY_FRAME_RADIUS},{OverlaySurface}]=await importShared(['src/shared/overlay-shape.ts','src/main/overlay-surface.ts'],{bundle:true});
let count=0;
const check=(name,fn)=>{fn();count++;console.log('PASS '+name);};
const contains=(rects,x,y)=>rects.some(r=>x>=r.x&&x<r.x+r.width&&y>=r.y&&y<r.y+r.height);
try {
 for(const zoom of [1,1.25,1.5,2,5])for(const [w,h] of [[860,140],[320,64],[1600,900],[18,18]])check(`rounded geometry ${w}x${h} zoom=${zoom}`,()=>{
  const rects=overlayShape(w,h,zoom),inset=Math.ceil(6*zoom);
  if(w-2*inset<4||h-2*inset<4){assert.equal(rects,null);return;}
  assert.ok(rects.length<=2*Math.ceil(16*zoom)+1);
  assert.ok(contains(rects,w/2,h/2));assert.ok(!contains(rects,0,h/2));assert.ok(!contains(rects,inset,inset));
  for(const r of rects){assert.ok(r.x>=inset&&r.y>=inset&&r.x+r.width<=w-inset&&r.y+r.height<=h-inset);assert.ok(r.width>0&&r.height>0);assert.ok(rects.some(b=>b.x===r.x&&b.width===r.width&&b.height===r.height&&b.y===h-r.y-r.height));}
 });
 check('invalid geometry never returns [] (which would unclip)',()=>{for(const args of [[0,0],[NaN,140],[860,Infinity],[860,140,0],[860,140,6]])assert.equal(overlayShape(...args),null);});
 function fixture(){const calls=[];const win={size:[860,140],zoom:1,fail:'',getContentSize(){return this.size;},webContents:{getZoomFactor:()=>win.zoom},setShape(r){calls.push(['shape',r]);if(win.fail==='shape'||win.fail==='shape-and-none')throw Error('shape failed');win.reentrant?.();},setBackgroundMaterial(m){calls.push(['material',m]);if(win.fail===m||win.fail==='allMaterials'||(m==='none'&&win.fail==='shape-and-none'))throw Error(m+' failed');}};return {win,calls};}
 check('clip precedes material; strength-only requests are idempotent',()=>{const s=new OverlaySurface(),{win,calls}=fixture();assert.equal(s.apply(win,false),false);assert.equal(calls.length,0);assert.equal(s.apply(win,true),true);assert.deepEqual(calls.map(c=>c[0]),['shape','material']);s.apply(win,true);assert.equal(calls.length,2);s.apply(win,false);assert.equal(calls[2][1],'none');assert.deepEqual(calls[3],['shape',[]]);});
 check('resize / zoom recompute geometry without reinstalling material',()=>{const s=new OverlaySurface(),{win,calls}=fixture();s.apply(win,true);win.size=[640,100];s.apply(win,true);win.zoom=1.5;s.apply(win,true);assert.equal(calls.filter(c=>c[0]==='material').length,1);assert.equal(calls.at(-1)[1][0].y,9);});
 check('monitor scale change rebuilds the region without double-scaling DIP geometry',()=>{const s=new OverlaySurface(),{win,calls}=fixture();s.apply(win,true,1);s.apply(win,true,1.5);assert.equal(calls.filter(c=>c[0]==='shape').length,2);assert.deepEqual(calls[0][1],calls[2][1]);assert.equal(calls.filter(c=>c[0]==='material').length,1);});
 check('replacement owner always installs its own material',()=>{const s=new OverlaySurface(),a=fixture(),b=fixture();s.apply(a.win,true);s.apply(b.win,true);assert.deepEqual(b.calls.map(c=>c[0]),['shape','material']);});
 check('shape failure never enables acrylic',()=>{const s=new OverlaySurface(),{win,calls}=fixture();win.fail='shape';assert.throws(()=>s.apply(win,true));assert.ok(!calls.some(c=>c[1]==='acrylic'));});
 check('material failure removes material before unclip',()=>{const s=new OverlaySurface(),{win,calls}=fixture();win.fail='acrylic';assert.throws(()=>s.apply(win,true));assert.deepEqual(calls.map(c=>c[0]),['shape','material','material','shape']);assert.equal(calls[2][1],'none');assert.deepEqual(calls[3][1],[]);});
 check('failed material removal retains clipped region and retries',()=>{const s=new OverlaySurface(),{win,calls}=fixture();s.apply(win,true);win.fail='none';assert.throws(()=>s.apply(win,false));assert.ok(!calls.some(c=>c[0]==='shape'&&c[1].length===0));win.fail='';s.apply(win,false);assert.deepEqual(calls.at(-1),['shape',[]]);});
 check('partially failing acrylic and cleanup never unclip on disable retry',()=>{const s=new OverlaySurface(),{win,calls}=fixture();win.fail='allMaterials';assert.throws(()=>s.apply(win,true));assert.throws(()=>s.apply(win,false));assert.ok(!calls.some(c=>c[0]==='shape'&&c[1].length===0));win.fail='';s.apply(win,false);assert.deepEqual(calls.at(-1),['shape',[]]);});
 check('failed shape plus cleanup never caches success and must reclip before retry',()=>{const s=new OverlaySurface(),{win,calls}=fixture();win.fail='shape-and-none';assert.throws(()=>s.apply(win,true));win.fail='';const before=calls.length;assert.equal(s.apply(win,true),true);assert.deepEqual(calls.slice(before).map(c=>c[0]),['shape','material']);});
 check('throwing region reset invalidates cache before re-enable',()=>{const s=new OverlaySurface(),{win,calls}=fixture();s.apply(win,true);win.fail='shape';assert.throws(()=>s.apply(win,false));win.fail='';const before=calls.length;s.apply(win,true);assert.deepEqual(calls.slice(before).map(c=>c[0]),['shape','material']);});
 check('unknown material state is retried instead of reported as already active',()=>{const s=new OverlaySurface(),{win,calls}=fixture();win.fail='allMaterials';assert.throws(()=>s.apply(win,true));win.fail='';const before=calls.length;assert.equal(s.apply(win,true),true);assert.deepEqual(calls.slice(before).map(c=>c[0]),['material']);assert.equal(calls.at(-1)[1],'acrylic');});
 check('native resize reentry is bounded',()=>{const s=new OverlaySurface(),{win,calls}=fixture();win.reentrant=()=>s.apply(win,true);s.apply(win,true);assert.equal(calls.length,2);});
 check('CSS frame uses shared geometry with matching fallback',()=>{const css=fs.readFileSync(new URL('../src/renderer/overlay.css',import.meta.url),'utf8');assert.ok(css.includes(`var(--overlay-frame-inset, ${OVERLAY_FRAME_INSET}px)`));assert.ok(css.includes(`var(--overlay-frame-radius, ${OVERLAY_FRAME_RADIUS}px)`));});
 console.log(`Overlay surface: ${count} checks passed`);
}finally{cleanupShared();}
