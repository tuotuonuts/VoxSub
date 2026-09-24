#!/usr/bin/env node
import assert from "node:assert/strict";
import { transformSync } from "esbuild";
import vm from "node:vm";
import { readFileSync } from "node:fs";
const source = readFileSync(new URL("../src/renderer/language-selection.ts", import.meta.url), "utf8");
const js = transformSync(source, { loader: "ts", format: "cjs", target: "es2020" }).code;
const module = { exports: {} };
vm.runInNewContext(js, { module, exports: module.exports, Promise });
const { persistLanguagePair } = module.exports;
const calls = [];
const call = async (name, args) => { calls.push([name, args]); };
await Promise.all([
  persistLanguagePair("ja", "zh", call, () => {}),
  persistLanguagePair("en", "ja", call, () => {}),
]);
assert.deepEqual(calls.map(([name]) => name), ["set_langs", "set_config", "set_langs", "set_config"]);
assert.equal(calls[0][1].source, "ja");
assert.equal(calls[1][1].updates.lang_pair, "ja-zh");
assert.equal(calls[3][1].updates.lang_pair, "en-ja");
console.log("PASS language selection sequencing and interleaving");
