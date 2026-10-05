/**
 * 屏幕捕获与 OCR 区域框选。
 *
 * 对照原 Qt 实现：
 *   screen_capture.py  多显示器框选、高 DPI 截图、捕获排除
 *   ocr_overlay.py     译文原位覆盖窗
 *
 * Windows 要点：
 *   · 框选前必须等主窗从 DWM 合成帧消失，否则会截到自己的虚影
 *   · 覆盖窗用 setContentProtection 排除，防止被下一轮截图再次识别
 */
import { BrowserWindow, desktopCapturer, screen } from "electron";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { bestDisplay, intersect, thumbnailRect, virtualBounds } from "../shared/ocr-geometry";
import { randomUUID } from "node:crypto";
import { protectWindow, isTrustedDocument } from "./window-security";

export interface SelectionArea {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface CaptureResult {
  path: string;
  area: SelectionArea;
  /** 缩放系数：高 DPI 下物理像素与逻辑像素不等 */
  scaleFactor: number;
}

let activeSelector: BrowserWindow | null = null;
export function cancelScreenSelection(): void {
  const selector = activeSelector;
  activeSelector = null;
  if (selector && !selector.isDestroyed()) selector.destroy();
}

/** 截取指定区域并写入临时 PNG，返回路径。 */
export async function captureRegion(area: SelectionArea): Promise<CaptureResult | null> {
  const display = bestDisplay(area, screen.getAllDisplays());
  if (!display) return null;
  const scaleFactor = display.scaleFactor || 1;
  const thumbnailSize = {
    width: Math.round(display.bounds.width * scaleFactor),
    height: Math.round(display.bounds.height * scaleFactor),
  };

  const sources = await desktopCapturer.getSources({
    types: ["screen"],
    thumbnailSize,
  });
  const source =
    sources.find((s) => s.display_id === String(display.id));
  if (!source) return null;

  const full = source.thumbnail;
  const size = full.getSize();

  // Electron may return a thumbnail smaller than requested: use actual dimensions.
  const clamped = thumbnailRect(area, display.bounds, size);
  if (!clamped) return null;
  const cropped = full.crop(clamped);
  const dir = path.join(os.tmpdir(), "voxsub-ocr");
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `capture-${randomUUID()}.png`);
  fs.writeFileSync(file, cropped.toPNG());

  return { path: file, area: intersect(area, display.bounds)!, scaleFactor };
}

/**
 * 弹出全屏框选窗，返回用户选择的区域（逻辑坐标）。
 * 实现方式：一个覆盖全部显示器的透明窗，内部 HTML 负责拖拽绘制矩形。
 */
export async function pickScreenArea(parent: BrowserWindow | null): Promise<SelectionArea | null> {
  cancelScreenSelection();
  const bounds = virtualBounds(screen.getAllDisplays().map(display => display.bounds));
  const parentWasVisible = Boolean(parent && !parent.isDestroyed() && parent.isVisible());

  const selector = new BrowserWindow({
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: bounds.height,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    movable: false,
    hasShadow: false,
    fullscreenable: false,
    show: false,
    ...(parent ? { parent } : {}),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  protectWindow(selector, path.join(__dirname, "..", "renderer", "selector.html"), false);
  activeSelector = selector;
  selector.setAlwaysOnTop(true, "screen-saver");

  try {
    if (parentWasVisible) parent!.hide();
    await new Promise(resolve => setTimeout(resolve, 220));
    if (selector.isDestroyed()) return null;
    // Register before showing/loading: fast cancellation must not be lost.
    const result = new Promise<SelectionArea | null>(resolve => {
      selector.webContents.ipc.on("selector:result", (event, payload: SelectionArea | null) => {
        if (isTrustedDocument(event, selector.webContents)) resolve(payload);
      });
      selector.once("closed", () => resolve(null));
    });
    await selector.loadFile(path.join(__dirname, "..", "renderer", "selector.html"));
    if (selector.isDestroyed()) return null;
    selector.showInactive();
    selector.focus(); // Interactive picker only, never invoked by silent tests.
    const area = await result;
    if (!area || Object.keys(area).length !== 4) return null;
    const display = bestDisplay(area, screen.getAllDisplays());
    // A mixed-DPI cross-screen area is clipped to its dominant screen.
    return display ? intersect(area, display.bounds) : null;
  } catch (error) {
    if (selector.isDestroyed()) return null;
    throw error;
  } finally {
    if (activeSelector === selector) activeSelector = null;
    if (!selector.isDestroyed()) selector.destroy();
    if (parentWasVisible && parent && !parent.isDestroyed()) parent.showInactive();
  }
}

/**
 * 实时区域覆盖窗。
 *
 * 关键点（与原 Qt 版一致）：
 *   · 无边框 + 透明 + 置顶 + 鼠标穿透
 *   · setContentProtection 排除自身，否则会被下一轮 OCR 识别到译文形成回路
 */
export function createOverlayForArea(area: SelectionArea): BrowserWindow {
  const win = new BrowserWindow({
    x: Math.round(area.x),
    y: Math.round(area.y),
    width: Math.round(area.width),
    height: Math.round(area.height),
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    movable: false,
    hasShadow: false,
    focusable: false,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  protectWindow(win, path.join(__dirname, "..", "renderer", "ocr-overlay.html"), false);
  win.setAlwaysOnTop(true, "screen-saver");
  win.setIgnoreMouseEvents(true, { forward: true });
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // 时序：必须先显示再设捕获排除（Electron 44 实测结论）
  win.once("ready-to-show", () => {
    if (win.isDestroyed()) return;
    win.showInactive();
    win.setContentProtection(true);
  });

  void win.loadFile(path.join(__dirname, "..", "renderer", "ocr-overlay.html")).catch(() => {
    // Closing the failed overlay also stops its owning live session.
    if (!win.isDestroyed()) win.destroy();
  });
  return win;
}
