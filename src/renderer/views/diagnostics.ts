/**
 * 诊断页 —— 对应原 Qt 版 diagnostics_window.py（1038 行）。
 *
 * 分页：自检结果 / 实时日志 / 设备与硬件
 * 纪律：状态必须如实呈现（ok/warn/fail），不得把失败写成"正常"。
 */
import { h, on } from "../dom";
import { call, store } from "../store";
import { CMD, type DeviceEntry, type HardwareProfile, type SelfCheckItem } from "../protocol";
import { tr } from "../i18n";

let resultsEl: HTMLElement | null = null;
let logEl: HTMLElement | null = null;
let deviceEl: HTMLElement | null = null;

const STATUS_MARK: Record<string, string> = { ok: "✓", warn: "!", fail: "✕" };
const STATUS_CLASS: Record<string, string> = { ok: "is-ok", warn: "is-warn", fail: "is-fail" };

async function runCheck(): Promise<void> {
  if (!resultsEl) return;
  resultsEl.replaceChildren(h("p", { class: "hint", text: "正在检查…" }));
  const result = await call<{ results: SelfCheckItem[] }>(CMD.runSelfCheck);
  const items = result?.results ?? [];

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
  resultsEl.replaceChildren(
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
  const logs = store.get().logs;
  if (logs.length === 0) {
    logEl.replaceChildren(h("p", { class: "hint", text: "暂无日志" }));
    return;
  }
  const rows = logs.slice(-300).map((entry) => {
    const row = h("div", { class: `log-row log-row--${entry.level.toLowerCase()}` });
    row.append(
      h("span", { class: "log-row__ts", text: entry.ts.slice(11) }),
      h("span", { class: "log-row__level", text: entry.level }),
      h("span", { class: "log-row__msg", text: entry.message }),
    );
    return row;
  });
  logEl.replaceChildren(...rows);
  logEl.scrollTop = logEl.scrollHeight;
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
    panes.replaceChildren(tabs[current]!.build());
    if (current === 1) renderLog();
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
  renderPane();
  return shell;
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
  const actions = h("div", { class: "tuning-actions" });
  const clear = h("button", { class: "btn btn--ghost", type: "button", text: tr("清空") });
  on(clear, "click", () => store.patch({ logs: [] }));
  actions.append(
    h("span", { class: "tuning-actions__state", text: "实时 · 自动跟随" }),
    h("span", { class: "catalog__spacer" }),
    clear,
  );
  page.append(actions);
  logEl = h("div", { class: "log-view" });
  page.append(logEl);
  return page;
}

function buildDeviceTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });
  deviceEl = h("div", { class: "device-wrap" });
  page.append(deviceEl);
  void loadDevicesAndHardware();
  return page;
}
