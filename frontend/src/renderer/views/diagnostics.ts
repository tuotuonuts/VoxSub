import { buildTabNav } from "../ui/tab-nav";
import { buildCardFrame } from "../ui/card";
import { buildSelect, buildTextInput } from "../ui/controls";
import { buildButton } from "../ui/button";
import { developerEnabled, checkSummary, matchesLog, pipelineSession } from "../../shared/diagnostic-controls";
import { buildDeveloperTab } from "./developer";
/**
 * 诊断页 —— 对应原 Qt 版 diagnostics_window.py（1038 行）。
 *
 * 分页：自检结果 / 实时日志 / 设备与硬件
 * 纪律：状态必须如实呈现（ok/warn/fail），不得把失败写成"正常"。
 *
 * 日志页的能力对应关系：
 *   实时日志    ← 主进程事件流（内存缓冲，只含本次运行）
 *   读取文件    ← recent_logs(source="file")，拿到历史运行记录
 *   导出日志    ← 保存到用户指定路径
 *   清除本机日志← clear_logs（截断活动日志 + 删轮转文件，不动模型与配置）
 *   打开文件夹  ← log_path + shell.openExternal
 */
import { h, on } from "../dom";
import { formatLogTime, parseFileLogs, exportLogEntries } from "../../shared/log-time";
import type { LogEntry } from "../protocol";
import { call, store } from "../store";
import { CMD, type DeviceEntry, type HardwareProfile, type SelfCheckItem } from "../protocol";
import { tr } from "../i18n";

import { type PageHandle } from "../../shared/page-lifecycle";
import { runAfterConfirm } from "../../shared/confirm-action";
import { buildStatusRow } from "../ui/status-row";

let resultsEl: HTMLElement | null = null;
let logEl: HTMLElement | null = null;
let deviceEl: HTMLElement | null = null;
let logStateEl: HTMLElement | null = null;
let checkRevision = 0;
let fileRevision = 0;
let logLevel = "all";
let logQuery = "";
let currentRunOnly = false;
let currentRunId = "";
let logPaused = false;
let pipelineSessionOnly = false;
let currentPipelineSession = "";
let developerPage: PageHandle | null = null;

function visibleLogs(): LogEntry[] {
  const entries = logSource === "file" ? fileEntries : store.get().logs.slice(-300);
  if (pipelineSessionOnly) currentPipelineSession = [...entries].reverse().map(e => pipelineSession(e.message)).find(Boolean) || "";
  return entries.filter(entry => matchesLog(entry, logLevel, logQuery, currentRunOnly ? currentRunId || "unknown" : "") &&
    (!pipelineSessionOnly || pipelineSession(entry.message) === (currentPipelineSession || "unknown")));
}

/**
 * 页面是否已失效。
 *
 * 自检要跑 4-6 秒、设备枚举与日志读取也是异步的 —— 结果回来时页面可能早已
 * 关闭或被替换。原先靠一个自定义 DOM 事件（`voxsub:pageclosed`）通知，
 * 而那个事件的投递方式是 `document.querySelector(".settings")`：设置页与
 * 诊断页共用同一个 `.settings` 类名，靠"文档里恰好只有一个"来选对节点。
 * 现在改成模块级标志，由页面句柄的 dispose 统一置位，不再依赖 DOM 巧合。
 */
let detached = false;

/** 日志来源：live=实时事件流；file=磁盘日志（含历史运行）。 */
let logSource: "live" | "file" = "live";
let fileEntries: LogEntry[] = [];

async function runCheck(): Promise<void> {
  if (!resultsEl) return;
  const target = resultsEl;
  const revision = ++checkRevision;
  target.replaceChildren(h("p", { class: "hint", text: tr("正在检查…") }));

  const result = await call<{ results: SelfCheckItem[] }>(CMD.runSelfCheck);
  const items = result?.results ?? [];

  // 自检要跑 4-6 秒；期间用户可能切走分页或关掉诊断页，
  // 那时 resultsEl 已被置空、target 也已脱离文档。这里必须复查，
  // 不能再依赖函数开头那次判断（否则 await 之后会往 null 上写而抛错）。
  if (detached || !resultsEl || resultsEl !== target || revision !== checkRevision) return;

  const list = h("div", { class: "check-list" });
  for (const item of items) {
    // 结果行统一由 StatusRow 组件渲染（三处调用点共用同一份状态→类名/标记映射）
    list.append(buildStatusRow({
      level: item.status,
      name: item.check,
      detail: [tr(item.status === "ok" ? "通过" : item.status === "fail" ? "异常" : item.status === "resource_limited" ? "资源不足" : item.status === "running" ? "检查中" : item.status === "not_run" ? "未检查" : "需要注意"), " · ", item.detail, item.impact ? " · " + tr("影响") + ": " + item.impact : "", item.suggestion ? " · " + tr("建议") + ": " + item.suggestion : ""],
    }));
  }

  const verdict = checkSummary(items);
  const summary = verdict === "not_run" ? tr("未检查：没有有效结果，不能判定正常")
    : verdict === "ok" ? tr("已执行的检查项通过；未检查的链路不作保证")
    : tr("存在异常或未验证项目，请查看影响与建议");
  target.replaceChildren(
    h("p", { class: "check-summary", text: summary }),
    list,
  );
}

function confirmDiagnosticExport(): boolean {
  return window.confirm(tr("导出包含版本、检查时间、检查范围和脱敏运行元数据；不含音频、识别/翻译正文及历史。普通日志正文默认省略。继续？"));
}

async function exportReport(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  if (!confirmDiagnosticExport()) return;
  const target = await api.dialog.saveReport();
  if (!target) return;
  await call(CMD.exportDiagnostics, { path: target });
}

function renderLog(): void {
  if (!logEl) return;

  if (logSource === "file") return; // 文件模式由 renderFileLog 负责

  if (logPaused) return;
  const logs = visibleLogs();
  if (logs.length === 0) {
    logEl.replaceChildren(h("p", { class: "hint", text: tr("暂无日志") }));
    return;
  }

  const rows = logs.slice(-300).map((entry) => {
    // 缺字段不能整页崩：曾因 entry.level 为 undefined 触发
    // TypeError，导致整个日志分页点不开（异常在 build 阶段抛出）。
    const level = String(entry?.level ?? "info");
    const ts = String(entry?.ts ?? "");
    const row = h("div", { class: `log-row log-row--${level.toLowerCase()}` });
    row.append(
      h("span", { class: "log-row__ts", text: formatLogTime(entry, tr), title: entry.raw ?? ts }),
      h("span", { class: "log-row__level", text: level.toUpperCase() }),
      h("span", { class: "log-row__msg", text: String(entry?.message ?? "") }),
    );
    return row;
  });
  logEl.replaceChildren(...rows);
  if (!logPaused) logEl.scrollTop = logEl.scrollHeight;
}

/** 读磁盘日志（含历史运行记录）——排障时用户真正需要的那份。 */
async function renderFileLog(): Promise<void> {
  if (!logEl) return;
  const target = logEl;
  const revision = ++fileRevision;
  target.replaceChildren(h("p", { class: "hint", text: tr("正在读取日志文件…") }));
  const result = await call<{ text: string; lines: number; run_id?: string }>(CMD.recentLogs, { limit: 500, source: "file" });
  if (detached || logEl !== target || revision !== fileRevision || logSource !== "file") return;
  if (result?.run_id) currentRunId = result.run_id;
  fileEntries = parseFileLogs(result?.text ?? "");
  renderFileEntries();
}

function renderFileEntries(): void {
  if (!logEl || logSource !== "file" || logPaused) return;
  const entries = visibleLogs();
  const rows = entries.map(entry => {
    const row = h("div", { class: "log-row log-row--" + entry.level.toLowerCase() });
    row.append(h("span", { class: "log-row__ts", text: formatLogTime(entry, tr), title: entry.raw ?? "" }),
                h("span", { class: "log-row__level", text: entry.level }),
                h("span", { class: "log-row__msg", text: entry.message }));
    return row;
  });
  logEl.replaceChildren(...(rows.length ? rows : [h("p", { class: "hint", text: tr("暂无匹配日志") })]));
  logEl.scrollTop = logEl.scrollHeight;
  if (logStateEl) logStateEl.textContent = tr("文件") + " · " + rows.length + " " + tr("行");
}

export function refreshLogView(): void {
  renderLog();
}

async function loadDevicesAndHardware(): Promise<void> {
  if (!deviceEl) return;
  const target = deviceEl;
  const [devices, profile] = await Promise.all([
    call<{ devices: DeviceEntry[] }>(CMD.listDevices),
    call<HardwareProfile>(CMD.hardwareProfile),
  ]);

  // 页面可能在这两次请求期间被关掉/替换（deviceEl 已被置空）。
  // 原先这里直接 `deviceEl.replaceChildren(...)`，会往 null 上写而抛错。
  if (detached || !deviceEl || target !== deviceEl) return;

  const blocks: HTMLElement[] = [];

  if (profile) {
    const rows: Array<[string, string]> = [
      ["CPU", `${profile.cpu}（${profile.physicalCores}核 / ${profile.logicalCores}线程）`],
      ["内存", `${profile.ramGb.toFixed(1)} GB`],
      ["显卡", profile.gpu ? `${profile.gpu}（${profile.vramGb.toFixed(1)} GB）` : "未检测到独立显卡"],
      [tr("可用推理后端（非实际运行设备）"), profile.gpuProvider || "CPU"],
      ["NPU", profile.npu || "未检测到"],
    ];
    const { element: card, body } = buildCardFrame(tr("硬件画像"));
    for (const [key, value] of rows) {
      const row = h("div", { class: "kv" });
      row.append(h("span", { class: "kv__key", text: key }), h("span", { class: "kv__value", text: value }));
      body.append(row);
    }
    blocks.push(card);
  }

  const { element: deviceCard, body: deviceBody } = buildCardFrame(tr("检测到的设备（运行未验证）"));
  for (const device of devices?.devices ?? []) {
    const row = h("div", { class: "kv" });
    row.append(
      h("span", { class: "kv__key", text: device.provider }),
      h("span", { class: "kv__value", text: device.name + (device.scoreMs !== null ? ` · ${device.scoreMs.toFixed(1)} ms` : "") }),
    );
    deviceBody.append(row);
  }
  if (!deviceBody.childElementCount) {
    deviceBody.append(h("p", { class: "hint", text: "未枚举到可用设备" }));
  }
  blocks.push(deviceCard);

  deviceEl.replaceChildren(...blocks);
}

export function buildDiagnostics(): PageHandle {
  const shell = h("div", { class: "settings" });
  detached = false;

  const tabs = [
    { label: tr("自检结果"), build: buildCheckTab },
    { label: tr("实时日志"), build: buildLogTab },
    { label: tr("硬件"), build: buildDeviceTab },
    ...(developerEnabled() ? [{ label: tr("开发者"), build: (): HTMLElement => {
      developerPage = buildDeveloperTab(() => {
        const last = nav.lastElementChild as HTMLElement | null;
        if (last) last.hidden = true;
        current = 0;
        nav.querySelectorAll(".settings__tab").forEach((n, i) => n.classList.toggle("is-active", i === 0));
        renderPane();
      });
      return developerPage.element;
    } }] : []),
  ];

  const panes = h("div", { class: "settings__panes" });

  let current = 0;

  const renderPane = (): void => {
    developerPage?.dispose(); developerPage = null;
    checkRevision++; fileRevision++;
    // 切换分页时把旧页的 DOM 引用置空：上一个分页的异步回调
    // （自检要跑 4-6 秒）如果继续往旧节点写，就会白做工。
    resultsEl = null;
    logEl = null;
    deviceEl = null;
    logStateEl = null;

    panes.replaceChildren(tabs[current]!.build());
  };

  const nav = buildTabNav(tabs.map(tab => tab.label), index => {
    current = index;
    renderPane();
  });

  shell.append(nav, panes);

  renderPane();
  return {
    element: shell,
    // 关闭/替换页面时统一在途结果失效：自检（4-6 秒）与设备枚举回来时
    // 不再往已移除的节点上写。由页面生命周期驱动，不再依赖 DOM 事件。
    dispose: () => detach(),
  };
}

/** 让诊断页的在途异步结果停止写 DOM（由页面句柄的 dispose 调用）。 */
export function detachDiagnostics(): void {
  detach();
}

function detach(): void {
  detached = true;
  checkRevision++; fileRevision++;
  developerPage?.dispose(); developerPage = null;
  resultsEl = null;
  logEl = null;
  deviceEl = null;
  logStateEl = null;
}

function buildCheckTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });
  const actions = h("div", { class: "tuning-actions" });
  const run = buildButton(tr("重新检查"), { variant: "primary" });
  on(run, "click", () => void runCheck());
  const exportBtn = buildButton(tr("导出报告"));
  on(exportBtn, "click", () => void exportReport());
  actions.append(run, exportBtn);
  page.append(actions);

  resultsEl = h("div", { class: "check-wrap" });
  page.append(resultsEl);
  void runCheck();
  return page;
}

function buildLogTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });

  const filters = h("div", { class: "tuning-actions diagnostic-actions" });
  const level = buildSelect(logLevel, ["all", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map(value => [value, value === "all" ? tr("全部级别") : value] as const));
  level.setAttribute("aria-label", tr("日志级别"));
  const query = buildTextInput(logQuery, undefined, { placeholder: tr("搜索日志关键词") });
  query.setAttribute("aria-label", tr("搜索日志关键词"));
  const session = buildButton(tr("仅本次运行"));
  session.setAttribute("aria-pressed", String(currentRunOnly));
  const taskSession = buildButton(tr("仅本次模型会话"));
  taskSession.setAttribute("aria-pressed", String(pipelineSessionOnly));
  on(taskSession, "click", () => {
    pipelineSessionOnly = !pipelineSessionOnly;
    const entries = logSource === "file" ? fileEntries : store.get().logs;
    currentPipelineSession = [...entries].reverse().map(e => pipelineSession(e.message)).find(Boolean) || "";
    taskSession.setAttribute("aria-pressed", String(pipelineSessionOnly)); redraw();
  });
  const pause = buildButton(tr(logPaused ? "恢复滚动" : "暂停滚动"));
  const redraw = (): void => { if (logSource === "file") renderFileEntries(); else renderLog(); };
  on(level, "change", () => { logLevel = level.value; redraw(); });
  on(query, "input", () => { logQuery = query.value; redraw(); });
  on(session, "click", () => { currentRunOnly = !currentRunOnly; session.setAttribute("aria-pressed", String(currentRunOnly)); redraw(); });
  on(pause, "click", () => { logPaused = !logPaused; pause.textContent = tr(logPaused ? "恢复滚动" : "暂停滚动"); if (!logPaused) redraw(); });
  filters.append(level, query, session, taskSession, pause); page.append(filters);
  void call<{ run_id: string }>(CMD.recentLogs, { limit: 1 }).then(result => { if (!detached && result?.run_id) { currentRunId = result.run_id; redraw(); } });

  // 来源切换：实时（本次运行） / 文件（含历史运行）
  const actions = h("div", { class: "tuning-actions" });

  const liveBtn = buildButton(tr("实时"), { small: true });
  const fileBtn = buildButton(tr("历史文件"), { small: true });
  const markSource = (source: "live" | "file"): void => {
    logSource = source;
    liveBtn.classList.toggle("btn--primary", source === "live");
    fileBtn.classList.toggle("btn--primary", source === "file");
    if (source === "file") void renderFileLog();
    else renderLog();
  };
  on(liveBtn, "click", () => markSource("live"));
  on(fileBtn, "click", () => markSource("file"));

  logStateEl = h("span", { class: "tuning-actions__state", text: "" });

  // 导出日志：把当前视图内容写到用户选的路径
  const exportBtn = buildButton(tr("导出日志"), { small: true });
  on(exportBtn, "click", () => void exportLog());

  // 打开日志所在文件夹：排障时用户常要自己翻
  const openBtn = buildButton(tr("打开文件夹"), { small: true });
  on(openBtn, "click", async () => {
    const result = await call<{ path: string }>(CMD.logPath);
    if (result?.path) {
      // 按钮文案是「打开文件夹」，用 revealInFolder 在资源管理器里选中该日志文件。
      // 原先走 openExternal 拼 file:/// —— 主进程只放行 http/https，调用必然失败。
      await window.voxsub?.dialog.revealInFolder(result.path);
    }
  });

  // 清除本机日志：破坏性操作，需二次确认
  const clearBtn = buildButton(tr("清除本机日志"), { small: true });
  on(clearBtn, "click", () => {
    // 确认闸门（ConfirmAction）：用户取消时 clearLocalLogs 一次都不会被调用
    runAfterConfirm(
      () => window.confirm(tr("将删除本机全部日志文件（不影响模型、配置与已导出的报告）。确定继续？")),
      () => void clearLocalLogs(),
    );
  });

  actions.append(
    liveBtn,
    fileBtn,
    logStateEl,
    h("span", { class: "catalog__spacer" }),
    openBtn,
    exportBtn,
    clearBtn,
  );
  page.append(actions);
  page.append(h("p", { class: "hint", text: tr("本地时间（含时区）· 按接收顺序显示，迟到记录不重排") }));

  logEl = h("div", { class: "log-view" });
  page.append(logEl);

  markSource("live");
  return page;
}

/**
 * 清除本机日志（截断活动日志 + 删轮转文件，不动模型与配置）。
 *
 * 由确认闸门在用户同意之后调用 —— 这里不再自己问一次，
 * 否则"确认"与"执行"会分散在两处、容易漏。
 */
async function clearLocalLogs(): Promise<void> {
  const result = await call<Record<string, number>>(CMD.clearLogs);
  store.patch({ logs: [] });
  if (logStateEl) {
    const removed = result ? Object.values(result).reduce((a, b) => a + Number(b || 0), 0) : 0;
    logStateEl.textContent = `${tr("已清除")} · ${removed} ${tr("个文件")}`;
  }
  if (logSource === "file") void renderFileLog();
  else renderLog();
}

/** 导出当前日志视图到用户指定文件。 */
async function exportLog(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;

  if (!confirmDiagnosticExport()) return;
  const target = await api.dialog.saveReport();
  if (!target) return;

  const text = exportLogEntries(visibleLogs(), tr);

  const saved = await call<{ path: string }>(CMD.exportDiagnostics, {
    path: target,
    log_text: text,
  });
  if (logStateEl) {
    logStateEl.textContent = saved ? `${tr("已导出")} → ${target}` : tr("导出失败");
  }
}

function buildDeviceTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });
  deviceEl = h("div", { class: "device-wrap" });
  page.append(deviceEl);
  void loadDevicesAndHardware();
  return page;
}
