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
let pendingRecent=null;
let throwCommand=false;
window.voxsub = { backend: { start: async()=>({ok:true}), onEvent: fn=>{listeners.add(fn);return ()=>listeners.delete(fn)},
  command: async (command,args) => {
    commands.push({command,args});
    if (throwCommand) throw Error('secret must never enter metadata');
    let data={};
    if (command===CMD.runSelfCheck) { if(pendingCheck) return pendingCheck;data={results:checkResults}; }
    if (command===CMD.recentLogs && args?.limit===1 && pendingRecent) return pendingRecent;
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

await test('multi-level OR composes with keyword/run AND; empty never means all',()=>{
  const item={level:'warning',message:'Request timeout',run_id:'current'};
  assert.ok(matchesLog(item,['WARNING','ERROR'],'TIMEOUT','current'));assert.equal(matchesLog(item,['ERROR','CRITICAL'],'timeout','current'),false);
  assert.equal(matchesLog(item,[],'',''),false);assert.equal(matchesLog(item,['WARNING'],'other','current'),false);assert.equal(matchesLog(item,['WARNING'],'timeout','old'),false);
  assert.ok(matchesLog({level:'custom',message:'unknown'},'all','',''));
  assert.ok(matchesLog({message:'missing level'},'all','',''));assert.ok(matchesLog({message:'missing level'},['INFO'],'',''));
});
const button=(page,text)=>[...page.element.querySelectorAll('button')].find(b=>b.textContent===text);
const logPage=async()=>{const page=buildDiagnostics();dom.mount(page.element);button(page,'实时日志').click();await settle();button(page,'全部级别').click();const input=page.element.querySelector('input');input.value='';input.dispatchEvent(new Event('input'));return page;};
const close=page=>{page.dispose();page.element.remove();};
const chooseWarningError=page=>{for(const level of ['DEBUG','INFO','CRITICAL'])button(page,level).click();};
const fixtureLogs=()=>{store.patch({logs:[]});for(const [i,level]of ['DEBUG','INFO','WARNING','ERROR','CRITICAL','CUSTOM'].entries())store.applyEvent({type:'log',ts:'2026-10-05T00:00:0'+i+'Z',level,message:level+' fixture timeout',run_id:level==='ERROR'?'old':'current'});};
await test('production default all includes unknown; WARNING/ERROR are both selected',async()=>{
  fixtureLogs();const page=await logPage();assert.equal(page.element.querySelectorAll('.log-row').length,6);chooseWarningError(page);const view=page.element.querySelector('.log-view');
  assert.equal(view.querySelectorAll('.log-row').length,2);assert.match(view.textContent,/WARNING fixture/);assert.match(view.textContent,/ERROR fixture/);
  for(const text of ['DEBUG fixture','INFO fixture','CRITICAL fixture','CUSTOM fixture'])assert.ok(!view.textContent.includes(text));
  assert.equal(button(page,'WARNING').getAttribute('aria-pressed'),'true');assert.equal(button(page,'ERROR').getAttribute('aria-pressed'),'true');assert.equal(button(page,'全部级别').getAttribute('aria-pressed'),'false');close(page);
});
await test('production empty selection shows no matches; all restores without IPC or lost records',async()=>{
  fixtureLogs();const page=await logPage();const before=commands.length;for(const level of ['DEBUG','INFO','WARNING','ERROR','CRITICAL'])button(page,level).click();
  assert.equal(page.element.querySelectorAll('.log-row').length,0);assert.match(page.element.querySelector('.log-view').textContent,/暂无匹配日志/);button(page,'全部级别').click();assert.equal(page.element.querySelectorAll('.log-row').length,6);assert.equal(commands.length,before);assert.equal(store.get().logs.length,6);close(page);
});
await test('production multi-level AND keyword AND current run filters compose',async()=>{
  fixtureLogs();const page=await logPage();chooseWarningError(page);const query=page.element.querySelector('input');query.value='TIMEOUT';query.dispatchEvent(new Event('input'));
  button(page,'仅本次运行').click();assert.equal(page.element.querySelectorAll('.log-row').length,1);assert.match(page.element.querySelector('.log-view').textContent,/WARNING fixture/);
  query.value='not found';query.dispatchEvent(new Event('input'));assert.equal(page.element.querySelectorAll('.log-row').length,0);query.value='';query.dispatchEvent(new Event('input'));button(page,'仅本次运行').click();button(page,'全部级别').click();close(page);
});
await test('paused live view supports deliberate filtering without automatic updates or scrolling',async()=>{
  fixtureLogs();const page=await logPage();button(page,'暂停滚动').click();store.applyEvent({type:'log',ts:'2026-10-05T00:00:08Z',level:'ERROR',message:'late record',run_id:'current'});mod.refreshLogView();assert.doesNotMatch(page.element.querySelector('.log-view').textContent,/late record/);
  chooseWarningError(page);assert.equal(page.element.querySelectorAll('.log-row').length,3);const view=page.element.querySelector('.log-view');view.scrollTop=7;button(page,'ERROR').click();assert.equal(view.querySelectorAll('.log-row').length,1);assert.equal(view.scrollTop,7);button(page,'恢复滚动').click();button(page,'全部级别').click();close(page);
});
await test('historical logs and export honor multiple level selection',async()=>{
  fixtureLogs();const page=await logPage();chooseWarningError(page);button(page,'历史文件').click();await settle();const view=page.element.querySelector('.log-view');assert.match(view.textContent,/timeout/);assert.doesNotMatch(view.textContent,/previous/);
  button(page,'导出日志').click();await settle();const exported=commands.filter(c=>c.command===CMD.exportDiagnostics).at(-1).args.log_text;assert.match(exported,/timeout/);assert.doesNotMatch(exported,/previous/);
  button(page,'WARNING').click();button(page,'ERROR').click();assert.equal(view.querySelectorAll('.log-row').length,0);button(page,'全部级别').click();assert.match(view.textContent,/previous/);close(page);
});
await test('selection survives tab rebuild; detached controls cannot alter a new page',async()=>{
  fixtureLogs();const old=await logPage();chooseWarningError(old);const stale=button(old,'ERROR');button(old,'自检结果').click();await settle();button(old,'实时日志').click();await settle();assert.equal(button(old,'ERROR').getAttribute('aria-pressed'),'true');assert.equal(button(old,'INFO').getAttribute('aria-pressed'),'false');
  close(old);const current=buildDiagnostics();dom.mount(current.element);button(current,'实时日志').click();await settle();stale.click();assert.equal(button(current,'ERROR').getAttribute('aria-pressed'),'true');assert.equal(current.element.querySelectorAll('.log-row').length,2);button(current,'全部级别').click();close(current);
});


await test('English multi-select label and group state remain accessible',async()=>{
  store.get().lang='en';const page=buildDiagnostics();dom.mount(page.element);button(page,'Live logs').click();await settle();
  const group=page.element.querySelector('.filter-bar');assert.equal(group.getAttribute('aria-label'),'Log levels (select multiple)');assert.match(group.textContent,/Log levels \(select multiple\)/);
  button(page,'INFO').click();assert.equal(button(page,'INFO').getAttribute('aria-pressed'),'false');button(page,'All levels').click();assert.equal(button(page,'INFO').getAttribute('aria-pressed'),'true');close(page);store.get().lang='zh';
});


await test('model-session selection intersects multiple log levels, never unions them',async()=>{
  store.patch({logs:[]});
  for(const [i,[level,id]]of [['ERROR','abcdef111111'],['WARNING','abcdef222222'],['ERROR','abcdef222222'],['INFO','abcdef222222']].entries())
    store.applyEvent({type:'log',ts:'2026-10-05T00:00:0'+i+'Z',level,message:'MODEL_TRACE '+JSON.stringify({pipeline_session_id:id}),run_id:'current'});
  const page=await logPage();chooseWarningError(page);button(page,'仅本次模型会话').click();
  const view=page.element.querySelector('.log-view');assert.equal(view.querySelectorAll('.log-row').length,2);assert.doesNotMatch(view.textContent,/abcdef111111/);assert.match(view.textContent,/abcdef222222/);
  button(page,'仅本次模型会话').click();button(page,'全部级别').click();close(page);
});
await test('late run metadata respects paused live view',async()=>{
  fixtureLogs();let resolve;pendingRecent=new Promise(r=>resolve=r);const page=await logPage();button(page,'暂停滚动').click();
  store.applyEvent({type:'log',ts:'2026-10-05T00:00:08Z',level:'ERROR',message:'late metadata must not unpause',run_id:'current'});mod.refreshLogView();
  resolve({ok:true,data:{run_id:'current'}});await settle();pendingRecent=null;assert.doesNotMatch(page.element.querySelector('.log-view').textContent,/late metadata must not unpause/);
  button(page,'恢复滚动').click();assert.match(page.element.querySelector('.log-view').textContent,/late metadata must not unpause/);close(page);
});
await test('stale run metadata cannot change a new page run filter',async()=>{
  fixtureLogs();let resolve;pendingRecent=new Promise(r=>resolve=r);const old=await logPage();close(old);pendingRecent=null;
  const current=await logPage();chooseWarningError(current);resolve({ok:true,data:{run_id:'old'}});await settle();button(current,'仅本次运行').click();
  assert.equal(current.element.querySelectorAll('.log-row').length,1);assert.match(current.element.querySelector('.log-view').textContent,/WARNING fixture/);
  button(current,'仅本次运行').click();button(current,'全部级别').click();close(current);
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
await test('native overlay IPC failure retains diagnostic snapshot as unverified',async()=>{
  const previous=window.voxsub.overlay;window.voxsub.overlay={getGlass:async()=>{throw Error('unavailable');}};
  mod.setDeveloperEnabled(true);const page=mod.buildDeveloperTab(()=>{});dom.mount(page.element);await settle();
  const snapshot=JSON.parse(page.element.querySelector('pre').textContent);
  assert.equal(snapshot.overlaySurface.active,false);assert.equal(snapshot.overlaySurface.desktopCheck,'not_run');
  assert.equal(snapshot.overlaySurface.clippingCheck,'not_run');
  page.dispose();page.element.remove();window.voxsub.overlay=previous;mod.setDeveloperEnabled(false);
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
