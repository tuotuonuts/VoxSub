/**
 * 语幕 VoxSub — Electron 主进程
 *
 * 职责边界（与 PRODUCT.md 一致）：
 *  - 创建窗口（主窗 / 字幕浮窗 / 框选窗 / OCR 覆盖窗 / 托盘）
 *  - 通过 IPC 拉起并驱动现有 Python 后端（voxsub 包，一行不改）
 *  - 原生对话框、屏幕捕获等必须由主进程完成的能力
 *  - 不实现任何业务逻辑：ASR、翻译、OCR 识别、硬件探测全在 Python 侧
 */
import {
  app,
  BrowserWindow,
  dialog,
  globalShortcut,
  ipcMain,
  Menu,
  nativeImage,
  screen,
  shell,
  Tray,
} from "electron";
import * as fs from "node:fs";
import * as path from "node:path";
import { initialMainWindowBounds } from "../shared/window-layout";
import type { OverlayGlassState } from "../shared/overlay-glass";

import { VerifiedOverlaySurface } from "./overlay-verification";
import { probeNativeOverlay } from "./overlay-native";
import { validOverlayFrame, type OverlayFrame } from "../shared/overlay-frame";
import { LiveOcrSession } from "./ocr-live";
import { BackendBridge, type BackendEvent } from "./backend";
import { createShortcutService } from "./shortcuts";
let shortcuts: ReturnType<typeof createShortcutService> | null = null;
import { cancelScreenSelection, captureRegion, createOverlayForArea, pickScreenArea, type SelectionArea } from "./capture";

const RENDERER_DIR = path.join(__dirname, "..", "renderer");

let mainWindow: BrowserWindow | null = null;
let overlayWindow: BrowserWindow | null = null;
let ocrOverlayWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let bridge: BackendBridge | null = null;
let quitting = false;

/** Only native destruction races are recoverable here; programming errors propagate. */
function ignoreDestroyed(operation: string, action: () => void): boolean {
  try {
    action();
    return true;
  } catch (error) {
    if (!/^((TypeError|Error): )?Object has been destroyed$/.test(String(error))) throw error;
    console.warn(`[window-lifecycle] dropped ${operation}: Object has been destroyed`);
    return false;
  }
}

function sendToContents(contents: Electron.WebContents, channel: string, ...args: unknown[]): boolean {
  if (quitting) return false;
  let sent = false;
  ignoreDestroyed(channel, () => {
    if (!contents.isDestroyed()) { contents.send(channel, ...args); sent = true; }
  });
  return sent;
}

function sendToWindow(win: BrowserWindow | null, channel: string, ...args: unknown[]): boolean {
  if (quitting || !win) return false;
  let sent = false;
  ignoreDestroyed(channel, () => {
    if (!win.isDestroyed()) sent = sendToContents(win.webContents, channel, ...args);
  });
  return sent;
}

/** Capture one owner for the whole operation; false means skipped or destroyed. */
function useWindow(win: BrowserWindow | null, operation: string, action: (owner: BrowserWindow) => void): boolean {
  if (quitting || !win) return false;
  let applied = false;
  const survived = ignoreDestroyed(operation, () => {
    if (win.isDestroyed()) return;
    action(win);
    applied = true;
  });
  return survived && applied;
}

function showWindow(win: BrowserWindow | null): void {
  if (!HEADLESS) useWindow(win, "show", owner => owner.show());
}


let overlayClickThrough = false;
let overlayOpacity = 0.92;
let overlaySurface: VerifiedOverlaySurface | null = null;
const overlayFrames = new WeakMap<BrowserWindow, OverlayFrame>();
let overlayPlainFallback = false;
let overlayEvidenceLog = "";
let overlayGlassEnabled = false;
let overlayGlassStrength = 50;
let overlayGlassRevision = 0;
let overlayGlassState: OverlayGlassState | null = null;

function publishOverlayGlass(): OverlayGlassState {
  const supported = process.platform === "win32" && Number(process.getSystemVersion().split(".")[2]) >= 22621;
  const evidence=overlaySurface?.evidence ?? {active:false,clippingCheck:"not_run" as const,materialCheck:"not_run" as const,desktopCheck:"not_run" as const};
  overlayGlassState={enabled:overlayGlassEnabled,strength:overlayGlassStrength,settingsRevision:overlayGlassRevision,supported,...evidence,
    reason:!supported?"unsupported":overlayPlainFallback||evidence.fallbackReason?"unavailable":null};
  sendToWindow(overlayWindow,"overlay:glass",overlayGlassState);
  const signature=JSON.stringify([evidence.active,evidence.clippingCheck,evidence.materialCheck,evidence.fallbackReason]);
  if(signature!==overlayEvidenceLog){
    overlayEvidenceLog=signature;
    const message="[overlay:surface] "+JSON.stringify({...evidence,desktopCheck:"not_run"});
    sendToWindow(mainWindow,"backend:event",{type:"log",ts:new Date().toISOString(),level:evidence.fallbackReason?"WARNING":"INFO",message});
    if(bridge?.isRunning())void bridge.command("record_overlay_diagnostic",evidence).catch(()=>{ /* Diagnostics must not interrupt the surface fallback. */ });
  }
  return overlayGlassState;
}
function applyOverlayGlass(force=false): OverlayGlassState {
  if(process.platform!=="win32")return publishOverlayGlass();
  const supported=process.platform==="win32"&&Number(process.getSystemVersion().split(".")[2])>=22621;
  useWindow(overlayWindow,"overlay:glass",win=>{
    overlaySurface?.update(supported&&overlayGlassEnabled&&overlayGlassStrength>0&&!overlayPlainFallback,
      overlayFrames.get(win)??null,screen.getDisplayMatching(win.getBounds()).scaleFactor,force);
  });
  return publishOverlayGlass();
}
function replaceUnsafeOverlay(owner:BrowserWindow): void {
  if(quitting||overlayWindow!==owner||owner.isDestroyed()||overlayPlainFallback)return;
  const bounds=owner.getBounds(),wasVisible=owner.isVisible();
  overlayPlainFallback=true;overlaySurface?.dispose();
  const replacement=createOverlayWindow(wasVisible);overlayWindow=replacement;
  owner.destroy();replacement.setBounds(bounds);
  replacement.setIgnoreMouseEvents(overlayClickThrough, { forward: overlayClickThrough });
  replacement.webContents.once("did-finish-load",()=>{
    if(overlayWindow!==replacement||replacement.isDestroyed())return;
    sendToWindow(replacement,"overlay:click-through",overlayClickThrough);
    sendToWindow(replacement,"overlay:font-size",overlayFontSize);
    sendToWindow(replacement,"overlay:opacity",overlayOpacity);
    sendToWindow(replacement,"overlay:display-mode",overlayDisplayMode);
  });
}

let overlayFontSize = 20;
let overlayDisplayMode = "bilingual";
/** 每个操作只释放自己的退出保护，迟到/无 owner 的释放不能影响其他任务。 */
interface BusyOwner {
  reason: string;
  migration?: { instance: object; clientId: string; jobId?: string };
}
const busyOwners = new Map<string, BusyOwner>();
function busyReason(): string {
  return [...busyOwners.values()].map((owner) => owner.reason).join("\n");
}

/** Transfer within the existing guard map, synchronously, using only backend evidence. */
function associateMigration(owner: BusyOwner, jobId: unknown): void {
  const task = owner.migration;
  if (!task || typeof jobId !== "string" || !jobId || (task.jobId && task.jobId !== jobId)) return;
  task.jobId = jobId;
  for (const [key, previous] of busyOwners) {
    if (previous !== owner && previous.migration?.instance === task.instance &&
        previous.migration.jobId === jobId) busyOwners.delete(key);
  }
}

function observeMigrationJob(instance: object, raw: unknown): void {
  if (!raw || typeof raw !== "object") return;
  const job = raw as Record<string, unknown>;
  if (job["command"] !== "start_migration" || typeof job["jobId"] !== "string" || !job["jobId"]) return;
  const terminal = ["succeeded", "failed", "cancelled"].includes(String(job["status"]));
  for (const [key, owner] of busyOwners) {
    const task = owner.migration;
    if (!task || task.instance !== instance) continue;
    if (!task.jobId && task.clientId === job["clientMigrationId"]) associateMigration(owner, job["jobId"]);
    if (task.jobId === job["jobId"] && terminal) busyOwners.delete(key);
  }
}

async function guardedBackendCommand(source: BackendBridge, name: string, args: unknown) {
  const instance = source.instanceIdentity;
  const input = (args && typeof args === "object" ? args : {}) as Record<string, unknown>;
  const clientId = input["clientMigrationId"];
  const owner = name === "start_migration" && typeof clientId === "string" ? busyOwners.get(clientId) : undefined;
  if (owner && !owner.migration) owner.migration = { instance, clientId: clientId as string };
  const result = await source.command(name, args);
  // A terminal may have removed this owner while the receipt was in flight. Never resurrect it.
  if (owner && busyOwners.get(clientId as string) === owner && owner.migration?.instance === instance) {
    const data = result.data as { accepted?: unknown; jobId?: unknown } | undefined;
    if (result.ok && data?.accepted === true) associateMigration(owner, data.jobId);
    else if (result.code === "active_job_exists" && result.delivery === "response") associateMigration(owner, result.jobId);
    else if (result.delivery === "not_sent" || (result.delivery === "response" && !result.ok)) busyOwners.delete(clientId as string);
  }
  if (name === "job_status" && result.ok) {
    const data = result.data as { ok?: unknown; job?: Record<string, unknown> } | undefined;
    if (data?.ok === true && data.job?.["jobId"] === input["job_id"]) observeMigrationJob(instance, data.job);
  }
  return result;
}
/** 实时 OCR 定时器：仅在用户开启后运行 */
let liveOcrTimer: NodeJS.Timeout | null = null;
let liveOcrArea: SelectionArea | null = null;
let liveOcrSelection = 0;
const liveOcr = new LiveOcrSession({
  capture: captureRegion,
  command: (name, args) => bridge ? bridge.command(name, args) : Promise.resolve({ok: false}),
  remove: file => fs.rmSync(file, {force: true}),
  translated: payload => { sendToWindow(ocrOverlayWindow, "ocr:translated", payload); },
  failed: () => { sendToWindow(ocrOverlayWindow, "ocr:frame-failed"); },
  status: (state, details) => { sendToWindow(mainWindow, "ocr:live-status", {state, ...details}); },
});

/**
 * 退出请求的唯一入口：托盘退出、窗口关闭、快捷键都走这里。
 *
 * 有后台长任务（模型迁移等）时不允许直接退——中途退出会把多 GB 的
 * 模型库留在半路。改成把用户带回设置页并说明原因，与 Qt 版一致。
 */
function requestQuit(): boolean {
  const reason = busyReason();
  if (reason) {
    showWindow(mainWindow);
    sendToWindow(mainWindow, "app:open-page", "settings");
    sendToWindow(mainWindow, "app:blocking-task", { reason });
    return false;
  }
  quitting = true;
  shortcuts?.dispose();
  stopLiveOcr();
  bridge?.dispose();
  app.quit();
  return true;
}

/* ------------------------------------------------------------------ 窗口 */

/**
 * 无头模式：窗口创建但不显示，供自动化测试与后台任务使用。
 *
 * 为什么需要：开发期要反复启动应用做验证，而每次启动都会在用户桌面上弹窗、
 * 抢走焦点 —— 用户可能正在做别的事。无头模式下窗口存在、渲染照常、CDP 可连，
 * 但桌面上看不到任何东西、也不会抢焦点。
 *
 * 两个必须配套的开关：
 *   · backgroundThrottling: false —— 隐藏窗口会被 Chromium 节流渲染，
 *     布局测量会拿到陈旧值，测试结果就不可信了
 *   · 不建托盘 —— 托盘图标本身也是"出现在用户屏幕上"的东西
 */
const HEADLESS = process.env["VOXSUB_HEADLESS"] === "1";

/* ------------------------------------------------------------------ 图标 */

/**
 * 应用图标路径。
 *
 * 文件放在 dist/renderer/assets 下（构建时由 scripts/copy-assets.mjs 从仓库
 * 根的 assets/ 复制过来）。放在这个位置的原因是：打包后主进程代码位于
 * app.asar 内，读不到仓库根的 assets/，而 dist/renderer 会被一起打进 asar。
 *
 * 有两处必须用到它，缺任何一处用户都会看到"没有图标"：
 *   · BrowserWindow.icon —— 任务栏 / Alt+Tab 的窗口图标
 *   · Tray               —— 系统托盘图标
 * 之前两处都没设：窗口图标是 Electron 默认的，托盘图标是 createEmpty()（空白）。
 */
const APP_ICON = path.join(RENDERER_DIR, "assets", "icon.ico");

/** 读应用图标。读不到时返回 null，调用方退化为系统默认图标而不是崩掉。 */
function appIcon(): Electron.NativeImage | null {
  try {
    const image = nativeImage.createFromPath(APP_ICON);
    return image.isEmpty() ? null : image;
  } catch {
    return null;
  }
}

function createMainWindow(): BrowserWindow {
  // 图标要按需展开而不是直接写 `icon: appIcon() ?? undefined`：
  // tsconfig 开了 exactOptionalPropertyTypes，Electron 的类型不接受 undefined。
  const icon = appIcon();
  const win = new BrowserWindow({
    ...initialMainWindowBounds(screen.getDisplayNearestPoint(screen.getCursorScreenPoint()).workArea),
    show: false,
    backgroundColor: "#101416",
    title: "语幕 VoxSub",
    // 任务栏与 Alt+Tab 的窗口图标
    ...(icon ? { icon } : {}),
    // 自绘标题栏：默认原生标题栏会被 Windows 强调色染色（实测 #0078D4），
    // 与本产品的纸面配色冲突。hidden + overlay 保留系统的最小化/最大化/关闭按钮。
    titleBarStyle: "hidden",
    titleBarOverlay: {
      color: "#101416",
      symbolColor: "#EEF1F2",
      height: 34,
    },
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      // 隐藏窗口下 Chromium 会节流后台渲染；测布局时那会让读数失真
      backgroundThrottling: false,
    },
  });

  void win.loadFile(path.join(RENDERER_DIR, "index.html"));

  // 无头模式绝不 show()：窗口始终不可见，但渲染与 CDP 都正常
  if (!HEADLESS) {
    win.once("ready-to-show", () => { if (mainWindow === win) showWindow(win); });
  }

  // 开发模式：由启动器通过环境变量开启 DevTools。
  // 在 ready-to-show 之后开，否则拿到的是空窗口。
  if (process.env["VOXSUB_DEVTOOLS"] === "1" && !HEADLESS) {
    win.webContents.openDevTools({ mode: "detach" });
  }

  win.on("closed", () => { if (mainWindow === win) { shortcuts?.endCapture(); mainWindow = null; } });
  const endShortcutCapture = (): void => { if (mainWindow === win) shortcuts?.endCapture(); };
  win.on("blur", endShortcutCapture);
  win.on("minimize", endShortcutCapture);
  win.webContents.on("did-start-loading", endShortcutCapture);
  win.webContents.on("destroyed", endShortcutCapture);

  // 有后台长任务时拦一次关闭，避免把模型库留在半路
  win.on("close", (event) => {
    if (quitting || mainWindow !== win) return;
    if (busyReason()) {
      event.preventDefault();
      requestQuit();
    }
  });
  return win;
}

/**
 * 字幕浮窗 —— 本项目技术风险最高的窗口。
 *
 * 对照原 Qt 实现：subtitle_overlay.py / screen_capture.py:74
 * 窗口特性：半透明 / 点击穿透 / 置顶无边框 / 允许用户截图
 */
function createOverlayWindow(showWhenReady=true): BrowserWindow {
  const display = screen.getPrimaryDisplay();
  const { width, height } = display.workAreaSize;

  const win = new BrowserWindow({
    width: 860,
    height: 140,
    x: Math.round((width - 860) / 2),
    y: height - 220,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: true,
    hasShadow: false,
    show: false,
    focusable: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      backgroundThrottling: false,
    },
  });

  // Prevent the main window's same-origin zoom from changing native/CSS alignment.
  win.webContents.setZoomMode("isolated");
  win.webContents.setZoomFactor(1);
  const monitor=new VerifiedOverlaySurface(win,
    (shape,material)=>{if(!bridge) return Promise.reject(Error("backend_unavailable"));return probeNativeOverlay(bridge.resolvePython(),win.getNativeWindowHandle(),shape,material);},
    ()=>{if(overlayWindow===win)publishOverlayGlass();},()=>replaceUnsafeOverlay(win));
  overlaySurface=monitor;
  win.on("show",()=>{if(overlayWindow===win)applyOverlayGlass(true);});
  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // Subtitle content must remain available to user screenshots/recordings.
  // Capture exclusion belongs only to the OCR replacement overlay (capture.ts),
  // where it prevents OCR from reading its own translations. Never apply it here.
  // Headless acceptance still must not show any window or change its native state.
  win.once("ready-to-show", () => {
    if (HEADLESS || quitting || overlayWindow !== win || !showWhenReady) return;
    ignoreDestroyed("overlay:ready", () => {
      if (win.isDestroyed()) return;
      applyOverlayGlass();
      win.showInactive();
      win.setContentProtection(false);
    });
  });

  const refreshSurface = () => {
    if (!quitting && !HEADLESS && overlayWindow === win) applyOverlayGlass();
  };
  win.on("resize", refreshSurface);
  win.on("move", refreshSurface); // Includes crossing monitors with different scale factors.
  win.webContents.on("zoom-changed", refreshSurface);
  win.on("closed", () => { monitor.dispose(); if (overlayWindow === win) { overlayWindow = null; overlaySurface=null; } });
  void win.loadFile(path.join(RENDERER_DIR, "overlay.html"));
  return win;
}

function applyOverlayClickThrough(enabled: boolean): void {
  if (quitting) return;
  overlayClickThrough = enabled;
  useWindow(overlayWindow, "overlay:click-through", win => {
    win.setIgnoreMouseEvents(enabled, { forward: enabled });
    sendToWindow(win, "overlay:click-through", enabled);
  });
}

/* ------------------------------------------------------------------ 托盘 */

function createTray(): Tray | null {
  // 托盘必须有真实图标。原先用 nativeImage.createEmpty()，托盘区里是一个
  // 空白占位 —— 用户报告"系统托盘没有 icon"就是这个原因。
  const image = appIcon();
  if (!image) return null; // 读不到图标就不建托盘，免得留一个点不中的空白
  try {
    const created = new Tray(image);
    const send = (mode: string) => {
      sendToWindow(mainWindow, "app:tray-mode", mode);
      showWindow(mainWindow);
    };
    const menu = Menu.buildFromTemplate([
      { label: "显示主窗", click: () => showWindow(mainWindow) },
      { type: "separator" },
      { label: "A 麦克风同传", click: () => send("a") },
      { label: "B 系统声音", click: () => send("b") },
      { label: "C 音视频文件", click: () => send("c") },
      { label: "D 屏幕 OCR", click: () => send("d") },
      { type: "separator" },
      { label: "显示/隐藏浮窗", click: () => toggleOverlay() },
      { label: "设置", click: () => openPage("settings") },
      { label: "诊断", click: () => openPage("diagnostics") },
      { type: "separator" },
      { label: "退出", click: () => void requestQuit() },
    ]);
    created.setToolTip("语幕 VoxSub");
    created.setContextMenu(menu);
    created.on("double-click", () => showWindow(mainWindow));
    return created;
  } catch {
    return null; // 无托盘环境不应阻断启动
  }
}

function openPage(page: string): void {
  showWindow(mainWindow);
  sendToWindow(mainWindow, "app:open-page", page);
}

function toggleOverlay(): boolean {
  let visible = false;
  const applied = useWindow(overlayWindow, "overlay:toggle-visible", win => {
    if (win.isVisible()) win.hide();
    else win.showInactive();
    visible = win.isVisible();
  });
  return applied && visible;
}

/* ------------------------------------------------------------ 实时 OCR */

function stopLiveOcr(): void {
  cancelScreenSelection();
  if (liveOcrTimer) {
    clearInterval(liveOcrTimer);
    liveOcrTimer = null;
  }
  liveOcrArea = null;
  liveOcr.stop();
  liveOcrSelection++;
  if (bridge?.isRunning()) void bridge.command("ocr_release", {});
  sendToWindow(mainWindow, "ocr:live-status", {state: "stopped"});
  const oldOverlay = ocrOverlayWindow;
  ocrOverlayWindow = null;
  if (oldOverlay && !oldOverlay.isDestroyed()) oldOverlay.destroy();
}

function validOcrLanguages(value: unknown): value is {source: string; target: string} {
  if (!value || typeof value !== "object") return false;
  const pair = value as {source: unknown; target: unknown};
  return [pair.source, pair.target].every(lang => typeof lang === "string" && /^[a-z]{2,12}(?:-[A-Za-z]{2,8})?$/.test(lang));
}

async function runLiveOcrTick(): Promise<void> {
  await liveOcr.tick();
}

/* ------------------------------------------------------------------ IPC */

function registerIpc(): void {
  /* ---- 全局快捷键：仅主窗可配置，不暴露任意主进程操作 ---- */
  const owned = (event: Electron.IpcMainInvokeEvent): boolean => event.sender === mainWindow?.webContents && !quitting;
  ipcMain.handle("shortcuts:get", event => owned(event) ? shortcuts?.snapshot ?? null : null);
  ipcMain.handle("shortcuts:check", (event, bindings: unknown) => owned(event) ? shortcuts?.check(bindings) ?? null : null);
  ipcMain.handle("shortcuts:save", (event, bindings: unknown) => owned(event) ? shortcuts?.check(bindings, true) ?? null : null);
  ipcMain.handle("shortcuts:capture-start", (event, token: unknown) => owned(event) && typeof token === "string" && token.length <= 80 && token.length > 0 ? shortcuts?.beginCapture(token) ?? false : false);
  ipcMain.handle("shortcuts:capture-end", (event, token: unknown) => { if (owned(event) && typeof token === "string") shortcuts?.endCapture(token); });

  /* ---- 后端 ---- */
  ipcMain.handle("backend:start", (event) => {
    if (!bridge) return { ok: false, error: "后端未初始化" };
    // 后端已经在跑：说明这是**渲染层重载**后的重新连接（或第二个窗口来连）。
    // ready 事件只在后端启动时发过一次，早错过了；这里给发请求的这个渲染进程
    // 补发一条，界面才能把"已连接"和会话状态恢复回来（缺陷 #11 的渲染层重载部分）。
    const alreadyRunning = bridge.isRunning();
    const result = bridge.start();
    if (result.ok && alreadyRunning && lastReadyEvent) {
      sendToContents(event.sender, "backend:event", lastReadyEvent);
    }
    return result;
  });

  ipcMain.handle("backend:stop", () => {
    if (!bridge) return { ok: false, error: "后端未初始化" };
    return bridge.stop();
  });

  ipcMain.handle("backend:command", (_e, name: string, args: unknown) => {
    if (!bridge) return { ok: false, error: "后端未初始化" };
    if (["set_config", "set_translator", "set_langs"].includes(name)) {
      liveOcr.invalidate();
      sendToWindow(ocrOverlayWindow, "ocr:region-ready", liveOcrArea);
    }
    return guardedBackendCommand(bridge, name, args);
  });

  /* ---- 浮窗 ---- */
  ipcMain.handle("overlay:toggle-visible", () => toggleOverlay());

  ipcMain.handle("overlay:show", () => useWindow(overlayWindow, "overlay:show", win => win.showInactive()));

  ipcMain.handle("overlay:hide", () => useWindow(overlayWindow, "overlay:hide", win => win.hide()));

  ipcMain.handle("overlay:set-click-through", (_e, enabled: boolean) => {
    applyOverlayClickThrough(Boolean(enabled));
    return overlayClickThrough;
  });

  ipcMain.handle("overlay:is-click-through", () => overlayClickThrough);

  ipcMain.handle("overlay:frame", (event, frame: unknown) => {
    if (!overlayWindow || event.sender !== overlayWindow.webContents || !validOverlayFrame(frame)) return;
    overlayFrames.set(overlayWindow, frame); applyOverlayGlass();
  });
  ipcMain.handle("overlay:get-glass", () => overlayGlassState ?? applyOverlayGlass());
  ipcMain.handle("overlay:set-glass", (_e, enabled: unknown, strength: unknown) => {
    overlayGlassRevision++;
    if (overlayGlassEnabled !== (enabled === true)) { overlayPlainFallback=false;overlaySurface?.reset(); }
    overlayGlassEnabled = enabled === true;
    overlayGlassStrength = typeof strength === "number" && Number.isFinite(strength)
      ? Math.round(Math.min(100, Math.max(0, strength))) : 50;
    return applyOverlayGlass();
  });

  ipcMain.handle("overlay:set-opacity", (_e, value: number) => {
    overlayOpacity = typeof value === "number" && Number.isFinite(value)
      ? Math.min(1, Math.max(0.2, value)) : 0.92;
    sendToWindow(overlayWindow, "overlay:opacity", overlayOpacity);
    return overlayOpacity;
  });

  ipcMain.handle("overlay:set-font-size", (_e, value: number) => {
    overlayFontSize = Math.min(48, Math.max(12, Math.round(Number(value) || 20)));
    sendToWindow(overlayWindow, "overlay:font-size", overlayFontSize);
    return overlayFontSize;
  });

  ipcMain.handle("overlay:set-size", (_e, w: number, h: number) => {
    const width = Math.max(320, Math.round(w));
    const height = Math.max(64, Math.round(h));
    return useWindow(overlayWindow, "overlay:set-size", win => win.setSize(width, height))
      ? { width, height } : null;
  });

  ipcMain.handle("overlay:set-display-mode", (_e, mode: string) => {
    const allowed = ["source", "translation", "bilingual"];
    overlayDisplayMode = allowed.includes(mode) ? mode : "bilingual";
    sendToWindow(overlayWindow, "overlay:display-mode", overlayDisplayMode);
    return overlayDisplayMode;
  });

  /* ---- 对话框 ---- */
  ipcMain.handle("dialog:pick-media", async () => {
    const result = await dialog.showOpenDialog(mainWindow ?? undefined!, {
      title: "选择音频或视频文件",
      properties: ["openFile"],
      filters: [
        { name: "音视频", extensions: ["mp4", "mkv", "mov", "avi", "wmv", "flv", "webm", "mp3", "wav", "m4a", "aac", "flac", "ogg"] },
        { name: "全部文件", extensions: ["*"] },
      ],
    });
    return result.canceled || !result.filePaths[0] ? null : result.filePaths[0];
  });

  ipcMain.handle("dialog:pick-image", async () => {
    const result = await dialog.showOpenDialog(mainWindow ?? undefined!, {
      title: "选择要翻译的图片",
      properties: ["openFile"],
      filters: [
        { name: "图片", extensions: ["png", "jpg", "jpeg", "bmp", "webp", "tif", "tiff"] },
        { name: "全部文件", extensions: ["*"] },
      ],
    });
    return result.canceled || !result.filePaths[0] ? null : result.filePaths[0];
  });

  ipcMain.handle("dialog:pick-directory", async () => {
    const result = await dialog.showOpenDialog(mainWindow ?? undefined!, {
      title: "选择模型存储位置",
      properties: ["openDirectory", "createDirectory"],
    });
    return result.canceled || !result.filePaths[0] ? null : result.filePaths[0];
  });

  ipcMain.handle("dialog:save-image", async () => {
    const result = await dialog.showSaveDialog(mainWindow ?? undefined!, {
      title: "导出译后图片",
      defaultPath: `voxsub-ocr-${stamp()}.png`,
      filters: [
        { name: "PNG 图片", extensions: ["png"] },
        { name: "JPEG 图片", extensions: ["jpg", "jpeg"] },
      ],
    });
    return result.canceled || !result.filePath ? null : result.filePath;
  });

  ipcMain.handle("dialog:save-report", async () => {
    const result = await dialog.showSaveDialog(mainWindow ?? undefined!, {
      title: "导出诊断报告",
      defaultPath: `voxsub-diagnostics-${stamp()}.txt`,
      filters: [{ name: "文本", extensions: ["txt"] }],
    });
    return result.canceled || !result.filePath ? null : result.filePath;
  });

  ipcMain.handle("dialog:save-session", async (_e, payload: { lines: unknown[] }) => {
    const chosen = await dialog.showSaveDialog(mainWindow ?? undefined!, {
      title: "导出会话字幕",
      defaultPath: `voxsub-session-${stamp()}.srt`,
      filters: [
        { name: "SRT 字幕", extensions: ["srt"] },
        { name: "WebVTT 字幕", extensions: ["vtt"] },
        { name: "纯文本", extensions: ["txt"] },
      ],
    });
    if (chosen.canceled || !chosen.filePath) return null;
    // 写入交给后端（复用 voxsub.subtitles 的格式化与原子写入），主进程只负责选路径
    const result = await bridge?.command("export_subtitles", {
      path: chosen.filePath,
      lines: payload.lines,
    });
    return result?.ok ? chosen.filePath : null;
  });

  ipcMain.handle("dialog:reveal-in-folder", (_e, target: string) => {
    // 在资源管理器里定位文件：用户刚保存完录音/图片，最想做的就是找到它
    if (typeof target === "string" && target) {
      try {
        shell.showItemInFolder(target);
        return true;
      } catch {
        return false;
      }
    }
    return false;
  });

  /**
   * 用系统默认程序打开一个本地路径（目录或文件）。
   *
   * 为什么不复用 dialog:open-external：那个只放行 http/https，
   * 渲染层传 file:///D:/... 会被直接拒掉、返回 false，用户看到的就是
   * "点了打开文件夹没反应"。这是实际踩到的缺陷。
   *
   * 这里只接受**绝对**本地路径，并先确认它存在 —— 避免把任意字符串
   * 交给 shell（相对路径会被解释成相对于当前工作目录，容易打开意外位置）。
   */
  ipcMain.handle("dialog:open-path", async (_e, target: string) => {
    if (typeof target !== "string" || !target.trim()) return false;
    const resolved = path.resolve(target);
    if (!path.isAbsolute(resolved)) return false;
    if (!fs.existsSync(resolved)) return false;
    // openPath 返回空字符串表示成功，非空是错误描述
    const error = await shell.openPath(resolved);
    return error === "";
  });

  ipcMain.handle("dialog:open-external", async (_e, url: string) => {
    if (typeof url === "string" && /^https?:\/\//i.test(url)) {
      await shell.openExternal(url);
      return true;
    }
    return false;
  });

  /* ---- OCR ---- */
  ipcMain.handle("ocr:select-area", async (event) => {
    if (!owned(event)) return null;
    const area = await pickScreenArea(mainWindow);
    if (!area) return null;
    const owner = mainWindow;
    const visible = owner && !owner.isDestroyed() && owner.isVisible();
    try {
      if (visible) owner.hide();
      await new Promise(resolve => setTimeout(resolve, 220));
      const shot = await captureRegion(area);
      return shot?.path ?? null;
    } finally {
      if (visible && owner && !owner.isDestroyed()) owner.showInactive();
    }
  });

  ipcMain.handle("ocr:start-live-region", async (event, languages: {source: string; target: string}) => {
    if (!owned(event) || !validOcrLanguages(languages)) return null;
    stopLiveOcr();
    const selection = liveOcrSelection;
    const area = await pickScreenArea(mainWindow);
    if (!area || selection !== liveOcrSelection || quitting) return null;
    liveOcrArea = area;
    liveOcr.start(area, languages);
    const win = createOverlayForArea(area);
    ocrOverlayWindow = win;
    win.once("ready-to-show", () => {
      if (ocrOverlayWindow !== win || win.isDestroyed()) return;
      sendToWindow(win, "ocr:region-ready", area);
      // Wait for display/protection setup before the first capture; never poll a loading overlay.
      liveOcrTimer = setInterval(() => void runLiveOcrTick(), 700);
    });
    win.on("closed", () => {
      if (ocrOverlayWindow === win) stopLiveOcr();
    });
    return area;
  });

  ipcMain.handle("ocr:update-languages", (event, languages: {source: string; target: string}) => {
    if (!owned(event) || !validOcrLanguages(languages)) return false;
    liveOcr.configure(languages);
    sendToWindow(ocrOverlayWindow, "ocr:region-ready", liveOcrArea);
    return true;
  });
  ipcMain.handle("ocr:stop-live-region", (event) => {
    if (!owned(event)) return false;
    stopLiveOcr();
    return true;
  });

  /* ---- 应用能力 ---- */
  ipcMain.handle("app:capabilities", () => ({
    contentProtection: process.platform === "win32",
    platform: process.platform,
    versions: {
      electron: process.versions.electron,
      chrome: process.versions.chrome,
      node: process.versions.node,
    },
  }));

  /* ---- 退出保护 ---- */
  // 模型迁移是长任务（多 GB 文件搬动），中途退出会留下半个模型库。
  // Qt 版的做法：拦截退出、跳到设置页提示。这里保持同样语义。
  ipcMain.handle("app:request-quit", async () => requestQuit());

  ipcMain.handle("app:set-busy", (_e, busy: boolean, reason?: string, owner?: string) => {
    if (typeof owner !== "string" || !owner) return "";
    const previous = busyOwners.get(owner);
    if (busy && !previous) busyOwners.set(owner, { reason: reason || "正在执行后台任务" });
    // Once sent, only backend evidence can release a migration, never a renderer.
    else if (!busy && !previous?.migration) busyOwners.delete(owner);
    return busyOwners.get(owner)?.reason ?? "";
  });

  ipcMain.handle("app:busy-reason", () => busyReason());
}

function stamp(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

/* -------------------------------------------------------------- 生命周期 */

function wireBackendEvents(source: BackendBridge): void {
  source.onEvent((event: BackendEvent, instance: object) => {
    if (event.type === "disconnected") lastReadyEvent = null;
    if ((event as { type: string }).type === "job") observeMigrationJob(instance, event);
    // 记住最后一条 ready：渲染层重载（或换窗口）后重新连上时，原始的 ready
    // 事件早已错过，必须能补发 —— 否则界面的就绪门一直等不到事件，
    // 所有命令都要先干等 30 秒超时（表现为"刚重载完什么都点不动"）。
    if (event.type === "ready") lastReadyEvent = event;
    sendToWindow(mainWindow, "backend:event", event);
    sendToWindow(overlayWindow, "backend:event", event);
    // 实时 OCR 期间，把识别结果转发给覆盖窗
    sendToWindow(ocrOverlayWindow, "backend:event", event);
  });
}

/** 最后一条 ready 事件（用于渲染层重载后的补发）。 */
let lastReadyEvent: BackendEvent | null = null;

/**
 * 单实例锁 —— 防止双开。
 *
 * 不锁的后果（实测）：第二个实例会以一堆看不懂的错误退出：
 *   bind() returned an error: 每个套接字地址(协议/网络地址/端口)只允许使用一次
 *   Cannot start http server for devtools
 *   Unable to move the cache: 拒绝访问 / Unable to create cache
 * 根因是两个实例共用同一 userData 目录与调试端口，互相踩。
 *
 * 拿到锁的实例在收到 second-instance 事件时把已有窗口带到前台 ——
 * 但无头模式下不这么做：那正是要避免的"抢用户焦点"。
 */
const HAS_SINGLE_INSTANCE = app.requestSingleInstanceLock();

if (!HAS_SINGLE_INSTANCE) {
  // 已有实例在运行：立刻退出，不去争 userData 与端口
  app.quit();
} else {
  app.on("second-instance", () => {
    if (HEADLESS) return; // 无头模式不抢焦点
    useWindow(mainWindow, "second-instance", win => {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    });
  });
}

void app.whenReady().then(() => {
  // 第二个实例：不建任何窗口（app.quit() 已在上面发起）
  if (!HAS_SINGLE_INSTANCE || quitting) return;

  // Windows 任务栏身份。
  //
  // 必须与安装器的 appId 一致（electron-builder.config.cjs 的
  // com.voxsub.electron）。不设的话 Windows 会把进程当成 electron.exe 的
  // 一个无名实例，任务栏图标显示 Electron 默认图标、也无法正确归组与固定。
  app.setAppUserModelId("com.voxsub.electron");

  // 移除 Electron 默认菜单栏（File/Edit/View/…）：本应用有自己的界面语言，
  // 残留的默认菜单会让成品显得未完成。快捷键改用 globalShortcut / 页面内绑定。
  Menu.setApplicationMenu(null);

  bridge = new BackendBridge();
  wireBackendEvents(bridge);

  mainWindow = createMainWindow();
  overlayWindow = createOverlayWindow();
  tray = HEADLESS ? null : createTray();
  // 托盘建没建起来要能从日志里查。图标读不到时 createTray 返回 null，
  // 界面上只会表现为"托盘里没有图标"，没有任何报错 —— 不打这行就只能靠猜。
  if (!HEADLESS) {
    console.log(
      tray
        ? `[tray] 已创建，图标 ${APP_ICON}`
        : `[tray] 未创建：读不到图标 ${APP_ICON}`,
    );
  }
  shortcuts = createShortcutService(app.getPath("userData"), {
    command: async (name, args) => {
      const started = Date.now();
      const result = bridge ? await guardedBackendCommand(bridge, name, args) : { ok: false, unavailable: true, delivery: "not_sent" as const };
      if (name !== "state" && name !== "job_list") sendToWindow(mainWindow, "backend:event", {
        type: "log", ts: new Date().toISOString(), level: result.ok ? "INFO" : "WARNING",
        message: `[global-shortcut] command=${name} receipt=${result.ok ? "accepted" : result.delivery ?? "unconfirmed"} durationMs=${Date.now() - started}`,
      });
      return result;
    },
    allowed: () => !quitting && !busyReason() && !!bridge?.isRunning(),
    local: action => {
      if (action === "toggle_overlay") { if (!HEADLESS) toggleOverlay(); }
      else if (action === "toggle_click_through") applyOverlayClickThrough(!overlayClickThrough);
      else if (action === "toggle_window" && !HEADLESS && !quitting) {
        if (!mainWindow || mainWindow.isDestroyed()) mainWindow = createMainWindow();
        useWindow(mainWindow, "shortcut:window", win => {
          if (win.isVisible() && !win.isMinimized()) win.hide();
          else { if (win.isMinimized()) win.restore(); win.show(); win.focus(); }
        });
      }
    },
    notice: key => {
      sendToWindow(mainWindow, "shortcuts:notice", key);
      sendToWindow(mainWindow, "backend:event", { type: "log", ts: new Date().toISOString(), level: "INFO", message: "[global-shortcut] " + key });
    },
    state: data => {
      sendToWindow(mainWindow, "backend:event", { ...data, type: "state" });
      sendToWindow(overlayWindow, "backend:event", { ...data, type: "state" });
    },
  }, () => sendToWindow(mainWindow, "shortcuts:changed", shortcuts?.snapshot), globalShortcut);
  registerIpc();

  app.on("activate", () => {
    if (quitting) return;
    if (BrowserWindow.getAllWindows().length === 0) mainWindow = createMainWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") void requestQuit();
});

app.on("before-quit", (event) => {
  if (busyReason()) {
    event.preventDefault();
    requestQuit();
    return;
  }
  quitting = true;
  shortcuts?.dispose();
  stopLiveOcr();
  bridge?.dispose();
  tray?.destroy();
  tray = null;
});

// 清理截图临时文件（OcrWorkspace 的缓存策略：用完即删）
app.on("will-quit", () => {
  const dir = path.join(app.getPath("temp"), "voxsub-ocr");
  try {
    if (fs.existsSync(dir)) {
      for (const file of fs.readdirSync(dir)) {
        fs.rmSync(path.join(dir, file), { force: true });
      }
    }
  } catch {
    // 临时目录清理失败不影响退出
  }
});
