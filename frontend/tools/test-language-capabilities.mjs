import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { installMiniDom } from './mini-dom.mjs';
import { ROOT } from './esbuild-ts.mjs';

// Bundle real controls/store/workspace: no copied controller logic, GUI or audio.
const scratch = mkdtempSync(join(tmpdir(), 'voxsub-language-matrix-'));
const dom = installMiniDom();
// Geometry is supplied only by the DOM fixture; behavior below is the production component.
Object.getPrototypeOf(dom.document.createElement('input')).getBoundingClientRect = () => ({left:20,top:80,bottom:110,width:160});
dom.window.innerWidth=1000; dom.window.innerHeight=700;
const listeners = new Set();
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const meta = (sources, targets) => ({ sources, targets, compatible: sources.length > 0, reason: '' });
const bilingual = meta(['auto', 'zh', 'en'], { auto: ['zh', 'en'], zh: ['zh', 'en'], en: ['zh', 'en'] });
const english = meta(['en'], { en: ['zh', 'en', 'ja', 'ko'] });
const all = meta(['zh', 'en', 'ja', 'ko'], Object.fromEntries(['zh', 'en', 'ja', 'ko'].map(s => [s, ['zh', 'en', 'ja', 'ko']])));
let matrix = bilingual;
let read = async () => ({ ok: true, data: matrix });
let write = async () => ({ ok: true, data: null });
let modelWrite = async () => ({ ok: true, data: null });
const calls = [];
let off, clean, page;
try {
  const result = await build({ absWorkingDir: ROOT, bundle: true, platform: 'node', format: 'esm', write: false,
    stdin: { contents: 'export * from "./src/renderer/language-capabilities"; export { store, connectBackend, callWithOutcome } from "./src/renderer/store"; export { buildWorkspace, updateStatus } from "./src/renderer/views/workspace";', resolveDir: ROOT, loader: 'ts' } });
  const compiled = join(scratch, 'entry.mjs'); writeFileSync(compiled, result.outputFiles[0].contents);
  const api = await import(pathToFileURL(compiled).href);
  const { store, connectBackend, callWithOutcome, initializeLanguageCapabilities, refreshLanguageCapabilities, buildLanguageControls, buildWorkspace, updateStatus } = api;
  dom.window.voxsub = { backend: {
    start: async () => ({ ok: true }),
    onEvent: fn => { listeners.add(fn); return () => listeners.delete(fn); },
    command: async (name, args) => {
      calls.push({ name, args });
      if (name === 'language_capabilities') return read(args);
      if (name === 'set_langs') return write(args);
      if (name === 'set_asr_model' || name === 'set_translator') return modelWrite(args);
      if (name === 'state') return { ok: true, data: { mode: 'a', running: false, paused: false } };
      return { ok: true, data: name === 'set_config' ? {} : null };
    },
  } };
  off = connectBackend();
  for (const fn of listeners) fn({ type: 'ready', version: 'test', session: { mode: 'a', running: false, paused: false } });
  await settle();
  clean = initializeLanguageCapabilities();
  const host = dom.document.createElement("div"); dom.mount(host);
  host.append(buildLanguageControls());
  page = buildWorkspace(); host.append(page.element);
  const src = () => dom.document.querySelector('[data-language-source]');
  const dst = () => dom.document.querySelector('[data-language-target]');
  const hint = () => dom.document.querySelector('[data-language-hint]').textContent;
  const options = el => { if (el.getAttribute('aria-expanded') !== 'true') el.click(); return [...el.closest('.searchable-select').querySelectorAll('[role="option"]')]; };
  const codes = el => options(el).map(o => o.dataset.value);
  const cta = () => page.element.querySelector('.btn--primary');
  const change = (el, value) => { const option = options(el).find(o => o.dataset.value === value); assert.ok(option, 'requested language must be offered'); option.click(); };

  await refreshLanguageCapabilities(); await settle(); updateStatus();
  assert.deepEqual(codes(src()), bilingual.sources);
  assert.equal(store.get().languageCompatible, true, 'void successful set_langs must be acknowledged');
  assert.equal(cta().disabled, false);
  const original = options(src())[0]; src().focus(); store.patch({ statusText: 'unrelated' });
  assert.equal(options(src())[0], original); assert.equal(dom.document.activeElement, src());

  matrix = english;
  await callWithOutcome('set_asr_model', { model_id: 'english' }); await settle(); updateStatus();
  assert.deepEqual(codes(src()), ['en']);
  assert.equal(store.get().sourceLang, 'en'); assert.equal(store.get().targetLang, 'zh');
  assert.ok(codes(dst()).includes('zh'), 'ASR cannot restrict translator target');
  assert.match(hint(), /调整/);
  await refreshLanguageCapabilities(); await settle();
  assert.equal(store.get().sourceLang, 'en'); assert.equal(store.get().targetLang, 'zh');

  const held = deferred(); write = () => held.promise;
  change(dst(), 'ja'); updateStatus();
  assert.equal(store.get().languagePending, true); assert.equal(cta().disabled, true, 'Start blocked until language acknowledgement');
  held.resolve({ ok: true, data: null }); await settle(); updateStatus();
  assert.equal(store.get().targetLang, 'ja'); assert.equal(cta().disabled, false);
  assert.ok(calls.some(c => c.name === 'set_config' && c.args.updates.lang_pair === 'en-ja'));

  write = async () => ({ ok: false, error: 'rejected' });
  change(dst(), 'ko'); await settle(); updateStatus();
  assert.equal(store.get().languagePending, false); assert.equal(cta().disabled, true); assert.match(hint(), /未确认/);
  write = async () => ({ ok: true, data: null });
  change(dst(), 'zh'); await settle(); updateStatus(); assert.equal(cta().disabled, false, 'failed write can be retried');

  matrix = meta([], {}); await refreshLanguageCapabilities(); await settle(); updateStatus();
  assert.equal(cta().disabled, true); assert.equal(src().disabled, true);
  store.patch({ running: true }); updateStatus(); assert.equal(cta().disabled, false, 'Stop remains available');
  store.patch({ running: false });

  const stale = deferred(); read = () => stale.promise;
  const oldRead = refreshLanguageCapabilities(); await settle();
  read = async () => ({ ok: true, data: english });
  await refreshLanguageCapabilities(); await settle();
  stale.resolve({ ok: true, data: bilingual }); await oldRead; await settle();
  assert.deepEqual(codes(src()), ['en'], 'late model response cannot replace latest matrix');

  const heldWrite = deferred(); write = () => heldWrite.promise;
  change(dst(), 'ja'); await settle();
  const before = calls.filter(c => c.name === 'set_config' && c.args.updates.lang_pair === 'en-ja').length;
  matrix = bilingual; read = async () => ({ ok: true, data: matrix });
  const modelCommand = callWithOutcome('set_asr_model', { model_id: 'bilingual' }); await settle();
  write = async () => ({ ok: true, data: null }); heldWrite.resolve({ ok: true, data: null });
  await modelCommand; await settle();
  assert.equal(calls.filter(c => c.name === 'set_config' && c.args.updates.lang_pair === 'en-ja').length, before, 'obsolete language must not persist after model change');
  assert.equal(store.get().targetLang, 'zh'); assert.equal(store.get().languagePending, false);

  const firstModel = deferred(), secondModel = deferred(); let mutation = 0;
  modelWrite = () => (++mutation === 1 ? firstModel.promise : secondModel.promise);
  const readsBefore = calls.filter(c => c.name === 'language_capabilities').length;
  const firstChange = callWithOutcome('set_asr_model', { model_id: 'english' });
  const secondChange = callWithOutcome('set_translator', { kind: 'cloud' }); await settle();
  firstModel.resolve({ ok: true, data: null }); await firstChange; await settle(); updateStatus();
  assert.equal(cta().disabled, true, 'pending second model mutation keeps Start blocked');
  assert.equal(calls.filter(c => c.name === 'language_capabilities').length, readsBefore, 'do not query a partially changed chain');
  secondModel.resolve({ ok: true, data: null }); await secondChange; await settle();
  assert.equal(calls.filter(c => c.name === 'language_capabilities').length, readsBefore + 1);
  modelWrite = async () => ({ ok: true, data: null });

  matrix = all; store.patch({ mode: 'd', sourceLang: 'ja', targetLang: 'zh' }); await settle();
  assert.deepEqual(codes(src()), all.sources);
  const count = calls.filter(c => c.name === 'set_langs').length;
  change(src(), 'ko'); await settle();
  assert.equal(calls.filter(c => c.name === 'set_langs').length, count, 'OCR routing must not touch audio ASR owner');
  assert.ok(calls.some(c => c.name === 'set_config' && c.args.updates.lang_pair === 'ko-zh'));
  store.patch({ mode: 'a' }); await settle();
  read = async () => ({ ok: false, error: 'offline' }); await refreshLanguageCapabilities(); await settle(); updateStatus();
  assert.equal(cta().disabled, true); assert.match(hint(), /后端/);
  read = async () => ({ ok: true, data: english });
  store.patch({ backendPhase: 'disconnected' }); updateStatus();
  assert.equal(cta().disabled, true, 'disconnect invalidates cached permission to Start');
  store.patch({ backendPhase: 'ready' }); await settle(); updateStatus();
  assert.deepEqual(codes(src()), ['en']); assert.equal(cta().disabled, false);

  const expanded = meta(['en', 'fr', 'de', 'yue'], Object.fromEntries(['en', 'fr', 'de', 'yue'].map(code => [code, ['zh', 'zh-hant', 'en', 'fr', 'de', 'yue']])));
  expanded.labels = { fr: { zh: '法语', en: 'French' }, de: { zh: '德语', en: 'German' }, yue: { zh: '粤语', en: 'Cantonese' }, 'zh-hant': { zh: '繁体中文', en: 'Traditional Chinese' } };
  read = async () => ({ ok: true, data: expanded });
  await refreshLanguageCapabilities(); await settle();
  assert.deepEqual(codes(src()), expanded.sources);
  assert.ok(options(src()).some(o => o.dataset.value === 'fr' && o.textContent.includes('法语')));
  change(src(), 'fr'); await settle(); change(dst(), 'zh-hant'); await settle();
  assert.ok(calls.some(c => c.name === 'set_config' && c.args.updates.lang_pair === 'fr-zh-hant'));
  await refreshLanguageCapabilities(); await settle();
  assert.equal(store.get().sourceLang, 'fr'); assert.equal(store.get().targetLang, 'zh-hant');
  expanded.sourceLanguageHint = 'load_time'; store.patch({ running: true });
  await refreshLanguageCapabilities(); await settle();
  assert.equal(src().disabled, true); assert.equal(dst().disabled, false);
  assert.match(hint(), /停止/);
  store.patch({ running: false }); await settle(); assert.equal(src().disabled, false);
  // Search is local-only, preserves drafts across subtitle/log events, and honors directional capabilities.
  const query = (el, value) => { el.value=value; el.dispatchEvent(new Event('input')); };
  const beforeSearch = calls.length;
  query(src(), 'FREN'); store.patch({statusText:'subtitle tick'});
  assert.equal(src().value, 'FREN'); assert.deepEqual(codes(src()), ['fr']);
  assert.equal(calls.length, beforeSearch, 'typing must not persist or query the backend');
  options(src())[0].click(); await settle(); assert.equal(store.get().sourceLang, 'fr');
  query(dst(), '繁体'); assert.deepEqual(codes(dst()), ['zh-hant']);
  options(dst())[0].click(); await settle(); assert.equal(store.get().targetLang, 'zh-hant');
  query(src(), 'unsupported'); assert.deepEqual(codes(src()), []);
  read = async () => ({ok:true,data:english}); await refreshLanguageCapabilities(); await settle();
  assert.equal(src().getAttribute('aria-expanded'), 'false', 'model refresh discards stale search');
  assert.deepEqual(codes(src()), ['en']);
  store.patch({mode:'c'}); await settle(); store.patch({running:true});
  assert.equal(src().disabled,true); assert.equal(dst().disabled,true);
  store.patch({running:false}); await settle();
  console.log('PASS production language controls: direction, preservation, adjustment, pending/failure, stale replies, OCR, reconnect, Start/Stop and focus');
} finally { page?.dispose(); clean?.(); off?.(); dom.restore(); rmSync(scratch, { recursive: true, force: true }); }
