#!/usr/bin/env node
/** Actual primitives, settings handlers, native IPC and startup races; no real windows/audio. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { transformSync } from 'esbuild';
import { installMiniDom } from './mini-dom.mjs';
import { importShared, cleanupShared } from './esbuild-ts.mjs';
const dom = installMiniDom();
const [{ h, on }, { buildPercentageSlider }, { buildToggleSwitch }, { glassTint }] = await importShared([
  'src/renderer/dom.ts', 'src/renderer/ui/percentage-slider.ts', 'src/renderer/ui/controls.ts', 'src/shared/overlay-glass.ts',
], { bundle: true });
const read = p => fs.readFileSync(new URL('../src/'+p, import.meta.url), 'utf8');
const run = (src, context) => vm.runInNewContext(transformSync(src, { loader:'ts', format:'cjs' }).code, context);
const tick = () => new Promise(resolve => setImmediate(resolve));
let count=0;
function check(name, test) { test(); count++; console.log('PASS '+name); }
try {
  check('shared percentage slider: boundaries, accessible readout, preview vs commit and disabled', () => {
    const preview=[],commits=[];
    const c=buildPercentageSlider(50,'Glass',{ onInput:v=>preview.push(v),onChange:v=>commits.push(v) });
    assert.equal(c.input.getAttribute('aria-label'),'Glass');
    c.input.value='73';c.input.dispatchEvent(dom.makeEvent('input'));
    assert.deepEqual(preview,[73]); assert.deepEqual(commits,[]); assert.match(c.element.textContent,/73%/);
    c.input.dispatchEvent(dom.makeEvent('change')); assert.deepEqual(commits,[73]);
    c.setDisabled(true); c.input.dispatchEvent(dom.makeEvent('input')); assert.deepEqual(preview,[73]);
    c.setDisabled(false);c.input.value='120';c.input.dispatchEvent(dom.makeEvent('change'));assert.equal(commits.at(-1),100);
    assert.equal(c.input.getAttribute('aria-valuetext'),'100%');
    assert.equal(buildPercentageSlider(NaN,'Opacity',{min:20}).input.value,'20');
  });
  check('tint only exposes native material; no text/window opacity or fake CSS blur', () => {
    assert.equal(glassTint(.92,{active:false,strength:100}),.92);
    assert.equal(glassTint(.92,{active:true,strength:0}),.92);
    assert.ok(Math.abs(glassTint(.92,{active:true,strength:100})-.276)<1e-8);
    assert.ok(!read('renderer/overlay.css').includes('filter: blur(') || !read('renderer/overlay.css').match(/\.shell\s*\{[^}]*filter:/));
  });
  const main=read('main/main.ts');
  const native=main.slice(main.indexOf('let overlayGlassEnabled'),main.indexOf('let overlayFontSize'));
  const ipc=main.slice(main.indexOf('  ipcMain.handle("overlay:get-glass"'),main.indexOf('  ipcMain.handle("overlay:set-opacity"'));
  const makeNative=(platform='win32',version='10.0.26300',fail=false,alive=true) => {
    const handlers=new Map(),materials=[],states=[];
    run(native+ipc,{ process:{ platform,getSystemVersion:()=>version },
      overlayWindow:{},useWindow:(_win,_op,fn)=>{if(!alive)return false;fn({setBackgroundMaterial:m=>{materials.push(m);if(fail&&m==='acrylic')throw Error('DWM unavailable');}});return true;},
      sendToWindow:(_w,_c,s)=>states.push(s),console:{warn(){}},ipcMain:{handle:(n,f)=>handlers.set(n,f)} });
    return { materials,states,get:()=>handlers.get('overlay:get-glass')(),set:(e,s)=>handlers.get('overlay:set-glass')({},e,s) };
  };
  check('native toggle and strength validate input, avoid repeat DWM calls, retain strength on close',()=>{
    const n=makeNative();assert.equal(n.get().active,false);assert.deepEqual(n.materials,[]);
    assert.equal(n.set(true,73).active,true);assert.deepEqual(n.materials,['acrylic']);
    n.set(true,84);assert.deepEqual(n.materials,['acrylic']);assert.equal(n.get().strength,84);
    assert.equal(n.set(false,84).active,false);assert.deepEqual(n.materials,['acrylic','none']);
    assert.equal(n.set(true,0).active,false);assert.equal(n.set(true,120).strength,100);
    for(const v of [NaN,Infinity,'50',null,true])assert.equal(n.set(true,v).strength,50);
    assert.equal(n.set('true',50).enabled,false);
  });
  check('unsupported OS and destroyed/native failure safely retain opaque background',()=>{
    for(const [p,v] of [['linux',''],['win32','10.0.19045'],['win32','10.0.22000']]){
      const n=makeNative(p,v);const state=n.set(true,70);assert.equal(state.active,false);assert.equal(state.reason,'unsupported');assert.deepEqual(n.materials,[]);
    }
    const failed=makeNative('win32','10.0.26300',true);assert.equal(failed.set(true,70).reason,'unavailable');assert.deepEqual(failed.materials,['acrylic','none']);
    assert.equal(makeNative('win32','10.0.26300',false,false).set(true,70).active,false);
  });
  const overlaySource=read('renderer/overlay.ts');
  const receiver=overlaySource.slice(overlaySource.indexOf('  window.voxsub?.overlay.onGlassChanged'),overlaySource.indexOf('  window.voxsub?.overlay.onOpacityChanged'));
  const visual=overlaySource.slice(overlaySource.indexOf('function applyVisuals()'),overlaySource.indexOf('const MODE_LABEL'));
  const styles=new Map();let receive;
  run('let glassRevision=0, glassState={active:false,strength:50},opacity=.92,fontSize=20,contentPadding=18,lineGap=6;'+visual+receiver,
    {glassTint,document:{documentElement:{style:{setProperty:(k,v)=>styles.set(k,v)}}},window:{voxsub:{overlay:{onGlassChanged:fn=>receive=fn}}},
      fontValueEl:null,paddingValueEl:null,gapValueEl:null,applyTextColors(){}});
  check('real glass receiver updates only background tint, off restores saved opacity',()=>{
    receive({active:true,strength:100});assert.ok(Math.abs(Number(styles.get('--overlay-glass-opacity'))-.276)<1e-8);
    assert.equal(styles.get('--overlay-opacity'),'0.92');
    receive({active:false,strength:100});assert.equal(styles.get('--overlay-glass-opacity'),'0.92');
    assert.equal(styles.get('--font-size'),'20px');
  });
  const i18n=read('renderer/i18n.ts').replace(/^import .*;$/m,'');
  const translate=run(i18n+'\ntr;',{module:{exports:{}},store:{get:()=>({theme:'dark',lang:'en'})}});
  check('glass label and fallbacks are translated in English',()=>{
    for(const label of ['悬浮窗毛玻璃','启用悬浮窗毛玻璃','毛玻璃百分比','正在检查毛玻璃支持情况…','毛玻璃暂时不可用，已保留原有背景。'])assert.notEqual(translate(label),label);
  });
  const settings=read('renderer/views/settings.ts');
  const appearance=settings.slice(settings.indexOf('function appearanceTab()'),settings.indexOf('function aboutTab()'));
  const saved=[],requests=[];
  let resolveSupport;
  const page=run(appearance+'\nappearanceTab();',{ h,on,buildPercentageSlider,toggleSwitch:buildToggleSwitch,
    config:{overlay_glass_strength:67},tr:x=>x,radioGroup:()=>h('div'),field:(_l,c)=>c,card:(_l,c)=>h('section',{},c),
    currentLanguage:()=> 'zh',applyThemeChoice(){},setLanguage(){},
    saveConfig:async s=>saved.push(JSON.parse(JSON.stringify(s))),window:{voxsub:{overlay:{setOpacity:async()=>{},getGlass:()=>new Promise(r=>resolveSupport=r),
      setGlass:async(enabled,strength)=>{requests.push({enabled,strength});return {enabled,strength,active:enabled&&strength>0,supported:true,reason:null};}}}} });
  const checkbox=page.querySelector('input[type="checkbox"]');const slider=page.querySelectorAll('input[type="range"]')[1];
  check('settings defaults disabled and remembers strength',()=>{assert.equal(checkbox.checked,false);assert.equal(slider.disabled,true);assert.equal(slider.value,'67');});
  checkbox.checked=true;checkbox.dispatchEvent(dom.makeEvent('change'));await tick();
  slider.value='81';slider.dispatchEvent(dom.makeEvent('input'));await tick();
  check('settings enabled preview is immediate without saving dragged values',()=>{assert.equal(slider.disabled,false);assert.deepEqual(requests.at(-1),{enabled:true,strength:81});assert.deepEqual(saved,[{overlay_glass_enabled:true}]);});
  slider.dispatchEvent(dom.makeEvent('change'));await tick();
  checkbox.checked=false;checkbox.dispatchEvent(dom.makeEvent('change'));await tick();
  check('release saves percentage; toggle off retains the percentage',()=>{assert.deepEqual(saved[1],{overlay_glass_strength:81});assert.deepEqual(requests.at(-1),{enabled:false,strength:81});assert.equal(slider.disabled,true);});
  resolveSupport({supported:false,reason:'unsupported'});await tick();
  check('stale support response cannot replace live state hint',()=>{assert.ok(!page.textContent.includes('当前系统不支持'));});
  const overlay=read('renderer/overlay.ts');
  const restore=overlay.slice(overlay.indexOf('async function restoreGlass()'),overlay.indexOf('/** 把显示模式写回配置'));
  for(const when of ['untouched','before','during']){
    let resolve;const calls=[];
    const ctx={window:{voxsub:{backend:{command:()=>new Promise(r=>resolve=r)},overlay:{setGlass:async(e,s)=>calls.push([e,s])}}}};
    const pending=run(`let glassRevision=${when==='before'?1:0};\n`+restore+`\nconst pending=restoreGlass();${when==='during'?'glassRevision++;':''}pending;`,ctx);
    if(resolve)resolve({ok:true,data:{overlay_glass_enabled:true,overlay_glass_strength:63}});await pending;
    assert.deepEqual(calls,when==='untouched'?[[true,63]]:[]);count++;console.log('PASS restore '+when);
  }
  check('boot and backend-ready both restore; opacity independent revision',()=>{
    assert.match(overlay.slice(overlay.indexOf('function boot()')),/void restoreGlass\(\)/);
    assert.match(overlay.slice(overlay.indexOf('function wireBackend()')),/raw.type === "ready".*restoreGlass/);
    assert.ok(!restore.includes('opacityRevision'));
  });
} finally { dom.restore();cleanupShared(); }
console.log(`Overlay glass: ${count} checks passed`);
