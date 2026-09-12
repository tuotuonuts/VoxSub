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

function createMainWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1240,
    height: 820,
    minWidth: 1000,
    minHeight: 660,
    show: false,
    backgroundColor: "#101416",
    title: "语幕 VoxSub",
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
    },
  });

  void win.loadFile(path.join(RENDERER_DIR, "index.html"));
  win.once("ready-to-show", () => win.show());

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
    },
  });

  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // 时序是关键：必须在窗口**已经显示**之后再设捕获排除。
  // 实测（Electron 34 与 44 行为一致）：ready-to-show 阶段调用会返回成功
  // 但 GetWindowDisplayAffinity 读回 WDA_NONE，窗口成为无保护的暴露面。
  win.once("ready-to-show", () => {
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
  const image = nativeImage.createEmpty();
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

void app.whenReady().then(() => {
  // 移除 Electron 默认菜单栏（File/Edit/View/…）：本应用有自己的界面语言，
  // 残留的默认菜单会让成品显得未完成。快捷键改用 globalShortcut / 页面内绑定。
  Menu.setApplicationMenu(null);

  bridge = new BackendBridge();
  wireBackendEvents(bridge);

  mainWindow = createMainWindow();
  overlayWindow = createOverlayWindow();
  tray = createTray();
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
