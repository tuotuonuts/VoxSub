#!/usr/bin/env node
/** Tests real shared primitives without a window, audio or backend. */
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { installMiniDom } from "./mini-dom.mjs";
import { importShared, cleanupShared, ROOT } from "./esbuild-ts.mjs";

const dom = installMiniDom();
const [buttons, cards, controls, tabs, multi] = await importShared([
  "src/renderer/ui/button.ts", "src/renderer/ui/card.ts",
  "src/renderer/ui/controls.ts", "src/renderer/ui/tab-nav.ts", "src/renderer/ui/multi-filter.ts",
], { bundle: true });
let passed = 0;
function test(name, run) { run(); passed++; console.log(`PASS ${name}`); }
try {
  test("button defaults to a native non-submit ghost action", () => {
    const b = buttons.buildButton("保存");
    assert.equal(b.tagName, "BUTTON"); assert.equal(b.type, "button");
    assert.equal(b.className, "btn btn--ghost"); assert.equal(b.textContent, "保存");
    assert.equal(b.hidden, false); assert.equal(b.disabled, false);
  });
  test("button variants retain CSS, state and caller-owned events", () => {
    const b = buttons.buildButton("Save", { variant: "primary", small: true, block: true, disabled: true, hidden: true, title: "Hint" });
    assert.equal(b.className, "btn btn--primary btn--sm btn--block");
    assert.ok(b.hidden && b.disabled); assert.equal(b.getAttribute("title"), "Hint");
    let clicks = 0; b.addEventListener("click", () => clicks++);
    b.disabled = false; b.hidden = false; b.click(); assert.equal(clicks, 1);
  });
  test("filter chips share active styling and accessible pressed state", () => {
    const off = buttons.buildFilterChip("原图"); const on = buttons.buildFilterChip("识别", true);
    assert.equal(off.className, "filter-chip"); assert.equal(off.getAttribute("aria-pressed"), "false");
    assert.equal(on.className, "filter-chip is-active"); assert.equal(on.getAttribute("aria-pressed"), "true"); assert.equal(on.type, "button");
  });
  test("button labels are plain text, not interpreted HTML", () => {
    const b = buttons.buildButton("<img src=x>"); assert.equal(b.textContent, "<img src=x>"); assert.equal(b.children.length, 0);
  });
  test("cards preserve structure, node identity and optional titles", () => {
    const input = dom.document.createElement("input");
    const card = cards.buildCard("设置", [null, input]);
    assert.equal(card.tagName, "SECTION"); assert.equal(card.className, "card");
    assert.equal(card.querySelector(".card__title").textContent, "设置");
    assert.equal(card.querySelector(".card__body").children[0], input);
    const empty = cards.buildCard("", [], "div"); assert.equal(empty.tagName, "DIV"); assert.equal(empty.querySelector(".card__title"), null);
  });
  test("card frames allow incremental updates without duplicate body nodes", () => {
    const { element, body } = cards.buildCardFrame("Hardware"); body.append(dom.document.createElement("p"));
    assert.equal(element.querySelector(".card__body"), body); assert.equal(element.querySelectorAll(".card__body").length, 1); assert.equal(body.children.length, 1);
  });
  test("text input emits only committed change values", () => {
    const values = []; const input = controls.buildTextInput("first", v => values.push(v), { type: "password", placeholder: "Key" });
    assert.equal(input.value, "first"); assert.equal(input.type, "password"); assert.equal(input.getAttribute("placeholder"), "Key");
    input.value = "second"; input.dispatchEvent(dom.makeEvent("input")); assert.deepEqual(values, []);
    input.dispatchEvent(dom.makeEvent("change")); assert.deepEqual(values, ["second"]);
  });
  test("caller-managed inputs do not require placeholder callbacks", () => {
    const input = controls.buildTextInput("query", undefined); input.dispatchEvent(dom.makeEvent("change"));
    const select = controls.buildSelect("GB", [["MB", "MB"], ["GB", "GB"]]); assert.equal(select.value, "GB"); select.dispatchEvent(dom.makeEvent("change"));
  });
  test("select uses actual option values, labels and selected state", () => {
    const values = []; const select = controls.buildSelect("zh", [["en", "English"], ["zh", "中文"]], v => values.push(v));
    assert.equal(select.value, "zh"); assert.equal(select.children[1].selected, true); assert.equal(select.children[1].textContent, "中文");
    select.value = "en"; select.dispatchEvent(dom.makeEvent("change")); assert.deepEqual(values, ["en"]);
    const empty = controls.buildSelect("", []); assert.equal(empty.children.length, 0);
  });
  test("radio groups keep one checked item and preserve explanatory badges", () => {
    const changes = []; const group = controls.buildRadioGroup("fast", [["fast", "快"], ["quality", "质量", "当前语言不可用"]], v => changes.push(v));
    assert.equal(group.getAttribute("role"), "radiogroup");
    const items = group.querySelectorAll(".radio"); assert.equal(items[0].getAttribute("aria-checked"), "true");
    assert.equal(items[1].querySelector(".radio__badge").textContent, "当前语言不可用"); assert.equal(items[1].disabled, false);
    items[1].click(); assert.deepEqual(changes, ["quality"]); assert.equal(items[0].getAttribute("aria-checked"), "false"); assert.equal(items[1].getAttribute("aria-checked"), "true");
    assert.equal(group.querySelectorAll(".is-checked").length, 1);
  });
  test("switch retains checkbox identity, labels and boolean changes", () => {
    const values = []; const root = controls.buildToggleSwitch(true, "同时录音", v => values.push(v));
    assert.equal(root.tagName, "LABEL"); assert.equal(root.className, "switch");
    const input = root.querySelector("input"); assert.equal(input.type, "checkbox"); assert.equal(input.checked, true);
    assert.ok(root.querySelector(".switch__track")); assert.equal(root.querySelector(".switch__label").textContent, "同时录音");
    input.checked = false; input.dispatchEvent(dom.makeEvent("change")); assert.deepEqual(values, [false]);
  });
  test("switch supports lifecycle-owned listeners without double delivery", () => {
    const root = controls.buildToggleSwitch(false, "Record"); const input = root.querySelector("input");
    let calls = 0; const handler = () => calls++; input.addEventListener("change", handler);
    input.dispatchEvent(dom.makeEvent("change")); input.removeEventListener("change", handler); input.dispatchEvent(dom.makeEvent("change")); assert.equal(calls, 1);
  });
  test("tab navigation updates selection before handing control to the page", () => {
    const changes = []; const nav = tabs.buildTabNav(["翻译", "语音", "关于"], index => { assert.equal(nav.children[index].getAttribute("aria-selected"), "true"); changes.push(index); });
    assert.equal(nav.getAttribute("role"), "tablist"); assert.equal(nav.className, "settings__nav");
    assert.deepEqual(changes, []); assert.equal(nav.children[0].getAttribute("aria-selected"), "true");
    nav.children[2].click(); nav.children[1].click(); assert.deepEqual(changes, [2, 1]); assert.equal(nav.querySelectorAll(".is-active").length, 1); assert.equal(nav.children[2].getAttribute("aria-selected"), "false");
  });
  test("independent tabs do not share selection state; empty tabs are safe", () => {
    const a = tabs.buildTabNav(["A", "B"], () => {}); const b = tabs.buildTabNav(["C", "D"], () => {}); a.children[1].click();
    assert.equal(b.children[0].getAttribute("aria-selected"), "true"); assert.equal(tabs.buildTabNav([], () => {}).children.length, 0);
  });
  test("UI primitives have no business or IPC dependencies", () => {
    for (const name of ["button", "card", "controls", "tab-nav", "multi-filter"]) {
      const source = readFileSync(join(ROOT, `src/renderer/ui/${name}.ts`), "utf8");
      assert.doesNotMatch(source, /from\s+["'][^"']*(?:store|protocol|views|i18n)["']|window\.voxsub/);
    }
  });
  test("renderer pages do not reimplement standard buttons, cards or tab navigation", () => {
    const renderer = join(ROOT, "src/renderer");
    const paths = [join(renderer, "index.ts"), ...readdirSync(join(renderer, "views")).filter(n => n.endsWith(".ts")).map(n => join(renderer, "views", n))];
    for (const path of paths) {
      const source = readFileSync(path, "utf8");
      assert.doesNotMatch(source, /h\("button",\s*\{[^}]*class:\s*["']btn(?: |["'])/s, path);
      assert.doesNotMatch(source, /h\("(?:section|div)",\s*\{\s*class:\s*"card"/s, path);
      assert.doesNotMatch(source, /h\("nav",\s*\{\s*class:\s*"settings__nav"/s, path);
    }
  });

  const makeFilter=(initial, onChange=()=>{})=>multi.buildMultiFilter([["WARNING","Warning"],["ERROR","Error"]],initial,{label:"Levels (select multiple)",allLabel:"All",onChange});
  const chip=(group,label)=>[...group.querySelectorAll("button")].find(b=>b.textContent===label);
  test("multi-filter exposes independent native pressed buttons, not radio options",()=>{
    let calls=0;const group=makeFilter(["WARNING","ERROR"],()=>calls++);
    assert.equal(group.getAttribute("role"),"group");assert.equal(group.getAttribute("aria-label"),"Levels (select multiple)");assert.equal(calls,0);
    for(const b of group.querySelectorAll("button")){assert.equal(b.type,"button");assert.equal(b.getAttribute("role"),null);assert.equal(b.getAttribute("aria-pressed"),"true");}
  });
  test("multi-filter toggles independently and keeps stable button identity",()=>{
    const changes=[];const group=makeFilter(["WARNING"],v=>changes.push([...v]));const error=chip(group,"Error");error.click();
    assert.deepEqual(changes.at(-1),["WARNING","ERROR"]);assert.equal(chip(group,"Warning").getAttribute("aria-pressed"),"true");
    error.click();assert.equal(chip(group,"Error"),error);assert.deepEqual(changes.at(-1),["WARNING"]);assert.equal(error.classList.contains("is-active"),false);
    assert.equal(chip(group,"All").getAttribute("aria-pressed"),"false");
  });
  test("zero selection is distinct from all; one-click restore emits only changes",()=>{
    const changes=[];const group=makeFilter(["WARNING"],v=>changes.push([...v]));chip(group,"Warning").click();assert.deepEqual(changes.at(-1),[]);
    assert.ok([...group.querySelectorAll("button")].every(b=>b.getAttribute("aria-pressed")==="false"));chip(group,"All").click();assert.deepEqual(changes.at(-1),["WARNING","ERROR"]);
    const n=changes.length;chip(group,"All").click();assert.equal(changes.length,n);
  });
  test("multi-filter state is isolated from initial arrays, callbacks and other instances",()=>{
    const initial=["WARNING"],received=[];const left=makeFilter(initial,v=>{received.push([...v]);v.length=0;}),right=makeFilter(["ERROR"]);
    chip(left,"Error").click();chip(left,"Warning").click();assert.deepEqual(initial,["WARNING"]);assert.deepEqual(received.at(-1),["ERROR"]);
    assert.equal(chip(right,"Warning").getAttribute("aria-pressed"),"false");assert.equal(chip(right,"Error").getAttribute("aria-pressed"),"true");
  });
  test("multi-filter normalizes duplicate/unknown values and keeps labels as plain text",()=>{
    const group=multi.buildMultiFilter([["X","<img src=x>"],["X","duplicate"]],["bad","X","X"],{label:"<label>",allLabel:"All",onChange:()=>{}});
    assert.equal(group.querySelectorAll("button").length,2);assert.equal(chip(group,"<img src=x>").children.length,0);assert.equal(chip(group,"All").getAttribute("aria-pressed"),"true");
  });
  test("empty multi-filter never claims all or emits a false change",()=>{
    let calls=0;const group=multi.buildMultiFilter([],[],{label:"Empty",allLabel:"All",onChange:()=>calls++});assert.equal(chip(group,"All").getAttribute("aria-pressed"),"false");chip(group,"All").click();assert.equal(calls,0);
  });

  console.log(`PASS shared UI ${passed}/${passed}`);
} finally { cleanupShared(); }
