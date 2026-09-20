/**
 * 后端连接状态的纯逻辑 —— 渲染层与单测共用。
 *
 * ## 缺陷背景（工作单 #11：后端断连的状态恢复不完整）
 *
 * 后端进程退出时，主进程只发了一条 `{type:"status", text:"后端已退出（code=…）"}`
 * （见 `src/main/backend.ts` 的 `child.on("exit")` 分支）。渲染层照单全收写进
 * statusText —— 而 `running` 仍是 `true`，于是界面上写着"后端已退出"，状态灯却
 * 还亮着"运行中"、主按钮还写着"结束"。用户点下去只会得到"后端未运行"。
 *
 * 所以需要一个**独立的断连态**：`phase = "disconnected"`。断连（以及启动失败）
 * 时，会话视图一律归零，绝不显示"运行中"。
 *
 * ## 渲染层重载后的重新同步
 *
 * 界面重载（或主进程重连）后，`ready` 事件与 `state` 事件都可能是**重载前**发出
 * 的，早已错过。所以 `connectBackend()` 在 ready 时会主动拉一次 `state`；本模块
 * 提供 `needsResync()` 与 `parseSessionPayload()`，把"要不要补拉"和"载荷怎么
 * 落库"变成可离线断言的纯函数 —— 载荷里带 `mode`，界面才能连模式一起恢复，
 * 而不是只恢复按钮文案。
 */

export type BackendPhase =
  /** 已发出启动请求，还没收到 ready（或启动失败） */
  | "connecting"
  | "ready"
  | "failed"
  /** 起来过又退出了 —— 与"启动失败"是两回事，提示与后续动作都不同 */
  | "disconnected";

export interface BackendStatus {
  phase: BackendPhase;
  /** 失败 / 断连的原因，供界面显示；null 表示无异常。 */
  reason: string | null;
}

export const INITIAL_BACKEND_STATUS: BackendStatus = { phase: "connecting", reason: null };

export type BackendStatusEvent =
  | { type: "ready" }
  | { type: "failed"; reason: string }
  | { type: "disconnected"; reason?: string };

/**
 * 后端状态的归约。
 *
 * 刻意允许 `disconnected → ready`：后端可以由 UI 重新拉起，恢复后必须能回到
 * "已连接"，否则界面会永远停在断连态。
 */
export function reduceBackendStatus(current: BackendStatus, event: BackendStatusEvent): BackendStatus {
  switch (event.type) {
    case "ready":
      return { phase: "ready", reason: null };
    case "failed":
      return { phase: "failed", reason: event.reason };
    case "disconnected":
      return { phase: "disconnected", reason: event.reason ?? current.reason };
    default:
      return current;
  }
}

export interface SessionView {
  running: boolean;
  paused: boolean;
}

/**
 * 界面上该显示的会话视图。
 *
 * **后端不在（断连 / 启动失败）时不能显示"运行中"** —— 这是缺陷 #11 的核心：
 * 后端已经没了，界面还说在跑，用户接下来做的每一步判断都是错的。
 */
export function sessionViewFor(phase: BackendPhase, commanded: SessionView): SessionView {
  if (phase === "ready" || phase === "connecting") return commanded;
  return { running: false, paused: false };
}

/**
 * 是否需要主动向后端补拉一次会话状态。
 *
 * 只在 `ready` 时补：connecting 时后端还没起来（拉了也是失败），
 * failed / disconnected 时根本没有后端可问。
 */
export function needsResync(phase: BackendPhase): boolean {
  return phase === "ready";
}

export type Mode = "a" | "b" | "c" | "d";

const MODES: readonly string[] = ["a", "b", "c", "d"];

/**
 * 校验模式值。
 *
 * 后端的 `state` 事件与 `state` 命令回传都带 `mode`（ipc_server.py 的
 * `_state_payload`），但界面在渲染层重载后可能收到旧值或空值。只接受这四个
 * 合法值，认不出就返回 null，让调用方**保持原样** —— 猜一个默认模式会让
 * 界面显示的模式与实际监听模式不一致（B 模式界面 / A 模式实际拾音）。
 */
export function normalizeMode(raw: unknown): Mode | null {
  if (typeof raw !== "string") return null;
  return MODES.includes(raw) ? (raw as Mode) : null;
}

/** 断连 / 启动失败时给用户看的一句话。 */
export function backendNotice(status: BackendStatus): string {
  switch (status.phase) {
    case "disconnected":
      return status.reason
        ? `后端已退出：${status.reason}`
        : "后端已退出，功能已停止";
    case "failed":
      return status.reason ? `后端启动失败：${status.reason}` : "后端启动失败";
    case "ready":
      return "后端已连接";
    default:
      return "正在连接后端…";
  }
}

/* ---------------------------------------------------------- ready 握手 */

/** 解析后的 ready 握手内容。 */
export interface ReadyHandshake {
  /** 后端版本（老后端也有）。 */
  version: string;
  /** 协议版本；老后端不发 → null。 */
  protocolVersion: string | null;
  /** 后端代号（用于识别"后端重启过"）；老后端不发 → null。 */
  backendGeneration: string | null;
  /** 就绪快照里的在途任务数；拿不到 → null。 */
  activeJobs: number | null;
  /** ready 里携带的会话状态；没有或非法 → null。 */
  session: { running: boolean; paused: boolean; mode?: Mode } | null;
}

function asText(raw: unknown): string | null {
  if (typeof raw === "string" && raw !== "") return raw;
  if (typeof raw === "number" && Number.isFinite(raw)) return String(raw);
  return null;
}

/**
 * 解析 ready 事件。
 *
 * **新增字段一律不得导致解析失败**：后端在 ready 里加了
 * `protocolVersion` / `backendGeneration` / `readiness` / `session` 之后，
 * 渲染层如果按"字段必须与预期完全一致"来读，就会把这些新字段当成异常
 * （表现为连不上或握手失败）。这里只挑认识的字段，其余忽略。
 */
export function parseReadyPayload(raw: unknown): ReadyHandshake | null {
  if (!raw || typeof raw !== "object") return null;
  const source = raw as Record<string, unknown>;
  if (source["type"] !== undefined && source["type"] !== "ready") return null;

  const readiness = source["readiness"];
  let activeJobs: number | null = null;
  if (readiness && typeof readiness === "object") {
    const count = (readiness as Record<string, unknown>)["activeJobs"];
    if (typeof count === "number" && Number.isFinite(count)) activeJobs = count;
  }

  const sessionRaw = source["session"];
  let session: ReadyHandshake["session"] = null;
  if (sessionRaw && typeof sessionRaw === "object") {
    const candidate = sessionRaw as Record<string, unknown>;
    if (typeof candidate["running"] === "boolean" && typeof candidate["paused"] === "boolean") {
      session = { running: candidate["running"], paused: candidate["paused"] };
      const mode = normalizeMode(candidate["mode"]);
      if (mode) session.mode = mode;
    }
  }

  return {
    version: typeof source["version"] === "string" ? source["version"] : "",
    protocolVersion: asText(source["protocolVersion"]),
    backendGeneration: asText(source["backendGeneration"]),
    activeJobs,
    session,
  };
}
