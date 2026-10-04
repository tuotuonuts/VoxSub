import assert from "node:assert/strict";
import { installMiniDom } from "./mini-dom.mjs";
import { importShared, cleanupShared } from "./esbuild-ts.mjs";
const dom = installMiniDom();
const { buildShortcutSettings, buildShortcutField, ShortcutManager, emptyBindings, setLanguage } = await importShared("tools/test-shortcut-ui-entry.ts", { bundle: true });
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
const delay = ms => new Promise(r => setTimeout(r, ms));
let count = 0;
async function test(name, fn) { await fn(); count++; console.log("PASS " + name); }
function viewFixture(initial = emptyBindings()) {
  const listeners = new Set(), registered = new Map(), writes = [], blocked = new Set();
  const manager = new ShortcutManager({ register(key, callback) { if (blocked.has(key)) return false; registered.set(key, callback); return true; }, unregister(key) { registered.delete(key); } }, b => writes.push(b), () => { throw Error("UI tests must not trigger operations"); }, () => { for (const listener of listeners) listener(manager.snapshot); });
  manager.load(initial);
  dom.window.voxsub = { shortcuts: {
    get: async () => manager.snapshot, check: async b => manager.check(b), save: async b => manager.check(b, true),
    beginCapture: async t => manager.beginCapture(t), endCapture: async t => manager.endCapture(t),
    onChanged(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  } };
  const page = buildShortcutSettings(); dom.mount(page.element);
  return { manager, registered, blocked, writes, page, listeners, close() { page.dispose(); page.element.remove(); manager.dispose(); } };
}
const button = (root, label) => [...root.querySelectorAll("button")].find(n => n.textContent === label);
const key = (type, extra = {}) => dom.makeEvent(type, { key: "T", code: "KeyT", ctrlKey: true, altKey: true, shiftKey: false, metaKey: false, repeat: false, stopPropagation() {}, ...extra });
try {
  await test("settings render seven empty bindings and do not register/write by default", async () => {
    const f = viewFixture(); await settle(); assert.equal(f.page.element.querySelectorAll("input").length, 7); assert.equal(f.registered.size, 0); assert.equal(f.writes.length, 0); assert.ok([...f.page.element.querySelectorAll("input")].every(n => n.value === "")); f.close();
  });
  await test("manual editing detects duplicates before saving and identifies both conflicting actions", async () => {
    const f = viewFixture(); await settle(); const inputs = f.page.element.querySelectorAll("input");
    inputs[0].value = "Ctrl+Alt+T"; inputs[0].dispatchEvent(dom.makeEvent("change")); inputs[1].value = "Alt+Control+T"; inputs[1].dispatchEvent(dom.makeEvent("change"));
    assert.match(f.page.element.textContent, /与“开始 \/ 结束当前会话”重复/); assert.match(f.page.element.textContent, /与“显示 \/ 隐藏主窗”重复/);
    button(f.page.element, "保存快捷键").click(); await settle(); assert.equal(f.writes.length, 0); assert.match(f.page.element.textContent, /未保存/); f.close();
  });
  await test("occupied combinations are probed automatically without changing saved registrations", async () => {
    const f = viewFixture(); await settle(); f.blocked.add("Ctrl+Alt+T"); const input = f.page.element.querySelector("input"); input.value = "Ctrl+Alt+T"; input.dispatchEvent(dom.makeEvent("change"));
    await delay(200); assert.match(f.page.element.textContent, /系统无法注册/); assert.equal(f.writes.length, 0); assert.equal(f.registered.size, 0); f.close();
  });
  await test("explicit Save enables bindings, clear-all is only a draft until saved", async () => {
    const f = viewFixture(); await settle(); const input = f.page.element.querySelector("input"); input.value = "ctrl+alt+t"; input.dispatchEvent(dom.makeEvent("change")); button(f.page.element, "保存快捷键").click(); await settle();
    assert.equal(input.value, "Ctrl+Alt+T"); assert.equal(f.writes.length, 1); assert.equal(f.registered.size, 1); assert.match(f.page.element.textContent, /已保存并生效/);
    button(f.page.element, "清空全部快捷键").click(); assert.equal(f.registered.size, 1); button(f.page.element, "保存快捷键").click(); await settle(); assert.equal(f.registered.size, 0); f.close();
  });
  await test("capture really suspends registered shortcuts and survives its own state broadcast", async () => {
    const f = viewFixture({ ...emptyBindings(), toggle_window: "Ctrl+Alt+A" }); await settle();
    const field = f.page.element.querySelector(".field"); button(field, "录入快捷键").click(); await settle();
    assert.equal(f.manager.snapshot.capturing, true); assert.equal(f.registered.size, 0); assert.equal(button(f.page.element, "保存快捷键").disabled, true);
    const input = field.querySelector("input"); input.dispatchEvent(key("keydown")); input.dispatchEvent(key("keyup")); await settle();
    assert.equal(f.manager.snapshot.capturing, false); assert.equal(input.value, "Ctrl+Alt+T"); assert.equal(f.manager.snapshot.bindings.toggle_window, "Ctrl+Alt+A"); assert.equal(f.writes.length, 0); assert.equal(button(f.page.element, "保存快捷键").disabled, false); f.close();
  });
  await test("Esc cancels capture without committing a draft or losing old registration", async () => {
    const f = viewFixture({ ...emptyBindings(), toggle_window: "Ctrl+Alt+A" }); await settle(); const field = f.page.element.querySelector(".field"); button(field, "录入快捷键").click(); await settle();
    field.querySelector("input").dispatchEvent(key("keydown", { key: "Escape", code: "Escape", ctrlKey: false, altKey: false })); await settle(); assert.equal(field.querySelector("input").value, "Ctrl+Alt+A"); assert.equal(f.registered.size, 1); assert.equal(f.writes.length, 0); f.close();
  });
  await test("closing settings during capture resumes shortcuts and removes its subscription", async () => {
    const f = viewFixture({ ...emptyBindings(), toggle_window: "Ctrl+Alt+A" }); await settle(); button(f.page.element.querySelector(".field"), "录入快捷键").click(); await settle(); f.page.dispose(); await settle();
    assert.equal(f.manager.snapshot.capturing, false); assert.equal(f.registered.size, 1); assert.equal(f.listeners.size, 0); f.close();
  });
  await test("main-window blur interruption cancels recorder and restores normal editing", async () => {
    const f = viewFixture(); await settle(); const field = f.page.element.querySelector(".field"); button(field, "录入快捷键").click(); await settle(); f.manager.endCapture(); await settle();
    assert.equal(field.querySelector("input").readOnly, false); assert.ok(button(field, "录入快捷键")); assert.equal(button(f.page.element, "保存快捷键").disabled, false); f.close();
  });
  await test("late capture permission cannot reactivate a disposed component", async () => {
    let resolve, starts = 0, ends = 0;
    const component = buildShortcutField({ label: "Test", value: "", translate: k => k, changed() { throw Error("late change"); }, beginCapture: () => { starts++; return new Promise(r => { resolve = r; }); }, endCapture: async () => { ends++; } });
    button(component.element, "录入快捷键").click(); component.dispose(); resolve(true); await settle(); assert.equal(starts, 1); assert.ok(ends >= 1); assert.equal(component.isCapturing, false);
  });
  await test("keys are ignored until capture permission arrives", async () => {
    let resolve; const values = [];
    const component = buildShortcutField({ label: "Test", value: "", translate: k => k, changed: v => values.push(v), beginCapture: () => new Promise(r => { resolve = r; }), endCapture: async () => {} });
    button(component.element, "录入快捷键").click(); const input = component.element.querySelector("input"); input.dispatchEvent(key("keydown")); input.dispatchEvent(key("keyup")); assert.deepEqual(values, []); resolve(true); await settle(); component.dispose();
  });
  await test("English controls are localized and expose no technical action IDs", async () => {
    setLanguage("en"); const f = viewFixture(); await settle(); assert.match(f.page.element.textContent, /Global shortcuts/); assert.ok(button(f.page.element, "Save shortcuts")); assert.match(f.page.element.textContent, /Start \/ end current session/); assert.doesNotMatch(f.page.element.textContent, /toggle_session|[\u3400-\u9fff]/); f.close(); setLanguage("zh");
  });
  console.log(`PASS shortcut UI ${count}/${count}`);
} finally { cleanupShared(); }
