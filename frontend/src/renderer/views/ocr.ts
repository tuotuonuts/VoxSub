import { localFileUrl } from "../../shared/file-url";
import { buildField } from "../ui/field";
import { buildToggleSwitch } from "../ui/controls";
import { buildCard } from "../ui/card";
import { buildButton, buildFilterChip } from "../ui/button";
/**
 * OCR 工作区 —— 对应原 Qt 版 ocr_workspace.py + ocr_overlay.py（1293 行）。
 *
 * 两种模式（与 Qt 版一致，用分页而非全部平铺）：
 *   截图翻译：框选 / 上传 → 识别 → 预览（原图⇄译后）+ 文本对照 + 导出
 *   实时区域：持续监视 → 译文原位覆盖（覆盖窗由主进程管理）
 *
 * 隐私纪律（PRODUCT.md）：截图像素只在本机处理（临时截图及译后预览存入本地缓存）；
 * 只有选择云翻译时，识别出的文字才会送出去。
 */
import { h, on } from "../dom";
import { call, store } from "../store";
import { CMD, type OcrResult, type OcrLine } from "../protocol";
import { tr } from "../i18n";
import { PageLifecycle, type PageHandle } from "../../shared/page-lifecycle";

type OcrMode = "shot" | "live";

let sourceEl: HTMLElement | null = null;
let translationEl: HTMLElement | null = null;
let statusEl: HTMLElement | null = null;
let previewEl: HTMLImageElement | null = null;
let previewWrapEl: HTMLElement | null = null;
let exportBtnEl: HTMLButtonElement | null = null;
let sourceTabBtn: HTMLButtonElement | null = null;
let translatedTabBtn: HTMLButtonElement | null = null;
let retryBtnEl: HTMLButtonElement | null = null;
let lastImagePath = "";
let translateImages = true;

/**
 * 页面生命周期（缺陷 #10）：OCR 的识别 + 译后渲染是本项目最长的
 * 前后端往返（截图 → 识别 → 翻译 → 画回图片），期间用户完全可以切走模式或
 * 关页。这里登记页面有效性，供每个 await 之后的复查使用。
 */
let pageLifecycle: PageLifecycle | null = null;

/**
 * 子页面代号。
 *
 * OCR 工作区分「截图翻译 / 实时区域」两页，切页会整块替换 DOM 并重新指向
 * 模块级节点引用。若只判断"页面还在不在"，切页期间回来的旧结果就会写进
 * **新的**子页面（表现为：切到实时区域，却看到上一次截图的识别结果）。
 * 每次构建子页面时 +1，请求发起时记住它，回来时不一致就丢弃。
 */
let viewToken = 0;
let imageRequest = 0;
let liveRequest = 0;
let liveActive = false;
let currentMode: OcrMode = "shot";

/** 异步结果回来时页面是否仍然有效（页面未释放 + 仍是同一个子页面）。 */
function onPage(token: number): boolean {
  return Boolean(pageLifecycle && !pageLifecycle.disposed) && token === viewToken;
}

/** 最近一次结果：预览切换与导出都基于它。 */
let lastResult: OcrResult | null = null;
/** 预览显示的是译后图还是原图。 */
let showingTranslated = true;
/** 译后图片的临时路径（由后端渲染）。 */
let translatedImagePath = "";

function setStatus(text: string): void {
  if (statusEl) statusEl.textContent = text;
}

/** 预览区渲染：原图或译后图，缺图时给出明确占位。 */
function renderPreview(): void {
  if (!previewEl || !previewWrapEl) return;

  const path = showingTranslated ? translatedImagePath : (lastResult?.sourcePath ?? "");
  const hasImage = Boolean(path);

  previewWrapEl.classList.toggle("is-empty", !hasImage);
  previewEl.hidden = !hasImage;
  if (hasImage) {
    // 本地文件用 file:// 直接加载（图片本体不外发）
    previewEl.src = localFileUrl(path);
  }

  if (sourceTabBtn) {
    sourceTabBtn.classList.toggle("is-active", !showingTranslated);
    sourceTabBtn.setAttribute("aria-pressed", String(!showingTranslated));
  }
  if (translatedTabBtn) {
    translatedTabBtn.classList.toggle("is-active", showingTranslated);
    translatedTabBtn.setAttribute("aria-pressed", String(showingTranslated));
  }
  if (exportBtnEl) exportBtnEl.disabled = !translatedImagePath;
}

function renderText(): void {
  if (sourceEl) {
    const lines = lastResult?.lines ?? [];
    sourceEl.textContent = lines.length
      ? lines.map((l) => l.text).join("\n")
      : (lastResult?.text ?? "");
    if (!sourceEl.textContent) sourceEl.textContent = tr("（未识别到文字）");
  }
  if (translationEl) {
    const lines = lastResult?.lines ?? [];
    translationEl.textContent = lines.length
      ? lines.map((l) => l.translation || "").join("\n")
      : (lastResult?.translation ?? "");
    if (!translationEl.textContent.trim()) translationEl.textContent = tr("（未翻译）");
  }
}

/** 识别 + 翻译 + 渲染译后图片。 */
async function processImage(imagePath: string): Promise<void> {
  // 记住发起请求时所在的子页面：回来时若已切页，结果就该丢弃
  const token = viewToken;
  const request = ++imageRequest;
  const active = (): boolean => onPage(token) && request === imageRequest;
  const state = store.get();
  const translate = translateImages;
  lastImagePath = imagePath;
  if (retryBtnEl) retryBtnEl.disabled = false;
  translatedImagePath = "";
  lastResult = null;
  renderText();
  renderPreview();
  setStatus(tr("正在识别…"));

  const result = await call<OcrResult>(CMD.ocrRecognize, {
    path: imagePath,
    translate,
    source: state.sourceLang,
    target: state.targetLang,
  });
  if (!active()) return;
  if (!result) {
    setStatus(tr("识别失败，详见日志"));
    return;
  }

  lastResult = result;
  renderText();

  const lines = result.lines ?? [];
  if (lines.length === 0) {
    setStatus(tr("没有检测到文字，请放大区域或提高对比度"));
    translatedImagePath = "";
    showingTranslated = false;
    renderPreview();
    return;
  }

  if (!translate) {
    showingTranslated = false;
    renderPreview();
    setStatus(`${tr("识别完成，未调用翻译模型")} · ${lines.length} ${tr("行")}`);
    return;
  }

  if (!lines.some(line => line.translation?.trim())) {
    showingTranslated = false;
    renderPreview();
    setStatus(tr(result.failedLines ? "部分翻译失败，可重试" : "已识别文字，但当前语言或处理上限下没有可显示的译文。"));
    return;
  }

  // 译后图片：把译文画回原图（后端用 PIL 渲染，等价于 Qt 的 render_translated_image）
  setStatus(tr("正在生成译后图片…"));
  const rendered = await call<{ path: string; truncatedLines?: number }>(CMD.renderOcrImage, {
    source: result.sourcePath,

    lines: lines.map((l: OcrLine) => ({ translation: l.translation, box: l.box })),
  });
  if (!active()) return;
  translatedImagePath = rendered?.path ?? "";
  showingTranslated = Boolean(translatedImagePath);
  if (!rendered) {
    renderPreview();
    setStatus(tr("已识别，但译后图片生成失败；可以复制文字或重试。"));
    return;
  }
  renderPreview();

  const parts = [
    `${tr(result.failedLines ? "部分翻译失败，可重试" : result.untranslatedLines ? "部分文字因语言或处理上限未翻译" : "完成")} · ${lines.length} ${tr("行")}`,
    `OCR ${result.ocrElapsedMs ?? 0}ms`,
  ];
  if (result.translateElapsedMs) parts.push(`${tr("翻译")} ${result.translateElapsedMs}ms`);
  if (rendered.truncatedLines) parts.push(tr("部分译文过长，图片中已省略；完整译文可复制。"));
  setStatus(parts.join(" · "));
}

/** Native dialogs/capture can reject (permission, missing display, renderer load). */
async function nativeOcrAction<T>(action: () => Promise<T>): Promise<T | undefined> {
  const token = viewToken;
  try { return await action(); }
  catch { if (onPage(token)) setStatus(tr("图片或框选操作失败，请重试；如持续失败，请查看诊断。")); return undefined; }
}

async function pickImage(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  const token = viewToken;
  const path = await nativeOcrAction(() => api.dialog.pickImage());
  // 选文件期间用户可能已经切页/关页：此时不该再启动识别
  if (!onPage(token) || path === undefined) return;
  if (!path) return;
  await processImage(path);
}

async function selectScreenArea(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  const token = viewToken;
  setStatus(tr("拖动选择区域，Esc 取消"));
  // 框选在主进程完成（需隐藏自身窗口、等待桌面合成），返回截图临时路径
  const path = await nativeOcrAction(() => api.ocr.selectArea());
  if (!onPage(token) || path === undefined) return;
  if (!path) {
    setStatus(tr("已取消框选"));
    return;
  }
  await processImage(path);
}

async function exportTranslatedImage(): Promise<void> {
  if (!translatedImagePath) {
    setStatus(tr("当前没有可导出的译后图片"));
    return;
  }
  const api = window.voxsub;
  if (!api) return;
  const token = viewToken, request = imageRequest;
  const source = translatedImagePath;
  const target = await nativeOcrAction(() => api.dialog.saveImage());
  if (!onPage(token) || request !== imageRequest) return;
  if (!target) return;
  const saved = await call<{ path: string }>(CMD.copyFile, {
    source,
    target,
  });
  if (!onPage(token)) return;
  setStatus(saved ? `${tr("已导出译后图片")}：${target}` : tr("导出失败"));
}

async function copyText(which: "source" | "translation"): Promise<void> {
  const token = viewToken;
  const text = which === "source" ? lastResult?.text ?? "" : lastResult?.translation ?? "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    if (!onPage(token)) return;
    setStatus(which === "source" ? tr("已复制原文") : tr("已复制译文"));
  } catch {
    if (!onPage(token)) return;
    setStatus(tr("复制失败"));
  }
}

async function startLiveRegion(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  const token = viewToken, request = ++liveRequest;
  setStatus(tr("拖动选择要持续翻译的区域，Esc 取消"));
  const state = store.get();
  const area = await nativeOcrAction(() => api.ocr.startLiveRegion({source: state.sourceLang, target: state.targetLang}));
  if (request !== liveRequest || area === undefined) return;
  if (!onPage(token)) { if (area) await nativeOcrAction(() => api.ocr.stopLiveRegion()); return; }
  liveActive = Boolean(area);
  setStatus(area ? tr("实时 OCR 运行中 · 译文将原位覆盖") : tr("已取消框选"));
}

async function stopLiveRegion(): Promise<void> {
  liveRequest++;
  liveActive = false;
  await nativeOcrAction(async () => window.voxsub?.ocr?.stopLiveRegion?.());
}

/* ------------------------------------------------------------- 界面构建 */

function buildShotPage(): HTMLElement {
  const page = h("div", { class: "ocr-page" });
  // 新子页面：作废旧代号，让在途的识别结果不再写进这一页
  viewToken += 1;

  // 动作行
  const actions = h("div", { class: "workspace__actions" });
  const areaBtn = buildButton(tr(translateImages ? "框选屏幕并翻译" : "框选屏幕并识别"), { variant: "primary" });
  on(areaBtn, "click", () => void selectScreenArea());
  const uploadBtn = buildButton(tr(translateImages ? "上传图片并翻译" : "上传图片并识别"));
  on(uploadBtn, "click", () => void pickImage());
  retryBtnEl = buildButton(tr("重新处理当前图片"), {disabled: !lastImagePath});
  on(retryBtnEl, "click", () => { if (lastImagePath) void processImage(lastImagePath); });
  actions.append(areaBtn, uploadBtn, retryBtnEl);
  page.append(actions);
  page.append(buildField({control: buildToggleSwitch(translateImages, tr("识别后自动翻译"), value => {
    translateImages = value;
    areaBtn.textContent = tr(value ? "框选屏幕并翻译" : "框选屏幕并识别");
    uploadBtn.textContent = tr(value ? "上传图片并翻译" : "上传图片并识别");
  }), hint: tr("关闭后只提取文字，不加载翻译模型，也不发送文字给云翻译服务。") }));

  // 预览区（原图⇄译后切换 + 导出）
  const previewBar = h("div", { class: "ocr-preview-bar" });
  sourceTabBtn = buildFilterChip(tr("原图"));
  on(sourceTabBtn, "click", () => {
    showingTranslated = false;
    renderPreview();
  });
  translatedTabBtn = buildFilterChip(tr("译后"));
  on(translatedTabBtn, "click", () => {
    showingTranslated = true;
    renderPreview();
  });
  exportBtnEl = buildButton(tr("导出译后图片"), { small: true });
  on(exportBtnEl, "click", () => void exportTranslatedImage());
  exportBtnEl.disabled = true;

  previewBar.append(
    sourceTabBtn,
    translatedTabBtn,
    h("span", { class: "catalog__spacer" }),
    exportBtnEl,
  );
  page.append(previewBar);

  previewWrapEl = h("div", { class: "ocr-preview is-empty" });
  previewEl = h("img", { class: "ocr-preview__img", alt: tr("识别结果预览") });
  previewWrapEl.append(
    previewEl,
    h("p", { class: "ocr-preview__hint", text: tr("框选或上传图片后，这里显示结果") }),
  );
  page.append(previewWrapEl);

  // 文本对照
  const body = h("div", { class: "ocr__body" });
  const srcCol = h("div", { class: "ocr__col" });
  const srcHead = h("div", { class: "ocr__col-head" });
  srcHead.append(
    h("h3", { class: "ocr__col-title", text: tr("识别原文") }),
    (() => {
      const btn = buildButton(tr("复制"), { small: true });
      on(btn, "click", () => void copyText("source"));
      return btn;
    })(),
  );
  srcCol.append(srcHead);
  sourceEl = h("pre", { class: "ocr__text" });
  srcCol.append(sourceEl);

  const dstCol = h("div", { class: "ocr__col" });
  const dstHead = h("div", { class: "ocr__col-head" });
  dstHead.append(
    h("h3", { class: "ocr__col-title", text: tr("译文") }),
    (() => {
      const btn = buildButton(tr("复制"), { small: true });
      on(btn, "click", () => void copyText("translation"));
      return btn;
    })(),
  );
  dstCol.append(dstHead);
  translationEl = h("pre", { class: "ocr__text" });
  dstCol.append(translationEl);

  body.append(srcCol, dstCol);
  page.append(body);

  page.append(
    h("p", {
      class: "privacy-note",
      text: tr("隐私：截图只在本机内存中送入 OCR；只有选择云翻译时，识别出的文字才会发送给对应服务。"),
    }),
  );

  renderPreview();
  return page;
}

function buildLivePage(): HTMLElement {
  const page = h("div", { class: "ocr-page" });
  // 新子页面：作废旧代号（见 viewToken 的说明）
  viewToken += 1;

  const intro = buildCard(tr("实时区域 OCR"), [
      h("p", { class: "field__hint", text: tr("原位覆盖，不重复识别静止画面。选中一块区域后持续识别，译文直接盖在原文位置上。") }),
      h("p", { class: "field__hint", text: tr("覆盖层已请求系统捕获排除；受保护内容可能无法截图。单帧失败会短暂保留译文，过期后自动清除。") }),
      h("p", { class: "field__hint", text: tr("支持副屏与高 DPI。跨屏选区会裁到主要所在的显示器，请分别选择不同显示器的区域。") }),
    ], "div");
  page.append(intro);

  const actions = h("div", { class: "workspace__actions" });
  const startBtn = buildButton(tr("选择区域并开始"), { variant: "primary" });
  on(startBtn, "click", () => void startLiveRegion());
  const stopBtn = buildButton(tr("结束实时 OCR"));
  on(stopBtn, "click", () => void stopLiveRegion());
  actions.append(startBtn, stopBtn);
  page.append(actions);

  page.append(
    h("p", { class: "privacy-note", text: tr("隐私：识别只在本机进行；译文的显示位置与原文框一致，不修改屏幕上的原应用。") }),
  );
  return page;
}

export function buildOcrWorkspace(): PageHandle {
  const pane = h("section", { class: "workspace ocr" });
  // 页面生命周期（缺陷 #10）：在途的识别/渲染结果靠它判断页面是否还在
  const lifecycle = new PageLifecycle();
  pageLifecycle = lifecycle;

  const head = h("header", { class: "workspace__head" });
  head.append(h("h2", { class: "workspace__title", text: tr("OCR 图片与屏幕翻译") }));
  statusEl = h("span", { class: "ocr__status", role: "status", "aria-live": "polite", text: tr("等待框选") });
  head.append(statusEl);
  pane.append(head);

  // 两个模式用分页：截图翻译是一次性的，实时区域是持续运行的，
  // 混在一屏会让状态语义打架（Qt 版也是分页）。
  const tabs: ReadonlyArray<readonly [OcrMode, string, () => HTMLElement]> = [
    ["shot", tr("截图翻译"), buildShotPage],
    ["live", tr("实时区域"), buildLivePage],
  ];
  const bar = h("div", { class: "filter-bar" });
  const body = h("div", { class: "ocr-mode-body" });

  currentMode = "shot";
  let current: OcrMode = "shot";
  const render = (): void => {
    const found = tabs.find(([id]) => id === current);
    body.replaceChildren(found ? found[2]() : h("div"));
    bar.querySelectorAll(".filter-chip").forEach((node) => {
      const active = (node as HTMLElement).dataset["mode"] === current;
      node.classList.toggle("is-active", active);
      node.setAttribute("aria-pressed", String(active));
    });
  };

  for (const [id, label] of tabs) {
    const btn = buildFilterChip(label);
    btn.dataset["mode"] = id;
    on(btn, "click", () => {
      if (current === id) return;
      if (current === "live") void stopLiveRegion();
      current = id;
      currentMode = id;
      imageRequest++;
      render();
    });
    bar.append(btn);
  }

  pane.append(bar, body);
  render();
  const api = window.voxsub;
  if (api?.ocr?.onLiveStatus) lifecycle.add(api.ocr.onLiveStatus(payload => {
    if (lifecycle.disposed || currentMode !== "live" || !payload || typeof payload !== "object") return;
    const state = (payload as {state?: string}).state;
    const labels: Record<string, string> = {
      recognizing: "正在识别…", running: "实时 OCR 运行中 · 译文将原位覆盖",
      "no-text": "没有检测到文字，请放大区域或提高对比度", "no-translation": "已识别文字，但当前语言或处理上限下没有可显示的译文。", partial: "部分翻译失败，可重试",
      error: "当前帧处理失败，正在重试；旧译文会自动过期。", stopped: "实时 OCR 已结束",
    };
    if (state && labels[state]) setStatus(tr(labels[state]!));
  }));
  let languages = `${store.get().sourceLang}->${store.get().targetLang}`;
  lifecycle.add(store.subscribe(() => {
    const state = store.get();
    const next = `${state.sourceLang}->${state.targetLang}`;
    if (next !== languages && liveActive) void api?.ocr?.updateLanguages?.({source: state.sourceLang, target: state.targetLang});
    languages = next;
  }));

  // 释放：让模块级节点引用失效（缺陷 #10）。
  // 在途的识别/渲染结果回来时页面可能已经被换走，置空可避免往脱离文档的节点写；
  // 同时清掉上一次的识别结果，避免下次进入时显示上一条截图的内容。
  return {
    element: pane,
    dispose: () => {
      void stopLiveRegion();
      imageRequest++;
      lifecycle.dispose();
      if (pageLifecycle === lifecycle) pageLifecycle = null;
      viewToken += 1;
      lastResult = null;
      translatedImagePath = "";
      showingTranslated = true;
      statusEl = null;
      sourceEl = null;
      translationEl = null;
      previewEl = null;
      previewWrapEl = null;
      exportBtnEl = null;
      sourceTabBtn = null;
      translatedTabBtn = null;
      retryBtnEl = null;
      lastImagePath = "";
      translateImages = true;
    },
  };
}
