#!/usr/bin/env node
// Execute the real main/preload modules with fail-closed fake Electron; never launch Electron.
import { readFileSync } from 'node:fs';
import * as path from 'node:path';
import vm from 'node:vm';
import ts from 'typescript';
import { ROOT, createReporter } from './esbuild-ts.mjs';
const { check, finish } = createReporter('Main quit owner isolation (fake Electron)');
const handlers = new Map();
const events = new Map();
const windows = [];
let quitCalls = 0;
let disposals = 0;
const messages = [];
class FakeWindow {
  constructor() { this.events = new Map(); this.webContents = { on() {}, isDestroyed: () => false, send: (...args) => messages.push(args) }; windows.push(this); }
  isDestroyed() { return false; }
  loadFile() { return Promise.resolve(); }
  on(name, fn) { this.events.set(name, fn); }
  once(name, fn) { this.events.set(name, fn); }
  show() {}
}
const electron = {
  app: {
    requestSingleInstanceLock: () => true,
    whenReady: () => new Promise(() => {}), // Do not start backend, tray, or capture.
    on: (name, fn) => events.set(name, fn),
    quit: () => { quitCalls++; },
  },
  BrowserWindow: FakeWindow,
  nativeImage: { createFromPath: () => ({ isEmpty: () => true }) },
  ipcMain: { handle: (name, fn) => handlers.set(name, fn), on: () => {} },
};
const compile = (file) => ts.transpileModule(readFileSync(path.join(ROOT, file), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const context = vm.createContext({
  exports: {}, __dirname: path.join(ROOT, 'src/main'),
  process: { env: {}, platform: 'win32' }, console,
  require: (name) => {
    if (name === 'electron') return electron;
    if (name === 'node:path') return path;
    if (name === 'node:fs') return new Proxy({}, { get() { throw Error('Real filesystem access forbidden'); } });
    if (name === './backend' || name === './capture' || name === './shortcuts') return {};
    throw Error(`Unexpected import: ${name}`);
  },
  fakeBridge: { dispose: () => { disposals++; } },
});
vm.runInContext(compile('src/main/main.ts') + '\nregisterIpc(); mainWindow = createMainWindow(); bridge = fakeBridge;', context);
const setBusy = (...args) => handlers.get('app:set-busy')({}, ...args);
const quit = () => handlers.get('app:request-quit')({});
const closeEvent = () => ({ prevented: false, preventDefault() { this.prevented = true; } });
setBusy(true, 'migration A', 'migration-A');
setBusy(true, 'import B', 'import-B');
setBusy(false, undefined, 'import-B');
check('unrelated completion cannot clear migration owner', await quit() === false && quitCalls === 0 && disposals === 0);
setBusy(false); // Unowned/old callers cannot clear another operation.
check('unowned release cannot clear migration owner', await quit() === false && quitCalls === 0);
setBusy(false, undefined, 'stale-owner');
const close = closeEvent();
windows[0].events.get('close')(close);
check('native close remains blocked after stale release', close.prevented && disposals === 0);
const beforeQuit = closeEvent();
events.get('before-quit')(beforeQuit);
check('before-quit blocks before backend disposal', beforeQuit.prevented && disposals === 0);
check('blocking notice retains surviving owner reason', messages.some(([channel, payload]) => channel === 'app:blocking-task' && payload.reason === 'migration A'));
setBusy(false, undefined, 'migration-A');
check('last owner completion allows quit', await quit() === true && quitCalls === 1);
const finalQuit = closeEvent();
events.get('before-quit')(finalQuit);
check('unguarded before-quit permits normal teardown', !finalQuit.prevented && disposals > 0);
let api;
const invokes = [];
vm.runInNewContext(compile('src/main/preload.ts'), {
  exports: {},
  require: (name) => {
    if (name !== 'electron') throw Error(`Unexpected preload import: ${name}`);
    return {
      contextBridge: { exposeInMainWorld: (_name, value) => { api = value; } },
      ipcRenderer: { invoke: (...args) => { invokes.push(args); return Promise.resolve('ok'); } },
    };
  },
});
await api.app.setBusy(true, 'cleanup', 'cleanup-owner');
check('preload forwards exact operation owner', invokes.at(-1)?.join('|') === 'app:set-busy|true|cleanup|cleanup-owner');
finish();
