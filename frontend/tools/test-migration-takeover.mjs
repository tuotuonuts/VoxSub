#!/usr/bin/env node
// Real main/preload/BackendBridge + fresh real renderer/store per reload.
// Only Electron, DOM and the sidecar stdio boundary are replaced. No Python/GUI/IO.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as path from 'node:path';
import vm from 'node:vm';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import * as readline from 'node:readline';
import { createRequire } from 'node:module';
import { build } from 'esbuild';
import ts from 'typescript';
import {securityContents,loadFixturePage,senderEvent} from './electron-security-fixture.mjs';
import { installMiniDom } from './mini-dom.mjs';
import { ROOT } from './esbuild-ts.mjs';

const compile = file => ts.transpileModule(readFileSync(file, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const rendererCode = (await build({ entryPoints: [path.join(ROOT, 'tools/test-migration-safety-entry.ts')],
  bundle: true, write: false, format: 'iife', globalName: 'migration', platform: 'browser' })).outputFiles[0].text;
const detection = { legacy: { found: true, install_location: 'fixture:/old', version: 'test',
  source: 'registry', notes: [] }, storage: [], overallRisk: 'conditional' };
const plan = { targetRoot: 'fixture:/new', steps: [{ key: 'models', source: 'fixture:/old/models',
  target: 'fixture:/new/models', bytes: 1, file_count: 1, purpose: '', mode: 'copy', note: '', rebuildable: false }],
  totalBytes: 1, freeBytes: 100 };
const report = { ok: true, done: [{ key: 'models', mode: 'copy', target: 'fixture:/new/models', verify: { ok: true } }], failed: [], elapsedMs: 1 };
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
let passed = 0;
function check(name, value) { assert.ok(value, name); console.log('PASS', name); passed++; }

function harness() {
  const handlers = new Map(), appEvents = new Map(), children = [], renderers = [];
  let currentRenderer, quitCalls = 0;
  const checkpoints = [];
  const sender = securityContents({ on() {}, isDestroyed: () => false, send: (channel, payload) => currentRenderer?.emit(channel, payload) });
  const electron = {
    screen: {getCursorScreenPoint:()=>({x:0,y:0}),getDisplayNearestPoint:()=>({workArea:{x:0,y:0,width:1280,height:900}})},
    app: { requestSingleInstanceLock: () => true, whenReady: () => new Promise(() => {}),
      on: (n, f) => appEvents.set(n, f), quit: () => quitCalls++, getAppPath: () => ROOT },
    BrowserWindow: class { constructor() { this.webContents = sender; this.events = new Map(); }
      isDestroyed() { return false; }
      loadFile(file) { loadFixturePage(this,file); return Promise.resolve(); } on(n, f) { this.events.set(n, f); } once(n, f) { this.events.set(n, f); } show() {} },
    nativeImage: { createFromPath: () => ({ isEmpty: () => true }) },
    ipcMain: { handle: (n, f) => handlers.set(n, f), on() {} },
  };
  function spawn() {
    const child = new EventEmitter();
    child.stdout = new PassThrough(); child.stderr = new PassThrough();
    child.stdin = new PassThrough(); child.kill = () => {};
    child.job = null; child.requests = []; child.hold = false; child.held = [];
    child.line = payload => child.stdout.write(JSON.stringify(payload) + '\n');
    child.terminal = (job = child.job) => {
      job.status = 'succeeded'; job.result = report;
      child.line({ event: 'job', ...job, sequence: 3 });
    };
    child.stdin.on('data', chunk => {
      const request = JSON.parse(chunk.toString()); child.requests.push(request);
      const { id, command, args } = request;
      let result;
      if (command === 'start_migration') {
        if (child.job?.status === 'running') result = { ok: false, code: 'active_job_exists', jobId: child.job.jobId, error: 'active' };
        else { child.job = { jobId: 'same-job-id', command, status: 'running', clientMigrationId: args.clientMigrationId };
          result = { ok: true, data: { accepted: true, jobId: child.job.jobId } }; }
      } else if (command === 'plan_migration') result = { ok: true, data: plan };
      else if (command === 'detect_legacy') result = { ok: true, data: detection };
      else if (command === 'job_status') result = { ok: true, data: { ok: true, job: child.job } };
      else if (command === 'job_list') result = { ok: true, data: { jobs: child.job ? [child.job] : [] } };
      else result = { ok: true, data: {} };
      const respond = () => child.line({ id, ...result });
      if (child.hold && command === 'start_migration') child.held.push(respond); else respond();
    });
    children.push(child); return child;
  }
  const modules = new Map();
  function load(file) {
    if (modules.has(file)) return modules.get(file);
    const exports = {}; modules.set(file, exports);
    const context = vm.createContext({ exports, __dirname: path.dirname(file), console, setTimeout, clearTimeout,
      setInterval, clearInterval, Buffer, process: { env: {}, platform: 'win32', resourcesPath: 'fixture:/resources' },
      require: name => {
        if (name === 'electron') return electron;
        if (name === 'node:fs') return { existsSync: p => p.endsWith('VoxSubBackend.exe') };
        if (name === 'node:child_process') return { spawn };
        if (name === 'node:readline') return readline;
        if (name === 'node:path') return path;
        if (name === './capture') return {cancelScreenSelection(){}};
        if (name.startsWith('.')) return load(path.resolve(path.dirname(file), name + '.ts'));
        return createRequire(import.meta.url)(name);
      } });
    let code = compile(file);
    if (file.endsWith('main.ts')) code += '\nregisterIpc(); bridge = new backend_1.BackendBridge(); wireBackendEvents(bridge); mainWindow = createMainWindow(); exports.getBridge = () => bridge; exports.getWindow = () => mainWindow; exports.ownerKeys = () => [...busyOwners.keys()];';
    vm.runInContext(code, context, { filename: file }); return exports;
  }
  const main = load(path.join(ROOT, 'src/main/main.ts'));
  async function renderer() {
    const dom = installMiniDom(), listeners = new Map();
    const win = dom.window;
    const context = vm.createContext({ exports: {}, window: win, document: dom.document, console,
      HTMLElement: globalThis.HTMLElement, Event: globalThis.Event, CustomEvent: globalThis.CustomEvent,
      AbortController, CSS: globalThis.CSS, navigator: globalThis.navigator, performance, crypto: globalThis.crypto,
      Date: class extends Date { static now() { return 1700000000000; } },
      setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
      require: name => {
        assert.equal(name, 'electron');
        return { contextBridge: { exposeInMainWorld: (_n, api) => { win.voxsub = api; } }, ipcRenderer: {
          invoke: async (n, ...args) => { const result = await handlers.get(n)(senderEvent(sender), ...args);
            checkpoints.push({ n, guarded: Boolean(await handlers.get('app:busy-reason')(senderEvent(sender))) }); return result; },
          on: (n, f) => { if (!listeners.has(n)) listeners.set(n, new Set()); listeners.get(n).add(f); },
          removeListener: (n, f) => listeners.get(n)?.delete(f),
        } };
      } });
    vm.runInContext(compile(path.join(ROOT, 'src/main/preload.ts')), context);
    vm.runInContext(rendererCode, context);
    const r = { dom, api: win.voxsub, mod: context.migration,
      emit: (n, payload) => { for (const f of listeners.get(n) ?? []) f({}, payload); },
      destroy: () => { r.handle?.dispose(); r.disconnect(); listeners.clear(); dom.restore(); } };
    currentRenderer = r; renderers.push(r); r.disconnect = r.mod.connectBackend();
    await flush(); children.at(-1).line({ event: 'ready', version: 'test' }); await flush();
    r.handle = r.mod.buildMigrationWizard(detection); dom.document.body.append(r.handle.element);
    r.submit = async () => {
      r.handle.element.querySelector('.wiz__actions .btn--primary').click();
      r.handle.element.querySelector('.wiz__actions .btn--primary').click(); await flush();
      r.handle.element.querySelector('#wiz-proceed').click(); await flush();
    };
    return r;
  }
  return { renderer, children, checkpoints, main,
    guarded: async () => Boolean(await handlers.get('app:busy-reason')(senderEvent(sender))),
    quit: () => handlers.get('app:request-quit')(senderEvent(sender)),
    closeBlocked: () => { const e = { prevented: false, preventDefault() { this.prevented = true; } };
      main.getWindow().events.get('close')(e); return e.prevented; },
    cleanup: () => { for (const r of renderers.reverse()) r.destroy(); main.getBridge().dispose(); },
  };
}

// Tracer bullet: main and the sidecar streams survive; renderer/store are actually recreated.
{
  const h = harness();
  try {
    const a = await h.renderer(); await a.submit();
    const child = h.children[0], oldOwner = child.job.clientMigrationId;
    check('A registers protection in real main', await h.guarded() && h.closeBlocked());
    h.checkpoints.length = 0;
    a.destroy();
    const b = await h.renderer(); await b.submit();
    check('B receives active_job_exists from preserved sidecar job', child.requests.filter(x => x.command === 'start_migration').length === 2);
    check('takeover remains guarded at every IPC checkpoint', h.checkpoints.filter(x => x.n === 'backend:command' || x.n === 'app:set-busy').every(x => x.guarded));
    child.line({ event: 'job', command: 'start_migration', jobId: 'unrelated', status: 'succeeded', sequence: 3, result: report }); await flush();
    check('unrelated terminal does not release task', await h.guarded());
    child.terminal(); await flush();
    check('B displays authoritative success report', b.handle.element.textContent.includes('迁移成功'));
    check('real quit allows exit after renderer takeover completion', await h.quit() === true);
  } finally { h.cleanup(); }
}
// Repeated takeover, page close/reopen, unrelated owner and late releases.
{
  const h = harness();
  try {
    const a = await h.renderer(); await a.submit(); const child = h.children[0];
    const ownerA = child.job.clientMigrationId;
    a.destroy(); const b = await h.renderer(); await b.submit();
    const ownerB = child.requests.filter(x => x.command === 'start_migration').at(-1).args.clientMigrationId;
    check('takeover atomically replaces A with B in the actual owner map',
      h.main.ownerKeys().length === 1 && h.main.ownerKeys()[0] === ownerB && ownerA !== ownerB);
    await a.api.app.setBusy(false, undefined, ownerA);
    await b.api.app.setBusy(false, undefined, ownerB);
    check('late A and premature B releases cannot release authoritative task guard', await h.guarded() && h.closeBlocked());
    await b.api.backend.command('start_migration', { async: true, clientMigrationId: ownerB, steps: plan.steps });
    check('duplicate takeover receipt retains one owner', h.main.ownerKeys().length === 1);
    b.destroy(); const c = await h.renderer(); await c.submit();
    const ownerC = child.requests.filter(x => x.command === 'start_migration').at(-1).args.clientMigrationId;
    check('second renderer takeover leaves only current owner', h.main.ownerKeys().length === 1 && h.main.ownerKeys()[0] === ownerC);
    c.handle.dispose(); c.handle.element.remove();
    c.handle = c.mod.buildMigrationWizard(detection); c.dom.document.body.append(c.handle.element); await flush();
    check('page close and reopen retains running task without resubmission', c.handle.element.textContent.includes('迁移进行中') && await h.guarded());
    await c.api.app.setBusy(true, 'unrelated import', 'import-other');
    child.terminal(); child.terminal(); await flush();
    check('duplicate terminals remove only migration protection', h.main.ownerKeys().length === 1 && h.main.ownerKeys()[0] === 'import-other');
    check('unrelated task still blocks the real quit decision', await h.quit() === false);
    await c.api.app.setBusy(false, undefined, 'import-other');
    check('all completed owners allow real quit', await h.quit() === true);
  } finally { h.cleanup(); }
}

// Sidecar restart with a colliding jobId is not evidence about the old process.
{
  const h = harness();
  try {
    const a = await h.renderer(); await a.submit(); const old = h.children[0];
    const ownerA = old.job.clientMigrationId;
    old.emit('close', 1); await flush();
    check('disconnect without task terminal remains guarded', await h.guarded() && await h.quit() === false);
    a.destroy(); const b = await h.renderer(); await b.submit(); const fresh = h.children[1];
    check('restarted bridge has same jobId but distinct protection', old.job.jobId === fresh.job.jobId && h.main.ownerKeys().length === 2);
    old.terminal(); old.emit('close', 1); await flush();
    check('old-process late events cannot release new-process guard', h.main.ownerKeys().length === 2 && h.main.getBridge().isRunning());
    fresh.terminal(); await flush();
    check('new-process terminal does not release old unknown task', h.main.ownerKeys().length === 1 && h.main.ownerKeys()[0] === ownerA && await h.quit() === false);
  } finally { h.cleanup(); }
}

// Terminal/receipt inversion is driven deterministically at the stdio boundary.
{
  const h = harness();
  try {
    const a = await h.renderer(); const child = h.children[0]; child.hold = true;
    await a.submit(); child.terminal(); await flush();
    check('terminal before receipt releases matching request guard', !await h.guarded());
    child.held.shift()(); await flush();
    check('late receipt never resurrects terminal owner', !await h.guarded() && await h.quit() === true);
  } finally { h.cleanup(); }
}

// A missed terminal event is recovered only from an authoritative matching status.
{
  const h = harness();
  try {
    const a = await h.renderer(); await a.submit(); const child = h.children[0];
    a.destroy(); child.hold = true; const b = await h.renderer(); await b.submit();
    child.job.status = 'succeeded'; child.job.result = report;
    child.held.shift()(); await flush();
    check('takeover status query resolves missed terminal with real quit allowed',
      b.handle.element.textContent.includes('迁移成功') && await h.quit() === true);
  } finally { h.cleanup(); }
}
console.log(`${passed} takeover checks passed`);
