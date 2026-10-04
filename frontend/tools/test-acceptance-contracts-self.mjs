#!/usr/bin/env node
// Mutate in-memory source only: prove comments/strings and disconnected code cannot pass.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { inspectFrontend } from './test-acceptance-contracts.mjs';
const read = path => readFileSync(new URL('../' + path, import.meta.url), 'utf8');
assert.ok(inspectFrontend(read).every(result => result.ok), 'current production modules satisfy contracts');
const mutations = [
  ['src/renderer/index.ts', '"./language-capabilities"', '"./unused-language-capabilities"', 'language import'],
  ['src/renderer/language-capabilities.ts', 'void savePair(pair[0], pair[1]);\n  });', 'void disconnected(pair[0], pair[1]);\n  });', 'source change'],
  ['src/renderer/language-capabilities.ts', 'void savePair(state.sourceLang, target);', 'void "savePair(state.sourceLang, target)";', 'target change'],
  ['src/renderer/language-capabilities.ts', 'await persistLanguagePair(source, target,', 'await disconnected(source, target,', 'language delegation'],
  ['src/renderer/language-selection.ts', 'call("set_langs", { source, target })', 'call("set_langs", { src: source, dst: target })', 'language payload'],
  ['src/renderer/language-selection.ts', 'call("set_config", { updates: { lang_pair: `${source}-${target}` } })', 'call("set_config", { updates: {} })', 'language persistence'],
  ['src/renderer/views/settings.ts', 'void saveConfig({ overlay_opacity: Number(opacity.value) / 100 });', '// void saveConfig({ overlay_opacity: Number(opacity.value) / 100 });', 'opacity save'],
  ['src/renderer/views/settings.ts', 'void window.voxsub?.overlay.setOpacity(Number(opacity.value) / 100);', 'void "window.voxsub?.overlay.setOpacity(Number(opacity.value) / 100)";', 'opacity preview'],
  ['src/renderer/overlay.ts', 'void restoreOpacity();', '// void restoreOpacity();', 'opacity boot'],
  ['src/renderer/overlay.ts', '["overlay_opacity"]', '["wrong_key"]', 'opacity restore key'],
  ['src/main/preload.ts', 'ipcRenderer.invoke("overlay:set-opacity", value)', 'ipcRenderer.invoke("wrong-channel", value)', 'opacity bridge'],
];
for (const [path, before, after, label] of mutations) {
  const source = read(path);
  assert.ok(source.includes(before), `${label}: mutation must match live source`);
  const results = inspectFrontend(file => file === path ? source.replace(before, after) : read(file));
  assert.ok(results.some(result => !result.ok), `${label}: mutation must fail closed`);
  console.log(`PASS rejects ${label} mutation`);
}
console.log(`PASS ${mutations.length} in-memory negative controls; no production files changed`);
