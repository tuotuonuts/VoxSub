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

/** 截取指定区域并写入临时 PNG，返回路径。 */
export async function captureRegion(area: SelectionArea): Promise<CaptureResult | null> {
  const display = screen.getDisplayNearestPoint({
    x: Math.round(area.x),
    y: Math.round(area.y),
  });
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
    sources.find((s) => s.display_id === String(display.id)) ?? sources[0];
  if (!source) return null;

  const full = source.thumbnail;
  const size = full.getSize();

  // 逻辑坐标 → 物理像素
  const rect = {
    x: Math.round((area.x - display.bounds.x) * scaleFactor),
    y: Math.round((area.y - display.bounds.y) * scaleFactor),
    width: Math.round(area.width * scaleFactor),
    height: Math.round(area.height * scaleFactor),
  };

  const clamped = {
    x: Math.max(0, Math.min(rect.x, size.width - 1)),
    y: Math.max(0, Math.min(rect.y, size.height - 1)),
    width: Math.max(1, Math.min(rect.width, size.width - Math.max(0, rect.x))),
    height: Math.max(1, Math.min(rect.height, size.height - Math.max(0, rect.y))),
  };

  const cropped = full.crop(clamped);
  const dir = path.join(os.tmpdir(), "voxsub-ocr");
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `capture-${Date.now()}.png`);
  fs.writeFileSync(file, cropped.toPNG());

  return { path: file, area: clamped, scaleFactor };
}

/**
 * 弹出全屏框选窗，返回用户选择的区域（逻辑坐标）。
 * 实现方式：一个覆盖全部显示器的透明窗，内部 HTML 负责拖拽绘制矩形。
 */
export async function pickScreenArea(parent: BrowserWindow | null): Promise<SelectionArea | null> {
  const primary = screen.getPrimaryDisplay();
  const bounds = primary.bounds;

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

  selector.setAlwaysOnTop(true, "screen-saver");

  // 主窗必须先让出画面，否则截图会带上自身的虚影。
  // 隐藏后等一帧 DWM 合成完成，这是原 Qt 版踩过的坑。
  if (parent) parent.hide();
  await new Promise((resolve) => setTimeout(resolve, 220));

  await selector.loadFile(path.join(__dirname, "..", "renderer", "selector.html"));
  selector.showInactive();
  selector.focus();

  const area = await new Promise<SelectionArea | null>((resolve) => {
    const onResult = (_e: unknown, payload: SelectionArea | null) => {
      resolve(payload);
    };
    selector.webContents.ipc.once("selector:result", onResult);
    selector.once("closed", () => resolve(null));
  });

  selector.destroy();
  if (parent) parent.show();
  return area;
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

  win.setAlwaysOnTop(true, "screen-saver");
  win.setIgnoreMouseEvents(true, { forward: true });
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // 时序：必须先显示再设捕获排除（Electron 44 实测结论）
  win.once("ready-to-show", () => {
    win.showInactive();
    win.setContentProtection(true);
  });

  void win.loadFile(path.join(__dirname, "..", "renderer", "ocr-overlay.html"));
  return win;
}
