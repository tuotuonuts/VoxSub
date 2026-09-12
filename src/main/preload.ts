/**
 * 预加载脚本 —— 渲染进程唯一的特权入口。
 *
 * 只暴露收窄后的 API，不暴露 ipcRenderer 本身（contextIsolation 保持开启）。
 * Electron 44 的 contextBridge 会复制对象；这里全部用函数，避免原型链问题。
 */
import { contextBridge, ipcRenderer } from "electron";

function subscribe<T>(channel: string, handler: (payload: T) => void): () => void {
  const listener = (_e: unknown, payload: T) => handler(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

const api = {
  backend: {
    start: () => ipcRenderer.invoke("backend:start"),
    stop: () => ipcRenderer.invoke("backend:stop"),
    command: (name: string, args: unknown) => ipcRenderer.invoke("backend:command", name, args),
    onEvent: (handler: (event: unknown) => void) => subscribe("backend:event", handler),
  },

  overlay: {
    toggleVisible: () => ipcRenderer.invoke("overlay:toggle-visible"),
    show: () => ipcRenderer.invoke("overlay:show"),
    hide: () => ipcRenderer.invoke("overlay:hide"),
    setClickThrough: (enabled: boolean) => ipcRenderer.invoke("overlay:set-click-through", enabled),
    isClickThrough: () => ipcRenderer.invoke("overlay:is-click-through"),
    setOpacity: (value: number) => ipcRenderer.invoke("overlay:set-opacity", value),
    setFontSize: (value: number) => ipcRenderer.invoke("overlay:set-font-size", value),
    setSize: (w: number, h: number) => ipcRenderer.invoke("overlay:resize", w, h),
    setDisplayMode: (mode: string) => ipcRenderer.invoke("overlay:set-display-mode", mode),
    onClickThroughChanged: (handler: (enabled: boolean) => void) => subscribe("overlay:click-through", handler),
    onOpacityChanged: (handler: (value: number) => void) => subscribe("overlay:opacity", handler),
    onFontSizeChanged: (handler: (value: number) => void) => subscribe("overlay:font-size", handler),
    onDisplayModeChanged: (handler: (value: string) => void) => subscribe("overlay:display-mode", handler),
  },

  dialog: {
    /** 选择音视频文件（C 模式）。 */
    pickMedia: () => ipcRenderer.invoke("dialog:pick-media"),
    /** 选择图片（OCR）。 */
    pickImage: () => ipcRenderer.invoke("dialog:pick-image"),
    /** 选择目录（模型存储位置）。 */
    pickDirectory: () => ipcRenderer.invoke("dialog:pick-directory"),
    /** 保存诊断报告。 */
    saveReport: () => ipcRenderer.invoke("dialog:save-report"),
    /** 保存会话字幕（SRT/VTT/TXT）。 */
    saveSession: (payload: { lines: unknown[] }) => ipcRenderer.invoke("dialog:save-session", payload),
    /** 打开外部链接。 */
    openExternal: (url: string) => ipcRenderer.invoke("dialog:open-external", url),
  },

  ocr: {
    /** 框选屏幕区域，返回截图的临时路径。 */
    selectArea: () => ipcRenderer.invoke("ocr:select-area"),
    /** 开始实时区域 OCR，返回区域信息或 null。 */
    startLiveRegion: () => ipcRenderer.invoke("ocr:start-live-region"),
    stopLiveRegion: () => ipcRenderer.invoke("ocr:stop-live-region"),
  },

  /** 框选窗专用：把用户拖出的矩形交回主进程。 */
  selector: {
    finish: (area: unknown) => ipcRenderer.send("selector:result", area),
  },

  /** OCR 覆盖窗专用：接收主进程下发的绘制指令。 */
  ocrOverlay: {
    onRegionReady: (handler: (area: unknown) => void) => subscribe("ocr:region-ready", handler),
    onTranslated: (handler: (payload: unknown) => void) => subscribe("ocr:translated", handler),
    onFrameFailed: (handler: () => void) => subscribe("ocr:frame-failed", handler),
  },

  app: {
    capabilities: () => ipcRenderer.invoke("app:capabilities"),
    onOpenPage: (handler: (page: string) => void) => subscribe("app:open-page", handler),
    onTrayMode: (handler: (mode: string) => void) => subscribe("app:tray-mode", handler),
  },
} as const;

contextBridge.exposeInMainWorld("voxsub", api);

export type VoxSubApi = typeof api;
