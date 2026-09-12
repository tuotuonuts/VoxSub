/**
 * 渲染进程可见的 API 类型声明（与 src/main/preload.ts 的 contextBridge 对应）。
 * 只声明实际暴露的方法，避免在渲染层误用 Node 能力。
 */

export interface BackendEventShape {
  type?: string;
  text?: string;
  source?: string;
  translation?: string;
  completed?: number;
  total?: number;
  stage?: string;
  modelId?: string;
  ts?: string;
  level?: string;
  message?: string;
  version?: string;
}

export interface CommandResult<T = unknown> {
  ok: boolean;
  error?: string;
  data?: T;
}

export interface SelectionAreaShape {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface VoxSubApi {
  backend: {
    start(): Promise<CommandResult>;
    stop(): Promise<CommandResult>;
    command(name: string, args: unknown): Promise<CommandResult>;
    onEvent(handler: (event: BackendEventShape) => void): () => void;
  };
  overlay: {
    toggleVisible(): Promise<boolean>;
    show(): Promise<boolean>;
    hide(): Promise<boolean>;
    setClickThrough(enabled: boolean): Promise<boolean>;
    isClickThrough(): Promise<boolean>;
    setOpacity(value: number): Promise<number>;
    setFontSize(value: number): Promise<number>;
    setSize(w: number, h: number): Promise<{ width: number; height: number } | null>;
    setDisplayMode(mode: string): Promise<string>;
    onClickThroughChanged(handler: (enabled: boolean) => void): () => void;
    onOpacityChanged(handler: (value: number) => void): () => void;
    onFontSizeChanged(handler: (value: number) => void): () => void;
    onDisplayModeChanged(handler: (value: string) => void): () => void;
  };
  dialog: {
    pickMedia(): Promise<string | null>;
    pickImage(): Promise<string | null>;
    pickDirectory(): Promise<string | null>;
    saveReport(): Promise<string | null>;
    saveImage(): Promise<string | null>;
    saveSession(payload: { lines: unknown[] }): Promise<string | null>;
    openExternal(url: string): Promise<boolean>;
    revealInFolder(path: string): Promise<boolean>;
  };
  ocr: {
    selectArea(): Promise<string | null>;
    startLiveRegion(): Promise<SelectionAreaShape | null>;
    stopLiveRegion(): Promise<boolean>;
  };
  selector: {
    finish(area: SelectionAreaShape | null): void;
  };
  ocrOverlay: {
    onRegionReady(handler: (area: unknown) => void): () => void;
    onTranslated(handler: (payload: unknown) => void): () => void;
    onFrameFailed(handler: () => void): () => void;
  };
  app: {
    capabilities(): Promise<{
      contentProtection: boolean;
      platform: string;
      versions: { electron: string; chrome: string; node: string };
    }>;
    onOpenPage(handler: (page: string) => void): () => void;
    onTrayMode(handler: (mode: string) => void): () => void;
    requestQuit(): Promise<boolean>;
    setBusy(busy: boolean, reason?: string): Promise<string>;
    onBlockingTask(handler: (payload: { reason: string }) => void): () => void;
  };
}

declare global {
  interface Window {
    voxsub?: VoxSubApi;
  }
}
