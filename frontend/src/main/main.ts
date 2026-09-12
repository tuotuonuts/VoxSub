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
  ipcMain,
  Menu,
  nativeImage,
  screen,
  shell,
  Tray,
} from "electron";
import * as fs from "node:fs";
import * as path from "node:path";

import { BackendBridge, type BackendEvent } from "./backend";
import { captureRegion, createOverlayForArea, pickScreenArea, type SelectionArea } from "./capture";

const RENDERER_DIR = path.join(__dirname, "..", "renderer");

let mainWindow: BrowserWindow | null = null;
let overlayWindow: BrowserWindow | null = null;
let ocrOverlayWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let bridge: BackendBridge | null = null;

let overlayClickThrough = false;
let overlayOpacity = 0.92;
let overlayFontSize = 20;
let overlayDisplayMode = "bilingual";
/** 后台长任务的原因（非空时阻止退出）。模型迁移/下载期间设置。 */
let busyReason = "";
/** 实时 OCR 定时器：仅在用户开启后运行 */
let liveOcrTimer: NodeJS.Timeout | null = null;
let liveOcrArea: SelectionArea | null = null;
let liveOcrBusy = false;

/**
 * 退出请求的唯一入口：托盘退出、窗口关闭、快捷键都走这里。
 *
 * 有后台长任务（模型迁移等）时不允许直接退——中途退出会把多 GB 的
 * 模型库留在半路。改成把用户带回设置页并说明原因，与 Qt 版一致。
 */
function requestQuit(): boolean {
  if (busyReason) {
    mainWindow?.show();
    mainWindow?.webContents.send("app:open-page", "settings");
    mainWindow?.webContents.send("app:blocking-task", { reason: busyReason });
    return false;
  }
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
    width: 1240,
    height: 820,
    minWidth: 1000,
    minHeight: 660,
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
    win.once("ready-to-show", () => win.show());
  }

  // 开发模式：由启动器通过环境变量开启 DevTools。
  // 在 ready-to-show 之后开，否则拿到的是空窗口。
  if (process.env["VOXSUB_DEVTOOLS"] === "1" && !HEADLESS) {
    win.webContents.openDevTools({ mode: "detach" });
  }

  // 有后台长任务时拦一次关闭，避免把模型库留在半路
  win.on("close", (event) => {
    if (busyReason) {
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
 * 四项硬能力：半透明 / 点击穿透 / 置顶无边框 / 捕获排除
 */
function createOverlayWindow(): BrowserWindow {
  const display = screen.getPrimaryDisplay();
  const { width, height } = display.workAreaSize;

  const win = new BrowserWindow({
    width: 860,
    height: 140,
    x: Math.round((width - 860) / 2),
    y: height - 220,
    frame: false,
    transparent: true,
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

  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // 时序是关键：必须在窗口**已经显示**之后再设捕获排除。
  // 实测（Electron 34 与 44 行为一致）：ready-to-show 阶段调用会返回成功
  // 但 GetWindowDisplayAffinity 读回 WDA_NONE，窗口成为无保护的暴露面。
  //
  // 无头模式例外：窗口不显示，也就没有"暴露面"可言，跳过整个流程 ——
  // 桌面上不该出现浮窗（它 alwaysOnTop，一旦显示就会压在用户所有窗口之上）。
  win.once("ready-to-show", () => {
    if (HEADLESS) return;
    win.showInactive();
    win.setContentProtection(true);
  });

  void win.loadFile(path.join(RENDERER_DIR, "overlay.html"));
  return win;
}

function applyOverlayClickThrough(enabled: boolean): void {
  overlayClickThrough = enabled;
  if (!overlayWindow) return;
  overlayWindow.setIgnoreMouseEvents(enabled, { forward: enabled });
  overlayWindow.webContents.send("overlay:click-through", enabled);
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
      mainWindow?.webContents.send("app:tray-mode", mode);
      mainWindow?.show();
    };
    const menu = Menu.buildFromTemplate([
      { label: "显示主窗", click: () => mainWindow?.show() },
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
    created.on("double-click", () => mainWindow?.show());
    return created;
  } catch {
    return null; // 无托盘环境不应阻断启动
  }
}

function openPage(page: string): void {
  mainWindow?.show();
  mainWindow?.webContents.send("app:open-page", page);
}

function toggleOverlay(): void {
  if (!overlayWindow) return;
  if (overlayWindow.isVisible()) overlayWindow.hide();
  else overlayWindow.showInactive();
}

/* ------------------------------------------------------------ 实时 OCR */

function stopLiveOcr(): void {
  if (liveOcrTimer) {
    clearInterval(liveOcrTimer);
    liveOcrTimer = null;
  }
  liveOcrArea = null;
  liveOcrBusy = false;
  if (ocrOverlayWindow && !ocrOverlayWindow.isDestroyed()) {
    ocrOverlayWindow.destroy();
  }
  ocrOverlayWindow = null;
}

async function runLiveOcrTick(): Promise<void> {
  // 只保留一个在途任务：慢任务期间丢弃重复帧，不让采集积压
  if (liveOcrBusy || !liveOcrArea || !mainWindow) return;
  liveOcrBusy = true;
  try {
    const shot = await captureRegion(liveOcrArea);
    if (!shot) return;
    mainWindow.webContents.send("ocr:live-frame", { path: shot.path });
  } catch {
    // 单帧失败不中断循环；下一帧继续
  } finally {
    liveOcrBusy = false;
  }
}

/* ------------------------------------------------------------------ IPC */

function registerIpc(): void {
  /* ---- 后端 ---- */
  ipcMain.handle("backend:start", () => {
    if (!bridge) return { ok: false, error: "后端未初始化" };
    return bridge.start();
  });

  ipcMain.handle("backend:stop", () => {
    if (!bridge) return { ok: false, error: "后端未初始化" };
    return bridge.stop();
  });

  ipcMain.handle("backend:command", (_e, name: string, args: unknown) => {
    if (!bridge) return { ok: false, error: "后端未初始化" };
    return bridge.command(name, args);
  });

  /* ---- 浮窗 ---- */
  ipcMain.handle("overlay:toggle-visible", () => {
    toggleOverlay();
    return overlayWindow?.isVisible() ?? false;
  });

  ipcMain.handle("overlay:show", () => {
    overlayWindow?.showInactive();
    return true;
  });

  ipcMain.handle("overlay:hide", () => {
    overlayWindow?.hide();
    return true;
  });

  ipcMain.handle("overlay:set-click-through", (_e, enabled: boolean) => {
    applyOverlayClickThrough(Boolean(enabled));
    return overlayClickThrough;
  });

  ipcMain.handle("overlay:is-click-through", () => overlayClickThrough);

  ipcMain.handle("overlay:set-opacity", (_e, value: number) => {
    overlayOpacity = Math.min(1, Math.max(0.2, Number(value) || 0.92));
    overlayWindow?.webContents.send("overlay:opacity", overlayOpacity);
    return overlayOpacity;
  });

  ipcMain.handle("overlay:set-font-size", (_e, value: number) => {
    overlayFontSize = Math.min(48, Math.max(12, Math.round(Number(value) || 20)));
    overlayWindow?.webContents.send("overlay:font-size", overlayFontSize);
    return overlayFontSize;
  });

  ipcMain.handle("overlay:set-size", (_e, w: number, h: number) => {
    if (!overlayWindow) return null;
    const width = Math.max(320, Math.round(w));
    const height = Math.max(64, Math.round(h));
    overlayWindow.setSize(width, height);
    return { width, height };
  });

  ipcMain.handle("overlay:set-display-mode", (_e, mode: string) => {
    const allowed = ["source", "translation", "bilingual"];
    overlayDisplayMode = allowed.includes(mode) ? mode : "bilingual";
    overlayWindow?.webContents.send("overlay:display-mode", overlayDisplayMode);
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
  ipcMain.handle("ocr:select-area", async () => {
    const area = await pickScreenArea(mainWindow);
    if (!area) return null;
    const shot = await captureRegion(area);
    return shot?.path ?? null;
  });

  ipcMain.handle("ocr:start-live-region", async () => {
    const area = await pickScreenArea(mainWindow);
    if (!area) return null;
    stopLiveOcr();
    liveOcrArea = area;
    ocrOverlayWindow = createOverlayForArea(area);
    ocrOverlayWindow.once("ready-to-show", () => {
      ocrOverlayWindow?.webContents.send("ocr:region-ready", area);
    });
    ocrOverlayWindow.on("closed", () => {
      ocrOverlayWindow = null;
    });
    // 轮询间隔与原 Qt 版一致（700ms），先比画面指纹再决定是否识别
    liveOcrTimer = setInterval(() => void runLiveOcrTick(), 700);
    return area;
  });

  ipcMain.handle("ocr:stop-live-region", () => {
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

  ipcMain.handle("app:set-busy", (_e, busy: boolean, reason?: string) => {
    busyReason = busy ? (reason ?? "正在执行后台任务") : "";
    return busyReason;
  });

  ipcMain.handle("app:busy-reason", () => busyReason);
}

function stamp(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

/* -------------------------------------------------------------- 生命周期 */

function wireBackendEvents(source: BackendBridge): void {
  source.onEvent((event: BackendEvent) => {
    mainWindow?.webContents.send("backend:event", event);
    overlayWindow?.webContents.send("backend:event", event);
    // 实时 OCR 期间，把识别结果转发给覆盖窗
    ocrOverlayWindow?.webContents.send("backend:event", event);
  });
}

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
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
}

void app.whenReady().then(() => {
  // 第二个实例：不建任何窗口（app.quit() 已在上面发起）
  if (!HAS_SINGLE_INSTANCE) return;

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
  registerIpc();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) mainWindow = createMainWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") void requestQuit();
});

app.on("before-quit", () => {
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
