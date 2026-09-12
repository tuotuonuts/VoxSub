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
import { call, store } from "../store";
import { CMD, type DeviceEntry, type HardwareProfile, type SelfCheckItem } from "../protocol";
import { tr } from "../i18n";

let resultsEl: HTMLElement | null = null;
let logEl: HTMLElement | null = null;
let deviceEl: HTMLElement | null = null;
let logStateEl: HTMLElement | null = null;

/** 日志来源：live=实时事件流；file=磁盘日志（含历史运行）。 */
let logSource: "live" | "file" = "live";

const STATUS_MARK: Record<string, string> = { ok: "✓", warn: "!", fail: "✕" };
const STATUS_CLASS: Record<string, string> = { ok: "is-ok", warn: "is-warn", fail: "is-fail" };

async function runCheck(): Promise<void> {
  if (!resultsEl) return;
  const target = resultsEl;
  target.replaceChildren(h("p", { class: "hint", text: tr("正在检查…") }));

  const result = await call<{ results: SelfCheckItem[] }>(CMD.runSelfCheck);
  const items = result?.results ?? [];

  // 自检要跑 4-6 秒；期间用户可能切走分页或关掉诊断页，
  // 那时 resultsEl 已被置空、target 也已脱离文档。这里必须复查，
  // 不能再依赖函数开头那次判断（否则 await 之后会往 null 上写而抛错）。
  if (!resultsEl || resultsEl !== target) return;

  const list = h("div", { class: "check-list" });
  for (const item of items) {
    const row = h("div", { class: `check-row ${STATUS_CLASS[item.status] ?? ""}` });
    row.append(
      h("span", { class: "check-row__mark", text: STATUS_MARK[item.status] ?? "?" }),
      h("span", { class: "check-row__name", text: item.check }),
      h("span", { class: "check-row__detail", text: item.detail }),
    );
    list.append(row);
  }

  const summary = items.every((i) => i.status === "ok")
    ? `全部 ${items.length} 项正常`
    : `${items.filter((i) => i.status !== "ok").length} 项需要注意`;
  target.replaceChildren(
    h("p", { class: "check-summary", text: summary }),
    list,
  );
}

async function exportReport(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  const target = await api.dialog.saveReport();
  if (!target) return;
  await call(CMD.exportDiagnostics, { path: target });
}

function renderLog(): void {
  if (!logEl) return;

  if (logSource === "file") return; // 文件模式由 renderFileLog 负责

  const logs = store.get().logs;
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
      h("span", { class: "log-row__ts", text: ts.length > 11 ? ts.slice(11) : ts }),
      h("span", { class: "log-row__level", text: level.toUpperCase() }),
      h("span", { class: "log-row__msg", text: String(entry?.message ?? "") }),
    );
    return row;
  });
  logEl.replaceChildren(...rows);
  logEl.scrollTop = logEl.scrollHeight;
}

/** 读磁盘日志（含历史运行记录）——排障时用户真正需要的那份。 */
async function renderFileLog(): Promise<void> {
  if (!logEl) return;
  logEl.replaceChildren(h("p", { class: "hint", text: tr("正在读取日志文件…") }));

  const result = await call<{ text: string; lines: number }>(CMD.recentLogs, {
    limit: 500,
    source: "file",
  });
  const text = result?.text ?? "";

  if (!text.trim()) {
    logEl.replaceChildren(h("p", { class: "hint", text: tr("日志文件为空或尚未生成") }));
    if (logStateEl) logStateEl.textContent = tr("文件 · 空");
    return;
  }

  // 文件日志是纯文本，按行渲染并识别级别
  const rows = text.split(/\r?\n/).filter(Boolean).map((line) => {
    const level = /(ERROR|CRITICAL)/.test(line)
      ? "error"
      : /WARN/.test(line)
        ? "warning"
        : "info";
    const row = h("div", { class: `log-row log-row--${level}` });
    row.append(
      h("span", { class: "log-row__ts", text: line.slice(0, 10) }),
      h("span", { class: "log-row__level", text: level === "info" ? "INFO" : level.toUpperCase() }),
      h("span", { class: "log-row__msg", text: line.slice(10) }),
    );
    return row;
  });
  logEl.replaceChildren(...rows);
  logEl.scrollTop = logEl.scrollHeight;
  if (logStateEl) logStateEl.textContent = `${tr("文件")} · ${result?.lines ?? rows.length} ${tr("行")}`;
}

export function refreshLogView(): void {
  renderLog();
}

async function loadDevicesAndHardware(): Promise<void> {
  if (!deviceEl) return;
  const [devices, profile] = await Promise.all([
    call<{ devices: DeviceEntry[] }>(CMD.listDevices),
    call<HardwareProfile>(CMD.hardwareProfile),
  ]);

  const blocks: HTMLElement[] = [];

  if (profile) {
    const rows: Array<[string, string]> = [
      ["CPU", `${profile.cpu}（${profile.physicalCores}核 / ${profile.logicalCores}线程）`],
      ["内存", `${profile.ramGb.toFixed(1)} GB`],
      ["显卡", profile.gpu ? `${profile.gpu}（${profile.vramGb.toFixed(1)} GB）` : "未检测到独立显卡"],
      ["推理后端", profile.gpuProvider || "CPU"],
      ["NPU", profile.npu || "未检测到"],
    ];
    const card = h("section", { class: "card" });
    card.append(h("h3", { class: "card__title", text: tr("硬件画像") }));
    const body = h("div", { class: "card__body" });
    for (const [key, value] of rows) {
      const row = h("div", { class: "kv" });
      row.append(h("span", { class: "kv__key", text: key }), h("span", { class: "kv__value", text: value }));
      body.append(row);
    }
    card.append(body);
    blocks.push(card);
  }

  const deviceCard = h("section", { class: "card" });
  deviceCard.append(h("h3", { class: "card__title", text: tr("运行设备") }));
  const deviceBody = h("div", { class: "card__body" });
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
  deviceCard.append(deviceBody);
  blocks.push(deviceCard);

  deviceEl.replaceChildren(...blocks);
}

export function buildDiagnostics(): HTMLElement {
  const shell = h("div", { class: "settings" });

  const tabs = [
    { label: tr("自检结果"), build: buildCheckTab },
    { label: tr("实时日志"), build: buildLogTab },
    { label: tr("硬件"), build: buildDeviceTab },
  ];

  const nav = h("nav", { class: "settings__nav", role: "tablist" });
  const panes = h("div", { class: "settings__panes" });

  let current = 0;

  const renderPane = (): void => {
    // 切换分页时把旧页的 DOM 引用置空：上一个分页的异步回调
    // （自检要跑 4-6 秒）如果继续往旧节点写，就会白做工。
    resultsEl = null;
    logEl = null;
    deviceEl = null;
    logStateEl = null;

    panes.replaceChildren(tabs[current]!.build());
  };

  tabs.forEach((tab, index) => {
    const btn = h("button", {
      class: index === current ? "settings__tab is-active" : "settings__tab",
      type: "button",
      role: "tab",
      text: tab.label,
    });
    on(btn, "click", () => {
      current = index;
      nav.querySelectorAll(".settings__tab").forEach((n, i) => n.classList.toggle("is-active", i === index));
      renderPane();
    });
    nav.append(btn);
  });

  shell.append(nav, panes);

  // 关闭页面时同样置空，避免在途结果写到已脱离文档的节点
  shell.addEventListener("voxsub:pageclosed", () => {
    resultsEl = logEl = deviceEl = logStateEl = null;
  });

  renderPane();
  return shell;
}

/** 关闭覆盖页时调用，让诊断页的在途异步结果停止写 DOM。 */
export function detachDiagnostics(): void {
  document.querySelector(".settings")?.dispatchEvent(new Event("voxsub:pageclosed"));
}

function buildCheckTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });
  const actions = h("div", { class: "tuning-actions" });
  const run = h("button", { class: "btn btn--primary", type: "button", text: tr("重新检查") });
  on(run, "click", () => void runCheck());
  const exportBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("导出报告") });
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

  // 来源切换：实时（本次运行） / 文件（含历史运行）
  const actions = h("div", { class: "tuning-actions" });

  const liveBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: tr("实时") });
  const fileBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: tr("历史文件") });
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
  const exportBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: tr("导出日志") });
  on(exportBtn, "click", () => void exportLog());

  // 打开日志所在文件夹：排障时用户常要自己翻
  const openBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: tr("打开文件夹") });
  on(openBtn, "click", async () => {
    const result = await call<{ path: string }>(CMD.logPath);
    if (result?.path) {
      // 按钮文案是「打开文件夹」，用 revealInFolder 在资源管理器里选中该日志文件。
      // 原先走 openExternal 拼 file:/// —— 主进程只放行 http/https，调用必然失败。
      await window.voxsub?.dialog.revealInFolder(result.path);
    }
  });

  // 清除本机日志：破坏性操作，需二次确认
  const clearBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: tr("清除本机日志") });
  on(clearBtn, "click", async () => {
    if (!window.confirm(tr("将删除本机全部日志文件（不影响模型、配置与已导出的报告）。确定继续？"))) {
      return;
    }
    const result = await call<Record<string, number>>(CMD.clearLogs);
    store.patch({ logs: [] });
    if (logStateEl) {
      const removed = result ? Object.values(result).reduce((a, b) => a + Number(b || 0), 0) : 0;
      logStateEl.textContent = `${tr("已清除")} · ${removed} ${tr("个文件")}`;
    }
    if (logSource === "file") void renderFileLog();
    else renderLog();
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

  logEl = h("div", { class: "log-view" });
  page.append(logEl);

  markSource("live");
  return page;
}

/** 导出当前日志视图到用户指定文件。 */
async function exportLog(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;

  const target = await api.dialog.saveReport();
  if (!target) return;

  let text = "";
  if (logSource === "file") {
    const result = await call<{ text: string }>(CMD.recentLogs, {
      limit: 2000,
      source: "file",
    });
    text = result?.text ?? "";
  } else {
    text = store
      .get()
      .logs.map((e) => `${e.ts} ${e.level} ${e.message}`)
      .join("\n");
  }

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
