import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { importShared, cleanupShared } from "./esbuild-ts.mjs";
const [contract, managers, actions, services] = await importShared([
  "src/shared/shortcuts.ts", "src/main/shortcut-manager.ts", "src/main/shortcut-actions.ts", "src/main/shortcuts.ts",
], { bundle: true });
const { emptyBindings, validateBindings, normalizeAccelerator, acceleratorFromKey } = contract;
let count = 0;
async function test(name, fn) { await fn(); count++; console.log("PASS " + name); }
function fixture() {
  const registered = new Map(), blocked = new Set(), executed = [], writes = [];
  let writeError = false;
  const driver = { register(key, fn) { if (blocked.has(key) || registered.has(key)) return false; registered.set(key, fn); return true; }, unregister(key) { registered.delete(key); } };
  const manager = new managers.ShortcutManager(driver, bindings => { if (writeError) throw Error("fixture denied"); writes.push({ ...bindings }); }, a => executed.push(a));
  manager.load(emptyBindings());
  return { manager, registered, blocked, executed, writes, driver, denyWrite: () => { writeError = true; } };
}
const binding = (action, key) => ({ ...emptyBindings(), [action]: key });
function runnerFixture(state = { mode: "a", running: false, paused: false, recordingEnabled: false, recordingSupported: true, recordingCanChange: true }) {
  const calls = [], notices = [], states = [], local = [];
  let active = [], allowed = true, fail = null, deferred = null;
  const runner = new actions.ShortcutActions({
    allowed: () => allowed, local: action => local.push(action), notice: text => notices.push(text), state: data => states.push(data),
    async command(name, args) { calls.push({ name, args }); if (deferred) return deferred;
      if (name === "job_list") return { ok: true, data: { active } };
      if (name === "state") return { ok: true, data: state };
      return fail ?? { ok: true, data: { accepted: true, jobId: "fixture" } };
    },
  });
  return { runner, calls, notices, states, local, active: value => { active = value; }, allowed: value => { allowed = value; }, fail: value => { fail = value; }, defer: value => { deferred = value; } };
}
try {
  await test("all seven shortcuts are empty by default; nothing is registered or written", () => {
    const f = fixture(); assert.equal(Object.keys(emptyBindings()).length, 7); assert.ok(Object.values(emptyBindings()).every(v => v === "")); assert.equal(f.registered.size, 0); assert.equal(f.writes.length, 0);
  });
  await test("modifier order, case, whitespace and aliases normalize identically", () => {
    for (const value of ["Control+alt+t", " alt + CTRL + T ", "Ctrl+Alt+T"]) assert.equal(normalizeAccelerator(value), "Ctrl+Alt+T");
    assert.equal(normalizeAccelerator("Shift+Ctrl+ArrowLeft"), "Ctrl+Shift+Left"); assert.equal(normalizeAccelerator("Ctrl+F24"), "Ctrl+F24");
  });
  await test("plain keys, modifier-only keys, unknown keys, duplicates and prototype keys are rejected", () => {
    for (const value of ["A", "Shift+A", "Ctrl", "Ctrl+Ctrl+A", "Ctrl++", "Ctrl+F25", "Win+L", "constructor+T", "Ctrl+__proto__"]) assert.equal(normalizeAccelerator(value), null, value);
    assert.equal(validateBindings(null).issues[0].code, "invalid"); assert.equal(validateBindings({ toggle_window: 4 }).issues[0].code, "invalid");
  });
  await test("reserved system shortcuts and application duplicates are detected", () => {
    assert.equal(validateBindings(binding("toggle_window", "Alt+F4")).issues[0].code, "reserved");
    assert.equal(validateBindings(binding("toggle_window", "Ctrl+Alt+Shift+Delete")).issues[0].code, "reserved");
    const result = validateBindings({ ...emptyBindings(), toggle_window: "Control+alt+t", toggle_overlay: "ALT+CTRL+T" });
    assert.equal(result.issues[0].code, "duplicate"); assert.equal(result.issues[0].other, "toggle_window");
  });
  await test("key capture supports modifiers and IME physical fallback without accepting Windows chords", () => {
    const event = { key: "文", code: "KeyT", ctrlKey: true, altKey: true, shiftKey: false, metaKey: false };
    assert.equal(acceleratorFromKey(event), "Ctrl+Alt+T"); assert.equal(acceleratorFromKey({ ...event, metaKey: true }), null);
  });
  await test("conflict probing never saves or changes existing registrations", () => {
    const f = fixture(); assert.ok(f.manager.check(binding("toggle_window", "Ctrl+Alt+A"), true).ok);
    const callback = f.registered.get("Ctrl+Alt+A"); assert.ok(f.manager.check(binding("toggle_overlay", "Ctrl+Alt+B")).ok);
    assert.deepEqual([...f.registered.keys()], ["Ctrl+Alt+A"]); assert.equal(f.registered.get("Ctrl+Alt+A"), callback); assert.equal(f.writes.length, 1);
  });
  await test("system registration rejection rolls back every staged key and preserves old bindings", () => {
    const f = fixture(); f.manager.check(binding("toggle_window", "Ctrl+Alt+A"), true); f.blocked.add("Ctrl+Alt+C");
    const result = f.manager.check({ ...emptyBindings(), toggle_window: "Ctrl+Alt+B", toggle_overlay: "Ctrl+Alt+C" }, true);
    assert.equal(result.ok, false); assert.equal(result.issues[0].code, "occupied"); assert.deepEqual([...f.registered.keys()], ["Ctrl+Alt+A"]); assert.equal(f.writes.length, 1); assert.equal(f.manager.snapshot.bindings.toggle_window, "Ctrl+Alt+A");
  });
  await test("persistence failure leaves the old registration and old settings intact", () => {
    const f = fixture(); f.manager.check(binding("toggle_window", "Ctrl+Alt+A"), true); f.denyWrite();
    const result = f.manager.check(binding("toggle_overlay", "Ctrl+Alt+B"), true); assert.equal(result.ok, false); assert.equal(result.issues[0].code, "storage"); assert.deepEqual([...f.registered.keys()], ["Ctrl+Alt+A"]);
  });
  await test("bindings can be swapped between actions without temporarily unregistering either key", () => {
    const f = fixture(); f.manager.check({ ...emptyBindings(), toggle_window: "Ctrl+Alt+A", toggle_overlay: "Ctrl+Alt+B" }, true);
    const a = f.registered.get("Ctrl+Alt+A"), b = f.registered.get("Ctrl+Alt+B");
    assert.ok(f.manager.check({ ...emptyBindings(), toggle_window: "Ctrl+Alt+B", toggle_overlay: "Ctrl+Alt+A" }, true).ok);
    assert.equal(f.registered.get("Ctrl+Alt+A"), a); assert.equal(f.registered.get("Ctrl+Alt+B"), b); a(); assert.deepEqual(f.executed, ["toggle_overlay"]);
  });
  await test("capture suspends execution, stale tokens cannot end another capture, resume suppresses repeats", () => {
    const f = fixture(); f.manager.check(binding("toggle_window", "Ctrl+Alt+A"), true); const callback = f.registered.get("Ctrl+Alt+A");
    assert.ok(f.manager.beginCapture("one")); assert.equal(f.registered.size, 0); callback(); assert.deepEqual(f.executed, []);
    assert.equal(f.manager.beginCapture("two"), false); f.manager.endCapture("two"); assert.equal(f.manager.snapshot.capturing, true);
    assert.equal(f.manager.check(emptyBindings(), true).issues[0].code, "capturing"); f.manager.endCapture("one"); assert.equal(f.manager.snapshot.capturing, false);
    f.registered.get("Ctrl+Alt+A")(); assert.deepEqual(f.executed, []);
  });
  await test("registration stolen during capture is surfaced as inactive, not enabled", () => {
    const f = fixture(); f.manager.check(binding("toggle_window", "Ctrl+Alt+A"), true); f.manager.beginCapture("one"); f.blocked.add("Ctrl+Alt+A"); f.manager.endCapture("one");
    assert.equal(f.manager.snapshot.active.length, 0); assert.equal(f.manager.snapshot.issues[0].code, "occupied"); assert.equal(f.manager.snapshot.bindings.toggle_window, "Ctrl+Alt+A");
  });
  await test("startup reports malformed data and unavailable keys without assigning replacement keys", () => {
    const f = fixture(); f.manager.load({ unexpected: "Ctrl+Alt+U" }); assert.equal(f.manager.snapshot.issues[0].code, "storage"); assert.equal(f.registered.size, 0);
    f.blocked.add("Ctrl+Alt+A"); f.manager.load(binding("toggle_window", "Ctrl+Alt+A")); assert.equal(f.manager.snapshot.issues[0].code, "occupied"); assert.equal(f.writes.length, 0);
  });
  await test("clearing bindings unregisters them, and disposal does not unregister unrelated registrations", () => {
    const f = fixture(); f.manager.check(binding("toggle_window", "Ctrl+Alt+A"), true); assert.ok(f.manager.check(emptyBindings(), true).ok); assert.equal(f.registered.size, 0);
    f.registered.set("foreign", () => {}); f.manager.check(binding("toggle_window", "Ctrl+Alt+B"), true); f.manager.dispose(); assert.deepEqual([...f.registered.keys()], ["foreign"]);
  });
  await test("session toggle uses fresh backend state, not renderer state", async () => {
    const f = runnerFixture(); await f.runner.run("toggle_session"); assert.equal(f.calls[2].name, "start"); assert.equal(f.calls[2].args.async, true); assert.equal(f.states.length, 1);
    const running = runnerFixture({ mode: "b", running: true, paused: false }); await running.runner.run("toggle_session"); assert.equal(running.calls[2].name, "stop");
  });
  await test("pause/resume routes from backend flags and file mode is not incorrectly paused", async () => {
    const f = runnerFixture({ mode: "b", running: true, paused: true }); await f.runner.run("pause_session"); assert.equal(f.calls[2].name, "resume");
    const file = runnerFixture({ mode: "c", running: true, paused: false }); await file.runner.run("pause_session"); assert.equal(file.calls.length, 2); assert.match(file.notices[0], /不支持/);
  });
  await test("recording shortcut honors backend capability and never starts capture", async () => {
    const f = runnerFixture(); await f.runner.run("toggle_recording"); assert.equal(f.calls[2].name, "set_recording"); assert.equal(f.calls[2].args.enabled, true); assert.equal(f.calls.some(c => c.name === "start"), false);
    const locked = runnerFixture({ mode: "b", running: true, paused: false, recordingEnabled: true, recordingSupported: true, recordingCanChange: false }); await locked.runner.run("toggle_recording"); assert.equal(locked.calls.length, 2);
  });
  await test("busy operations, OCR and malformed state fail closed", async () => {
    const f = runnerFixture(); f.active(["start"]); await f.runner.run("toggle_session"); assert.equal(f.calls.length, 1);
    const d = runnerFixture({ mode: "d", running: false, paused: false }); await d.runner.run("toggle_session"); assert.equal(d.calls.length, 2);
    const bad = runnerFixture({ mode: "x", running: false, paused: false }); await bad.runner.run("toggle_session"); assert.equal(bad.calls.length, 2);
  });
  await test("no active session means stop does not issue a backend mutation", async () => {
    const f = runnerFixture(); await f.runner.run("stop_session"); assert.equal(f.calls.length, 2); assert.match(f.notices[0], /没有运行/);
  });
  await test("unconfirmed command receipt is not reported as successful execution", async () => {
    const f = runnerFixture(); f.fail({ ok: false, delivery: "unknown", timedOut: true }); await f.runner.run("toggle_session"); assert.match(f.notices[0], /未确认/); assert.equal(f.states.length, 0);
  });
  await test("holding/pressing repeatedly cannot enqueue duplicate session transitions", async () => {
    const f = runnerFixture(); let resolve; const gate = new Promise(r => { resolve = r; }); f.defer(gate); const first = f.runner.run("toggle_session"); await f.runner.run("toggle_session"); assert.equal(f.calls.length, 1); resolve({ ok: false }); await first;
  });
  await test("window/overlay actions execute in main process even while session work is pending", async () => {
    const f = runnerFixture(); f.allowed(false); for (const a of ["toggle_window", "toggle_overlay", "toggle_click_through"]) await f.runner.run(a); assert.equal(f.calls.length, 0); assert.equal(f.local.length, 3);
  });
  await test("atomic settings survive service restart; absent defaults do not create a settings file", () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "voxsub-shortcuts-test-"));
    assert.ok(path.resolve(directory).startsWith(path.join(path.resolve(os.tmpdir()), "voxsub-shortcuts-test-")));
    const deps = { allowed: () => false, command: async () => ({ ok: false }), local() {}, notice() {}, state() {} };
    try {
      const f = fixture(); const service = services.createShortcutService(directory, deps, () => {}, f.driver);
      const file = path.join(directory, "global-shortcuts.json"); assert.equal(fs.existsSync(file), false);
      assert.ok(service.check(binding("toggle_window", "Ctrl+Alt+A"), true).ok); service.dispose();
      assert.equal(JSON.parse(fs.readFileSync(file, "utf8")).bindings.toggle_window, "Ctrl+Alt+A");
      const restored = services.createShortcutService(directory, deps, () => {}, f.driver); assert.deepEqual(restored.snapshot.active, ["toggle_window"]); restored.dispose();
      fs.writeFileSync(file, "broken"); const broken = services.createShortcutService(directory, deps, () => {}, f.driver); assert.equal(broken.snapshot.issues[0].code, "storage"); assert.equal(fs.readFileSync(file, "utf8"), "broken"); broken.dispose();
    } finally { for (const file of fs.readdirSync(directory)) fs.unlinkSync(path.join(directory, file)); fs.rmdirSync(directory); }
  });
  console.log(`PASS shortcuts core ${count}/${count}`);
} finally { cleanupShared(); }
