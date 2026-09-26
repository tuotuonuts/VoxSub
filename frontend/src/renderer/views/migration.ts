/**
 * 旧版迁移向导 —— 首次启动时识别老用户，引导数据搬迁。
 *
 * 设计要点（按已确认的方案）：
 *   · 只读检测：进入向导不写任何文件，用户确认后才动数据
 *   · 分级触发：safe 不打扰，conditional 建议，exposed 强制
 *   · 复制优先：任何情况下都不先删源
 *   · 逐项进度：每项独立进度条 + 总进度
 *   · 校验后报告：三层校验，失败给可复制的诊断摘要
 *   · 失败不退出：留在应用里并给出日志位置（关掉程序用户反而看不到原因）
 *
 * 为什么是独立一页而不是弹窗：步骤多（检测→清单→确认→进度→报告），
 * 弹窗塞不下且用户无法回看。页面化后每一步都能返回上一步。
 */
import { h, on } from "../dom";
import { call, callWithOutcome, store } from "../store";
import { CMD, type CleanupResult } from "../protocol";
import { tr } from "../i18n";
import { PageLifecycle, type PageHandle } from "../../shared/page-lifecycle";
import { runAfterConfirm } from "../../shared/confirm-action";
import { buildProgressBar, type ProgressBar } from "../ui/progress";
import { buildStatusRow } from "../ui/status-row";
import { describeOutcome, isTerminalJobStatus, parseJobEvent } from "../../shared/request-outcome";
import {
  buildCleanupRequest,
  cleanupNotice,
  cleanupSucceeded,
  isCleanupRequest,
} from "../../shared/migration-cleanup";

/* ---------------------------------------------------------------- 类型 */

interface LegacyInfo {
  found: boolean;
  install_location: string;
  version: string;
  display_name: string;
  uninstall_string: string;
  registry_key: string;
  source: string;
  notes: string[];
}

interface StorageCheck {
  key: string;
  path: string;
  exists: boolean;
  bytes: number;
  file_count: number;
  inside_install: boolean;
  risk: "safe" | "conditional" | "exposed";
  purpose: string;
  detail: string;
}

interface MigrationStep {
  key: string;
  source: string;
  target: string;
  bytes: number;
  file_count: number;
  purpose: string;
  mode: string;
  note: string;
  rebuildable: boolean;
}

interface DetectResult {
  legacy: LegacyInfo;
  storage: StorageCheck[];
  overallRisk: "safe" | "conditional" | "exposed";
}

interface PlanResult {
  targetRoot: string;
  steps: MigrationStep[];
  totalBytes: number;
  freeBytes: number;
}

interface VerifyLayer {
  ok: boolean | null;
  detail?: string;
  checked?: number;
  missing?: string[];
  mismatch?: string[];
}

interface MigrationReport {
  done: Array<{
    key: string;
    mode: string;
    target: string;
    verify: { ok: boolean };
    /**
     * 后端为这次搬迁生成的记录 ID。
     *
     * 清理原目录只能用它（`cleanup_migrated_source` 已不再接受 `path`）。
     * 后端不返回时该字段缺失：界面就不提供"清理"入口，而不是硬拼一个路径去调。
     */
    recordId?: string;
  }>;
  failed: Array<{ key: string; error: string; verify?: { layers: Record<string, VerifyLayer> } }>;
  elapsedMs: number;
  ok: boolean;
}

type Step = "detect" | "overview" | "plan" | "running" | "report";

/* ---------------------------------------------------------------- 状态 */

let containerEl: HTMLElement | null = null;
let wizardRenderGeneration = 0;
let planRequestGeneration = 0;
/**
 * 向导级生命周期（缺陷 #10）：逐步切换时各步自己退订，但整个向导被
 * 关闭/替换时也要能一次收掉（原先只靠 `voxsub:leaving` 事件，页面被顶掉时
 * 事件不会来人，订阅于是留着不走）。
 */
let wizardLifecycle: PageLifecycle | null = null;
let current: Step = "detect";
let detection: DetectResult | null = null;
let plan: PlanResult | null = null;
let report: MigrationReport | null = null;
/** 清理操作结果归对应迁移运行持有，页面重建时可恢复而不依赖旧 DOM。 */
type CleanupState = { skipped: boolean; detail: string; ok?: boolean; pending?: boolean; uncertain?: boolean };
/** 最新迁移任务由应用级运行记录持有，不能随某一页的 dispose 一起释放。 */
let migrationRun: MigrationRun | null = null;
let cleanupState: CleanupState | null = null;
/** 用户在清单里勾选的项（key 集合）。 */
const selected = new Set<string>();
/** 迁移期间后端推来的每项进度：key -> 百分比 */
const progress = new Map<string, number>();
const progressPhase = new Map<string, "start" | "progress" | "done" | "failed">();
let overallProgress = 0;

type MigrationJobCompletion = {
  jobId: string;
  status: string;
  error: string | null;
  result: unknown;
};

type MigrationRunPhase = "submitting" | "running" | "unknown" | "terminal";

interface MigrationJobTracker {
  waitFor(): Promise<MigrationJobCompletion | null>;
  setJobId(jobId: string): void;
  jobId(): string | null;
  peek(): { settled: boolean; completion: MigrationJobCompletion | null };
  confirmedExit(): boolean;
  dispose(): void;
}

interface MigrationRun {
  token: symbol;
  clientMigrationId: string;
  jobId: string | null;
  phase: MigrationRunPhase;
  awaitingReport: boolean;
  report: MigrationReport | null;
  cleanupState: CleanupState | null;
  cleanupOperationId: number;
  statusQueryGeneration: number;
  tracker: MigrationJobTracker;
}

function isTerminalMigrationRun(run: MigrationRun): boolean {
  return run.phase === "terminal" && !run.awaitingReport;
}

interface CleanupOperationOwner {
  requestId: number;
  busyOwner: string;
  run: MigrationRun;
  report: MigrationReport;
  lifecycle: PageLifecycle;
  host: HTMLElement;
  renderGeneration: number;
}

/** 按本次迁移 ID 跟踪终态；它独立于任何页面生命周期。 */
function createMigrationJobTracker(clientMigrationId: string): MigrationJobTracker {
  let expectedJobId: string | null = null;
  const completions = new Map<string, MigrationJobCompletion>();
  let disconnected = false;
  let waiter: ((value: MigrationJobCompletion | null) => void) | null = null;
  let waitPromise: Promise<MigrationJobCompletion | null> | null = null;
  const matchingCompletion = (): MigrationJobCompletion | null => {
    if (expectedJobId) return completions.get(expectedJobId) ?? null;
    return completions.values().next().value ?? null;
  };
  const notify = (): void => {
    if (!waiter) return;
    const completion = matchingCompletion();
    if (!completion && !disconnected) return;
    const resolve = waiter;
    waiter = null;
    waitPromise = null;
    resolve(completion);
  };
  const off = window.voxsub?.backend.onEvent((raw) => {
    if (!raw || typeof raw !== "object") return;
    const event = raw as Record<string, unknown>;
    if (event["type"] === "migration") {
      recordMigrationProgress(event);
      return;
    }
    if (event["type"] === "disconnected") {
      disconnected = true;
      notify();
      return;
    }
    if (event["type"] !== "job") return;
    const matchesClientRequest = event["clientMigrationId"] === clientMigrationId;
    const matchesTrackedJob = Boolean(expectedJobId) && event["jobId"] === expectedJobId;
    if (!matchesClientRequest && !matchesTrackedJob) return;
    const job = parseJobEvent(event);
    if (!job || job.command !== CMD.startMigration) return;
    if (expectedJobId && expectedJobId !== job.jobId) return;
    expectedJobId = job.jobId;
    if (!isTerminalJobStatus(job.status)) return;
    if (!completions.has(job.jobId)) {
      completions.set(job.jobId, {
        jobId: job.jobId,
        status: job.status,
        error: job.error,
        result: event["result"],
      });
    }
    notify();
  });
  return {
    waitFor: () => {
      const completion = matchingCompletion();
      if (completion) return Promise.resolve(completion);
      if (disconnected) return Promise.resolve(null);
      if (!waitPromise) {
        waitPromise = new Promise((resolve) => { waiter = resolve; });
      }
      return waitPromise;
    },
    setJobId: (jobId) => {
      if (expectedJobId && expectedJobId !== jobId) return;
      expectedJobId = jobId;
      notify();
    },
    jobId: () => expectedJobId,
    peek: () => ({ settled: Boolean(matchingCompletion()) || disconnected, completion: matchingCompletion() }),
    confirmedExit: () => disconnected,
    dispose: () => {
      off?.();
      if (waiter) {
        const resolve = waiter;
        waiter = null;
        waitPromise = null;
        resolve(null);
      }
    },
  };
}

function isMigrationReport(value: unknown): value is MigrationReport {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return Array.isArray(candidate["done"]) && Array.isArray(candidate["failed"]) &&
    typeof candidate["elapsedMs"] === "number" && typeof candidate["ok"] === "boolean";
}

function failedMigrationReport(error: string): MigrationReport {
  return { done: [], failed: [{ key: "?", error }], elapsedMs: 0, ok: false };
}

function recordMigrationProgress(event: Record<string, unknown>): void {
  const key = event["key"];
  const phase = event["phase"];
  if (typeof key !== "string" || (phase !== "start" && phase !== "progress" && phase !== "done" && phase !== "failed")) return;

  progressPhase.set(key, phase);
  if (phase === "done" || phase === "failed") {
    progress.set(key, 100);
  } else if (phase === "progress") {
    const completed = typeof event["completed"] === "number" ? event["completed"] : 0;
    const total = typeof event["total"] === "number" ? event["total"] : 0;
    const raw = total ? Math.round((completed / total) * 100) : 100;
    progress.set(key, Math.max(0, Math.min(100, raw)));
  }

  const chosen = plan?.steps.filter((step) => selected.has(step.key)) ?? [];
  const finished = chosen.filter((step) => progress.get(step.key) === 100).length;
  overallProgress = chosen.length ? Math.round((finished / chosen.length) * 100) : 0;
}


/* ---------------------------------------------------------------- 工具 */

function human(bytes: number): string {
  if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(2)} GB`;
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`;
  if (bytes >= 1 << 10) return `${(bytes / (1 << 10)).toFixed(0)} KB`;
  return `${bytes} B`;
}

const RISK_CLASS: Record<string, string> = {
  safe: "is-ok",
  conditional: "is-warn",
  exposed: "is-fail",
};

/**
 * 风险等级 → 结果行等级。
 *
 * 业务词汇（safe/conditional/exposed）留在这里，组件只认 ok/warn/fail ——
 * 这样"状态 → 类名 + 标记符号"的映射只有 buildStatusRow 一处，而"风险等级叫什么"
 * 仍然只由迁移页定义。
 */
const RISK_LEVEL: Record<string, "ok" | "warn" | "fail"> = {
  safe: "ok",
  conditional: "warn",
  exposed: "fail",
};

/* ---------------------------------------------------------------- 渲染 */

function render(): void {
  if (!containerEl) return;
  wizardRenderGeneration += 1;

  const views: Record<Step, () => HTMLElement> = {
    detect: viewDetect,
    overview: viewOverview,
    plan: viewPlan,
    running: viewRunning,
    report: viewReport,
  };
  containerEl.replaceChildren(views[current]());
}

function isCurrentWizardView(
  lifecycle: PageLifecycle,
  host: HTMLElement,
  renderGeneration: number,
  expectedStep?: Step,
): boolean {
  return wizardLifecycle === lifecycle &&
    containerEl === host &&
    !lifecycle.disposed &&
    wizardRenderGeneration === renderGeneration &&
    (expectedStep === undefined || current === expectedStep);
}

/** 步骤指示器：让用户知道走到哪一步了。 */
function stepBar(): HTMLElement {
  const steps: Array<[Step, string]> = [
    ["detect", "检测旧版"],
    ["overview", "数据风险"],
    ["plan", "确认清单"],
    ["running", "正在迁移"],
    ["report", "结果"],
  ];
  const bar = h("div", { class: "wiz__steps" });
  const currentIndex = steps.findIndex(([id]) => id === current);

  steps.forEach(([, label], index) => {
    const item = h("div", {
      class:
        index === currentIndex
          ? "wiz__step is-active"
          : index < currentIndex
            ? "wiz__step is-done"
            : "wiz__step",
    });
    item.append(
      h("span", { class: "wiz__step-no", text: index < currentIndex ? "✓" : String(index + 1) }),
      h("span", { class: "wiz__step-label", text: label }),
    );
    bar.append(item);
  });
  return bar;
}

function frame(title: string, subtitle: string, body: HTMLElement, actions: HTMLElement): HTMLElement {
  const page = h("div", { class: "wiz" });
  const head = h("header", { class: "wiz__head" });
  head.append(
    h("h1", { class: "wiz__title", text: title }),
    h("p", { class: "wiz__sub", text: subtitle }),
  );
  page.append(stepBar(), head, body, actions);
  return page;
}

function actionsRow(buttons: HTMLElement[]): HTMLElement {
  const row = h("div", { class: "wiz__actions" });
  row.append(...buttons);
  return row;
}

function button(label: string, kind: "primary" | "ghost", onClick: () => void): HTMLElement {
  const btn = h("button", {
    class: kind === "primary" ? "btn btn--primary" : "btn btn--ghost",
    type: "button",
    text: label,
  });
  on(btn, "click", onClick);
  return btn;
}

/* ------------------------------------------------------------ 步骤 1 */

function viewDetect(): HTMLElement {
  const body = h("div", { class: "wiz__body" });

  if (!detection) {
    body.append(h("p", { class: "hint", text: tr("正在检测旧版安装…") }));
    return frame(tr("检测旧版"), tr("只读取信息，不会修改任何文件"), body,
      actionsRow([button(tr("关闭"), "ghost", () => closeWizard())]));
  }

  const legacy = detection.legacy;
  const card = h("div", { class: "card" });
  card.append(h("h3", { class: "card__title", text: tr("检测结果") }));

  const info = h("div", { class: "card__body" });
  const rows: Array<[string, string]> = [
    [tr("状态"), legacy.found ? tr("检测到旧版") : tr("未检测到旧版")],
    [tr("版本"), legacy.version || "—"],
    [tr("安装位置"), legacy.install_location || "—"],
    [tr("来源"), legacy.source === "registry" ? tr("系统卸载记录") : tr("配置文件")],
  ];
  for (const [key, value] of rows) {
    const row = h("div", { class: "kv" });
    row.append(h("span", { class: "kv__key", text: key }), h("span", { class: "kv__value", text: value }));
    info.append(row);
  }
  if (legacy.notes.length) {
    for (const note of legacy.notes) {
      info.append(h("p", { class: "field__hint", text: note }));
    }
  }
  card.append(info);
  body.append(card);

  const buttons: HTMLElement[] = [];
  if (legacy.found) {
    buttons.push(button(tr("查看数据风险"), "primary", () => go("overview")));
  }
  buttons.push(button(legacy.found ? tr("稍后再说") : tr("关闭"), "ghost", () => closeWizard()));

  return frame(
    tr("检测到旧版 VoxSub"),
    tr("新版本可以直接沿用旧版的模型与设置，无需重复下载"),
    body,
    actionsRow(buttons),
  );
}

/* ------------------------------------------------------------ 步骤 2 */

function viewOverview(): HTMLElement {
  const body = h("div", { class: "wiz__body" });
  const checks = detection?.storage ?? [];
  const risk = detection?.overallRisk ?? "safe";

  // 整体结论先行：用户最想知道"要不要做点什么"
  const verdict = h("div", { class: `wiz__verdict ${RISK_CLASS[risk]}` });
  const verdictText: Record<string, string> = {
    safe: "你的数据都在安全位置，可以直接使用新版本，无需迁移。",
    conditional: "数据与旧版放在同一目录。卸载旧版本身不会删除它，但如果你手动删除整个安装文件夹，数据会一起丢失。建议复制到独立位置。",
    exposed: "有数据位于旧版安装器会删除的目录中。卸载旧版会连同这些数据一起删除，必须先迁移。",
  };
  verdict.append(h("p", { text: tr(verdictText[risk] ?? "") }));
  body.append(verdict);

  const list = h("div", { class: "check-list" });
  for (const check of checks) {
    // 状态 → 结果行交给 buildStatusRow：类名与标记符号的映射只该有一处。
    // 业务词汇（safe/conditional/exposed）到 ok/warn/fail 的翻译留在本页 ——
    // 组件刻意不认识业务词汇。
    // 未知等级退到 "fail"：原实现会给空类名 + "✕" 标记，这里类名变成 is-fail；
    // 风险等级只由后端 assess_storage 产出三种值，未知值不可能出现。
    list.append(buildStatusRow({
      level: RISK_LEVEL[check.risk] ?? "fail",
      name: check.key,
      detail: [
        h("strong", { text: `${human(check.bytes)} · ${check.file_count} ${tr("文件")}` }),
        h("br"),
        h("code", { text: check.path }),
        ...(check.purpose ? [h("br"), h("span", { text: check.purpose })] : []),
        ...(check.detail ? [h("br"), h("span", { text: check.detail })] : []),
      ],
    }));
  }
  body.append(list);

  const lifecycle = wizardLifecycle;
  const host = containerEl;
  const viewGeneration = wizardRenderGeneration;
  const buttons: HTMLElement[] = [button(tr("返回"), "ghost", () => go("detect"))];
  if (risk !== "safe" || checks.some((c) => c.key === "models")) {
    buttons.push(button(tr("规划迁移"), "primary", () => {
      if (!lifecycle || !host || !isCurrentWizardView(lifecycle, host, viewGeneration, "overview")) return;
      void loadPlan(lifecycle, host, viewGeneration);
    }));
  }
  buttons.push(button(tr("暂时跳过"), "ghost", () => closeWizard()));

  return frame(tr("数据风险"), tr("列出旧版留下的数据及其位置"), body, actionsRow(buttons));
}

/* ------------------------------------------------------------ 步骤 3 */

async function loadPlan(
  lifecycle: PageLifecycle,
  host: HTMLElement,
  viewGeneration: number,
): Promise<void> {
  const requestGeneration = ++planRequestGeneration;
  const result = await call<PlanResult>(CMD.planMigration, {});
  if (requestGeneration !== planRequestGeneration ||
      !isCurrentWizardView(lifecycle, host, viewGeneration, "overview")) return;
  if (!result) return;
  plan = result;

  // 默认勾选：非可重建项（tools 这类程序能自取的默认不选）
  selected.clear();
  for (const step of result.steps) {
    if (!step.rebuildable) selected.add(step.key);
  }
  go("plan");
}

function viewPlan(): HTMLElement {
  const body = h("div", { class: "wiz__body" });
  const lifecycle = wizardLifecycle;
  const host = containerEl;
  const viewGeneration = wizardRenderGeneration;
  const isCurrentPlan = (): boolean => Boolean(
    lifecycle && host && isCurrentWizardView(lifecycle, host, viewGeneration, "plan"),
  );

  if (!plan || plan.steps.length === 0) {
    body.append(h("p", { class: "hint", text: tr("没有需要迁移的项目") }));
    return frame(tr("确认迁移清单"), "", body,
      actionsRow([button(tr("返回"), "ghost", () => {
        if (isCurrentPlan()) go("overview");
      })]));
  }

  // 空间检查：不够就直接拦住，别等复制到一半才失败
  const enough = plan.freeBytes > plan.totalBytes;
  if (!enough) {
    const warn = h("div", { class: "wiz__verdict is-fail" });
    warn.append(h("p", {
      text: `${tr("目标磁盘空间不足")}：${tr("需要")} ${human(plan.totalBytes)}，${tr("可用")} ${human(plan.freeBytes)}`,
    }));
    body.append(warn);
  }

  const targetRow = h("div", { class: "wiz__target" });
  targetRow.append(
    h("span", { class: "kv__key", text: tr("迁移到") }),
    h("code", { text: plan.targetRoot }),
  );
  body.append(targetRow);

  const list = h("div", { class: "mig-list" });
  for (const step of plan.steps) {
    const item = h("label", { class: "mig-item" });

    const checkbox = h("input", { type: "checkbox" }) as HTMLInputElement;
    checkbox.checked = selected.has(step.key);
    on(checkbox, "change", () => {
      if (!isCurrentPlan()) return;
      if (checkbox.checked) selected.add(step.key);
      else selected.delete(step.key);
      updateSummary();
    });

    const info = h("div", { class: "mig-item__info" });
    info.append(
      h("div", { class: "mig-item__head" }, [
        h("strong", { text: step.key }),
        h("span", { class: "mig-item__size", text: `${human(step.bytes)} · ${step.file_count} ${tr("文件")}` }),
        ...(step.rebuildable
          ? [h("span", { class: "mig-item__tag", text: tr("程序可自行重建") })]
          : []),
      ]),
      h("div", { class: "mig-item__path" }, [
        h("code", { text: step.source }),
        h("span", { class: "mig-item__arrow", text: "→" }),
        h("code", { text: step.target }),
      ]),
      step.purpose ? h("p", { class: "mig-item__purpose", text: step.purpose }) : h("span"),
      h("p", { class: "mig-item__note", text: step.note }),
    );

    item.append(checkbox, info);
    list.append(item);
  }
  body.append(list);

  const summary = h("p", { class: "wiz__summary" });
  body.append(summary);

  const proceed = button(tr("开始迁移"), "primary", () => {
    if (!lifecycle || !host || !isCurrentPlan()) return;
    void startMigration(lifecycle, host, viewGeneration, "plan");
  });
  proceed.id = "wiz-proceed";

  function updateSummary(): void {
    if (!isCurrentPlan()) return;
    const chosen = plan!.steps.filter((s) => selected.has(s.key));
    const total = chosen.reduce((sum, s) => sum + s.bytes, 0);
    summary.textContent = `${tr("已选")} ${chosen.length} ${tr("项")} · ${human(total)}`;
    (proceed as HTMLButtonElement).disabled = chosen.length === 0 || !enough;
  }
  // 先渲染再算一次，保证初始摘要正确
  queueMicrotask(updateSummary);

  return frame(
    tr("确认迁移清单"),
    tr("请核对每一项的来源、去向与用途。迁移过程中不会删除原始数据"),
    body,
    actionsRow([
      button(tr("返回"), "ghost", () => {
        if (isCurrentPlan()) go("overview");
      }),
      proceed,
      button(tr("取消"), "ghost", () => {
        if (isCurrentPlan()) closeWizard();
      }),
    ]),
  );
}

/* ------------------------------------------------------------ 步骤 4 */

let nextMigrationClientRequestId = 0;
let nextCleanupOperationId = 0;

function showMigrationRunStatus(run: MigrationRun, statusText: string): void {
  if (migrationRun !== run || current !== "running" || !containerEl || !wizardLifecycle || wizardLifecycle.disposed) return;
  store.patch({ statusText });
}

async function queryMigrationRunStatus(run: MigrationRun): Promise<void> {
  if (migrationRun !== run || run.phase === "terminal") return;
  const generation = ++run.statusQueryGeneration;
  const isCurrent = (): boolean =>
    migrationRun === run && run.phase !== "terminal" && run.statusQueryGeneration === generation;

  try {
    let jobId = run.jobId ?? run.tracker.jobId();
    if (!jobId) {
      const list = await call<{ jobs?: unknown }>(CMD.jobList);
      if (!isCurrent()) return;
      const jobs = Array.isArray(list) ? list : (Array.isArray(list?.jobs) ? list.jobs : []);
      const active = jobs.filter((candidate) => {
        if (!candidate || typeof candidate !== "object") return false;
        const job = candidate as Record<string, unknown>;
        const status = job["status"];
        return job["command"] === CMD.startMigration &&
          (status === "queued" || status === "running" || status === "cancelling") &&
          typeof job["jobId"] === "string" && job["jobId"] !== "";
      });
      if (active.length !== 1) {
        showMigrationRunStatus(run, tr("无法唯一确认迁移任务状态；退出保护仍保持开启。"));
        return;
      }
      jobId = (active[0] as Record<string, unknown>)["jobId"] as string;
    }

    const result = await call<{ ok?: unknown; job?: unknown }>(CMD.jobStatus, { job_id: jobId });
    if (!isCurrent()) return;
    if (result?.ok !== true || !result.job || typeof result.job !== "object") {
      showMigrationRunStatus(run, tr("暂时无法查询迁移任务状态；退出保护仍保持开启。"));
      return;
    }

    const job = result.job as Record<string, unknown>;
    const status = job["status"];
    if (job["jobId"] !== jobId || job["command"] !== CMD.startMigration || typeof status !== "string") {
      showMigrationRunStatus(run, tr("迁移状态查询与当前任务不匹配；退出保护仍保持开启。"));
      return;
    }

    const trackedJobId = run.tracker.jobId();
    if (trackedJobId && trackedJobId !== jobId) {
      showMigrationRunStatus(run, tr("迁移状态查询与已跟踪任务不匹配；退出保护仍保持开启。"));
      return;
    }
    run.jobId = jobId;
    run.tracker.setJobId(jobId);
    if (isTerminalJobStatus(status)) {
      finishMigrationRun(run, {
        jobId,
        status,
        error: typeof job["error"] === "string" ? job["error"] : null,
        result: job["result"],
      }, undefined, true);
      return;
    }
    if (status === "queued" || status === "running" || status === "cancelling") {
      run.phase = "running";
      const message = status === "queued"
        ? tr("迁移任务仍在队列中，退出保护保持开启。")
        : status === "cancelling"
          ? tr("迁移任务正在取消；等待后台确认终态后再退出。")
          : tr("后台确认迁移仍在运行，退出保护保持开启。");
      showMigrationRunStatus(run, message);
      return;
    }

    run.phase = "unknown";
    showMigrationRunStatus(run, tr("迁移状态仍未知；退出保护保持开启，可再次查询。"));
  } catch {
    if (isCurrent()) {
      showMigrationRunStatus(run, tr("暂时无法查询迁移任务状态；退出保护仍保持开启。"));
    }
  }
}

function finishMigrationRun(
  run: MigrationRun,
  completion: MigrationJobCompletion | null,
  rejectedMessage?: string,
  awaitReport = false,
): void {
  if (migrationRun !== run || isTerminalMigrationRun(run)) return;
  const ownsVisibleReport = current === "report" && report === run.report;

  let nextReport: MigrationReport;
  if (completion && run.jobId && completion.jobId !== run.jobId) {
    nextReport = failedMigrationReport(tr("迁移回执与终态任务编号不匹配；请重新检测，不要清理原目录。"));
  } else if (completion?.status === "succeeded" && isMigrationReport(completion.result)) {
    nextReport = completion.result;
  } else if (completion?.status === "succeeded") {
    nextReport = failedMigrationReport(tr("迁移任务已结束，但未收到有效报告；请重新检测，不要清理原目录。"));
  } else if (completion) {
    const detail = completion.error || (completion.status === "cancelled"
      ? tr("迁移任务已取消")
      : tr("迁移任务失败"));
    nextReport = failedMigrationReport(detail);
  } else {
    nextReport = failedMigrationReport(rejectedMessage ??
      tr("后端已断开，迁移结果未知；请重新检测目标目录，不要清理原目录。"));
  }

  if (completion && !run.jobId) run.jobId = completion.jobId;
  run.phase = "terminal";
  // job_status can confirm completion before the event carrying the full report arrives.
  run.awaitingReport = awaitReport && completion?.status === "succeeded" && !isMigrationReport(completion.result);
  run.report = nextReport;
  report = nextReport;
  if (!run.awaitingReport) run.tracker.dispose();
  try {
    void Promise.resolve(window.voxsub?.app.setBusy(false, undefined, run.clientMigrationId)).catch(() => undefined);
  } catch {
    // 退出路径尽力撤销保护；窗口可能已在关闭。
  }
  if ((current === "running" || ownsVisibleReport) && containerEl && wizardLifecycle && !wizardLifecycle.disposed) go("report");
}

async function monitorMigrationRun(run: MigrationRun, steps: PlanResult["steps"]): Promise<void> {
  const terminal = run.tracker.waitFor();
  const submission = callWithOutcome<{ jobId?: unknown; accepted?: unknown }>(
    CMD.startMigration,
    { steps, async: true, clientMigrationId: run.clientMigrationId },
  ).then(
    (result) => ({ kind: "receipt" as const, result }),
    (error: unknown) => ({ kind: "exception" as const, error }),
  );
  const first = await Promise.race([
    submission,
    terminal.then((completion) => ({ kind: "terminal" as const, completion })),
  ]);
  if (migrationRun !== run) return;
  if (run.phase === "terminal") {
    if (run.awaitingReport) finishMigrationRun(run, await terminal);
    return;
  }
  const observedJobId = run.tracker.jobId();
  if (observedJobId) run.jobId = observedJobId;

  if (first.kind === "terminal") {
    finishMigrationRun(run, first.completion);
    return;
  }

  if (first.kind === "exception") {
    run.phase = "unknown";
    store.pushLog({
      ts: new Date().toISOString(),
      level: "WARNING",
      message: "迁移提交回执异常，任务可能仍在运行；继续等待匹配的后台终态。",
    });
    showMigrationRunStatus(run, tr("迁移提交回执状态未知，任务可能仍在运行；正在等待后台终态。"));
  } else {
    const { outcome, data, delivery } = first.result;
    const jobId = data?.accepted === true && typeof data.jobId === "string" && data.jobId.length > 0
      ? data.jobId
      : null;
    if (jobId && (!observedJobId || observedJobId === jobId)) {
      run.jobId = jobId;
      run.tracker.setJobId(jobId);
    }

    const cached = run.tracker.peek();
    if (cached.completion) {
      finishMigrationRun(run, cached.completion);
      return;
    }
    if (run.tracker.confirmedExit()) {
      finishMigrationRun(run, null);
      return;
    }

    if (delivery === "response" && outcome === "failed" && first.result.code === "active_job_exists") {
      const conflictingJobId = typeof first.result.jobId === "string" && first.result.jobId.length > 0
        ? first.result.jobId
        : null;
      if (conflictingJobId && (!observedJobId || observedJobId === conflictingJobId)) {
        run.jobId = conflictingJobId;
        run.tracker.setJobId(conflictingJobId);
        run.phase = "unknown";
        showMigrationRunStatus(run, tr("已有迁移任务正在运行；正在重新关联其状态，退出保护保持开启。"));
        void queryMigrationRunStatus(run);
      } else {
        run.phase = "unknown";
        showMigrationRunStatus(run, tr("已有迁移任务正在运行，但任务编号无法安全匹配；退出保护保持开启，可重新查询。"));
        if (!observedJobId) void queryMigrationRunStatus(run);
      }
    } else if (delivery === "not_sent" || (delivery === "response" && outcome !== "ok" && outcome !== "timeout")) {
      finishMigrationRun(run, null, describeOutcome(outcome, tr("数据迁移"), tr));
      return;
    }

    if (first.result.code !== "active_job_exists") {
      run.phase = delivery === "response" && outcome === "ok" && jobId ? "running" : "unknown";
      showMigrationRunStatus(run, run.phase === "running"
        ? tr("迁移任务已受理，正在等待后台完成。")
        : (outcome === "timeout"
          ? describeOutcome(outcome, tr("数据迁移"), tr)
          : tr("迁移受理回执不完整或状态未知，任务仍可能正在运行；正在等待后台终态。")));
    }
  }

  const completion = await terminal;
  if (migrationRun !== run || isTerminalMigrationRun(run)) return;
  if (completion) {
    finishMigrationRun(run, completion);
  } else if (run.tracker.confirmedExit()) {
    finishMigrationRun(run, null);
  } else {
    // 理论上只有终态或断连会兑现 tracker；若其生命周期意外结束，保留退出保护。
    run.phase = "unknown";
    showMigrationRunStatus(run, tr("迁移终态尚未确认，仍保留退出保护。"));
  }
}

async function startMigration(
  lifecycle: PageLifecycle,
  host: HTMLElement,
  viewGeneration: number,
  expectedStep: "plan" | "report",
): Promise<void> {
  if (!isCurrentWizardView(lifecycle, host, viewGeneration, expectedStep)) return;
  if (migrationRun && migrationRun.phase !== "terminal") {
    const message = tr("已有迁移任务仍在运行，不能重复提交。");
    if (current === "running") showMigrationRunStatus(migrationRun, message);
    else store.patch({ statusText: message });
    return;
  }
  if (migrationRun?.cleanupState?.pending || migrationRun?.cleanupState?.uncertain) {
    store.patch({ statusText: tr("上一次原目录清理仍在确认，不能开始新的迁移。") });
    return;
  }
  if (!plan) return;
  const steps = plan.steps.filter((s) => selected.has(s.key));
  if (steps.length === 0) return;

  const clientMigrationId = `migration-${Date.now().toString(36)}-${++nextMigrationClientRequestId}`;
  const run: MigrationRun = {
    token: Symbol("migration-run"),
    clientMigrationId,
    jobId: null,
    phase: "submitting",
    awaitingReport: false,
    report: null,
    cleanupState: null,
    cleanupOperationId: 0,
    statusQueryGeneration: 0,
    tracker: createMigrationJobTracker(clientMigrationId),
  };
  migrationRun?.tracker.dispose();
  migrationRun = run;
  report = null;
  progress.clear();
  progressPhase.clear();
  overallProgress = 0;
  cleanupState = null;

  // 退出保护必须先由主进程确认，再向后端提交可能持续很久的迁移任务。
  try {
    const busyReason = tr("数据正在迁移，请等待完成后再退出应用。");
    const acknowledgedBusyReason = await window.voxsub?.app.setBusy(true, busyReason, run.clientMigrationId);
    if (acknowledgedBusyReason !== busyReason) throw new Error("主进程未确认退出保护状态");
  } catch {
    finishMigrationRun(run, null, tr("无法启用退出保护，迁移未启动。"));
    if (isCurrentWizardView(lifecycle, host, viewGeneration, expectedStep)) go("report");
    return;
  }

  if (migrationRun !== run || run.phase === "terminal") return;
  if (isCurrentWizardView(lifecycle, host, viewGeneration, expectedStep)) go("running");
  // 即使原页面已关闭，提交仍归已受理的任务 owner；不能因页面销毁而丢弃。
  void monitorMigrationRun(run, steps);
}

function viewRunning(): HTMLElement {
  const body = h("div", { class: "wiz__body" });

  const warn = h("div", { class: "wiz__verdict is-warn" });
  warn.append(h("p", {
    text: tr("迁移进行中，请不要关闭应用。中断可能导致目标目录不完整（原始数据不会被删除）。"),
  }));
  body.append(warn);

  // 总进度 + 每项独立进度条（内芯用 JobProgress 组件：轨道 + 填充 + 标签）
  const overall = buildProgressBar({ labelText: `${overallProgress}%` });
  overall.setState("running");
  overall.setPercent(overallProgress);
  body.append(h("div", { class: "progress" }, [overall.track, overall.label]));

  // 每项独立进度条
  const list = h("div", { class: "mig-progress" });
  const chosen = plan?.steps.filter((s) => selected.has(s.key)) ?? [];
  /** 每项一条：按 key 取回，避免再从 DOM 上查 fill/label（查一次就多一处耦合）。 */
  const bars = new Map<string, ProgressBar>();
  for (const step of chosen) {
    const bar = buildProgressBar({ labelText: `${progress.get(step.key) ?? 0}%` });
    const savedProgress = progress.get(step.key);
    const savedPhase = progressPhase.get(step.key);
    if (savedProgress !== undefined) bar.setPercent(savedProgress);
    if (savedPhase === "start") {
      bar.setLabel(tr("进行中…"));
      bar.setState("running");
    } else if (savedPhase === "done") {
      bar.setLabel(tr("完成"));
      bar.setState("done");
    } else if (savedPhase === "failed") {
      bar.setLabel(tr("失败"));
      bar.setState("failed");
    } else if (savedProgress !== undefined) {
      bar.setLabel(`${savedProgress}%`);
      bar.setState("running");
    }
    const row = h("div", { class: "mig-progress__row" }, [
      h("span", { class: "mig-progress__name", text: step.key }),
      bar.track,
      bar.label,
    ]);
    row.dataset["key"] = step.key;
    bars.set(step.key, bar);
    list.append(row);
  }
  body.append(list);

  // 订阅后端进度事件，实时更新
  const off = window.voxsub?.backend.onEvent((raw) => {
    const event = raw as Record<string, unknown>;
    const key = event["key"];
    if (event["type"] !== "migration" || typeof key !== "string") return;

    recordMigrationProgress(event);
    const bar = bars.get(key);
    const phase = event["phase"];
    if (phase === "start") {
      bar?.setLabel(tr("进行中…"));
      return;
    }
    if (phase === "done" || phase === "failed") {
      if (bar) {
        bar.setPercent(100);
        bar.setLabel(phase === "done" ? tr("完成") : tr("失败"));
        bar.setState(phase === "done" ? "done" : "failed");
      }
    } else if (phase === "progress") {
      const pct = progress.get(key);
      if (bar && pct !== undefined) {
        bar.setPercent(pct);
        bar.setLabel(`${pct}%`);
      }
    }

    overall.setPercent(overallProgress);
    overall.setLabel(`${overallProgress}%`);
  });

  // 离开这一页时退订，避免回调打到已移除的节点
  body.addEventListener("voxsub:leaving", () => off?.(), { once: true });
  // 向导被整体替换/关闭时也要退订（缺陷 #10：订阅只增不减）
  wizardLifecycle?.add(() => off?.());

  const queryStatus = button(tr("查询迁移任务状态"), "ghost", () => {
    const run = migrationRun;
    if (run) void queryMigrationRunStatus(run);
  });
  queryStatus.id = "wiz-query-status";

  return frame(tr("正在迁移"), tr("每项数据有独立进度，完成后会自动校验"), body,
    actionsRow([queryStatus, h("span", { class: "tuning-actions__state", text: tr("请勿关闭应用") })]));
}

/* ------------------------------------------------------------ 步骤 5 */

/**
 * 报告页的清理按钮：先问，再动。
 *
 * 确认闸门（ConfirmAction）在这里负责三件事，缺一不可：
 *   · 不确认 → **绝不发清理命令**，只在报告页留下"已取消"的说明；
 *   · 确认 → 才进入 `cleanupMigratedSources`（那里构造带 `confirm: true` 的请求）；
 *   · 操作结果保存在所属迁移运行上；旧页回执不得导航或改写新页。
 */
function requestCleanup(
  lifecycle: PageLifecycle,
  host: HTMLElement,
  renderGeneration: number,
): void {
  if (!isCurrentWizardView(lifecycle, host, renderGeneration, "report")) return;
  const ownerRun = migrationRun;
  const ownerReport = report;
  if (!ownerRun || !ownerReport || ownerRun.report !== ownerReport || (ownerRun.cleanupState?.pending || ownerRun.cleanupState?.uncertain)) return;
  const records = ownerReport.done.filter((item) => item.recordId);

  runAfterConfirm(
    () => window.confirm(
      `${tr("删除是不可逆的")}：${tr("确认清理已迁移的原目录？")}\n${records.map((r) => r.key).join("、")}`,
    ),
    () => {
      if (!isCurrentWizardView(lifecycle, host, renderGeneration, "report") ||
          migrationRun !== ownerRun || ownerRun.report !== ownerReport) return;
      const requestId = ++nextCleanupOperationId;
      const owner: CleanupOperationOwner = {
        requestId,
        busyOwner: `${ownerRun.clientMigrationId}-cleanup-${requestId}`,
        run: ownerRun,
        report: ownerReport,
        lifecycle,
        host,
        renderGeneration,
      };
      ownerRun.cleanupOperationId = requestId;
      const pending: CleanupState = {
        skipped: false,
        detail: tr("清理请求已提交，正在等待后端确认；请勿重复执行。"),
        pending: true,
      };
      ownerRun.cleanupState = pending;
      cleanupState = pending;
      go("report");
      owner.renderGeneration = wizardRenderGeneration;
      void cleanupMigratedSources(records, owner);
    },
    () => {
      if (!isCurrentWizardView(lifecycle, host, renderGeneration, "report") ||
          migrationRun !== ownerRun || ownerRun.report !== ownerReport) return;
      const cancelled: CleanupState = {
        skipped: true,
        detail: tr("已取消：没有确认就不会删除任何原目录"),
      };
      ownerRun.cleanupState = cancelled;
      cleanupState = cancelled;
      go("report");
    },
  );
}

/**
 * 清理已迁移的原目录。完成结果归属迁移运行，而不是启动请求的页面。
 * 页面已释放时仍保存真实副作用结果；但只在原页面实例仍有效时更新 DOM、状态文案或导航。
 */
async function cleanupMigratedSources(
  records: Array<{ key: string; recordId?: string }>,
  owner: CleanupOperationOwner,
): Promise<void> {
  const decision = buildCleanupRequest(records, true);
  if (!isCleanupRequest(decision)) {
    const skipped: CleanupState = {
      skipped: true,
      detail: tr("没有可清理的记录（后端只接受记录 ID）"),
    };
    if (owner.run.cleanupOperationId !== owner.requestId || owner.run.report !== owner.report) return;
    owner.run.cleanupState = skipped;
    if (migrationRun === owner.run && isCurrentWizardView(owner.lifecycle, owner.host, owner.renderGeneration, "report")) {
      cleanupState = skipped;
      go("report");
    }
    return;
  }

  let result: CleanupResult | null = null;
  let delivery: "not_sent" | "unknown" | "response" = "not_sent";
  let outcome: "ok" | "timeout" | "failed" | "unavailable" = "unavailable";
  let guardConfirmed = false;
  try {
    const reason = tr("原目录正在清理，请等待确认结果后再退出应用。");
    const acknowledged = await window.voxsub?.app.setBusy(true, reason, owner.busyOwner);
    if (acknowledged !== reason) throw new Error("Quit protection was not acknowledged");
    guardConfirmed = true;
    delivery = "unknown";
    const response = await callWithOutcome<CleanupResult>(CMD.cleanupMigratedSource, decision);
    result = response.data;
    delivery = response.delivery;
    outcome = response.outcome;
  } catch {
    // 命令可能已送达后断连；保留“不确定，可能已执行”的口径，禁止伪装成未执行。
  }

  if (owner.run.cleanupOperationId !== owner.requestId || owner.run.report !== owner.report) return;
  const uncertain = delivery === "unknown" || outcome === "timeout";
  if (!uncertain) {
    try {
      await window.voxsub?.app.setBusy(false, undefined, owner.busyOwner);
    } catch {
      // Fail closed if main cannot acknowledge release.
    }
  }
  const detail = !guardConfirmed
    ? tr("无法启用退出保护，清理未启动。")
    : result
      ? cleanupNotice(result)
      : uncertain
        ? `${tr("清理结果未知，操作可能已执行；请先核对原目录，不要重复清理。")} ${tr("清理回执无法自动恢复；请保持应用开启并人工核对结果。")}`
        : describeOutcome(outcome, tr("清理已迁移的原目录"), tr);
  const succeeded = cleanupSucceeded(result);
  const completed: CleanupState = {
    skipped: false,
    detail,
    ok: succeeded,
    pending: false,
    uncertain,
  };
  owner.run.cleanupState = completed;

  const ownsVisibleReport = migrationRun === owner.run && report === owner.report &&
    isCurrentWizardView(owner.lifecycle, owner.host, owner.renderGeneration, "report");
  store.pushLog({
    ts: new Date().toISOString(),
    level: succeeded ? "INFO" : (uncertain ? "WARNING" : "ERROR"),
    message: `${tr("清理已迁移的原目录")}: ${detail}`,
  });
  if (!ownsVisibleReport) return;

  cleanupState = completed;
  store.patch({ statusText: detail });
  go("report");
}

function viewReport(): HTMLElement {
  const body = h("div", { class: "wiz__body" });
  const reportRun = migrationRun?.report === report ? migrationRun : null;
  cleanupState = reportRun?.cleanupState ?? null;
  if (!report) return frame(tr("迁移结果"), "", body, actionsRow([]));
  const lifecycle = wizardLifecycle;
  const host = containerEl;
  const viewGeneration = wizardRenderGeneration;
  const isCurrentReport = (): boolean => Boolean(
    lifecycle && host && isCurrentWizardView(lifecycle, host, viewGeneration, "report"),
  );

  const ok = report.ok;
  const verdict = h("div", { class: `wiz__verdict ${ok ? "is-ok" : "is-fail"}` });
  verdict.append(h("p", {
    text: ok
      ? `${tr("迁移完成")}：${report.done.length} ${tr("项已校验通过")}，${tr("耗时")} ${(report.elapsedMs / 1000).toFixed(1)}s`
      : `${tr("迁移未完全成功")}：${report.done.length} ${tr("项成功")}，${report.failed.length} ${tr("项失败")}`,
  }));
  body.append(verdict);

  // 成功项
  if (report.done.length) {
    const list = h("div", { class: "check-list" });
    for (const item of report.done) {
      // 成功项固定 ok，交给结果行组件（与上面风险清单同一处映射）。
      list.append(buildStatusRow({
        level: "ok",
        name: item.key,
        detail: item.target,
      }));
    }
    body.append(list);
  }

  // 清理区：搬迁完成后才问"要不要删掉原来的目录"。
  // 删除不可逆，所以这里的按钮只负责唤起一次确认，确认之后才带 confirm: true 发命令。
  const cleanable = report.done.filter((item) => item.recordId);
  if (cleanable.length > 0) {
    const card = h("div", { class: `card ${cleanupState?.ok === false ? "is-fail" : ""}` });
    card.append(h("h3", { class: "card__title", text: tr("原目录清理") }));
    card.append(h("p", {
      class: "field__hint",
      text: tr("数据已在新位置并通过校验。是否删除已迁移的原目录由你决定，删除不可逆。"),
    }));
    card.append(h("p", {
      class: `field__hint ${cleanupState?.ok === false ? "is-fail" : ""}`,
      text: cleanupState ? cleanupState.detail : `${cleanable.length} ${tr("项可清理")}`,
    }));
    const cleanupButton = button(tr("清理已迁移的原目录"), "ghost", () => {
      if (!lifecycle || !host) return;
      requestCleanup(lifecycle, host, viewGeneration);
    });
    (cleanupButton as HTMLButtonElement).disabled = Boolean(cleanupState?.pending || cleanupState?.uncertain);
    card.append(h("div", { class: "tuning-actions" }, [cleanupButton]));
    body.append(card);
  }

  // 失败项：给出可复制的摘要，便于反馈开发者
  if (report.failed.length) {
    const card = h("div", { class: "card" });
    card.append(h("h3", { class: "card__title", text: tr("失败详情（请复制给开发者）") }));
    const box = h("div", { class: "card__body" });

    const lines: string[] = [];
    for (const item of report.failed) {
      lines.push(`【${item.key}】${item.error}`);
      const layers = item.verify?.layers ?? {};
      for (const [name, layer] of Object.entries(layers)) {
        if (layer.ok === false) {
          lines.push(`   ${name}: ${layer.detail ?? ""}`);
          if (layer.missing?.length) lines.push(`   缺失: ${layer.missing.slice(0, 8).join(", ")}`);
          if (layer.mismatch?.length) lines.push(`   不符: ${layer.mismatch.slice(0, 8).join(", ")}`);
        }
      }
    }
    const text = lines.join("\n");
    box.append(h("pre", { class: "wiz__log", text }));

    const copyBtn = button(tr("复制诊断摘要"), "ghost", async () => {
      if (!isCurrentReport()) return;
      try {
        await navigator.clipboard.writeText(text);
        if (isCurrentReport()) copyBtn.textContent = tr("已复制");
      } catch {
        if (isCurrentReport()) copyBtn.textContent = tr("复制失败");
      }
    });
    const openLogBtn = button(tr("打开日志文件夹"), "ghost", async () => {
      if (!isCurrentReport()) return;
      const result = await call<{ path: string }>(CMD.logPath);
      if (!isCurrentReport()) return;
      if (result?.path) {
        // 同诊断页：目标是一个日志**文件**，用 revealInFolder 定位它
        await window.voxsub?.dialog.revealInFolder(result.path);
      }
    });
    box.append(h("div", { class: "tuning-actions" }, [copyBtn, openLogBtn]));
    card.append(box);
    body.append(card);

    body.append(h("p", {
      class: "field__hint",
      text: tr("原始数据未被删除，可以修正问题后重新迁移。"),
    }));
  }

  const buttons: HTMLElement[] = [];
  if (ok) {
    buttons.push(button(tr("完成并返回"), "primary", () => {
      if (isCurrentReport()) closeWizard(true);
    }));
  } else {
    buttons.push(button(tr("返回主界面"), "primary", () => {
      if (isCurrentReport()) closeWizard();
    }));
    buttons.push(button(tr("重试迁移"), "ghost", () => {
      if (isCurrentReport() && lifecycle && host) {
        void startMigration(lifecycle, host, viewGeneration, "report");
      }
    }));
  }

  return frame(
    ok ? tr("迁移成功") : tr("迁移遇到问题"),
    ok ? tr("数据已在新位置并通过校验，原始数据保持不变") : tr("已保留现场，未删除任何原始数据"),
    body,
    actionsRow(buttons),
  );
}

/* ---------------------------------------------------------------- 导航 */

function go(step: Step): void {
  // 通知上一页做清理（例如退订事件）
  containerEl?.querySelector(".wiz__body")?.dispatchEvent(new Event("voxsub:leaving"));
  current = step;
  render();
}

/**
 * 关闭向导。
 *
 * completed=true 表示"走完了迁移流程"，false 表示"用户主动跳过"。
 * 两种情况都记为已决定 —— 否则每次启动都弹同一个向导，比不提示更糟。
 */
export function closeWizard(completed = false): void {
  if (!migrationRun || migrationRun.phase === "terminal") {
    void call(CMD.migrationDecision, { decision: completed ? "complete" : "dismiss" });
  }
  const layer = containerEl?.closest<HTMLElement>(".page-layer");
  releaseWizard();
  layer?.setAttribute("hidden", "");
  // 与 closePage 一致地清空内容：留着隐藏的向导会继续持有 DOM 与订阅
  layer?.replaceChildren();
  containerEl = null;
}

/**
 * 释放向导持有的资源（订阅）。
 *
 * 刻意**不碰**退出保护：迁移进行中时 `setBusy(true)` 必须继续有效，直到
 * `startMigration()` 拿到真正的终态（见那里的超时处理）。
 */
function releaseWizard(expectedLifecycle?: PageLifecycle, expectedHost?: HTMLElement): void {
  // 旧页面句柄可能在新向导建好后才迟到 dispose；它只能释放自己捕获的生命周期，
  // 不能碰当前向导的订阅或容器。
  if (expectedLifecycle && wizardLifecycle !== expectedLifecycle) {
    expectedLifecycle.dispose();
    return;
  }

  wizardLifecycle?.dispose();
  wizardLifecycle = null;
  // 通知当前步骤做清理（例如运行页的进度订阅）
  if (!expectedHost || containerEl === expectedHost) {
    containerEl?.querySelector(".wiz__body")?.dispatchEvent(new Event("voxsub:leaving"));
  }
}

/* ---------------------------------------------------------------- 入口 */

/**
 * 首次启动检查。返回 true 表示需要展示向导。
 *
 * 触发条件：检测到旧版 **且** 整体风险不是 safe。
 * 数据本来就在安全位置的用户不该被打扰 —— 他们直接能用。
 */
export async function shouldOfferMigration(): Promise<DetectResult | null> {
  const result = await call<DetectResult & { state?: { dismissed?: boolean; completed?: boolean } }>(
    CMD.detectLegacy,
  );
  if (!result?.legacy?.found) return null;
  if (result.overallRisk === "safe") return null;
  // 用户已经做过决定（完成过或明确跳过）就不再打扰 —— 每次启动都弹同一个向导比不提示更烦
  if (result.state?.dismissed || result.state?.completed) return null;
  return result;
}

export function buildMigrationWizard(initial: DetectResult, restoreTerminal = false): PageHandle {
  // 上一个向导若还在（重开向导、或检测被触发两次），先释放它的订阅
  releaseWizard();
  const lifecycle = new PageLifecycle();
  wizardLifecycle = lifecycle;

  detection = initial;
  const activeRun = migrationRun && migrationRun.phase !== "terminal" ? migrationRun : null;
  const terminalRun = restoreTerminal && migrationRun?.phase === "terminal" ? migrationRun : null;
  cleanupState = activeRun?.cleanupState ?? terminalRun?.cleanupState ?? null;

  report = activeRun?.report ?? terminalRun?.report ?? null;
  current = activeRun ? "running" : (terminalRun?.report ? "report" : "detect");
  containerEl = h("div", { class: "wiz-host" });
  render();
  const host = containerEl;

  return {
    element: host,
    dispose: () => {
      releaseWizard(lifecycle, host);
      if (containerEl === host) containerEl = null;
    },
  };
}

/** 从设置页重新打开向导（用户跳过之后反悔的入口）。 */
export async function reopenWizard(
  host: HTMLElement,
  isCurrent: () => boolean = () => true,
): Promise<PageHandle> {
  const emptyHandle = (): PageHandle => ({ element: host, dispose: () => undefined });
  if (!isCurrent()) return emptyHandle();
  if (migrationRun && detection && (migrationRun.phase !== "terminal" || migrationRun.report)) {
    const handle = buildMigrationWizard(detection, true);
    if (!isCurrent()) {
      handle.dispose();
      return emptyHandle();
    }
    host.replaceChildren(handle.element);
    return handle;
  }

  let result: DetectResult | null;
  try {
    result = await call<DetectResult>(CMD.detectLegacy);
  } catch {
    return emptyHandle();
  }
  if (!isCurrent() || !result) return emptyHandle();

  const handle = buildMigrationWizard(result);
  if (!isCurrent()) {
    handle.dispose();
    return emptyHandle();
  }
  host.replaceChildren(handle.element);
  return handle;
}
