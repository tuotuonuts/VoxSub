#!/usr/bin/env node
/** Real headless Chromium DOM regression. Requires local Chrome/Edge, Node >=22.
 * No downloads/prebuild/dist writes; esbuild bundles production modules in memory.
 * VOXSUB_SCROLL_EVIDENCE and isolated APPDATA/LOCALAPPDATA/TEMP/TMP/TMPDIR required.
 * VOXSUB_BROWSER can select an explicit Chromium executable. --layout-only = layout tracer.
 */
import assert from 'node:assert/strict';
import { readFileSync, mkdirSync, mkdtempSync, writeFileSync, existsSync } from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { build } from 'esbuild';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const evidence = process.env.VOXSUB_SCROLL_EVIDENCE;
assert(evidence, 'Set VOXSUB_SCROLL_EVIDENCE to an isolated evidence directory');
for (const key of ['APPDATA','LOCALAPPDATA','TEMP','TMP','TMPDIR']) {
  assert(process.env[key] && resolve(process.env[key]).startsWith(resolve(evidence)), `${key} must be isolated beneath evidence`);
  mkdirSync(process.env[key], { recursive: true });
}
mkdirSync(evidence, { recursive: true });
const run = mkdtempSync(join(evidence, 'browser-'));
const browserPath = process.env.VOXSUB_BROWSER || [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
].find(existsSync);
assert(browserPath, 'No local Chromium; set VOXSUB_BROWSER (no automatic install)');
const bundle = await build({ entryPoints: [join(root,'tools/workspace-scroll-entry.ts')], bundle:true, write:false, format:'iife', target:'chrome120' });
const css = readFileSync(join(root,'src/renderer/app.css'));
const server = createServer((req,res) => {
  res.setHeader('Cache-Control','no-store');
  if(req.url === '/app.js') { res.setHeader('Content-Type','text/javascript'); res.end(bundle.outputFiles[0].contents); }
  else if(req.url === '/app.css') { res.setHeader('Content-Type','text/css'); res.end(css); }
  else { res.setHeader('Content-Type','text/html; charset=utf-8'); res.end('<!doctype html><html><head><link rel="stylesheet" href="/app.css"></head><body><div id="app"></div><script src="/app.js"></script></body></html>'); }
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const port = server.address().port;
let browser, socket;
const report = { browser:browserPath, environment:Object.fromEntries(['APPDATA','LOCALAPPDATA','TEMP','TMP','TMPDIR'].map(k=>[k,process.env[k]])), cases:[], errors:[] };
try {
  browser = spawn(browserPath, ['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--disable-sync','--disable-extensions','--mute-audio','--remote-debugging-port=0',`--user-data-dir=${join(run,'profile')}`,'about:blank'], {windowsHide:true,stdio:['ignore','ignore','pipe']});
  let err = ''; browser.stderr.on('data', b=>{err+=b;});
  const active = join(run,'profile','DevToolsActivePort');
  const deadline = Date.now()+20000;
  while(!existsSync(active)) { assert(Date.now()<deadline, `Chromium startup timeout ${err}`); await new Promise(r=>setTimeout(r,50)); }
  const debugPort = readFileSync(active,'utf8').split('\n')[0];
  const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  socket = new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
  await new Promise((r,j)=>{socket.addEventListener('open',r,{once:true});socket.addEventListener('error',j,{once:true});});
  let id=0; const pending=new Map();
  socket.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown') report.errors.push(m.params.exceptionDetails);});
  function cdp(method,params={}) {return new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});socket.send(JSON.stringify({id:n,method,params}));});}
  async function js(expression) {const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!r.exceptionDetails,JSON.stringify(r.exceptionDetails));return r.result.value;}
  const settle = () => js('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
  await cdp('Runtime.enable');
  report.version=await cdp('Browser.getVersion');
  const metrics = () => js(`(()=>{const e=document.querySelector('.subtitle-stream'),a=document.querySelector('.workspace__actions');const rect=n=>{const r=n.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,width:r.width,height:r.height}};return {height:e.clientHeight,scrollHeight:e.scrollHeight,top:e.scrollTop,width:e.clientWidth,scrollWidth:e.scrollWidth,rect:rect(e),controls:rect(a),overflow:getComputedStyle(e).overflowY,wrap:getComputedStyle(e.querySelector('.sub-row__src')).overflowWrap,scrollbar:getComputedStyle(e,'::-webkit-scrollbar').width,ancestors:['.workspace','.workspace-slot','.shell__body','.shell'].map(s=>{const n=document.querySelector(s);return {selector:s,height:n.clientHeight,scrollHeight:n.scrollHeight,overflow:getComputedStyle(n).overflowY}}),viewport:{width:innerWidth,height:innerHeight},pageWidth:document.documentElement.scrollWidth}})()`);
  async function fill(n=100) {await js(`scrollTest.store.patch({draft:null,subtitles:Array.from({length:${n}},(_,i)=>({source:'第 '+i+' 句：中英文字幕长句。 '+ 'Continuous recognition and translation. '.repeat(4),translation:'Translation '+i+' https://example.invalid/'+ 'abcdef'.repeat(45),tsMs:i*1000}))})`); await settle();}
  async function append() {await js(`scrollTest.store.commitSubtitle('New '+scrollTest.store.get().subtitles.length,'新字幕 '.repeat(25))`);await settle();}
  const matrix = [
    [1240,820,1,'zh'],[1000,680,1.25,'en'],[820,600,1.5,'zh'],[1600,1000,2,'en'],[640,480,1,'en'],
  ];
  for(const [width,height,scale,lang] of matrix) {
    await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:scale,mobile:false});
    await cdp('Page.navigate',{url:`http://127.0.0.1:${port}/?lang=${lang}`});
    for(let i=0;i<100 && !await js(`!!document.querySelector('.subtitle-stream')`);i++) await new Promise(r=>setTimeout(r,20));
    await fill();
    const m=await metrics(); report.cases.push({scenario:'layout',width,height,scale,lang,...m});
    assert(m.height>40 && m.scrollHeight>m.height+100,`subtitle region must scroll: ${JSON.stringify(m)}`);
    assert(m.rect.bottom<=height+1 && m.controls.bottom<=height+1 && m.controls.top>=0,'stream and controls remain within viewport');
    assert(m.scrollWidth<=m.width+1 && m.pageWidth<=width+1,'long URL must wrap, not overflow horizontally');
    assert.equal(m.overflow,'auto'); assert.equal(m.wrap,'anywhere'); assert.notEqual(m.scrollbar,'0px');
    assert(m.ancestors.every(a=>a.scrollHeight<=a.height+1), 'no vertical overflow on stream ancestors');
    if(process.argv.includes('--layout-only')) continue;
    const atBottom=async()=>{const x=await metrics();assert(x.scrollHeight-x.height-x.top<=2,`must follow latest ${JSON.stringify(x)}`);};
    await atBottom();
    // Pre-update near-bottom measurement: a tall new row must still be followed.
    await js(`document.querySelector('.subtitle-stream').scrollTop-=20`); await append(); await atBottom();
    for(let i=0;i<8;i++) {await append();await atBottom();}
    await js(`document.querySelector('.subtitle-stream').scrollTop=180`);await settle();
    const history=(await metrics()).top;
    for(let i=0;i<4;i++) {await append();assert.equal((await metrics()).top,history,'history reader must not be yanked on commit');}
    await js(`scrollTest.store.patch({draft:{source:'Draft '.repeat(50),translation:'草稿 '.repeat(50)}})`);await settle();
    assert.equal((await metrics()).top,history,'history reader must not be yanked on draft');
    await js(`scrollTest.store.patch({draft:{source:'Updated draft '.repeat(90),translation:'更新草稿 '.repeat(90)}})`);await settle();
    assert.equal((await metrics()).top,history,'draft revision must preserve reading position');
    await js(`document.querySelector('.subtitle-stream').scrollTop=1e9`);await append();await atBottom();
    await js(`scrollTest.store.patch({draft:{source:'bottom draft '.repeat(100),translation:'底部草稿 '.repeat(100)}})`);await settle();await atBottom();
    await js(`scrollTest.store.patch({draft:{source:'bottom revision '.repeat(200),translation:'底部更新 '.repeat(200)}})`);await settle();await atBottom();
    // Real wheel input (trackpad also uses Chromium wheel path).
    const beforeWheel=await metrics();
    await cdp('Input.dispatchMouseEvent',{type:'mouseWheel',x:beforeWheel.rect.left+30,y:beforeWheel.rect.top+30,deltaX:0,deltaY:-250});
    for(let i=0;i<30 && (await metrics()).top===beforeWheel.top;i++) await settle();
    assert((await metrics()).top<beforeWheel.top,'native wheel scrolls history');
    // Real clear button, then new content resumes following.
    await js(`document.querySelectorAll('.workspace__actions button')[3].click()`);await settle();
    assert.equal(await js(`document.querySelectorAll('.sub-row').length`),0,'clear removes all content');
    assert.equal(await js(`document.querySelector('.subtitle-stream').scrollTop`),0);
    await fill(80);await atBottom();
    // A/B/C and D round-trip use real mode controls + real workspace disposal.
    for(const mode of ['b','c','d','a']) {
      await js(`document.querySelector('[data-mode="${mode}"]').click()`);await settle();
      if(mode==='d') {assert.equal(await js(`!!document.querySelector('.subtitle-stream')`),false);continue;}
      await append();await atBottom();
      const mm=await metrics();assert(mm.rect.bottom<=height+1 && mm.controls.bottom<=height+1,'mode controls remain visible');
      report.cases.push({scenario:'mode',mode,width,height,scale,lang,...mm});
    }
    report.cases.push({scenario:'follow/history/draft/wheel/clear/modes',width,height,scale,lang,passed:true});
  }
  assert.equal(report.errors.length,0,'no browser runtime exceptions');
  report.passed=true;
  console.log(`PASS workspace scroll: ${matrix.length} viewport/DPR/language configurations; ${report.cases.length} evidence records`);
} catch(error) {report.passed=false;report.failure=String(error.stack);console.error(error);process.exitCode=1;}
finally {
  writeFileSync(join(run,'evidence.json'),JSON.stringify(report,null,2)); console.log(`Evidence: ${join(run,'evidence.json')}`);
  socket?.close();
  if(browser) {await new Promise(r=>{if(browser.exitCode!==null)return r();browser.once('exit',r);browser.kill();setTimeout(r,5000).unref();});}
  server.close();
}
