/**
 * 模型目录 —— 同人展目录方向的核心落点。
 *
 * 对应原 Qt 版的 model_hub_window.py（623 行）。
 * 结构沿用"展位格"：密铺网格、格内贴元数据小字、选中被圈出。
 */
import { h, on, scorePips, percent } from "../dom";
import { call, store } from "../store";
import { CMD, type ModelEntry } from "../protocol";
import { tr } from "../i18n";

type TaskFilter = "all" | "asr" | "translate" | "tts" | "ocr";

const TASK_LABEL: Record<string, string> = {
  asr: "识别",
  translate: "翻译",
  tts: "朗读",
  ocr: "OCR",
};

let models: ModelEntry[] = [];
let filter: TaskFilter = "all";
let gridEl: HTMLElement | null = null;
let countEl: HTMLElement | null = null;
let filterBarEl: HTMLElement | null = null;

export async function loadModels(): Promise<void> {
  const modelsRoot = document.documentElement.dataset["modelsRoot"] ?? "";
  const result = await call<{ models: ModelEntry[] }>(CMD.listModels, {
    models_root: modelsRoot || null,
  });
  models = result?.models ?? [];
  renderGrid();
}

/** 硬件标签：如实标注，未验证不得写成可用。 */
function hardwareBadge(model: ModelEntry): HTMLElement {
  const parts: string[] = [];
  if (model.npuSupported) parts.push(tr("NPU 已验证"));
  else if (model.gpuSupported) parts.push("GPU");
  else parts.push(tr("不支持 NPU"));

  const badge = h("span", { class: "cell__badge", text: parts.join(" · ") });
  if (!model.npuSupported) badge.classList.add("is-unverified");
  badge.title = [
    `GPU: ${model.gpuSupported ? "✓" : "—"}`,
    `核显: ${model.igpuSupported ? "✓" : "—"}`,
    `NPU: ${model.npuSupported ? "✓" : "—"}`,
    model.minRamGb ? `最低内存 ${model.minRamGb} GB` : "",
  ]
    .filter(Boolean)
    .join("\n");
  return badge;
}

function modelCell(model: ModelEntry): HTMLElement {
  const active = store.get().downloads[model.id];
  const card = h("article", { class: "cell", "data-task": model.task, "data-id": model.id });
  if (model.installed) card.classList.add("is-installed");

  // 头：编号 + 任务标签（像目录的摊位号）
  const head = h("header", { class: "cell__head" });
  head.append(
    h("span", { class: "cell__no", text: model.id.slice(-6).toUpperCase() }),
    h("span", { class: "cell__task", text: TASK_LABEL[model.task] ?? model.task }),
  );
  card.append(head);

  card.append(h("h3", { class: "cell__name", text: model.name }));

  if (model.description) {
    card.append(h("p", { class: "cell__desc", text: model.description }));
  }

  // 元数据行：大小 + 质量分
  const meta = h("div", { class: "cell__meta" });
  meta.append(
    h("span", { class: "cell__size", text: model.sizeLabel || "—" }),
    scorePips(model.quality),
  );
  card.append(meta);

  // 语言与运行时
  const facts = h("div", { class: "cell__facts" });
  if (model.languages) facts.append(h("span", { text: model.languages }));
  if (model.runtime) facts.append(h("span", { text: model.runtime }));
  if (model.license) facts.append(h("span", { text: model.license }));
  if (facts.childElementCount) card.append(facts);

  // 下载进度（只在下载中显示）
  if (active) {
    const bar = h("div", { class: "cell__progress" });
    bar.append(
      h("div", { class: "progress__track" }, [
        h("div", { class: "progress__fill", style: `width:${percent(active.completed, active.total)}` }),
      ]),
      h("span", { class: "cell__progress-label", text: `${active.stage} ${percent(active.completed, active.total)}` }),
    );
    card.append(bar);
  }

  // 底部：硬件标签 + 操作
  const foot = h("footer", { class: "cell__foot" });
  foot.append(hardwareBadge(model));

  const isActiveModel = isSelectedModel(model);

  if (isActiveModel) {
    foot.append(h("span", { class: "cell__state", text: tr("使用中") }));
  } else if (model.builtin && !model.installed) {
    // 内置模型无需下载，直接可选
    const use = h("button", { class: "cell__action", type: "button", text: tr("使用") });
    on(use, "click", () => void selectModel(model));
    foot.append(use);
  } else if (model.installed) {
    const use = h("button", { class: "cell__action", type: "button", text: tr("使用") });
    on(use, "click", () => void selectModel(model));
    const del = h("button", { class: "cell__action is-quiet", type: "button", text: tr("卸载") });
    on(del, "click", () => void uninstallModel(model));
    foot.append(use, del);
  } else {
    const dl = h("button", { class: "cell__action", type: "button", text: tr("下载") });
    on(dl, "click", () => void installModel(model));
    foot.append(dl);
  }

  card.append(foot);
  return card;
}

function isSelectedModel(model: ModelEntry): boolean {
  const selected = document.documentElement.dataset["activeModels"] ?? "";
  return selected.split(",").includes(model.id) && model.installed;
}

function renderGrid(): void {
  if (!gridEl) return;
  const shown = filter === "all" ? models : models.filter((m) => m.task === filter);
  if (countEl) countEl.textContent = `${shown.length} 项`;
  gridEl.replaceChildren(...shown.map(modelCell));
}

function renderFilterBar(): HTMLElement {
  const bar = h("div", { class: "filter-bar", role: "tablist", "aria-label": tr("按任务筛选") });
  const options: ReadonlyArray<readonly [TaskFilter, string]> = [
    ["all", tr("全部")],
    ["translate", tr("翻译")],
    ["asr", tr("识别")],
    ["tts", tr("朗读")],
    ["ocr", "OCR"],
  ];
  for (const [value, label] of options) {
    const chip = h("button", {
      class: value === filter ? "filter-chip is-active" : "filter-chip",
      type: "button",
      text: label,
      "aria-pressed": String(value === filter),
    });
    on(chip, "click", () => {
      filter = value;
      filterBarEl?.replaceWith(renderFilterBar());
      renderGrid();
    });
    bar.append(chip);
  }
  return bar;
}

async function selectModel(model: ModelEntry): Promise<void> {
  const command =
    model.task === "asr" ? CMD.setAsrModel
    : model.task === "translate" ? null
    : null;

  if (command === CMD.setAsrModel) {
    await call(CMD.setAsrModel, { model_id: model.id });
  } else if (model.task === "translate") {
    await call(CMD.setConfig, { updates: { translate_model_id: model.id } });
    await call(CMD.setTranslator, { kind: "qwen-quality", config: {} });
  } else if (model.task === "tts") {
    const updates: Record<string, string> = {};
    if (model.languages.includes("中") || model.languages.toLowerCase().includes("zh")) {
      updates["tts_model_id_zh"] = model.id;
    }
    if (model.languages.includes("英") || model.languages.toLowerCase().includes("en")) {
      updates["tts_model_id_en"] = model.id;
    }
    if (!Object.keys(updates).length) updates["tts_model_id_zh"] = model.id;
    await call(CMD.setConfig, { updates });
  } else if (model.task === "ocr") {
    await call(CMD.setConfig, { updates: { ocr_model_id: model.id } });
  }

  const active = (document.documentElement.dataset["activeModels"] ?? "")
    .split(",")
    .filter(Boolean);
  const next = active.filter((id) => {
    const other = models.find((m) => m.id === id);
    return other ? other.task !== model.task : false;
  });
  next.push(model.id);
  document.documentElement.dataset["activeModels"] = next.join(",");
  renderGrid();
}

async function installModel(model: ModelEntry): Promise<void> {
  const modelsRoot = document.documentElement.dataset["modelsRoot"] ?? "";
  store.patch({
    downloads: {
      ...store.get().downloads,
      [model.id]: { completed: 0, total: model.sizeBytes || 1, stage: tr("开始下载") },
    },
  });
  renderGrid();
  const result = await call(CMD.installModel, {
    model_id: model.id,
    models_root: modelsRoot || null,
  });
  const downloads = { ...store.get().downloads };
  delete downloads[model.id];
  store.patch({ downloads });
  if (result) await loadModels();
  else renderGrid();
}

async function uninstallModel(model: ModelEntry): Promise<void> {
  const modelsRoot = document.documentElement.dataset["modelsRoot"] ?? "";
  await call(CMD.uninstallModel, { model_id: model.id, models_root: modelsRoot || null });
  await loadModels();
}

/** 被 store 的下载事件驱动时只重绘网格，不整页重建。 */
export function refreshDownloads(): void {
  renderGrid();
}

export function buildModelCatalog(): HTMLElement {
  const section = h("section", { class: "catalog" });

  const head = h("header", { class: "catalog__head" });
  head.append(h("h2", { class: "catalog__title", text: tr("模型目录") }));
  countEl = h("span", { class: "catalog__count", text: "" });
  head.append(countEl);
  const refresh = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: tr("刷新") });
  on(refresh, "click", () => void loadModels());
  head.append(h("span", { class: "catalog__spacer" }), refresh);
  section.append(head);

  filterBarEl = renderFilterBar();
  section.append(filterBarEl);

  gridEl = h("div", { class: "catalog__sheet" });
  section.append(gridEl);

  void loadModels();
  return section;
}
