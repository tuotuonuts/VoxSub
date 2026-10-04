/**
 * 应用状态与后端连接。
 *
 * 刻意做成"单一 store + 订阅"而不是事件总线：
 * 页面渲染只读 store，写只经 command 封装，避免多个页面各自持有副本后失同步。
 */
import { CMD, type BackendEvent, type CommandName, type LogEntry } from "./protocol";
import {
  INITIAL_BACKEND_STATUS,
  backendNotice,
  needsResync,
  normalizeMode,
  parseReadyPayload,
  reduceBackendStatus,
  sessionViewFor,
  type BackendPhase,
  type BackendStatus,
  type BackendStatusEvent,
  type SessionView,
} from "../shared/backend-status";
import { reduceSessionEvent } from "../shared/session-timeline";
import {
  classifyCommandResult,
  describeOutcome,
  isJobFailure,
  isTerminalJobStatus,
  parseJobEvent,
  type CommandResultLike,
  type CommandDelivery,
  type RequestOutcome,
} from "../shared/request-outcome";

import { parseRecordingState, type RecordingState } from "./recording-control";
import { normalizeLog } from "../shared/log-time";

type Listener = () => void;
type UiTranslator = (source: string) => string;

let uiTranslator: UiTranslator = (source) => source;

/** i18n registers its lookup here so store-owned system messages follow the UI language. */
export function setUiTranslator(translate: UiTranslator): void {
  uiTranslator = translate;
}

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
  /** 字幕流（稳定终句）。tsMs 是相对会话开始的毫秒数（导出 SRT 用）。 */
  subtitles: Array<{ source: string; translation: string; tsMs: number }>;
  /** 会话开始的单调时刻（performance.now()），用于计算每句相对时间 */
  sessionStartedAt: number | null;
  /** 当前句草稿（原位替换，不进入历史） */
  draft: { source: string; translation: string } | null;
  /** 文件模式进度 */
  progress: { completed: number; total: number; stage: string } | null;
  /** 模型下载进度：modelId -> 百分比 */
  downloads: Record<string, { completed: number; total: number; stage: string }>;
  logs: LogEntry[];
  theme: "dark" | "light";
  recording: boolean;
  readonly recordingState?: import("../renderer/recording-control").RecordingState | null;
  /** 模型根目录（由 list_models 回传，供设置页与目录页展示） */
  modelsRoot: string;
  /** 临时缓存目录（OCR 译后图等落盘位置，由后端回传） */
  cacheRoot: string;
  /** 更新日志（版本 → 说明），供设置页「关于」渲染 */
  releaseNotes: Array<{ version: string; date?: string; body: string }>;
  /**
   * 后端连接阶段。
   *
   * 独立于 `connected` 的原因（缺陷 #11）：后端**进程退出**与"从来没连上"
   * 是两回事，提示与后续动作都不同；界面也必须在断连时把"运行中"收回去，
   * 而不是只把 statusText 改一句话。
   */
  backendPhase: BackendPhase;
  /** 断连 / 启动失败的原因；null 表示无异常。 */
  backendReason: string | null;
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
    sessionStartedAt: null,
    draft: null,
    progress: null,
    downloads: {},
    logs: [],
    theme: window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
    recording: false,
    recordingState: null,
    modelsRoot: "",
    cacheRoot: "",
    releaseNotes: [],
    backendPhase: "connecting",
    backendReason: null,
  };
}

class Store {
  private state: AppState = initialState();
  private listeners = new Set<Listener>();
  private logSequence = 0;

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
    if (last && last.source === source && last.translation === translation) {
      this.patch({ draft: null });
      return;
    }

    // 每句带相对时间：SRT/VTT 导出需要真实时间轴，
    // 用序号乘固定间隔是假的（长句与停顿都会被拉平）。
    const startedAt = this.state.sessionStartedAt ?? performance.now();
    const tsMs = Math.max(0, Math.round(performance.now() - startedAt));
    const subtitles = [...this.state.subtitles, { source, translation, tsMs }];
    this.patch({ subtitles, draft: null, sessionStartedAt: startedAt });
  }

  pushLog(entry: LogEntry): void {
    const logs = [...this.state.logs, normalizeLog(entry, Date.now(), ++this.logSequence)];
    if (logs.length > MAX_LOG_LINES) logs.splice(0, logs.length - MAX_LOG_LINES);
    this.patch({ logs });
  }

  applyEvent(event: BackendEvent): void {
    switch (event.type) {
      case "ready": {
        // 后端新增了握手字段（protocolVersion / backendGeneration /
        // readiness / session）：**只挑认识的字段，多出来的字段一律忽略**，
        // 否则老渲染层会把新字段当异常。
        this.patch({ connected: true, version: event.version, recordingState: null });
        setBackendStatus({ type: "ready" });
        const handshake = parseReadyPayload(event);
        if (handshake) {
          const extras: string[] = [];
          if (handshake.protocolVersion) extras.push(`协议 ${handshake.protocolVersion}`);
          if (handshake.backendGeneration) extras.push(`后端代号 ${handshake.backendGeneration}`);
          if (handshake.activeJobs !== null) extras.push(`在途任务 ${handshake.activeJobs.length}`);
          if (extras.length > 0) {
            this.pushLog({
              ts: new Date().toISOString(),
              level: "INFO",
              message: `后端握手：v${handshake.version}${extras.length ? ` · ${extras.join(" · ")}` : ""}`,
            });
          }
          // 握手自带的会话状态：比再发一次 state 命令更快，且避免"重载后先显示错状态"
          if (handshake.session) {
            applySessionState(event.session ?? null);
            if ("recordingEnabled" in handshake.session) this.pushLog({ ts: new Date().toISOString(), level: "DEBUG", message: "录音状态已从握手恢复" });
          }
        }
        break;
      }
      case "job": {
        // 后台任务事件（async 命令的进度与终态）。缺陷 #4 的"超时后仍能收到
        // 后续事件与最终终态"就是靠这条通道：超时不结束任务，任务继续发事件。
        const job = parseJobEvent(event);
        if (!job) break;
        const terminal = isTerminalJobStatus(job.status);
        this.pushLog({
          ts: event.ts ?? "",
          source: "job",
          raw: JSON.stringify(event),
          level: isJobFailure(job.status) ? "ERROR" : "INFO",
          message: `任务 ${job.command || job.jobId} ${job.status}${job.error ? `：${job.error}` : ""}`,
        });
        if (terminal) {
          // 终态：把任务的去向写进状态行，用户不必去日志里找。
          // 三个终态各有各的说法 —— 尤其"已取消"不能显示成"失败"（用户自己
          // 点的取消，弹红字是骗人的），也不能显示成"已完成"。
          const label =
            job.status === "succeeded" ? "已完成" : job.status === "failed" ? "失败" : "已取消";
          this.patch({ statusText: `${job.command || job.jobId} ${label}` });
        }
        break;
      }
      case "disconnected": {
        // 后端进程退出（缺陷 #11）。
        //
        // 原先这里什么都不做：主进程只发一条 status 文案，界面把文案写进
        // statusText，`running` 仍是 true —— 状态灯还亮着"运行中"、主按钮还写着
        // "结束"。后端已经没了，界面说的每一句都是错的。
        //
        // 现在：独立的断连态 + 会话视图强制归零 + 明确的一句话。
        this.patch({ connected: false });
        const reason = event.reason;
        setBackendStatus(reason ? { type: "disconnected", reason } : { type: "disconnected" });
        this.patch({
          ...sessionViewFor(backendStatus.phase, { running: this.state.running, paused: this.state.paused }),
          statusText: backendNotice(backendStatus, uiTranslator),
        });
        break;
      }
      case "request-timeout":
        if (event.command === CMD.setRecording || event.command === CMD.state) applySessionState(null);
        // 请求超时**不是失败**：任务可能仍在进行（缺陷 #4）。
        // 记一条 WARNING 说明情况，不写 ERROR —— 写 ERROR 会让用户以为任务挂了。
        this.pushLog({
          ts: new Date().toISOString(),
          level: "WARNING",
          message: describeOutcome("timeout", event.command, uiTranslator),
        });
        break;
      case "status":
        this.patch({ statusText: event.text });
        break;
      case "session": {
        // 按 action 区分（缺陷 #2）：只有 start 才重置字幕与时间基准。
        //
        // 原先无条件 `subtitles: []`，而停止会话时后端恰好会发
        // `{type:"session", action:"stop"}`，于是用户一点"结束"，整场字幕被清空，
        // 接着"导出会话"因为字幕为空直接 return（workspace.ts 的早退），
        // 表现得像点了没反应。
        const action = event.action;
        this.patch({
          ...reduceSessionEvent(action, performance.now()),
          // 会话事件同时也是最直接的运行状态来源：start 即在跑，stop 即已停。
          // 认不出的 action 不乱改状态（reduceSessionEvent 会按 stop 保守处理）。
          running: action === "start" ? true : this.state.running,
          ...(action === "stop" ? { paused: false } : {}),
        });
        break;
      }
      case "state":
        // 会话状态（运行中 / 已暂停）。
        //
        // 这条事件是"暂停/继续能用、结束按钮会出现"的前提：渲染层原先从不
        // 接收也不查询状态，running/paused 永远是初始的 false —— 于是主按钮
        // 永远显示"开始"、结束按钮永远隐藏、暂停分支永远走不到。
        applySessionState(event);

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
        this.pushLog({ ts: event.ts, level: event.level, message: event.message, source: "backend", raw: JSON.stringify(event) });
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
  code?: string;
  jobId?: string;
  data?: T;
  /** 请求在时限内没返回（后端仍在处理）；超时**不是**失败，也不是取消。 */
  timedOut?: boolean;
  /** 请求没能发出（后端未运行 / 未初始化）。 */
  unavailable?: boolean;
  /** 命令是否确定未发送、收到回复或仍处于不确定状态。 */
  delivery?: CommandDelivery;
}

/* ------------------------------------------------------------ 后端状态机 */

/**
 * 当前后端状态（渲染层的单一事实来源）。
 *
 * 与 `backendReady`（就绪门开关）分开：就绪门只关心"能不能发命令"，
 * 状态机还要表达"后端是起来了、还是退了、还是压根没起来"。
 */
let backendStatus: BackendStatus = INITIAL_BACKEND_STATUS;
let sessionRevision = 0;

/** Includes repeated unknown snapshots: null -> null can still revoke a pending intent. */
export function getSessionRevision(): number { return sessionRevision; }

/** 把状态机事件落到 store。所有相位变化都必须走这里，避免两处口径不一致。 */
function setBackendStatus(event: BackendStatusEvent): void {
  sessionRevision++;
  backendStatus = reduceBackendStatus(backendStatus, event);
  store.patch({ backendPhase: backendStatus.phase, backendReason: backendStatus.reason,
    ...(backendStatus.phase !== "ready" ? { recordingState: null } : {}) });
}

export function getBackendStatus(): BackendStatus {
  return backendStatus;
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
  setBackendStatus(failure ? { type: "failed", reason: failure } : { type: "ready" });
  settleReadiness();
}

export function isBackendReady(): boolean {
  return backendReady;
}

function whenBackendReady(): Promise<void> {
  if (backendReady) return Promise.resolve();
  return new Promise((resolve) => {
    readinessWaiters.push(resolve);
    // 超时兜底：后端起不来时给出明确报错，而不是让界面永远转圈。
    // 注意这里只表示"就绪事件没等到"，不代表后端失败 —— 失败由 backendFailed
    // 与 backendPhase 单独表达。
    setTimeout(resolve, READY_TIMEOUT_MS);
  });
}

/** 一次命令调用的结果：结果分类 + 数据。 */
export interface CallOutcome<T> {
  outcome: RequestOutcome;
  data: T | null;
  /** 实际投递状态；unknown 必须继续持有长任务退出保护。 */
  delivery: CommandDelivery;
  /** 远端拒绝码：供需要恢复已有 owner 的长任务命令辨别冲突。 */
  code?: string;
  /** 远端冲突时关联到已有任务的编号。 */
  jobId?: string;
}

/**
 * 统一的命令调用：等待后端就绪，结果写入日志，避免调用点各自处理。
 *
 * **超时不是失败**（缺陷 #4）：`outcome === "timeout"` 表示本次请求没在时限内
 * 返回，任务可能仍在进行。调用点若要区分（长任务），用这个函数；
 * 只关心数据的调用点用 `call()`（返回 data，超时同样是 null）。
 */
export async function callWithOutcome<T = unknown>(
  command: CommandName,
  args: unknown = null,
): Promise<CallOutcome<T>> {
  const api = window.voxsub;
  if (!api) {
    store.pushLog({
      ts: new Date().toISOString(),
      level: "ERROR",
      message: `IPC 不可用，无法执行 ${command}`,
    });
    return { outcome: "unavailable", data: null, delivery: "not_sent" };
  }

  await whenBackendReady();

  if (backendFailed) {
    store.pushLog({
      ts: new Date().toISOString(),
      level: "ERROR",
      message: `${command} 未执行：后端启动失败（${backendFailed}）`,
    });
    return { outcome: "unavailable", data: null, delivery: "not_sent" };
  }

  const result = (await api.backend.command(command, args)) as CommandResult<T>;
  const outcome = classifyCommandResult(result as CommandResultLike);
  const delivery: CommandDelivery = result.delivery === "not_sent" ||
    result.delivery === "unknown" || result.delivery === "response"
    ? result.delivery
    : (result.ok || (!result.timedOut && !result.unavailable) ? "response" : "unknown");
  if (outcome === "ok") {
    return { outcome, data: (result.data ?? null) as T | null, delivery };
  }

  // 失败与超时分开记：把"还没回来"写成 ERROR「失败」会让用户以为任务挂了，
  // 而实际上后端仍在搬数据/下模型（缺陷 #4）。
  store.pushLog({
    ts: new Date().toISOString(),
    level: outcome === "timeout" || delivery === "unknown" ? "WARNING" : "ERROR",
    message: delivery === "unknown"
      ? `${command}: ${uiTranslator("回执状态未知，任务可能仍在运行")}${result.error ? `: ${result.error}` : ""}`
      : `${describeOutcome(outcome, command, uiTranslator)}${result.error ? `: ${result.error}` : ""}`,
  });
  return {
    outcome,
    data: null,
    delivery,
    ...(typeof result.code === "string" ? { code: result.code } : {}),
    ...(typeof result.jobId === "string" ? { jobId: result.jobId } : {}),
  };
}

/** 只要数据的调用点用这个（超时同样是 null，语义与原先一致）。 */
export async function call<T = unknown>(
  command: CommandName,
  args: unknown = null,
): Promise<T | null> {
  return (await callWithOutcome<T>(command, args)).data;
}

/**
 * 把会话状态写进 store。事件与主动拉取共用，避免两处口径不一致。
 *
 * 两条纪律：
 *   1. **后端不在时不得显示"运行中"**（缺陷 #11）—— 由 sessionViewFor 统一裁掉；
 *   2. payload 里带 `mode` 时一并恢复模式 —— 渲染层重载后界面显示的模式必须
 *      跟后端实际模式一致，否则"暂停按钮该不该出现"这类判断会跟着错。
 */
export function applySessionState(
  payload: { running?: boolean; paused?: boolean; mode?: string } | null,
): void {
  sessionRevision++;
  if (!payload) { store.patch({ recordingState: null }); return; }
  const commanded: SessionView = { running: Boolean(payload.running), paused: Boolean(payload.paused) };
  const recordingState = backendStatus.phase === "ready" ? parseRecordingState(payload) : null;
  const patch: Partial<AppState> = {
    recordingState,
    ...(recordingState ? { recording: recordingState.recordingEnabled } : {}),
    ...sessionViewFor(backendStatus.phase, commanded),
  };
  const mode = normalizeMode(payload.mode);
  if (mode) patch.mode = mode;
  store.patch(patch);
}

/** Request authority is independent of the DOM owner and shared by reads and writes. */
function sessionAuthority(): () => boolean {
  const revision = sessionRevision;
  const recordingSnapshot = store.get().recordingState;
  const mode = store.get().mode;
  // A mode click supersedes an in-flight read even before its set_mode response.
  return () => revision === sessionRevision && recordingSnapshot === store.get().recordingState
    && mode === store.get().mode;
}

let recordingWrite: { current: () => boolean; done: Promise<unknown> } | null = null;

/** Shared request owner outlives a workspace; the callback suppresses only its local echo. */
export async function setRecordingState(
  enabled: boolean,
  beforePublish: (snapshot: RecordingState | null) => void,
): Promise<RecordingState | null> {
  sessionRevision++;
  beforePublish(null);
  store.patch({ recordingState: null });
  const current = sessionAuthority();
  const done = call<RecordingState>(CMD.setRecording, { enabled });
  const write = { current, done };
  recordingWrite = write;
  let result: unknown = null;
  try { result = await done; } catch { /* No acknowledgement means unknown. */ }
  if (recordingWrite === write) recordingWrite = null;
  if (!current()) return store.get().recordingState ?? null;
  const snapshot = parseRecordingState(result);
  sessionRevision++;
  beforePublish(snapshot);
  store.patch({ recordingState: snapshot, ...(snapshot ? { recording: snapshot.recordingEnabled } : {}) });
  return snapshot;
}

export async function refreshSessionState(): Promise<void> {
  // A page rebuild must join the sent write, not race it with a pre-write state read.
  if (recordingWrite?.current()) {
    try { await recordingWrite.done; } catch { /* The write owner publishes unknown. */ }
    return;
  }
  await requestSessionState(CMD.state);
}

/** All session acknowledgements use the same event/connection/snapshot guard as resync. */
export async function requestSessionState(
  command: typeof CMD.state | typeof CMD.start | typeof CMD.pause | typeof CMD.resume | typeof CMD.stop,
): Promise<void> {
  // A newer command supersedes older requests even before either response arrives.
  if (command !== CMD.state) sessionRevision++;
  const current = sessionAuthority();
  let result: { running: boolean; paused: boolean; mode?: string } | null = null;
  try {
    result = await call<typeof result>(command);
  } catch (error) {
    store.pushLog({ ts: new Date().toISOString(), level: "WARNING", message: `${command}: ${String(error)}` });
  }
  // An event, connection change or recording acknowledgement supersedes this read.
  if (!current()) return;
  applySessionState(result);
}

/* ------------------------------------------------------------ 后台任务 */

export interface JobSummary {
  jobId?: string;
  command?: string;
  status?: string;
  sequence?: number;
  detail?: string;
}

/** 异步发起命令时后端立刻返回的受理凭据。 */
export interface JobAccept {
  jobId: string;
  accepted: boolean;
  status: string;
}

/**
 * 列出后台任务（后端 `job_list`）。
 *
 * 用途：界面重载后仍能看到后端正在跑的长任务（迁移 / 下载），
 * 而不是"重载完就什么都不知道了"。
 */
export async function listJobs(): Promise<JobSummary[] | null> {
  const result = await call<{ jobs?: JobSummary[] } | JobSummary[]>(CMD.jobList);
  if (!result) return null;
  return Array.isArray(result) ? result : (result.jobs ?? []);
}

/** 查询单个后台任务（后端 `job_status`，arg 名固定为 job_id）。 */
export async function jobStatus(jobId: string): Promise<JobSummary | null> {
  return call<JobSummary>(CMD.jobStatus, { job_id: jobId });
}

/** 取消后台任务（后端 `cancel_job`，arg 名固定为 job_id）。 */
export async function cancelJob(jobId: string): Promise<JobSummary | null> {
  return call<JobSummary>(CMD.cancelJob, { job_id: jobId });
}

/**
 * 以异步方式发起一条命令（`args.async = true`）。
 *
 * 后端立刻返回 `{jobId, accepted, status}`，之后的进度与**最终终态**通过
 * `event: "job"` 事件送达（见 store 的 `case "job"`）。这正是缺陷 #4 需要的能力：
 * 长任务不必再和"请求超时"纠缠 —— 它要么同步返回，要么立刻受理 + 事件收尾。
 * 不带 async 时后端保持原同步语义不变。
 */
export async function callAsync(
  command: CommandName,
  args: Record<string, unknown> = {},
): Promise<JobAccept | null> {
  const data = await call<JobAccept>(command, { ...args, async: true });
  if (!data || typeof data.jobId !== "string" || data.jobId === "") return null;
  return data;
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
    if (event.type === "ready") {
      markBackendReady(null);
      // 主动拉一次会话状态。
      //
      // 为什么不能只靠 state 事件：界面可能在后端**已经在跑**的情况下才连上
      // （后端重启、渲染层重载）。那时事件已经错过了，界面会停在"开始"，
      // 而实际会话正在运行 —— 用户点下去反而会再启一个会话。
      //
      // 补拉的条件收在 needsResync 里：只有真正连上才拉，且拉回来的载荷
      // 会把 running / paused / mode 一起恢复（见 applySessionState）。
      if (needsResync(backendStatus.phase)) void refreshSessionState();
    }
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
