/**
 * 应用状态与后端连接。
 *
 * 刻意做成"单一 store + 订阅"而不是事件总线：
 * 页面渲染只读 store，写只经 command 封装，避免多个页面各自持有副本后失同步。
 */
import type { BackendEvent, CommandName, LogEntry } from "./protocol";

type Listener = () => void;

export interface AppState {
  /** 后端是否已连接（收到 ready） */
  connected: boolean;
  version: string;
  /** 会话 */
  running: boolean;
  paused: boolean;
  mode: "a" | "b" | "c" | "d";
  sourceLang: string;
  targetLang: string;
  statusText: string;
  /** 字幕流（稳定终句） */
  subtitles: Array<{ source: string; translation: string }>;
  /** 当前句草稿（原位替换，不进入历史） */
  draft: { source: string; translation: string } | null;
  /** 文件模式进度 */
  progress: { completed: number; total: number; stage: string } | null;
  /** 模型下载进度：modelId -> 百分比 */
  downloads: Record<string, { completed: number; total: number; stage: string }>;
  logs: LogEntry[];
  theme: "dark" | "light";
  recording: boolean;
}

const MAX_LOG_LINES = 400;

function initialState(): AppState {
  return {
    connected: false,
    version: "",
    running: false,
    paused: false,
    mode: "a",
    sourceLang: "auto",
    targetLang: "zh",
    statusText: "待机",
    subtitles: [],
    draft: null,
    progress: null,
    downloads: {},
    logs: [],
    theme: window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
    recording: false,
  };
}

class Store {
  private state: AppState = initialState();
  private listeners = new Set<Listener>();

  get(): Readonly<AppState> {
    return this.state;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** 合并更新并通知；所有写入都走这里，保证订阅者不会被漏掉。 */
  patch(partial: Partial<AppState>): void {
    this.state = { ...this.state, ...partial };
    for (const listener of this.listeners) listener();
  }

  /** 追加字幕。同一句草稿转为终句时替换而非追加重复行。 */
  commitSubtitle(source: string, translation: string): void {
    const last = this.state.subtitles[this.state.subtitles.length - 1];
    const subtitles =
      last && last.source === source && last.translation === translation
        ? this.state.subtitles
        : [...this.state.subtitles, { source, translation }];
    this.patch({ subtitles, draft: null });
  }

  pushLog(entry: LogEntry): void {
    const logs = [...this.state.logs, entry];
    if (logs.length > MAX_LOG_LINES) logs.splice(0, logs.length - MAX_LOG_LINES);
    this.patch({ logs });
  }

  applyEvent(event: BackendEvent): void {
    switch (event.type) {
      case "ready":
        this.patch({ connected: true, version: event.version });
        break;
      case "status":
        this.patch({ statusText: event.text });
        break;
      case "utterance":
        this.commitSubtitle(event.source, event.translation);
        break;
      case "draft":
        this.patch({ draft: { source: event.source, translation: event.translation } });
        break;
      case "partial":
        // partial 只有原文，译文等待 draft
        this.patch({
          draft: { source: event.text, translation: this.state.draft?.translation ?? "" },
        });
        break;
      case "progress":
        this.patch({
          progress: { completed: event.completed, total: event.total, stage: event.stage },
        });
        break;
      case "download":
        this.patch({
          downloads: {
            ...this.state.downloads,
            [event.modelId]: {
              completed: event.completed,
              total: event.total,
              stage: event.stage,
            },
          },
        });
        break;
      case "log":
        this.pushLog({ ts: event.ts, level: event.level, message: event.message });
        break;
      case "error":
        this.pushLog({ ts: new Date().toISOString(), level: "ERROR", message: event.message });
        break;
      default:
        break;
    }
  }
}

export const store = new Store();

/* ------------------------------------------------------------ 后端调用封装 */

export interface CommandResult<T = unknown> {
  ok: boolean;
  error?: string;
  data?: T;
}

/* ------------------------------------------------------------ 后端就绪门 */

/**
 * 后端就绪状态。
 *
 * 为什么需要：Python 侧要 spawn 解释器 + 导入 onnxruntime/ctypes 等，实测需要
 * 数秒；而界面在同一帧就开始拉模型列表，先到的命令必然失败（表现为"目录空白"
 * 这种看不出原因的故障）。把等待收进 call()，调用点就不必关心启动时序。
 */
let backendReady = false;
let backendFailed: string | null = null;
const readinessWaiters: Array<() => void> = [];

const READY_TIMEOUT_MS = 30_000;

function settleReadiness(): void {
  while (readinessWaiters.length > 0) {
    readinessWaiters.pop()?.();
  }
}

/** 由 connectBackend 在收到 ready 事件或启动失败时调用。 */
export function markBackendReady(failure: string | null = null): void {
  backendReady = true;
  backendFailed = failure;
  settleReadiness();
}

export function isBackendReady(): boolean {
  return backendReady;
}

function whenBackendReady(): Promise<void> {
  if (backendReady) return Promise.resolve();
  return new Promise((resolve) => {
    readinessWaiters.push(resolve);
    // 超时兜底：后端起不来时给出明确报错，而不是让界面永远转圈
    setTimeout(resolve, READY_TIMEOUT_MS);
  });
}

/** 统一的命令调用：等待后端就绪，失败时写入日志，避免调用点各自处理。 */
export async function call<T = unknown>(
  command: CommandName,
  args: unknown = null,
): Promise<T | null> {
  const api = window.voxsub;
  if (!api) {
    store.pushLog({
      ts: new Date().toISOString(),
      level: "ERROR",
      message: `IPC 不可用，无法执行 ${command}`,
    });
    return null;
  }

  await whenBackendReady();

  if (backendFailed) {
    store.pushLog({
      ts: new Date().toISOString(),
      level: "ERROR",
      message: `${command} 未执行：后端启动失败（${backendFailed}）`,
    });
    return null;
  }

  const result = (await api.backend.command(command, args)) as CommandResult<T>;
  if (!result.ok) {
    store.pushLog({
      ts: new Date().toISOString(),
      level: "ERROR",
      message: `${command} 失败: ${result.error ?? "未知错误"}`,
    });
    return null;
  }
  return (result.data ?? null) as T | null;
}

/** 启动后端并接上事件流。返回取消订阅函数。 */
export function connectBackend(): () => void {
  const api = window.voxsub;
  if (!api) {
    store.pushLog({
      ts: new Date().toISOString(),
      level: "ERROR",
      message: "预加载脚本未注入，应用无法与后端通信",
    });
    markBackendReady("预加载脚本未注入");
    return () => undefined;
  }

  const off = api.backend.onEvent((raw) => {
    const event = raw as BackendEvent & { type?: string };
    if (!event || typeof event.type !== "string") return;
    store.applyEvent(event as BackendEvent);
    if (event.type === "ready") markBackendReady(null);
  });

  void api.backend.start().then((result) => {
    if (!result.ok) {
      store.pushLog({
        ts: new Date().toISOString(),
        level: "ERROR",
        message: `后端启动失败: ${result.error ?? "未知错误"}`,
      });
      markBackendReady(result.error ?? "未知错误");
    }
  });

  return off;
}
