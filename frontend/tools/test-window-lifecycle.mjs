import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import ts from 'typescript';
import { ROOT } from './esbuild-ts.mjs';
let checks=0;
function makeRuntime() {
  const handlers=new Map(), appEvents=new Map(), warnings=[], windows=[];
  class Window {
    constructor(){ this.dead=false; this.events=new Map(); this.messages=[]; this.contentsDead=false; this.race=false;
      this.webContents={isDestroyed:()=>this.contentsDead, send:(...args)=>{this.assertLive(); if(this.contentsDead||this.race){this.contentsDead=true;throw new TypeError('Object has been destroyed');} if(this.sendError) throw this.sendError; this.messages.push(args);}};windows.push(this); }
    assertLive(){if(this.dead)throw new TypeError('Object has been destroyed');}
    isDestroyed(){return this.dead;}
    on(n,f){this.events.set(n,f);return this;}
    once(n,f){return this.on(n,f);}
    loadFile(){this.assertLive();return Promise.resolve();}
    show(){this.assertLive();}
    showInactive(){this.assertLive();}
    setContentProtection(){this.assertLive();}
    setAlwaysOnTop(){this.assertLive();}
    setVisibleOnAllWorkspaces(){this.assertLive();}
    setIgnoreMouseEvents(){this.assertLive();}
    destroy(){this.dead=true;this.contentsDead=true;this.events.get('closed')?.();}
    close(){this.destroy();}
  }
  const electron={BrowserWindow:Window, app:{on:(n,f)=>appEvents.set(n,f),whenReady:()=>new Promise(()=>{}),requestSingleInstanceLock:()=>true,quit:()=>{}},
    screen:{getPrimaryDisplay:()=>({workAreaSize:{width:1280,height:900}})},nativeImage:{createFromPath:()=>({isEmpty:()=>true})},
    ipcMain:{handle:(n,f)=>handlers.set(n,f),on:()=>{}}};
  const cache=new Map();let context;
  const load=(file)=>{
    const full=path.resolve(ROOT,file);if(cache.has(full))return cache.get(full).exports;
    const mod={exports:{}};cache.set(full,mod);
    const source=readFileSync(full,'utf8');
    const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
    const req=(name)=>{
      if(name==='electron')return electron;
      if(name==='./capture')return {};
      if(name.startsWith('.'))return load(path.relative(ROOT,path.resolve(path.dirname(full),name+'.ts')));
      if(name==='node:path')return path;
      if(name==='node:fs')return {existsSync:()=>false};
      if(name==='node:child_process')return {spawn:()=>{throw Error('Real spawn forbidden');}};
      if(name==='node:readline')return {};
      throw Error(name);
    };
    vm.runInContext('(function(require,module,exports,__dirname){'+code+'\n})',context)(req,mod,mod.exports,path.dirname(full));return mod.exports;
  };
  context=vm.createContext({console:{...console,warn:(...a)=>warnings.push(a)},process:{env:{VOXSUB_HEADLESS:'1'},platform:'win32'},setTimeout,clearTimeout,setInterval,clearInterval});
  const mainFile=path.join(ROOT,'src/main/main.ts');
  const suffix=`\nglobalThis.api={createMain:()=>mainWindow=createMainWindow(),createOverlay:()=>overlayWindow=createOverlayWindow(),getMain:()=>mainWindow,getOverlay:()=>overlayWindow,quit:requestQuit,bridge:()=>{const b=new BackendBridge();wireBackendEvents(b);return b;}};registerIpc();`;
  const code=ts.transpileModule(readFileSync(mainFile,'utf8')+suffix,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  context.exports={};context.__dirname=path.dirname(mainFile);context.require=(name)=>{
    if(name==='electron')return electron;if(name==='./capture')return {};if(name==='node:path')return path;if(name==='node:fs')return {};
    if(name==='./backend')return load('src/main/backend.ts');throw Error(name);
  };
  vm.runInContext(code,context);
  return {api:context.api,appEvents,warnings,handlers,windows};
}
function test(name,fn){const r=makeRuntime();fn(r);checks++;console.log('PASS '+name);}
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
console.log(`${checks} window lifecycle checks passed`);
