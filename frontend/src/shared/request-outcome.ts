/**
 * 命令请求结果的分类 —— 纯逻辑，主进程、渲染层与单测共用。
 *
 * ## 缺陷背景（工作单 #4：30 秒超时不能当成任务失败或被取消）
 *
 * 前端请求层对每条命令都挂了一个约 30 秒的兜底（`src/main/backend.ts` 的
 * `BackendBridge.command()`）。它原本在超时后把这条请求**直接判成失败**：
 * `resolve({ ok: false, error: "命令超时" })`，并从 `pending` 里删掉这条请求。
 * 后果有三层，每一层都是用户看得见的错：
 *
 *   1. 渲染层的 `call()` 拿 `ok:false` 记一条 ERROR「失败: 命令超时」——
 *      **超时被说成失败**，可后端的长任务（迁移、模型下载、OCR）根本没失败。
 *   2. 因为 pending 被删掉，后端**稍后送回的真实结果会被丢弃**
 *      （`handleLine` 找不到 resolver 就 return）—— 前端再也拿不到终态。
 *   3. 调用点以为任务结束了：`views/migration.ts:397-399` 在 `await call(...)`
 *      之后无条件 `app.setBusy(false)` 并跳到"迁移失败"的报告页 ——
 *      **数据还在搬，退出保护却已经解除**，用户这时退出就留下半个模型库。
 *
 * 正确口径（本模块把它固化成函数）：
 *   · 超时只表示"本次请求未在时限内返回"，**不等于失败，不等于已取消**；
 *   · 超时后任务仍在进行，界面必须继续显示"进行中"；
 *   · 长任务在跑到终态之前，退出保护一直有效；
 *   · 后续事件与最终结果必须还能到达（超时只发通知，不释放请求）。
 */

/** 一次命令请求的分类结果。 */
export type RequestOutcome =
  /** 后端正常返回 */
  | "ok"
  /** 时限内没返回：任务**可能仍在进行**，不是失败，也不是取消 */
  | "timeout"
  /** 后端明确报错 */
  | "failed"
  /** 请求根本没发出去（后端未运行 / 未就绪） */
  | "unavailable";

/** 任务本身可能处的阶段（与"这次请求的结果"是两回事）。 */
export type TaskPhase = "done" | "running" | "failed" | "unknown";

/** 第一次超时提示的时间点：只发通知，不改变任务状态。 */
export const SLOW_REQUEST_NOTICE_MS = 30_000;

/**
 * 硬性上限。
 *
 * 超时不再把请求判失败之后，必须留一条真正的兜底：后端**进程还活着但永远不回**时
 * 请求不能永久挂着。30 分钟远大于任何正常长任务（多 GB 迁移/下载），
 * 拿它当"确实拿不到结果"的界线，且结果仍要标成 timedOut（不是 failed）。
 */
export const REQUEST_HARD_DEADLINE_MS = 30 * 60_000;

export interface CommandResultLike {
  ok: boolean;
  error?: string;
  /** 请求在时限内没返回（后端仍在处理）。 */
  timedOut?: boolean;
  /** 请求没能发出（后端未运行 / 未初始化 / 未就绪）。 */
  unavailable?: boolean;
}

export function classifyCommandResult(result: CommandResultLike | null | undefined): RequestOutcome {
  if (!result) return "failed";
  if (result.ok) return "ok";
  if (result.timedOut) return "timeout";
  if (result.unavailable) return "unavailable";
  return "failed";
}

/**
 * 请求返回后，**任务本身**处在什么阶段。
 *
 * 这张表就是缺陷 #4 的核心断言：`timeout` → `running`。
 * 别的映射（尤其"超时 = 失败"）都是错误口径。
 */
export function taskPhaseAfter(outcome: RequestOutcome): TaskPhase {
  switch (outcome) {
    case "ok":
      return "done";
    case "timeout":
      return "running";
    case "failed":
    case "unavailable":
      return "failed";
    default:
      return "unknown";
  }
}

/** 任务是否仍在进行（界面据此显示"进行中"，而不是"失败"）。 */
export function isTaskRunning(outcome: RequestOutcome): boolean {
  return taskPhaseAfter(outcome) === "running";
}

/**
 * 任务还能继续跑时，退出保护必须保留。
 *
 * 对应 `views/migration.ts` 里 `app.setBusy(true)` / `setBusy(false)` 那对调用：
 * 超时之后不能解除 —— 数据还在搬。
 */
export function keepsQuitGuard(outcome: RequestOutcome): boolean {
  return isTaskRunning(outcome);
}

/**
 * 给用户的一句话。
 *
 * 超时那一支**不能出现"失败"二字**：它只是"还没回来"。
 */
export function describeOutcome(outcome: RequestOutcome, command: string): string {
  switch (outcome) {
    case "ok":
      return `${command} 已完成`;
    case "timeout":
      return `${command} 尚未返回：任务可能仍在进行，完成后会有结果或进度（超时既不是失败，也不代表已取消）`;
    case "unavailable":
      return `${command} 未执行：后端未运行`;
    default:
      return `${command} 失败`;
  }
}

/* ------------------------------------------------------------ 后台任务 */

/**
 * 任务状态（后端 `job` 事件与 `job_status` 的 status 字段）。
 *
 * **这里的词汇表必须与后端 `frontend/backend/job_runner.py` 的状态机逐字一致。**
 * 权威定义在那边（它才是状态机的实现），前端只是消费方。历史上前端用的是
 * 另一套自造词（`done`/`error`/`pending`/`started`），结果是后端说 `succeeded`
 * 而前端不认识 → `jobPhase()` 落到 "unknown"，任务跑完了界面也不显示"已完成"，
 * 而且**不报错**。这类静默漂移由 `tests/test_contracts.py` 的棘轮清单盯着。
 *
 * 六个状态：`queued` / `running` / `cancelling` 是进行中（**`cancelling` 不等于
 * 已取消**，任务还在收尾）；`succeeded` / `failed` / `cancelled` 是终态。
 * `cancelled` 既不是成功也不是失败 —— 界面不能显示成"完成"，也不能显示成"失败"。
 */
export const JOB_RUNNING_STATUSES: readonly string[] = ["queued", "running", "cancelling"];
export const JOB_TERMINAL_STATUSES: readonly string[] = ["succeeded", "failed", "cancelled"];

export interface JobEventLike {
  jobId: string;
  command: string;
  status: string;
  sequence: number | null;
  /** 失败/取消的原因（后端 `error` 字段）。 */
  error: string | null;
  /** 可识别错误码（后端 `code` 字段），例如 `cancelled`。 */
  code: string | null;
}

/** 解析后端 `job` 事件；字段不全时返回 null（不猜）。 */
export function parseJobEvent(raw: unknown): JobEventLike | null {
  if (!raw || typeof raw !== "object") return null;
  const source = raw as Record<string, unknown>;
  const jobId = source["jobId"];
  const status = source["status"];
  const command = source["command"];
  if (typeof jobId !== "string" || jobId === "") return null;
  if (typeof status !== "string" || status === "") return null;
  return {
    jobId,
    command: typeof command === "string" ? command : "",
    status,
    sequence: typeof source["sequence"] === "number" ? source["sequence"] : null,
    error: typeof source["error"] === "string" ? source["error"] : null,
    code: typeof source["code"] === "string" ? source["code"] : null,
  };
}

/** 是不是终态（到了终态就不该再等后续事件）。 */
export function isTerminalJobStatus(status: string): boolean {
  return JOB_TERMINAL_STATUSES.includes(status);
}

/**
 * 任务状态映射到任务阶段。
 *
 * `cancelled` 映射成 `unknown` 而不是 `failed`：用户主动取消不是失败，
 * 它也不该让界面弹出"失败"的红字。是不是终态由 `isTerminalJobStatus` 单独回答。
 */
export function jobPhase(status: string): TaskPhase {
  if (status === "succeeded") return "done";
  if (status === "failed") return "failed";
  if (JOB_RUNNING_STATUSES.includes(status)) return "running";
  return "unknown";
}

/** `job` 事件是否代表任务失败（只认 failed，取消不算）。 */
export function isJobFailure(status: string): boolean {
  return status === "failed";
}
