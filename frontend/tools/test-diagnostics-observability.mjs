import assert from 'node:assert/strict';
import { installMiniDom } from './mini-dom.mjs';
import { importShared } from './esbuild-ts.mjs';
const dom = installMiniDom();
const { window } = dom;
const mod = await importShared('tools/test-diagnostics-entry.ts', { bundle: true });
const { CMD, store, buildDiagnostics, DeveloperGesture, matchesLog, logBudgetMB, checkSummary } = mod;
const settle = async () => { for (let i=0;i<8;i++) await new Promise(resolve => setImmediate(resolve)); };
let count=0;
const test = async (name, fn) => { await fn(); count++; console.log('PASS ' + name); };
const commands=[]; const listeners=new Set();
let checkResults=[];
let pendingCheck=null;
let throwCommand=false;
window.voxsub = { backend: { start: async()=>({ok:true}), onEvent: fn=>{listeners.add(fn);return ()=>listeners.delete(fn)},
  command: async (command,args) => {
    commands.push({command,args});
    if (throwCommand) throw Error('secret must never enter metadata');
    let data={};
    if (command===CMD.runSelfCheck) { if(pendingCheck) return pendingCheck;data={results:checkResults}; }
    if (command===CMD.recentLogs) data={logs:[],text:'2026-10-05T01:00:00Z ERROR [run=current] timeout\n2026-10-04T01:00:00Z INFO [run=old] previous\n',run_id:'current'};
    if (command===CMD.getConfig) data={language:'zh',mode:'a',log_limit_mb:50};
    if (command===CMD.listModels) data={models:[]};
    if (command===CMD.releaseNotes) data={notes:[]};
    if (command===CMD.developerMode) data={enabled:args.enabled};
    if (command===CMD.diagnosticSnapshot) data={pipeline:{state:'not_loaded'},trace:{events:[]},verbose_session:null,environment:args?.environment?{os:'Windows',drivers:[]}:undefined};
    if (command===CMD.diagnosticSession) data={session:null};
    if (command===CMD.languageCapabilities) data={sources:['zh'],targets:{zh:['en']},defaultSource:'zh',defaultTarget:'en'};
    return {ok:true,data};
  } }, dialog: {saveReport:async()=>{commands.push({command:'saveDialog'});return 'report.txt'}} };
const off=mod.connectBackend();for(const fn of listeners)fn({type:'ready',version:'test',session:{running:false,mode:'a'}});await settle();
window.confirm=()=>true;
await test('ten clicks within five seconds, no nine-click activation',()=>{
  const gesture=new DeveloperGesture(); for(let i=0;i<9;i++)assert.equal(gesture.hit(i*100),false);assert.equal(gesture.hit(900),true);assert.equal(gesture.hit(1000),false);
  const slow=new DeveloperGesture(); for(let i=0;i<10;i++)assert.equal(slow.hit(i*1000),false);
});
await test('summary and capacity units reject false positives',()=>{
  assert.equal(checkSummary([]),'not_run');assert.equal(checkSummary([{status:'not_run'}]),'attention');assert.equal(checkSummary([{status:'ok'}]),'ok');
  assert.equal(logBudgetMB(0.5,'GB'),512);assert.equal(logBudgetMB(9,'MB'),null);assert.equal(logBudgetMB(11,'GB'),null);assert.equal(logBudgetMB(NaN,'MB'),null);assert.equal(logBudgetMB(50,'TB'),null);
  assert.equal(mod.pipelineSession('MODEL_TRACE '+JSON.stringify({pipeline_session_id:'abcdef123456'})),'abcdef123456');
});
await test('level, keyword and run filters compose',()=>{
  assert.ok(matchesLog({level:'error',message:'Request timeout',run_id:'current'},'ERROR','TIMEOUT','current'));
  assert.equal(matchesLog({level:'INFO',message:'timeout',run_id:'old'},'ERROR','timeout','current'),false);
});
await test('production diagnostics empty results never report healthy',async()=>{
  const page=buildDiagnostics();dom.mount(page.element);await settle();assert.match(page.element.textContent,/未检查/);assert.doesNotMatch(page.element.textContent,/全部 0 项正常/);page.dispose();page.element.remove();
});
await test('production diagnosis preserves impacts and suggestions',async()=>{
  checkResults=[{check:'Model',status:'resource_limited',detail:'memory',impact:'cannot load',suggestion:'retry later'}];
  const page=buildDiagnostics();dom.mount(page.element);await settle();assert.match(page.element.textContent,/资源不足/);assert.match(page.element.textContent,/cannot load/);assert.match(page.element.textContent,/retry later/);page.dispose();page.element.remove();checkResults=[];
});
await test('production live logs filter and pause without losing store records',async()=>{
  store.patch({logs:[]});store.applyEvent({type:'log',ts:'2026-10-05T00:00:00Z',level:'ERROR',message:'timeout',run_id:'current'});
  const page=buildDiagnostics();dom.mount(page.element);[...page.element.querySelectorAll('button')].find(b=>b.textContent==='实时日志').click();await settle();
  assert.match(page.element.querySelector('.log-view').textContent,/timeout/);
  [...page.element.querySelectorAll('button')].find(b=>b.textContent==='暂停滚动').click();
  store.applyEvent({type:'log',ts:'2026-10-05T00:00:01Z',level:'INFO',message:'new record',run_id:'current'});mod.refreshLogView();
  assert.doesNotMatch(page.element.querySelector('.log-view').textContent,/new record/);
  [...page.element.querySelectorAll('button')].find(b=>b.textContent==='恢复滚动').click();assert.match(page.element.querySelector('.log-view').textContent,/new record/);
  const query=page.element.querySelector('input');query.value='timeout';query.dispatchEvent(new Event('input'));assert.doesNotMatch(page.element.querySelector('.log-view').textContent,/new record/);
  page.dispose();page.element.remove();
});
await test('export cancel never opens save dialog or sends export',async()=>{
  const page=buildDiagnostics();dom.mount(page.element);await settle();const before=commands.length;window.confirm=()=>false;
  [...page.element.querySelectorAll('button')].find(b=>b.textContent==='导出报告').click();await settle();assert.equal(commands.slice(before).filter(c=>c.command==='saveDialog'||c.command===CMD.exportDiagnostics).length,0);page.dispose();page.element.remove();window.confirm=()=>true;
});
await test('closed check cannot update a replacement page',async()=>{
  let resolve;pendingCheck=new Promise(r=>resolve=r);const old=buildDiagnostics();await settle();old.dispose();pendingCheck=null;const current=buildDiagnostics();await settle();resolve({ok:true,data:{results:[{check:'stale',status:'ok',detail:'wrong'}]}});await settle();assert.doesNotMatch(current.element.textContent,/stale/);current.dispose();
});
await test('developer gate is hidden by default and closes on request',async()=>{
  mod.setDeveloperEnabled(false);const page=buildDiagnostics();assert.doesNotMatch(page.element.querySelector('.settings__nav').textContent,/开发者/);page.dispose();
  mod.setDeveloperEnabled(true);const dev=buildDiagnostics();dom.mount(dev.element);[...dev.element.querySelectorAll('button')].find(b=>b.textContent==='开发者').click();await settle();
  [...dev.element.querySelectorAll('button')].find(b=>b.textContent==='关闭开发者模式').click();await settle();assert.equal(mod.developerEnabled(),false);assert.ok(commands.some(c=>c.command===CMD.developerMode&&c.args.enabled===false));dev.dispose();dev.element.remove();
});
await test('developer timer is disposed with its page',async()=>{
  mod.setDeveloperEnabled(true);const before=dom.timers.created-dom.timers.cleared;const page=mod.buildDeveloperTab(()=>{});await settle();page.dispose();page.dispose();assert.equal(dom.timers.created-dom.timers.cleared,before);mod.setDeveloperEnabled(false);
});
await test('capacity is explicit-save and shrinking is confirmed',async()=>{
  const saves=[];const cap=mod.buildLogCapacity({log_limit_mb:100,log_limit_unit:'MB'},async updates=>saves.push(updates));
  const input=cap.querySelector('input');input.value='50';input.dispatchEvent(new Event('change'));assert.equal(saves.length,0);
  window.confirm=()=>false;cap.querySelector('button').click();await settle();assert.equal(saves.length,0);
  window.confirm=()=>true;cap.querySelector('button').click();await settle();assert.equal(saves[0].log_limit_mb,50);
});
await test('rejected IPC transport returns unknown delivery without leaking error body',async()=>{
  throwCommand=true;const result=await mod.callWithOutcome(CMD.ping);throwCommand=false;assert.equal(result.delivery,'unknown');assert.doesNotMatch(JSON.stringify(mod.ipcSnapshot()),/secret must/);
});
off();dom.restore();console.log('PASS diagnostics observability '+count+'/'+count);
