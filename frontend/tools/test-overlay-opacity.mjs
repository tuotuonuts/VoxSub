#!/usr/bin/env node
// Offline behavior tests: execute real TS functions with isolated DOM/IPC boundaries.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { transformSync } from 'esbuild';
import { installMiniDom } from './mini-dom.mjs';
import { importShared } from './esbuild-ts.mjs';
const dom = installMiniDom();
const [{ h, on }] = await importShared(['src/renderer/dom.ts'], { bundle: true });
const read = (p) => fs.readFileSync(new URL('../src/' + p, import.meta.url), 'utf8');
const run = (source, context) => vm.runInNewContext(transformSync(source, { loader: 'ts', format: 'cjs' }).code, context);
const tick = () => new Promise(resolve => setImmediate(resolve));
const settings = read('renderer/views/settings.ts');
const appearance = settings.slice(settings.indexOf('function appearanceTab()'), settings.indexOf('function aboutTab()'));
const saved = [], applied = [];
const context = { h, on, config: { overlay_opacity: 0.64 }, tr: x => x,
  currentLanguage: () => 'zh', setLanguage() {}, applyThemeChoice() {},
  radioGroup: () => h('div'), field: (_l, control) => control,
  card: (_l, children) => h('section', {}, children),
  saveConfig: async updates => { saved.push(updates); return { ...updates }; },
  window: { voxsub: { overlay: { setOpacity: async v => { applied.push(v); return v; } } } },
};
const page = run(appearance + '\nappearanceTab();', context);
const slider = page.querySelector('input[type="range"]');
assert.ok(slider, 'Appearance provides an opacity slider');
assert.equal(slider.value, '64');
assert.equal(slider.getAttribute('min'), '20');
assert.equal(slider.getAttribute('max'), '100');
slider.value = '37';
slider.dispatchEvent(dom.makeEvent('input'));
await tick();
assert.equal(applied.at(-1), 0.37, 'input previews immediately');
slider.dispatchEvent(dom.makeEvent('change'));
await tick();
assert.equal(saved.at(-1).overlay_opacity, 0.37, 'change persists unified config');
assert.match(page.textContent, /37%/);
console.log('PASS settings: saved value, bounds, immediate preview, persistence, percentage');
dom.restore();

const main = read('main/main.ts');
const ipcSource = main.slice(main.indexOf('  ipcMain.handle("overlay:set-opacity"'), main.indexOf('  ipcMain.handle("overlay:set-font-size"'));
let handler;
const sent = [];
run('let overlayOpacity = 0.92;\n' + ipcSource, {
  ipcMain: { handle: (_name, fn) => { handler = fn; } },
  sendToWindow: (win, channel, ...args) => win?.webContents.send(channel, ...args),
  overlayWindow: { webContents: { send: (_channel, value) => sent.push(value) } },
});
for (const [value, expected] of [[0, 0.2], [-1, 0.2], [2, 1], [0.2, 0.2], [1, 1], [0.92, 0.92], [NaN, 0.92], [Infinity, 0.92], [true, 0.92], ['0.5', 0.92], [null, 0.92]]) {
  assert.equal(handler(null, value), expected, `IPC validates ${String(value)}`);
  assert.equal(sent.at(-1), expected);
}
console.log('PASS IPC: clamps finite numbers, defaults invalid types/nonfinite values');

const overlay = read('renderer/overlay.ts');
assert.ok(overlay.includes('async function restoreOpacity()'), 'renderer restores opacity on startup');
const restore = overlay.slice(overlay.indexOf('async function restoreOpacity()'), overlay.indexOf('/** 把显示模式写回配置'));
for (const value of [0.2, 0.48, 0.92, 1]) {
  const css = [], sync = [];
  const ctx = { window: { voxsub: {
    backend: { command: async () => ({ ok: true, data: { overlay_opacity: value } }) },
    overlay: { setOpacity: async v => sync.push(v) },
  } }, applyVisuals: () => {}, css, sync };
  await run('let opacity = 0.92; let opacityRevision = 0;\n' + restore + '\nrestoreOpacity().then(() => css.push(opacity));', ctx);
  assert.equal(css[0], value);
  assert.equal(sync[0], value);
}
assert.match(overlay.slice(overlay.indexOf('function boot()')), /void restoreOpacity\(\)/);
assert.match(overlay.slice(overlay.indexOf('function wireBackend()')), /raw.type === "ready"[\s\S]*restoreOpacity/);
console.log('PASS renderer: restore saved values on boot and retry on backend ready');

// Run the actual receiver and visual renderer, not a mock of the CSS update.
const visual = overlay.slice(overlay.indexOf('function applyVisuals()'), overlay.indexOf('const MODE_LABEL'));
const receiver = overlay.slice(overlay.indexOf('  window.voxsub?.overlay.onOpacityChanged'), overlay.indexOf('  window.voxsub?.overlay.onFontSizeChanged'));
let receive;
const styles = new Map();
const renderContext = { document: { documentElement: { style: { setProperty: (k, v) => styles.set(k, v) } } },
  window: { voxsub: { overlay: { onOpacityChanged: fn => { receive = fn; } } } },
  applyTextColors() {}, fontValueEl: null, paddingValueEl: null, gapValueEl: null,
};
run('let opacity = 0.92, opacityRevision = 0, fontSize = 20, contentPadding = 18, lineGap = 6;\n' + visual + receiver, renderContext);
receive(0.31);
assert.equal(styles.get('--overlay-opacity'), '0.31');
let resolveRead;
const staleSync = [];
const raceContext = { window: { voxsub: {
  backend: { command: () => new Promise(resolve => { resolveRead = resolve; }) },
  overlay: { setOpacity: v => staleSync.push(v) },
} }, applyVisuals() {}, staleSync };
const pendingRestore = run('let opacity = 0.71, opacityRevision = 0;\n' + restore + '\nconst pending = restoreOpacity(); opacityRevision++; pending;', raceContext);
resolveRead({ ok: true, data: { overlay_opacity: 0.22 } });
await pendingRestore;
assert.equal(staleSync.length, 0, 'stale config cannot overwrite live preview');
console.log('PASS renderer: IPC changes CSS and stale restore cannot overwrite live input');

// Reproduce preview BEFORE backend-ready restoration, not only during a read.
// Wire the real settings input/change handlers through the real opacity receiver.
const raceDom = installMiniDom();
for (const timing of ['before-read', 'during-read', 'after-save']) {
  const reads = [], syncs = [], css = new Map();
  let receiveOpacity;
  let stored = 0.22;
  const setOpacity = async value => { syncs.push(value); receiveOpacity(value); return value; };
  const renderer = vm.createContext({
    document: { documentElement: { style: { setProperty: (k, v) => css.set(k, v) } } },
    window: { voxsub: {
      backend: { command: () => new Promise(resolve => reads.push(resolve)) },
      overlay: { setOpacity, onOpacityChanged: fn => { receiveOpacity = fn; } },
    } },
    applyTextColors() {}, fontValueEl: null, paddingValueEl: null, gapValueEl: null,
  });
  const evaluate = source => vm.runInContext(transformSync(source, { loader: 'ts', format: 'cjs' }).code, renderer);
  evaluate('let opacity = 0.92, opacityRevision = 0, fontSize = 20, contentPadding = 18, lineGap = 6;\n' + visual + receiver + restore);
  const settingsPage = run(appearance + '\nappearanceTab();', { ...context,
    window: { voxsub: { overlay: { setOpacity } } },
    saveConfig: async updates => { stored = updates.overlay_opacity; return updates; },
  });
  const input = settingsPage.querySelector('input[type="range"]');
  let pending;
  if (timing === 'during-read') pending = evaluate('restoreOpacity()');
  input.value = '71';
  input.dispatchEvent(raceDom.makeEvent('input'));
  if (timing === 'after-save') input.dispatchEvent(raceDom.makeEvent('change'));
  if (!pending) pending = evaluate('restoreOpacity()');
  for (const resolve of reads) resolve({ ok: true, data: { overlay_opacity: 0.22 } });
  await pending;
  assert.equal(css.get('--overlay-opacity'), '0.71', `${timing}: old restore must not overwrite preview`);
  input.dispatchEvent(raceDom.makeEvent('change'));
  await tick();
  assert.equal(stored, 0.71);
  assert.equal(Number(css.get('--overlay-opacity')), stored, `${timing}: saved and visible values agree`);
  assert.deepEqual(syncs, [0.71], `${timing}: restoration must not rebroadcast stale opacity`);
}
raceDom.restore();
console.log('PASS renderer/settings: previews before/during restore and after save stay authoritative');

// Exercise only the existing translation lookup; no language-selection changes.
const i18n = read('renderer/i18n.ts').replace(/^import .*;$/m, '');
for (const lang of ['zh', 'en']) {
  const translate = run(i18n + '\ntr;', { module: { exports: {} }, store: { get: () => ({ theme: 'dark', lang }) } });
  for (const text of ['浮窗背景不透明度', '20%–100%，默认 92%。越低越透明，字幕文字保持清晰；松开后自动保存。']) {
    if (lang === 'zh') assert.equal(translate(text), text);
    else assert.notEqual(translate(text), text, `English opacity copy is missing: ${text}`);
  }
}
console.log('PASS i18n: opacity label and hint resolve in Chinese and English');
