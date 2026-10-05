import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import ts from 'typescript';
import { ROOT } from './esbuild-ts.mjs';
let checks=0, failures=0;
function makeRuntime({headless=false}={}) {
  const handlers=new Map(), appEvents=new Map(), warnings=[], windows=[];
  let ready;
  class Window {
    constructor(options){ this.options=options; this.dead=false; this.events=new Map(); this.messages=[]; this.calls=[]; this.contentsDead=false; this.race=false; this.visible=false; this.minimized=false;
      this.webContents={once:()=>{},setZoomMode:mode=>this.calls.push(["setZoomMode",mode]),setZoomFactor:factor=>this.calls.push(["setZoomFactor",factor]),getZoomFactor:()=>1,on:()=>{},isDestroyed:()=>this.contentsDead, send:(...args)=>{this.assertLive(); if(this.contentsDead||this.race){this.contentsDead=true;throw new TypeError('Object has been destroyed');} if(this.sendError) throw this.sendError; this.messages.push(args);}};windows.push(this); }
    getBounds(){return {x:0,y:0,width:860,height:140};}
    getContentSize(){ return this.size ?? [860,140]; }
    setBounds(bounds){this.call('setBounds',bounds);}
    setShape(rects){ this.call("setShape",rects); }
    setBackgroundMaterial(m){ this.call("setBackgroundMaterial",m); }
    setBackgroundColor(c){ this.call("setBackgroundColor",c); }
    assertLive(){if(this.dead)throw new TypeError('Object has been destroyed');}
    call(name,...args){if(this.raceMethod===name)this.dead=true;this.assertLive();if(this.errorMethod===name)throw this.methodError;this.calls.push([name,...args]);}
    isDestroyed(){return this.dead;} // Electron's safe liveness probe.
    on(n,f){this.call('on',n);this.events.set(n,f);return this;}
    once(n,f){return this.on(n,f);}
    loadFile(){this.call('loadFile');return Promise.resolve();}
    show(){this.call('show');this.visible=true;}
    showInactive(){this.call('showInactive');this.visible=true;}
    hide(){this.call('hide');this.visible=false;}
    isVisible(){this.call('isVisible');return this.visible;}
    isMinimized(){this.call('isMinimized');return this.minimized;}
    restore(){this.call('restore');this.minimized=false;}
    focus(){this.call('focus');}
    setSize(...a){this.call('setSize',...a);}
    setContentProtection(...a){this.call('setContentProtection',...a);}
    setAlwaysOnTop(...a){this.call('setAlwaysOnTop',...a);}
    setVisibleOnAllWorkspaces(...a){this.call('setVisibleOnAllWorkspaces',...a);}
    setIgnoreMouseEvents(...a){this.call('setIgnoreMouseEvents',...a);}
    destroy(){this.call('destroy');this.dead=true;this.contentsDead=true;this.events.get('closed')?.();}
    close(){this.call('close');this.destroy();}
    static getAllWindows(){return windows.filter(w=>!w.dead);}
  }
  class Tray {
    constructor(){this.events=new Map();}
    setToolTip(){}
    setContextMenu(menu){this.menu=menu;}
    on(n,f){this.events.set(n,f);}
    destroy(){}
  }
  const electron={BrowserWindow:Window,Tray,Menu:{buildFromTemplate:x=>x,setApplicationMenu(){}},
    app:{on:(n,f)=>appEvents.set(n,f),whenReady:()=>({then:f=>{ready=f;}}),requestSingleInstanceLock:()=>true,quit:()=>{},setAppUserModelId(){},getPath:()=>"fixture:/userData"},
    screen:{getDisplayMatching:()=>({scaleFactor:1}),getPrimaryDisplay:()=>({workAreaSize:{width:1280,height:900}}),getCursorScreenPoint:()=>({x:0,y:0}),getDisplayNearestPoint:()=>({workArea:{x:0,y:0,width:1280,height:900}})},nativeImage:{createFromPath:()=>({isEmpty:()=>false})},
    ipcMain:{handle:(n,f)=>handlers.set(n,f),on:()=>{}}};
  const cache=new Map();let context;
  const load=(file)=>{
    const full=path.resolve(ROOT,file);if(cache.has(full))return cache.get(full).exports;
    const mod={exports:{}};cache.set(full,mod);
    const source=readFileSync(full,'utf8');
    const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
    const req=(name)=>{
      if(name==='electron')return electron;
      if(name==='./capture')return {cancelScreenSelection(){}};
      if(name.startsWith('.'))return load(path.relative(ROOT,path.resolve(path.dirname(full),name+'.ts')));
      if(name==='node:path')return path;
      if(name==='node:fs')return {existsSync:()=>false};
      if(name==='node:child_process')return {execFile:(_c,_a,_o,cb)=>cb(Error('fixture unavailable')),spawn:()=>{throw Error('Real spawn forbidden');}};
      if(name==='node:readline')return {};
      throw Error(name);
    };
    vm.runInContext('(function(require,module,exports,__dirname){'+code+'\n})',context)(req,mod,mod.exports,path.dirname(full));return mod.exports;
  };
  context=vm.createContext({console:{...console,log(){},warn:(...a)=>warnings.push(a)},process:{env:{VOXSUB_HEADLESS:headless?'1':'0'},platform:'win32',getSystemVersion:()=> '10.0.26300'},setTimeout,clearTimeout,setInterval,clearInterval});
  const mainFile=path.join(ROOT,'src/main/main.ts');
  const suffix=`\nglobalThis.api={createMain:()=>mainWindow=createMainWindow(),createOverlay:()=>overlayWindow=createOverlayWindow(),replaceOverlay:()=>replaceUnsafeOverlay(overlayWindow),createTray,getMain:()=>mainWindow,getOverlay:()=>overlayWindow,quit:requestQuit,bridge:()=>{const b=new BackendBridge();wireBackendEvents(b);return b;}};registerIpc();`;
  const code=ts.transpileModule(readFileSync(mainFile,'utf8')+suffix,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  context.exports={};context.__dirname=path.dirname(mainFile);context.require=(name)=>{
    if(name==='electron')return electron;if(name==='./capture')return {cancelScreenSelection(){}};if(name==='node:path')return path;if(name==='node:fs')return {};
    if(name==='../shared/window-layout')return load('src/shared/window-layout.ts');if(name==='./backend')return load('src/main/backend.ts');if(name==='./shortcuts')return load('src/main/shortcuts.ts');if(name==='./ocr-live')return load('src/main/ocr-live.ts');if(name.startsWith('.'))return load(path.relative(ROOT,path.resolve(path.dirname(mainFile),name+'.ts')));throw Error(name);
  };
  vm.runInContext(code,context);
  return {api:context.api,appEvents,warnings,handlers,windows,boot:()=>ready(),ipc:(name,...args)=>handlers.get(name)({},...args)};
}
function test(name,fn,options){try{const r=makeRuntime(options);fn(r);checks++;console.log('PASS '+name);}catch(error){failures++;console.error('FAIL '+name,error);}}
test('native main window uses fitted monitor DIP bounds and remains initially hidden',({api})=>{const w=api.createMain();assert.equal(w.options.width,1240);assert.equal(w.options.height,820);assert.equal(w.options.x,20);assert.equal(w.options.y,40);assert.equal(w.options.minWidth,640);assert.equal(w.options.minHeight,420);assert.equal(w.options.show,false);});
const message=(bridge)=>bridge.handleLine(JSON.stringify({event:'status',text:'late backend message'}));
test('live windows receive real BackendBridge.handleLine events',({api})=>{const a=api.createMain(),b=api.createOverlay();message(api.bridge());assert.equal(a.messages.length,1);assert.equal(b.messages.length,1);});
test('main closed before late backend event',({api})=>{const a=api.createMain(),b=api.createOverlay();a.destroy();assert.doesNotThrow(()=>message(api.bridge()));assert.equal(b.messages.length,1);assert.equal(api.getMain(),null);});
test('overlay destroyed while main remains alive',({api})=>{const a=api.createMain(),b=api.createOverlay();b.destroy();assert.doesNotThrow(()=>message(api.bridge()));assert.equal(a.messages.length,1);assert.equal(api.getOverlay(),null);});
test('webContents destroyed before its BrowserWindow',({api})=>{const a=api.createMain();a.contentsDead=true;assert.doesNotThrow(()=>message(api.bridge()));assert.equal(a.messages.length,0);});
test('old closed and ready callbacks cannot affect recreated windows',({api})=>{const old=api.createMain(),oldOverlay=api.createOverlay();const a=api.createMain(),b=api.createOverlay();old.destroy();oldOverlay.destroy();oldOverlay.events.get('ready-to-show')?.();assert.equal(api.getMain(),a);assert.equal(api.getOverlay(),b);message(api.bridge());assert.equal(a.messages.length,1);assert.equal(b.messages.length,1);});
test('duplicate closed callbacks are harmless',({api})=>{const a=api.createMain();a.destroy();a.events.get('closed')?.();assert.doesNotThrow(()=>message(api.bridge()));});
test('quit teardown ignores late UI delivery',({api,appEvents})=>{const a=api.createMain();appEvents.get('before-quit')({preventDefault(){throw Error('unexpected protection');}});message(api.bridge());assert.equal(a.messages.length,0);});
test('destruction between check and send is diagnosed and contained',({api,warnings})=>{const a=api.createMain(),b=api.createOverlay();a.race=true;assert.doesNotThrow(()=>message(api.bridge()));assert.equal(b.messages.length,1);assert.ok(warnings.some(a=>a.join(' ').includes('backend:event')));});
test('unrelated errors are not swallowed',({api})=>{const a=api.createMain();a.sendError=new Error('unrelated programming fault');assert.throws(()=>message(api.bridge()),/unrelated programming fault/);});
const operations = [
  ['click-through', 'setIgnoreMouseEvents', r => r.ipc('overlay:set-click-through', true)],
  ['hide', 'hide', r => r.ipc('overlay:hide')],
  ['show', 'showInactive', r => r.ipc('overlay:show')],
  ['toggle hide', 'hide', r => r.ipc('overlay:toggle-visible'), true],
  ['toggle show', 'showInactive', r => r.ipc('overlay:toggle-visible')],
  ['resize', 'setSize', r => r.ipc('overlay:set-size', 480, 90)],
];
for (const [name, method, invoke, visible = false] of operations) {
  test(`IPC ${name}: destroyed reference before closed callback`, r => {
    const w = r.api.createOverlay(); w.visible = visible; w.dead = true;
    assert.doesNotThrow(() => invoke(r)); assert.equal(w.messages.length, 0);
    if (name === 'resize') assert.equal(invoke(r), null);
    if (['hide', 'show', 'toggle hide', 'toggle show'].includes(name)) assert.equal(invoke(r), false);
  });
  test(`IPC ${name}: check-to-call destruction`, r => {
    const w = r.api.createOverlay(); w.visible = visible; w.raceMethod = method;
    assert.doesNotThrow(() => invoke(r)); assert.equal(w.dead, true);
    assert.equal(w.messages.length, 0); assert.equal(r.warnings.length, 1);
  });
  test(`IPC ${name}: live operation still executes`, r => {
    const w = r.api.createOverlay(); w.visible = visible;
    const result = invoke(r);
    assert.equal(w.calls.filter(c => c[0] === method).length, 1);
    if (name === 'resize') assert.equal(JSON.stringify(result), JSON.stringify({ width: 480, height: 90 }));
    if (['hide', 'show'].includes(name)) assert.equal(result, true);
    if (name === 'toggle hide') assert.equal(result, false);
    if (name === 'toggle show') assert.equal(result, true);
    if (name === 'click-through') assert.equal(w.messages[0][0], 'overlay:click-through');
  });
}
for (const method of ['isMinimized', 'restore', 'show', 'focus']) {
  test(`second-instance race at ${method}`, r => {
    const w = r.api.createMain(); w.minimized = true; w.raceMethod = method;
    assert.doesNotThrow(() => r.appEvents.get('second-instance')());
    assert.equal(w.dead, true); assert.equal(r.warnings.length, 1);
  });
}
test('second-instance skips destroyed reference', r => { const w = r.api.createMain(); w.dead = true; assert.doesNotThrow(() => r.appEvents.get('second-instance')()); });
test('second-instance restores shows and focuses live owner', r => { const w = r.api.createMain(); w.minimized = true; r.appEvents.get('second-instance')(); assert.deepEqual(w.calls.slice(-4).map(c => c[0]), ['isMinimized', 'restore', 'show', 'focus']); });
test('tray toggle and double-click use live then destroyed owners', r => {
  const a = r.api.createMain(), b = r.api.createOverlay(), tray = r.api.createTray();
  const toggle = tray.menu.find(x => x.label === '显示/隐藏浮窗').click;
  toggle(); assert.equal(b.visible, true); toggle(); assert.equal(b.visible, false);
  tray.events.get('double-click')(); assert.equal(a.visible, true);
  a.dead = true; b.dead = true; assert.doesNotThrow(toggle); assert.doesNotThrow(() => tray.events.get('double-click')());
});
test('old ready callbacks never operate on old or replacement owners', r => {
  const old = r.api.createMain(), oldOverlay = r.api.createOverlay(), a = r.api.createMain(), b = r.api.createOverlay();
  const counts = [old.calls.length, oldOverlay.calls.length, a.calls.length, b.calls.length];
  old.events.get('ready-to-show')?.(); oldOverlay.events.get('ready-to-show')?.();
  assert.deepEqual([old.calls.length, oldOverlay.calls.length, a.calls.length, b.calls.length], counts);
  old.destroy(); oldOverlay.destroy(); assert.equal(r.api.getMain(), a); assert.equal(r.api.getOverlay(), b);
});
test('late IPC tray second-instance and ready callbacks do nothing during quit', r => {
  const a = r.api.createMain(), b = r.api.createOverlay(), tray = r.api.createTray();
  r.appEvents.get('before-quit')({ preventDefault() { throw Error('unexpected protection'); } });
  const counts = [a.calls.length, b.calls.length];
  for (const [, , invoke] of operations) invoke(r);
  for (const entry of tray.menu) if (entry.click && entry.label !== '退出') entry.click();
  tray.events.get('double-click')(); r.appEvents.get('second-instance')();
  a.events.get('ready-to-show')?.(); b.events.get('ready-to-show')?.();
  assert.deepEqual([a.calls.length, b.calls.length], counts);
  assert.equal(a.messages.length + b.messages.length, 0);
});
test('late app-ready does not create windows after quit', r => { r.api.quit(); r.boot(); assert.equal(r.windows.length, 0); });
test('late activate does not recreate a quitting main owner', r => {
  r.boot(); r.appEvents.get('before-quit')({ preventDefault() { throw Error('unexpected protection'); } });
  for (const w of r.windows) w.dead = true;
  const count = r.windows.length; r.appEvents.get('activate')(); assert.equal(r.windows.length, count);
});
for (const error of [new Error('Object has been destroyed unexpectedly'), new Error('prefix Object has been destroyed'), new RangeError('Object has been destroyed'), new Error('unrelated programming fault')]) {
  test(`non-destruction error propagates exactly: ${error}`, r => {
    const w = r.api.createOverlay(); w.errorMethod = 'setSize'; w.methodError = error;
    assert.throws(() => r.ipc('overlay:set-size', 500, 100), e => e === error);
  });
}
for (const error of [new Error('Object has been destroyed'), new TypeError('Object has been destroyed')]) {
  test(`exact native destruction error is locally diagnosed: ${error.name}`, r => {
    const w = r.api.createOverlay(); w.errorMethod = 'setSize'; w.methodError = error;
    assert.equal(r.ipc('overlay:set-size', 500, 100), null); assert.equal(r.warnings.length, 1);
  });
}
test('headless second-instance and ready callbacks remain silent', r => {
  const a = r.api.createMain(), b = r.api.createOverlay(), count = a.calls.length + b.calls.length;
  r.appEvents.get('second-instance')(); a.events.get('ready-to-show')?.(); b.events.get('ready-to-show')?.();
  assert.equal(a.calls.length + b.calls.length, count);
}, { headless: true });
test('stale main close cannot redirect a replacement with a busy owner', r => {
  const old = r.api.createMain(), current = r.api.createMain();
  r.ipc('app:set-busy', true, 'another task', 'other-owner');
  const count = current.calls.length;
  let prevented = false;
  old.events.get('close')({ preventDefault() { prevented = true; } });
  assert.equal(prevented, false);
  assert.equal(current.calls.length, count);
  assert.equal(current.messages.length, 0);
  assert.equal(r.ipc('app:busy-reason'), 'another task');
  current.events.get('close')({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.ok(current.messages.some(m => m[0] === 'app:blocking-task'));
});
test('subtitle overlay explicitly permits screenshots without taking focus', r => {
  const w = r.api.createOverlay(); w.events.get('ready-to-show')();
  assert.deepEqual(w.calls.filter(c => c[0] === 'setContentProtection'), [['setContentProtection', false]]);
  assert.equal(w.visible, true);
  assert.equal(w.options.focusable, false);
  assert.equal(w.calls.some(c => c[0] === 'show' || c[0] === 'focus'), false);
  assert.ok(w.calls.some(c => c[0] === 'setAlwaysOnTop' && c[1] === true));
});
test('hide show and click-through never re-enable subtitle screenshot exclusion', r => {
  const w = r.api.createOverlay(); w.events.get('ready-to-show')();
  r.ipc('overlay:hide'); r.ipc('overlay:show'); r.ipc('overlay:toggle-visible'); r.ipc('overlay:toggle-visible');
  r.ipc('overlay:set-click-through', true); r.ipc('overlay:set-click-through', false);
  assert.equal(w.visible, true);
  assert.deepEqual(w.calls.filter(c => c[0] === 'setContentProtection'), [['setContentProtection', false]]);
});
test('recreated subtitle window keeps screenshots allowed and ignores stale readiness', r => {
  const old = r.api.createOverlay(); old.events.get('ready-to-show')(); old.destroy();
  const current = r.api.createOverlay(); current.events.get('ready-to-show')();
  const calls = old.calls.length; old.events.get('ready-to-show')();
  assert.equal(old.calls.length, calls);
  assert.deepEqual(current.calls.filter(c => c[0] === 'setContentProtection'), [['setContentProtection', false]]);
});
test('native fallback replacement preserves hidden state without a transient show', r => {
  const old=r.api.createOverlay();r.ipc("overlay:set-click-through",true);r.api.replaceOverlay();
  const replacement=r.api.getOverlay();assert.notEqual(old,replacement);assert.equal(old.isDestroyed(),true);
  assert.ok(replacement.calls.some(c=>c[0]==="setIgnoreMouseEvents"&&c[1]===true));
  replacement.events.get('ready-to-show')();
  assert.equal(replacement.calls.some(c=>c[0]==='showInactive'||c[0]==='show'),false);
  replacement.destroy();
});
test('glass waits for actual owner geometry and isolates zoom', r => {
  const old=r.api.createOverlay();
  assert.ok(old.calls.some(c=>c[0]==='setZoomMode'&&c[1]==='isolated'));
  assert.equal(r.ipc('overlay:set-glass',true,70).active,false);
  old.events.get('ready-to-show')();
  assert.equal(old.calls.filter(c=>c[0]==='setBackgroundMaterial').length,0);
  const frame={x:6,y:6,width:848,height:128,radius:16,viewportWidth:860,viewportHeight:140};
  r.handlers.get('overlay:frame')({sender:{}},frame);
  assert.equal(old.calls.filter(c=>c[0]==='setShape').length,0);
  r.handlers.get('overlay:frame')({sender:old.webContents},frame);
  assert.equal(old.options.backgroundColor,'#00000000');
  assert.ok(old.calls.some(c=>c[0]==='setBackgroundMaterial'&&c[1]==='none'));
  assert.ok(old.calls.every(c=>c[0]!=='setBackgroundMaterial'||c[1]!=='acrylic'));
  assert.equal(r.ipc('overlay:get-glass').active,false);
  old.destroy();const current=r.api.createOverlay();current.events.get('ready-to-show')();
  const oldCount=old.calls.length;old.events.get('resize')();old.events.get('move')();
  assert.equal(old.calls.length,oldCount);
  assert.equal(current.calls.filter(c=>c[0]==='setBackgroundMaterial').length,0);
  assert.deepEqual(current.calls.filter(c=>c[0]==='setContentProtection'),[['setContentProtection',false]]);
  current.destroy();
});
console.log(`${checks} window lifecycle checks passed; ${failures} failed`);
if (failures) process.exitCode = 1;
