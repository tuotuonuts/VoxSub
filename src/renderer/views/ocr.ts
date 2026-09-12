/**
 * OCR 工作区 —— 对应原 Qt 版 ocr_workspace.py + ocr_overlay.py（1293 行）。
 *
 * 两种模式（与 Qt 版一致，用分页而非全部平铺）：
 *   截图翻译：框选 / 上传 → 识别 → 预览（原图⇄译后）+ 文本对照 + 导出
 *   实时区域：持续监视 → 译文原位覆盖（覆盖窗由主进程管理）
 *
 * 隐私纪律（PRODUCT.md）：截图像素只在本机内存处理；
 * 只有选择云翻译时，识别出的文字才会送出去。
 */
import { h, on } from "../dom";
import { call, store } from "../store";
import { CMD, type OcrResult, type OcrLine } from "../protocol";
import { tr } from "../i18n";

type OcrMode = "shot" | "live";

let sourceEl: HTMLElement | null = null;
let translationEl: HTMLElement | null = null;
let statusEl: HTMLElement | null = null;
let previewEl: HTMLImageElement | null = null;
let previewWrapEl: HTMLElement | null = null;
let exportBtnEl: HTMLButtonElement | null = null;
let sourceTabBtn: HTMLButtonElement | null = null;
let translatedTabBtn: HTMLButtonElement | null = null;

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
    previewEl.src = `file:///${path.replace(/\\/g, "/")}?t=${Date.now()}`;
  }

  if (sourceTabBtn) sourceTabBtn.classList.toggle("is-active", !showingTranslated);
  if (translatedTabBtn) translatedTabBtn.classList.toggle("is-active", showingTranslated);
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
    if (!translationEl.textContent) translationEl.textContent = tr("（未翻译）");
  }
}

/** 识别 + 翻译 + 渲染译后图片。 */
async function processImage(imagePath: string): Promise<void> {
  const state = store.get();
  setStatus(tr("正在识别…"));

  const result = await call<OcrResult>(CMD.ocrRecognize, {
    path: imagePath,
    translate: true,
    source: state.sourceLang,
    target: state.targetLang,
  });
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
    renderPreview();
    return;
  }

  // 译后图片：把译文画回原图（后端用 PIL 渲染，等价于 Qt 的 render_translated_image）
  setStatus(tr("正在生成译后图片…"));
  const rendered = await call<{ path: string }>(CMD.renderOcrImage, {
    source: result.sourcePath,
    target: temporaryPath("translated"),
    lines: lines.map((l: OcrLine) => ({ translation: l.translation, box: l.box })),
  });
  translatedImagePath = rendered?.path ?? "";
  showingTranslated = true;
  renderPreview();

  const parts = [
    `${tr("完成")} · ${lines.length} ${tr("行")}`,
    `OCR ${result.ocrElapsedMs ?? 0}ms`,
  ];
  if (result.translateElapsedMs) parts.push(`${tr("翻译")} ${result.translateElapsedMs}ms`);
  setStatus(parts.join(" · "));
}

/** 生成临时输出路径（译后图片缓存，用完由主进程清理）。 */
function temporaryPath(kind: string): string {
  const stamp = Date.now();
  const root = store.get().cacheRoot || "";
  const base = root || "";
  return `${base}/ocr-${kind}-${stamp}.png`;
}

async function pickImage(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  const path = await api.dialog.pickImage();
  if (!path) return;
  await processImage(path);
}

async function selectScreenArea(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  setStatus(tr("拖动选择区域，Esc 取消"));
  // 框选在主进程完成（需隐藏自身窗口、等待桌面合成），返回截图临时路径
  const path = await api.ocr.selectArea();
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
  const target = await api.dialog.saveImage();
  if (!target) return;
  const saved = await call<{ path: string }>(CMD.copyFile, {
    source: translatedImagePath,
    target,
  });
  setStatus(saved ? `${tr("已导出译后图片")}：${target}` : tr("导出失败"));
}

async function copyText(which: "source" | "translation"): Promise<void> {
  const text = which === "source" ? sourceEl?.textContent ?? "" : translationEl?.textContent ?? "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    setStatus(which === "source" ? tr("已复制原文") : tr("已复制译文"));
  } catch {
    setStatus(tr("复制失败"));
  }
}

async function startLiveRegion(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  setStatus(tr("拖动选择要持续翻译的区域，Esc 取消"));
  const area = await api.ocr.startLiveRegion();
  if (area) setStatus(tr("实时 OCR 运行中 · 译文将原位覆盖"));
  else setStatus(tr("已取消框选"));
}

async function stopLiveRegion(): Promise<void> {
  await window.voxsub?.ocr.stopLiveRegion();
  setStatus(tr("实时 OCR 已结束"));
}

/* ------------------------------------------------------------- 界面构建 */

function buildShotPage(): HTMLElement {
  const page = h("div", { class: "ocr-page" });

  // 动作行
  const actions = h("div", { class: "workspace__actions" });
  const areaBtn = h("button", { class: "btn btn--primary", type: "button", text: tr("框选屏幕并翻译") });
  on(areaBtn, "click", () => void selectScreenArea());
  const uploadBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("上传图片并翻译") });
  on(uploadBtn, "click", () => void pickImage());
  actions.append(areaBtn, uploadBtn);
  page.append(actions);

  // 预览区（原图⇄译后切换 + 导出）
  const previewBar = h("div", { class: "ocr-preview-bar" });
  sourceTabBtn = h("button", { class: "filter-chip", type: "button", text: tr("原图") });
  on(sourceTabBtn, "click", () => {
    showingTranslated = false;
    renderPreview();
  });
  translatedTabBtn = h("button", { class: "filter-chip", type: "button", text: tr("译后") });
  on(translatedTabBtn, "click", () => {
    showingTranslated = true;
    renderPreview();
  });
  exportBtnEl = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: tr("导出译后图片") });
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
      const btn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: tr("复制") });
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
      const btn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: tr("复制") });
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

  const intro = h("div", { class: "card" });
  intro.append(
    h("h3", { class: "card__title", text: tr("实时区域 OCR") }),
    h("div", { class: "card__body" }, [
      h("p", { class: "field__hint", text: tr("原位覆盖，不重复识别静止画面。选中一块区域后持续识别，译文直接盖在原文位置上。") }),
      h("p", { class: "field__hint", text: tr("覆盖窗不会被下一轮截图识别到（已排除捕获），因此不会出现译文被再翻译的回路。") }),
    ]),
  );
  page.append(intro);

  const actions = h("div", { class: "workspace__actions" });
  const startBtn = h("button", { class: "btn btn--primary", type: "button", text: tr("选择区域并开始") });
  on(startBtn, "click", () => void startLiveRegion());
  const stopBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("结束实时 OCR") });
  on(stopBtn, "click", () => void stopLiveRegion());
  actions.append(startBtn, stopBtn);
  page.append(actions);

  page.append(
    h("p", { class: "privacy-note", text: tr("隐私：识别只在本机进行；译文的显示位置与原文框一致，不修改屏幕上的原应用。") }),
  );
  return page;
}

export function buildOcrWorkspace(): HTMLElement {
  const pane = h("section", { class: "workspace ocr" });

  const head = h("header", { class: "workspace__head" });
  head.append(h("h2", { class: "workspace__title", text: tr("OCR 图片与屏幕翻译") }));
  statusEl = h("span", { class: "ocr__status", text: tr("等待框选") });
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

  let current: OcrMode = "shot";
  const render = (): void => {
    const found = tabs.find(([id]) => id === current);
    body.replaceChildren(found ? found[2]() : h("div"));
    bar.querySelectorAll(".filter-chip").forEach((node) => {
      node.classList.toggle("is-active", (node as HTMLElement).dataset["mode"] === current);
    });
  };

  for (const [id, label] of tabs) {
    const btn = h("button", {
      class: "filter-chip",
      type: "button",
      "data-mode": id,
      text: label,
    });
    on(btn, "click", () => {
      current = id;
      render();
    });
    bar.append(btn);
  }

  pane.append(bar, body);
  render();
  return pane;
}
