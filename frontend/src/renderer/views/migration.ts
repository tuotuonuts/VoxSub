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
import { describeOutcome, isTaskRunning } from "../../shared/request-outcome";
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
/** 最近一次清理原目录的结果（供报告页展示；null 表示还没清理过）。 */
let cleanupState: { skipped: boolean; detail: string; ok?: boolean } | null = null;
/** 用户在清单里勾选的项（key 集合）。 */
const selected = new Set<string>();
/** 迁移期间后端推来的每项进度：key -> 百分比 */
const progress = new Map<string, number>();
let overallProgress = 0;

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

/* ---------------------------------------------------------------- 渲染 */

function render(): void {
  if (!containerEl) return;

  const views: Record<Step, () => HTMLElement> = {
    detect: viewDetect,
    overview: viewOverview,
    plan: viewPlan,
    running: viewRunning,
    report: viewReport,
  };
  containerEl.replaceChildren(views[current]());
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
    const row = h("div", { class: `check-row ${RISK_CLASS[check.risk] ?? ""}` });
    row.append(
      h("span", { class: "check-row__mark", text: check.risk === "safe" ? "✓" : check.risk === "conditional" ? "!" : "✕" }),
      h("span", { class: "check-row__name", text: check.key }),
      h("span", { class: "check-row__detail" }, [
        h("strong", { text: `${human(check.bytes)} · ${check.file_count} ${tr("文件")}` }),
        h("br"),
        h("code", { text: check.path }),
        ...(check.purpose ? [h("br"), h("span", { text: check.purpose })] : []),
        ...(check.detail ? [h("br"), h("span", { text: check.detail })] : []),
      ]),
    );
    list.append(row);
  }
  body.append(list);

  const buttons: HTMLElement[] = [button(tr("返回"), "ghost", () => go("detect"))];
  if (risk !== "safe" || checks.some((c) => c.key === "models")) {
    buttons.push(button(tr("规划迁移"), "primary", () => void loadPlan()));
  }
  buttons.push(button(tr("暂时跳过"), "ghost", () => closeWizard()));

  return frame(tr("数据风险"), tr("列出旧版留下的数据及其位置"), body, actionsRow(buttons));
}

/* ------------------------------------------------------------ 步骤 3 */

async function loadPlan(): Promise<void> {
  const result = await call<PlanResult>(CMD.planMigration, {});
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

  if (!plan || plan.steps.length === 0) {
    body.append(h("p", { class: "hint", text: tr("没有需要迁移的项目") }));
    return frame(tr("确认迁移清单"), "", body,
      actionsRow([button(tr("返回"), "ghost", () => go("overview"))]));
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

  const proceed = button(tr("开始迁移"), "primary", () => void startMigration());
  proceed.id = "wiz-proceed";

  function updateSummary(): void {
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
      button(tr("返回"), "ghost", () => go("overview")),
      proceed,
      button(tr("取消"), "ghost", () => closeWizard()),
    ]),
  );
}

/* ------------------------------------------------------------ 步骤 4 */

async function startMigration(): Promise<void> {
  if (!plan) return;
  const steps = plan.steps.filter((s) => selected.has(s.key));
  if (steps.length === 0) return;

  progress.clear();
  overallProgress = 0;
  cleanupState = null;
  go("running");

  // 迁移是长任务，期间必须阻止退出（与设置页的 busy 语义一致）
  void window.voxsub?.app.setBusy(true, tr("数据正在迁移，请等待完成后再退出应用。"));

  // 超时**不等于失败**（缺陷 #4）。
  //
  // 请求层到时限只会发一条"超过时限"的通知，请求本身保持挂起，后端送回真实
  // 结果时这个 await 才继续 —— 所以这里可以按"还没结束"来写：
  //   · 超时（含硬性上限）绝不能解除退出保护；
  //   · 也绝不能跳到"迁移失败"的报告页；
  //   · 迁移进度事件照常到达，运行页继续更新。
  const { outcome, data } = await callWithOutcome<MigrationReport>(CMD.startMigration, { steps });

  if (isTaskRunning(outcome)) {
    // 任务仍在进行：留在「正在迁移」页，退出保护保持有效。
    // 文案只说明"这次请求还没回来"，不出现"失败"。
    store.patch({ statusText: describeOutcome(outcome, tr("数据迁移")) });
    return;
  }

  void window.voxsub?.app.setBusy(false);
  report = data ?? { done: [], failed: [{ key: "?", error: tr("迁移未返回结果") }], elapsedMs: 0, ok: false };
  go("report");
}

function viewRunning(): HTMLElement {
  const body = h("div", { class: "wiz__body" });

  const warn = h("div", { class: "wiz__verdict is-warn" });
  warn.append(h("p", {
    text: tr("迁移进行中，请不要关闭应用。中断可能导致目标目录不完整（原始数据不会被删除）。"),
  }));
  body.append(warn);

  // 总进度
  const overall = h("div", { class: "progress" });
  const overallFill = h("div", { class: "progress__fill" });
  overall.append(
    h("div", { class: "progress__track" }, [overallFill]),
    h("div", { class: "progress__label", text: "0%" }),
  );
  body.append(overall);

  // 每项独立进度条
  const list = h("div", { class: "mig-progress" });
  const chosen = plan?.steps.filter((s) => selected.has(s.key)) ?? [];
  for (const step of chosen) {
    const row = h("div", { class: "mig-progress__row" });
    const fill = h("div", { class: "progress__fill" });
    const label = h("span", { class: "progress__label", text: "0%" });
    row.append(
      h("span", { class: "mig-progress__name", text: step.key }),
      h("div", { class: "progress__track" }, [fill]),
      label,
    );
    row.dataset["key"] = step.key;
    list.append(row);
  }
  body.append(list);

  // 订阅后端进度事件，实时更新
  const off = window.voxsub?.backend.onEvent((raw) => {
    const event = raw as { type?: string; phase?: string; key?: string; completed?: number; total?: number };
    if (event.type !== "migration" || !event.key) return;

    const row = list.querySelector<HTMLElement>(`[data-key="${CSS.escape(event.key)}"]`);
    if (event.phase === "start") {
      row?.querySelector(".progress__label")!.replaceChildren(document.createTextNode(tr("进行中…")));
      return;
    }
    if (event.phase === "done" || event.phase === "failed") {
      progress.set(event.key, 100);
      row?.querySelector(".progress__fill")!.setAttribute("style", "width:100%");
      row?.querySelector(".progress__label")!.replaceChildren(
        document.createTextNode(event.phase === "done" ? tr("完成") : tr("失败")),
      );
    } else if (event.phase === "progress") {
      // percent() 返回的是带 % 的字符串，这里要的是数值宽度
      const raw = event.total ? Math.round(((event.completed ?? 0) / event.total) * 100) : 100;
      const pct = Math.max(0, Math.min(100, raw));
      progress.set(event.key, pct);
      row?.querySelector(".progress__fill")!.setAttribute("style", `width:${pct}%`);
      row?.querySelector(".progress__label")!.replaceChildren(document.createTextNode(`${pct}%`));
    }

    // 总进度 = 已完成项数占比
    const finished = chosen.filter((s) => progress.get(s.key) === 100).length;
    overallProgress = chosen.length ? Math.round((finished / chosen.length) * 100) : 0;
    overallFill.setAttribute("style", `width:${overallProgress}%`);
    overall.querySelector(".progress__label")!.replaceChildren(
      document.createTextNode(`${overallProgress}%`),
    );
  });

  // 离开这一页时退订，避免回调打到已移除的节点
  body.addEventListener("voxsub:leaving", () => off?.(), { once: true });
  // 向导被整体替换/关闭时也要退订（缺陷 #10：订阅只增不减）
  wizardLifecycle?.add(() => off?.());

  return frame(tr("正在迁移"), tr("每项数据有独立进度，完成后会自动校验"), body,
    actionsRow([h("span", { class: "tuning-actions__state", text: tr("请勿关闭应用") })]));
}

/* ------------------------------------------------------------ 步骤 5 */

/**
 * 清理已迁移的原目录。
 *
 * 后端契约（**已变更**）：`cleanup_migrated_source` 不再接受 `path`，
 * 只接受 `record_id` / `record_ids`，并且**必须显式带 `confirm: true`**。
 * 标识只从 `start_migration` 回传的 `done[].recordId` 取 —— 路径不是标识。
 * 失败（含 `path_not_accepted` / `missing_record_id` / 缺确认）时把后端给的
 * `detail` **原样展示**，绝不吞掉：否则用户只看到"清理失败"却不知道为什么。
 */
async function cleanupMigratedSources(): Promise<void> {
  const records = (report?.done ?? []).filter((item) => item.recordId);
  const decision = buildCleanupRequest(records, window.confirm(
    `${tr("删除是不可逆的")}：${tr("确认清理已迁移的原目录？")}\n${records.map((r) => r.key).join("、")}`,
  ));
  if (!isCleanupRequest(decision)) {
    cleanupState = {
      skipped: true,
      detail: decision.reason === "no_user_confirm"
        ? tr("已取消：没有确认就不会删除任何原目录")
        : tr("没有可清理的记录（后端只接受记录 ID）"),
    };
    go("report");
    return;
  }

  const result = await call<CleanupResult>(CMD.cleanupMigratedSource, decision);
  cleanupState = { skipped: false, detail: cleanupNotice(result), ok: cleanupSucceeded(result) };
  store.patch({ statusText: cleanupNotice(result) });
  store.pushLog({
    ts: new Date().toISOString(),
    level: cleanupSucceeded(result) ? "INFO" : "WARNING",
    message: `清理已迁移原目录：${cleanupNotice(result)}`,
  });
  go("report");
}

function viewReport(): HTMLElement {
  const body = h("div", { class: "wiz__body" });
  if (!report) return frame(tr("迁移结果"), "", body, actionsRow([]));

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
      const row = h("div", { class: "check-row is-ok" });
      row.append(
        h("span", { class: "check-row__mark", text: "✓" }),
        h("span", { class: "check-row__name", text: item.key }),
        h("span", { class: "check-row__detail", text: item.target }),
      );
      list.append(row);
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
    card.append(h("div", { class: "tuning-actions" }, [
      button(tr("清理已迁移的原目录"), "ghost", () => void cleanupMigratedSources()),
    ]));
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
      try {
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = tr("已复制");
      } catch {
        copyBtn.textContent = tr("复制失败");
      }
    });
    const openLogBtn = button(tr("打开日志文件夹"), "ghost", async () => {
      const result = await call<{ path: string }>(CMD.logPath);
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
    buttons.push(button(tr("完成并返回"), "primary", () => closeWizard(true)));
  } else {
    buttons.push(button(tr("返回主界面"), "primary", () => closeWizard()));
    buttons.push(button(tr("重试迁移"), "ghost", () => void startMigration()));
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
  void call(CMD.migrationDecision, { decision: completed ? "complete" : "dismiss" });
  const layer = containerEl?.closest<HTMLElement>(".page-layer");
  releaseWizard();
  layer?.setAttribute("hidden", "");
  // 与 closePage 一致地清空内容：留着隐藏的向导会继续持有 DOM 与订阅
  layer?.replaceChildren();
  containerEl = null;
  void window.voxsub?.app.setBusy(false);
}

/**
 * 释放向导持有的资源（订阅）。
 *
 * 刻意**不碰**退出保护：迁移进行中时 `setBusy(true)` 必须继续有效，直到
 * `startMigration()` 拿到真正的终态（见那里的超时处理）。
 */
function releaseWizard(): void {
  wizardLifecycle?.dispose();
  wizardLifecycle = null;
  // 通知当前步骤做清理（例如运行页的进度订阅）
  containerEl?.querySelector(".wiz__body")?.dispatchEvent(new Event("voxsub:leaving"));
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

export function buildMigrationWizard(initial: DetectResult): PageHandle {
  // 上一个向导若还在（重开向导、或检测被触发两次），先释放它的订阅
  releaseWizard();
  const lifecycle = new PageLifecycle();
  wizardLifecycle = lifecycle;

  detection = initial;
  current = "detect";
  containerEl = h("div", { class: "wiz-host" });
  render();
  const host = containerEl;

  return {
    element: host,
    dispose: () => {
      releaseWizard();
      if (containerEl === host) containerEl = null;
    },
  };
}

/** 从设置页重新打开向导（用户跳过之后反悔的入口）。 */
export async function reopenWizard(host: HTMLElement): Promise<PageHandle> {
  const result = await call<DetectResult>(CMD.detectLegacy);
  if (!result) {
    // 检测结果拿不到就不开向导（与原先一致）。返回一个空句柄，调用点不必分支。
    return { element: host, dispose: () => undefined };
  }
  const handle = buildMigrationWizard(result);
  host.replaceChildren(handle.element);
  return handle;
}
