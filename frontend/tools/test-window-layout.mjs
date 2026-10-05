import assert from 'node:assert/strict';
import {importShared,cleanupShared} from './esbuild-ts.mjs';
const {initialMainWindowBounds:bounds}=await importShared('src/shared/window-layout.ts');
let count=0;
try {
 for(const [width,height] of [[1920,1040],[1366,728],[1280,680],[960,500],[853,440],[540,920],[360,600],[320,240]]){
  const area={x:-1920,y:72,width,height},b=bounds(area);
  assert.ok(b.x>=area.x&&b.y>=area.y&&b.x+b.width<=area.x+width&&b.y+b.height<=area.y+height);
  assert.ok(b.minWidth<=b.width&&b.minHeight<=b.height);
  assert.ok(b.width>0&&b.height>0);count++;console.log('PASS DIP work area '+width+'x'+height);
 }
 const standard=bounds({x:0,y:0,width:1920,height:1080});assert.equal(standard.width,1240);assert.equal(standard.height,820);count++;
 const fallback=bounds({x:NaN,y:Infinity,width:0,height:NaN});assert.ok(Object.values(fallback).every(Number.isFinite));count++;
 console.log('Window layout: '+count+' checks passed');
}finally{cleanupShared();}
