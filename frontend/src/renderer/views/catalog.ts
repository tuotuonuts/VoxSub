import { buildSearchField } from "../ui/search-field";
import { buildEmptyState } from "../ui/empty-state";
import { buildSelect, buildToggleSwitch } from "../ui/controls";
import { createSearchDocument, searchModels, type SearchDocument, type ModelSearchScope } from "../../shared/model-search";
import { buildBadge, type BadgeTone } from "../ui/badge";
import { buildRating } from "../ui/rating";
import { buildRepositoryLink } from "../ui/repository-link";
import { buildButton, buildFilterChip } from "../ui/button";
/**
 * 模型目录 —— 同人展目录方向的核心落点。
 *
 * 对应原 Qt 版的 model_hub_window.py（623 行）。
 * 结构沿用"展位格"：密铺网格、格内贴元数据小字、选中被圈出。
 */
import { h, on, percent } from "../dom";
import { call, callWithOutcome, store } from "../store";
import { CMD, type ModelCatalogResult, type ModelEntry } from "../protocol";
import { tr, englishText } from "../i18n";
import { describeOutcome, isTaskRunning } from "../../shared/request-outcome";
import { type PageHandle } from "../../shared/page-lifecycle";
import { buildProgressBar } from "../ui/progress";

type TaskFilter = "all" | "asr" | "translate" | "tts" | "ocr";

const TASK_LABEL: Record<string, string> = {
  asr: "识别",
  translate: "翻译",
  tts: "朗读",
  ocr: "OCR",
};

let models: ModelEntry[] = [];
let searchDocuments: SearchDocument<ModelEntry>[] = [];
let searchQuery = "";
let searchScope: ModelSearchScope = "all";
let installedOnly = false;
let searchField: ReturnType<typeof buildSearchField> | null = null;
let scopeEl: HTMLSelectElement | null = null;
let installedInput: HTMLInputElement | null = null;
let downloadSource: "auto" | "global" | "china" = "auto";
let filter: TaskFilter = "all";
let gridEl: HTMLElement | null = null;
let countEl: HTMLElement | null = null;
let filterBarEl: HTMLElement | null = null;
let diskEl: HTMLElement | null = null;
let nextPageId = 0;
let activePageId = 0;
let loadRequestId = 0;

export async function loadModels(pageId = activePageId): Promise<void> {
  const requestId = ++loadRequestId;
  const isCurrent = (): boolean => pageId === activePageId && requestId === loadRequestId;
  const modelsRoot = document.documentElement.dataset["modelsRoot"] ?? "";
  let result: ModelCatalogResult | null = null;
  try {
    result = await call<ModelCatalogResult>(CMD.listModels, {
      models_root: modelsRoot || null,
    });
  } catch (error) {
    if (!isCurrent()) return;
    store.pushLog({
      ts: new Date().toISOString(),
      level: "ERROR",
      message: error instanceof Error ? error.message : String(error),
    });
  }
  if (!isCurrent() || !result) return;

  models = result.models;
  const aliases = (text: string): string[] => [text, englishText(text)];
  searchDocuments = models.map(model => createSearchDocument(model, {
    names: [model.name, model.id],
    tags: [model.languages, ...(model.tags ?? [])].flatMap(aliases),
    descriptions: [model.description, TASK_LABEL[model.task] ?? model.task].flatMap(aliases),
  }));
  if (result?.modelsRoot) store.patch({ modelsRoot: result.modelsRoot });

  // OCR 临时目录：译后图片要落盘，界面需要知道往哪写
  let cache: { path: string } | null = null;
  try {
    cache = await call<{ path: string }>(CMD.ocrCacheDir);
  } catch (error) {
    if (!isCurrent()) return;
    store.pushLog({
      ts: new Date().toISOString(),
      level: "ERROR",
      message: error instanceof Error ? error.message : String(error),
    });
  }
  if (!isCurrent()) return;
  if (cache?.path) store.patch({ cacheRoot: cache.path });
  renderGrid();
}

/** Four usable tiers plus insufficient/unknown states; color is never the only cue. */
function recommendationBadge(model: ModelEntry): HTMLElement {
  const tiers: Record<string, readonly [string, BadgeTone]> = {
    basic: ["基础款", "neutral"], recommended: ["推荐", "positive"],
    elevated: ["中高负载", "attention"], heavy: ["高负载", "danger"],
    insufficient: ["配置不足", "neutral"], unknown: ["待评估", "neutral"],
  };
  const [label, tone] = tiers[model.recommendation?.level ?? "unknown"] ?? tiers.unknown!;
  const explanation = model.recommendation?.reason || tr("暂时无法评估本机配置");
  const badge = buildBadge(tr(label), tone, `${tr("按本机配置估算，不是实际运行负载")}\n${tr(explanation)}`);
  badge.classList.add("cell__recommendation");
  return badge;
}

function modelCell(model: ModelEntry): HTMLElement {
  const active = store.get().downloads[model.id];
  const card = h("article", { class: "cell", "data-task": model.task, "data-id": model.id });
  if (model.installed) card.classList.add("is-installed");

  // 头：本机适配评级 + 原有任务标签
  const head = h("header", { class: "cell__head" });
  head.append(
    recommendationBadge(model),
    h("span", { class: "cell__task", text: tr(TASK_LABEL[model.task] ?? model.task) }),
  );
  card.append(head);

  card.append(h("h3", { class: "cell__name", text: model.name }));

  if (model.description) {
    card.append(h("p", { class: "cell__desc", text: tr(model.description), title: tr(model.description) }));
  }

  // 元数据行：大小 + 质量分
  const meta = h("div", { class: "cell__meta" });
  meta.append(
    h("span", { class: "cell__size", text: model.sizeLabel || "—" }),
    buildRating(model.quality, tr("能力"), tr("同类模型的能力参考，点亮越多通常越擅长复杂内容；不是本机运行速度。")),
  );
  card.append(meta);

  // 普通用户可读的语言与用途标签，不显示运行库/许可证代码
  const facts = h("div", { class: "cell__facts" });
  if (model.languages) facts.append(h("span", { text: tr(model.languages) }));
  for (const tag of [...new Set(model.tags ?? [])].slice(0, 3)) {
    if (tag !== model.languages) facts.append(h("span", { text: tr(tag) }));
  }
  if (facts.childElementCount) card.append(facts);

  // 下载进度（只在下载中显示）—— 内芯用 ProgressBar 组件，外层仍是本页的
  // `.cell__progress`（标签字号与独立页面不同，所以类名单独传）。
  if (active) {
    const bar = buildProgressBar({ labelClass: "cell__progress-label" });
    bar.setPercent(active.total > 0 ? (active.completed / active.total) * 100 : null);
    bar.setLabel(`${active.stage} ${percent(active.completed, active.total)}`);
    bar.setState("running");
    const box = h("div", { class: "cell__progress" }, [bar.track, bar.label]);
    card.append(box);
  }

  // 底部：官方仓库 + 清晰的已下载状态 + 原有操作
  const foot = h("footer", { class: "cell__foot" });
  const repository = buildRepositoryLink(model.officialRepo ?? "", tr("模型官方仓库"), url => {
    const warn = (): void => {
      store.pushLog({ ts: new Date().toISOString(), level: "WARNING", message: tr("无法打开模型官方仓库") });
    };
    void window.voxsub?.dialog.openExternal(url).then(opened => { if (!opened) warn(); }).catch(warn);
  });
  if (repository) foot.append(repository);
  if (model.installed) {
    const installed = buildBadge(`✓ ${tr("已下载")}`, "positive", tr("模型文件已在本机"));
    installed.classList.add("cell__installed");
    foot.append(installed);
  }
  foot.append(h("span", { class: "cell__spacer" }));

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
  const eligible = searchDocuments.filter(({ value }) =>
    (filter === "all" || value.task === filter) && (!installedOnly || value.installed));
  const shown = searchModels(eligible, searchQuery, searchScope);
  if (countEl) countEl.textContent = searchQuery.trim()
    ? tr("{shown} / {total} 项").replace("{shown}", String(shown.length)).replace("{total}", String(eligible.length))
    : tr("{count} 项").replace("{count}", String(shown.length));
  if (shown.length) gridEl.replaceChildren(...shown.map(modelCell));
  else {
    const owner = activePageId;
    const reset = buildButton(tr("重置筛选"));
    on(reset, "click", () => { if (owner === activePageId) resetFilters(); });
    gridEl.replaceChildren(buildEmptyState(tr("没有找到匹配的模型"),
      tr("试试更短的名称或标签，或重置用途与下载状态筛选。"), reset));
  }
}

function resetFilters(): void {
  searchQuery = ""; searchScope = "all"; installedOnly = false; filter = "all";
  searchField?.setValue("");
  if (scopeEl) scopeEl.value = "all";
  if (installedInput) installedInput.checked = false;
  filterBarEl?.replaceWith(renderFilterBar());
  renderGrid();
}

function renderFilterBar(): HTMLElement {
  const bar = h("div", { class: "filter-bar", role: "tablist", "aria-label": tr("按任务筛选") });
  const owner = activePageId;
  const options: ReadonlyArray<readonly [TaskFilter, string]> = [
    ["all", tr("全部")],
    ["translate", tr("翻译")],
    ["asr", tr("识别")],
    ["tts", tr("朗读")],
    ["ocr", "OCR"],
  ];
  for (const [value, label] of options) {
    const chip = buildFilterChip(label, value === filter);
    on(chip, "click", () => {
      if (owner !== activePageId) return;
      filter = value;
      filterBarEl?.replaceWith(renderFilterBar());
      renderGrid();
    });
    bar.append(chip);
  }
  filterBarEl = bar;
  return bar;
}

async function selectModel(model: ModelEntry): Promise<void> {
  const command =
    model.task === "asr" ? CMD.setAsrModel
    : model.task === "translate" ? null
    : null;

  if (command === CMD.setAsrModel) {
    const result = await callWithOutcome(CMD.setAsrModel, { model_id: model.id });
    if (result.outcome !== "ok") return;
  } else if (model.task === "translate") {
    const tier = model.runtime === "opus-onnx" ? "fast" : "quality";
    const result = await callWithOutcome(CMD.setConfig, { updates: { translate_model_id: model.id, translate_tier: tier } });
    if (result.outcome !== "ok") return;
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

  // 下载是长任务：30 秒超时**不是失败**（缺陷 #4）。
  // 请求层到时限只发通知、请求保持挂起，后端送回真实结果时这里才继续 ——
  // 因此中途不做任何"清理下载条目"的动作，否则界面会显示成"没在下载"，
  // 而磁盘上其实还在写。
  const { outcome, data } = await callWithOutcome<unknown>(CMD.installModel, {
    model_id: model.id,
    models_root: modelsRoot || null,
    source: downloadSource,
  });

  if (isTaskRunning(outcome)) {
    // 仍在下载：保留下载条目与进度，让 download 事件继续驱动界面
    store.pushLog({
      ts: new Date().toISOString(),
      level: "WARNING",
      message: describeOutcome(outcome, `${tr("下载")} ${model.id}`, tr),
    });
    return;
  }

  const downloads = { ...store.get().downloads };
  delete downloads[model.id];
  store.patch({ downloads });
  if (data !== null) await loadModels();
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

/**
 * 模型目录 —— 独立页面。
 *
 * 页面化的理由（使用逻辑）：装模型是低频操作，字幕是每次打开都要看的。
 * 之前把网格塞在首屏底部，等于让低频内容长期占着屏幕、挤压高频内容。
 *
 * 页面里提供：标题 + 计数 + 空间提示 + 刷新，按任务筛选，密铺网格。
 */
export function buildModelCatalog(): PageHandle {
  const pageId = ++nextPageId;
  activePageId = pageId;
  const page = h("div", { class: "catalog-page" });

  // 标题由二级页面外壳统一渲染（返回栏里已有「模型」），
  // 这里只留说明文字，避免同一个页名出现两次。
  const head = h("header", { class: "catalog-page__head" });
  head.append(
    h("p", {
      class: "catalog-page__sub",
      text: tr("识别、翻译、语音模型按用途分组。下载后即在本机运行，不上传你的音频。"),
    }),
  );
  page.append(head);

  // 工具行：计数 · 空间 · 筛选 · 刷新
  const bar = h("div", { class: "catalog-page__bar" });
  countEl = h("span", { class: "catalog__count", text: "", "aria-live": "polite", "aria-atomic": "true" });
  diskEl = h("span", { class: "catalog__disk", text: "" });
  const refresh = buildButton(tr("刷新"), { small: true });
  on(refresh, "click", () => void loadModels(pageId).then(() => updateDiskUsage(pageId)));
  const sourceLabel = h("label", { text: tr("下载源") });
  const source = h("select", { class: "input", "aria-label": tr("下载源"), "data-download-source": "" });
  for (const [value, label] of [["auto", "自动（失败切换备用源）"], ["global", "海外优先"], ["china", "中国大陆优先"]] as const) {
    source.append(h("option", { value, text: tr(label) }));
  }
  source.value = downloadSource;
  on(source, "change", () => {
    if (source.value === "auto" || source.value === "global" || source.value === "china") {
      downloadSource = source.value;
    }
  });
  sourceLabel.append(source);
  bar.append(countEl, diskEl, h("span", { class: "catalog__spacer" }), sourceLabel, refresh);
  page.append(bar);

  const search = buildSearchField(searchQuery, {
    label: tr("搜索模型"), placeholder: tr("搜索名称、标签或用途，例如：Moonshine 英语"), clear: tr("清空搜索"),
  }, value => { if (pageId === activePageId) { searchQuery = value; renderGrid(); } });
  searchField = search;
  scopeEl = buildSelect<ModelSearchScope>(searchScope,
    [["all", tr("全部内容")], ["name", tr("仅名称")], ["tags", tr("仅标签")]],
    value => { if (pageId === activePageId) { searchScope = value; renderGrid(); } });
  scopeEl.setAttribute("aria-label", tr("搜索范围")); scopeEl.setAttribute("data-search-scope", "");
  const installedToggle = buildToggleSwitch(installedOnly, tr("只看已下载"), checked => {
    if (pageId === activePageId) { installedOnly = checked; renderGrid(); }
  });
  installedInput = installedToggle.querySelector("input");
  if (installedInput) installedInput.setAttribute("data-installed-filter", "");
  page.append(h("div", { class: "catalog-search", role: "search", "aria-label": tr("搜索模型") },
    [search.element, scopeEl, installedToggle]));
  page.append(h("p", { class: "catalog-search__hint", text: tr("支持中英文、部分名称和轻微拼写错误；空格分隔的关键词需同时匹配。") }));

  filterBarEl = renderFilterBar();
  page.append(filterBarEl);

  const sheet = h("div", { class: "catalog__sheet" });
  gridEl = sheet;
  page.append(sheet);

  void loadModels(pageId).then(() => updateDiskUsage(pageId));

  // 每个页面都有独立代次；异步模型/缓存请求在每个 await 后检查页面与请求代次，
  // 迟到响应不能覆盖新页共享状态或 DOM。只有仍为当前页的句柄 dispose 才会
  // 使代次失效并清空模块级 DOM 引用，旧句柄迟到时不影响新页。
  return {
    element: page,
    dispose: () => {
      search.dispose();
      if (activePageId !== pageId) return;
      activePageId = ++nextPageId;
      loadRequestId += 1;
      gridEl = null;
      countEl = null;
      diskEl = null;
      filterBarEl = null;
      searchField = null; scopeEl = null; installedInput = null;
    },
  };
}

/** 显示模型目录占用，以及已装模型的合计体积。 */
async function updateDiskUsage(pageId = activePageId): Promise<void> {
  if (pageId !== activePageId || !diskEl) return;
  const installedBytes = models
    .filter((m) => m.installed)
    .reduce((sum, m) => sum + (m.installedBytes || 0), 0);
  const totalBytes = models.reduce((sum, m) => sum + (m.sizeBytes || 0), 0);

  const parts: string[] = [];
  if (installedBytes > 0) parts.push(`${tr("已占")} ${human(installedBytes)}`);
  if (totalBytes > 0) parts.push(`${tr("全部安装需")} ${human(totalBytes)}`);

  const root = store.get().modelsRoot;
  if (root) parts.push(root);

  diskEl.textContent = parts.join(" · ");
  diskEl.title = parts.join("\n");
}

function human(bytes: number): string {
  if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(2)} GB`;
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(0)} MB`;
  return `${bytes} B`;
}
