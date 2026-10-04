/** Execute real main wiring with native-window/OS fixtures; never spawn Electron or capture audio. */
import assert from "node:assert/strict";
import vm from "node:vm";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import ts from "typescript";
import { importShared, cleanupShared, ROOT } from "./esbuild-ts.mjs";
const { createShortcutService } = await importShared("src/main/shortcuts.ts", { bundle: true });
const directory = fs.mkdtempSync(path.join(os.tmpdir(), "voxsub-shortcut-main-"));
assert.ok(path.resolve(directory).startsWith(path.join(path.resolve(os.tmpdir()), "voxsub-shortcut-main-")));
const handlers = new Map(), appEvents = new Map(), registered = new Map(), windows = [];
let ready, service, disposed = 0, count = 0;
class Window {
  constructor() { this.visible = false; this.minimized = false; this.events = new Map(); this.calls = []; this.webContents = { on() {}, isDestroyed: () => false, send() {} }; windows.push(this); }
  isDestroyed() { return false; } isVisible() { return this.visible; } isMinimized() { return this.minimized; }
  on(name, callback) { this.events.set(name, callback); } once(name, callback) { this.events.set(name, callback); }
  loadFile() { return Promise.resolve(); } setAlwaysOnTop() {} setVisibleOnAllWorkspaces() {} setContentProtection() {} setIgnoreMouseEvents() {}
  show() { this.visible = true; this.calls.push("show"); } hide() { this.visible = false; this.calls.push("hide"); }
  restore() { this.minimized = false; this.calls.push("restore"); } focus() { this.calls.push("focus"); }
}
class Backend { isRunning() { return true; } onEvent() {} dispose() { disposed++; } }
const driver = { register(key, fn) { if (registered.has(key)) return false; registered.set(key, fn); return true; }, unregister(key) { registered.delete(key); } };
const electron = {
  BrowserWindow: Window, globalShortcut: driver,
  app: { requestSingleInstanceLock: () => true, whenReady: () => ({ then: fn => { ready = fn; } }), on: (n, fn) => appEvents.set(n, fn), getPath: () => directory, setAppUserModelId() {}, quit() {} },
  nativeImage: { createFromPath: () => ({ isEmpty: () => true }) }, screen: { getPrimaryDisplay: () => ({ workAreaSize: { width: 1200, height: 800 } }) },
  Menu: { setApplicationMenu() {} }, ipcMain: { handle: (n, fn) => handlers.set(n, fn), on() {} },
};
const context = vm.createContext({ exports: {}, __dirname: path.join(ROOT, "src/main"), process: { env: { VOXSUB_HEADLESS: "0" }, platform: "win32" }, console: { ...console, log() {} }, setTimeout, clearTimeout, setInterval, clearInterval,
  require(name) {
    if (name === "electron") return electron;
    if (name === "node:fs") return { existsSync: () => false };
    if (name === "node:path") return path;
    if (name === "./backend") return { BackendBridge: Backend };
    if (name === "./capture") return {};
    if (name === "./shortcuts") return { createShortcutService(...args) { service = createShortcutService(...args); return service; } };
    throw Error("Unapproved import " + name);
  },
});
const delay = () => new Promise(r => setTimeout(r, 330));
function check(name, condition) { assert.ok(condition, name); count++; console.log("PASS " + name); }
try {
  const source = fs.readFileSync(path.join(ROOT, "src/main/main.ts"), "utf8");
  vm.runInContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, context); ready();
  const main = windows[0];
  check("app readiness initializes an empty service without OS registrations", service && registered.size === 0);
  check("overlay/foreign renderer cannot configure shortcuts", handlers.get("shortcuts:save")({ sender: windows[1].webContents }, {}) === null);
  const result = handlers.get("shortcuts:save")({ sender: main.webContents }, { toggle_window: "Ctrl+Alt+T" });
  check("main renderer saves through real manager and registration service", result.ok && registered.has("Ctrl+Alt+T"));
  main.visible = true; main.minimized = true; registered.get("Ctrl+Alt+T")();
  check("global callback restores a minimized window without renderer participation", main.calls.includes("restore") && main.calls.includes("show") && main.calls.includes("focus"));
  await delay(); registered.get("Ctrl+Alt+T")(); check("visible window hides to background and retains global registration", !main.visible && registered.size === 1);
  await delay(); registered.get("Ctrl+Alt+T")(); check("hidden/tray-style window is brought back by main callback", main.visible && registered.size === 1);
  main.events.get("closed")(); await delay(); registered.get("Ctrl+Alt+T")();
  const replacement = windows[2]; check("tray shortcut recreates a closed main window", replacement?.visible && registered.size === 1);
  const token = "fixture-capture"; assert.equal(handlers.get("shortcuts:capture-start")({ sender: replacement.webContents }, token), true);
  main.events.get("blur")(); check("stale window blur cannot release replacement capture", service.snapshot.capturing);
  replacement.events.get("minimize")(); check("minimizing cancels capture suspension and restores registration", !service.snapshot.capturing && registered.size === 1);
  appEvents.get("before-quit")({ preventDefault() { throw Error("unexpected exit guard"); } });
  check("real before-quit unregisters owned shortcuts and disposes backend", registered.size === 0 && disposed === 1);
  console.log(`PASS shortcut main wiring ${count}/${count}`);
} finally { service?.dispose(); for (const file of fs.readdirSync(directory)) fs.unlinkSync(path.join(directory, file)); fs.rmdirSync(directory); cleanupShared(); }
