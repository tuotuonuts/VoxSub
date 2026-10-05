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
const call = async (name, args) => { calls.push([name, args]); return { outcome: "ok", data: { saved: true } }; };
await Promise.all([
  persistLanguagePair("ja", "zh", call, () => {}),
  persistLanguagePair("en", "ja", call, () => {}),
]);
assert.deepEqual(calls.map(([name]) => name), ["set_langs", "set_config", "set_langs", "set_config"]);
assert.equal(calls[0][1].source, "ja");
assert.equal(calls[1][1].updates.lang_pair, "ja-zh");
assert.equal(calls[3][1].updates.lang_pair, "en-ja");

const failures = [];
const failedCall = async (name) => {
  calls.push([name]);
  return name === "set_langs" ? { outcome: "error", data: null } : { outcome: "ok", data: {} };
};
await assert.rejects(() => persistLanguagePair("fr", "de", failedCall, (message) => failures.push(message)));
assert.equal(failures.length, 1);
assert.equal(failures[0].includes("fr-de"), true);
assert.equal(calls.filter(([name]) => name === "set_config").length, 2);

let current = false;
const before = calls.length;
await persistLanguagePair("en", "zh", call, () => {}, () => current);
assert.equal(calls.length, before, "obsolete queued work must not send commands");
current = true;
await persistLanguagePair("en", "zh", async (name, args) => {
  calls.push([name, args]); current = false; return { outcome: "ok", data: null };
}, () => {}, () => current);
assert.equal(calls.length, before + 1, "model change during set_langs must suppress stale config write");

console.log("PASS language selection sequencing and failure semantics");
const { splitLanguagePair } = module.exports;
for (const [pair, expected] of [["fr-zh",["fr","zh"]], ["en-zh-hant",["en","zh-hant"]], ["zh-hant-fr",["zh-hant","fr"]], ["zh-hant-zh-hant",["zh-hant","zh-hant"]]]) {
  if (JSON.stringify(splitLanguagePair(pair)) !== JSON.stringify(expected)) throw new Error(`Invalid pair restore: ${pair}`);
}
