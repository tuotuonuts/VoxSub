import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { installMiniDom } from './mini-dom.mjs';
import { ROOT } from './esbuild-ts.mjs';

// Exercise the real index switchMode + store + workspace in one bundle.
// Only expose private entry points in the temporary test build, never production.
const scratch = mkdtempSync(join(tmpdir(), 'voxsub-mode-recording-'));
const dom = installMiniDom();
const listeners = new Set();
const settle = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const snapshot = (mode, enabled = false) => ({ mode, running: false, paused: false,
  recordingEnabled: enabled, recordingActive: false,
  recordingSupported: ['a', 'b'].includes(mode), recordingCanChange: ['a', 'b'].includes(mode) });
let backend = snapshot('c');
let read = async () => ({ ok: true, data: backend });
let setMode = async args => { if (args.mode !== 'd') backend = snapshot(args.mode, backend.recordingEnabled); return { ok: true, data: null }; };
const commands = [];
let off;
try {
  const result = await build({ absWorkingDir: ROOT, bundle: true, platform: 'node', format: 'esm', write: false,
    stdin: { contents: 'export { switchMode, mountModeWorkspace } from "./src/renderer/index"; export { store, connectBackend, refreshSessionState } from "./src/renderer/store";', resolveDir: ROOT, loader: 'ts' },
    plugins: [{ name: 'private-mode-entry', setup(builder) {
      builder.onLoad({ filter: /index[.]ts$/ }, args => ({
        contents: readFileSync(args.path, 'utf8') + '\nexport { switchMode }; export function mountModeWorkspace(slot: HTMLElement) { workspaceSlot = slot; renderWorkspace(); }',
        loader: 'ts',
      }));
    } }],
  });
  const compiled = join(scratch, 'entry.mjs'); writeFileSync(compiled, result.outputFiles[0].contents);
  const api = await import(pathToFileURL(compiled).href);
  const { switchMode, mountModeWorkspace, store, connectBackend, refreshSessionState } = api;
  dom.window.voxsub = { backend: {
    start: async () => ({ ok: true }),
    onEvent: fn => { listeners.add(fn); return () => listeners.delete(fn); },
    command: async (name, args) => {
      commands.push({ name, args });
      if (name === 'state') return read();
      if (name === 'set_mode') return setMode(args);
      if (name === 'set_recording') {
        assert.equal(backend.recordingCanChange, true);
        backend = snapshot(backend.mode, args.enabled); return { ok: true, data: backend };
      }
      if (name === 'last_recording') return { ok: true, data: { path: null } };
      throw Error('unexpected command: ' + name);
    },
  } };
  off = connectBackend();
  const emit = event => { for (const fn of listeners) fn(event); };
  emit({ type: 'ready', version: 'test', session: backend }); await settle();
  const slot = dom.document.createElement('div'); dom.mount(slot); mountModeWorkspace(slot);
  const input = () => slot.querySelector('input[type="checkbox"]');
  const hint = () => slot.querySelector('.rec-hint').textContent;
  assert.equal(input().disabled, true, 'C completion snapshot starts unsupported');
  commands.length = 0;
  await switchMode('a');
  assert.deepEqual(commands.filter(c => ['set_mode', 'state'].includes(c.name)).map(c => c.name), ['set_mode', 'state']);
  assert.equal(store.get().mode, 'a');
  assert.equal(input().disabled, false, 'C -> A must render fresh recording authority');
  assert.equal(input().indeterminate, false);
  assert.equal(hint(), '仅生成字幕，不保存麦克风音频');
  input().checked = true; input().dispatchEvent(new Event('change')); await settle();
  assert.equal(input().checked, true, 'recording write remains usable after mode switch');
  assert.equal(backend.recordingEnabled, true);
  console.log('PASS C completion -> A refresh -> actual workspace recording write');

  for (const mode of ['b', 'a', 'c', 'a']) {
    await switchMode(mode);
    assert.equal(store.get().mode, mode);
    assert.equal(input().disabled, !['a', 'b'].includes(mode));
    assert.equal(input().checked, true, 'mode refresh preserves backend recording preference');
  }
  await switchMode('b');
  input().checked = false; input().dispatchEvent(new Event('change')); await settle();
  assert.equal(backend.recordingEnabled, false, 'B must accept actual recording writes');
  assert.equal(hint(), '仅生成字幕，不保存系统或指定应用音频');
  input().checked = true; input().dispatchEvent(new Event('change')); await settle();
  assert.equal(backend.recordingEnabled, true);
  console.log('PASS repeated A/B/C switches and real B writes reflect backend capabilities and preference');

  const beforeD = commands.length;
  await switchMode('d');
  assert.equal(store.get().mode, 'd', 'audio state must not overwrite renderer-only OCR mode');
  assert.equal(commands.slice(beforeD).some(c => c.name === 'state'), false);
  console.log('PASS D remains renderer-only without an audio-state refresh');

  await switchMode('a');
  const heldRead = deferred(); read = () => heldRead.promise;
  const oldRead = refreshSessionState(); await settle();
  await switchMode('d'); heldRead.resolve({ ok: true, data: snapshot('a', false) }); await oldRead;
  assert.equal(store.get().mode, 'd', 'late state response must not undo newer D click');
  assert.equal(store.get().recordingState.recordingEnabled, true, 'late read must not overwrite newer mode authority');
  read = async () => ({ ok: true, data: backend });
  console.log('PASS in-flight state read cannot clobber a newer mode click');

  await switchMode('c');
  const heldMode = deferred(); setMode = args => {
    if (args.mode === 'a') return heldMode.promise;
    if (args.mode !== 'd') backend = snapshot(args.mode, backend.recordingEnabled);
    return Promise.resolve({ ok: true, data: null });
  };
  const oldSwitch = switchMode('a'); await settle();
  const beforeNewD = commands.length; await switchMode('d');
  heldMode.resolve({ ok: true, data: null }); await oldSwitch;
  assert.equal(store.get().mode, 'd');
  assert.equal(commands.slice(beforeNewD).some(c => c.name === 'state'), false, 'old mode acknowledgement must not refresh newer workspace');
  console.log('PASS delayed set_mode acknowledgement cannot overwrite newer D click');

  setMode = async args => { backend = snapshot(args.mode, backend.recordingEnabled); return { ok: true, data: null }; };
  await switchMode('c');
  const atRejected = commands.length; setMode = async () => ({ ok: false, error: 'mode rejected', delivery: 'response' });
  await switchMode('a');
  assert.equal(commands.slice(atRejected).some(c => c.name === 'state'), false);
  assert.equal(input().disabled, true, 'rejection must not fabricate supported recording');
  console.log('PASS failed set_mode does not publish invented recording capability');

  setMode = async args => { backend = snapshot(args.mode); return { ok: true, data: null }; };
  read = async () => ({ ok: true, data: null });
  await switchMode('a');
  assert.equal(input().disabled, true); assert.equal(input().indeterminate, true);
  console.log('PASS missing authority remains unknown rather than optimistic enabled');
  console.log('7 mode recording regression scenarios passed');
} finally {
  off?.(); dom.restore();
  rmSync(scratch, { recursive: true, force: true });
}
