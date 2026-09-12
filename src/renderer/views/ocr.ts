/**
 * OCR 工作区 —— 对应原 Qt 版 ocr_workspace.py + ocr_overlay.py（1293 行）。
 *
 * 两种模式：
 *   截图翻译：框选一次 → 识别 → 对照显示
 *   实时区域：持续监视 → 译文原位覆盖（覆盖窗在主进程单独管理）
 *
 * 隐私纪律（PRODUCT.md）：截图像素只在本机内存处理；
 * 只有选择云翻译时，识别出的文字才会送出去。
 */
import { h, on } from "../dom";
import { call, store } from "../store";
import { CMD, type OcrResult } from "../protocol";
import { tr } from "../i18n";

let sourceEl: HTMLElement | null = null;
let translationEl: HTMLElement | null = null;
let statusEl: HTMLElement | null = null;

function setStatus(text: string): void {
  if (statusEl) statusEl.textContent = text;
}

/** 把识别结果 + 译文渲染出来。 */
function renderResult(result: OcrResult, translation: string): void {
  if (sourceEl) {
    const lines = result.lines.length
      ? result.lines.map((l) => l.text).join("\n")
      : result.text;
    sourceEl.textContent = lines || "（未识别到文字）";
  }
  if (translationEl) {
    translationEl.textContent = translation || "（未翻译）";
  }
}

async function recognizeAndTranslate(imagePath: string): Promise<void> {
  const state = store.get();
  setStatus(tr("正在识别…"));

  const recognized = await call<OcrResult>(CMD.ocrRecognize, { path: imagePath });
  if (!recognized) {
    setStatus("识别失败");
    return;
  }

  setStatus("正在翻译…");
  const translated = await call<{ translation: string }>(CMD.ocrTranslate, {
    text: recognized.text,
    source: state.sourceLang,
    target: state.targetLang,
  });

  renderResult(recognized, translated?.translation ?? "");
  setStatus(`识别 ${recognized.lines.length} 行`);
}

async function pickImage(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  const path = await api.dialog.pickImage();
  if (!path) return;
  await recognizeAndTranslate(path);
}

async function selectScreenArea(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  setStatus(tr("等待框选"));
  // 框选在主进程完成（需要隐藏自身窗口、等待桌面合成），返回截图的临时路径
  const path = await api.ocr.selectArea();
  if (!path) {
    setStatus("已取消框选");
    return;
  }
  await recognizeAndTranslate(path);
}

async function startLiveRegion(): Promise<void> {
  const api = window.voxsub;
  if (!api) return;
  setStatus("正在选择要持续翻译的区域…");
  const area = await api.ocr.startLiveRegion();
  if (area) setStatus("实时区域已启动 · 译文将原位覆盖");
  else setStatus("已取消");
}

async function stopLiveRegion(): Promise<void> {
  await window.voxsub?.ocr.stopLiveRegion();
  setStatus("实时区域已停止");
}

export function buildOcrWorkspace(): HTMLElement {
  const pane = h("section", { class: "workspace ocr" });

  const head = h("header", { class: "workspace__head" });
  head.append(h("h2", { class: "workspace__title", text: tr("屏幕 OCR") }));
  statusEl = h("span", { class: "ocr__status", text: tr("等待框选") });
  head.append(statusEl);
  pane.append(head);

  const actions = h("div", { class: "workspace__actions" });
  const areaBtn = h("button", { class: "btn btn--primary", type: "button", text: tr("框选屏幕") });
  on(areaBtn, "click", () => void selectScreenArea());
  const uploadBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("上传图片") });
  on(uploadBtn, "click", () => void pickImage());
  const liveBtn = h("button", { class: "btn btn--ghost", type: "button", text: "实时区域" });
  on(liveBtn, "click", () => void startLiveRegion());
  const stopBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("结束") });
  on(stopBtn, "click", () => void stopLiveRegion());
  actions.append(areaBtn, uploadBtn, liveBtn, stopBtn);
  pane.append(actions);

  // 对照区：原文与译文并排（沿用词典对照的排版）
  const body = h("div", { class: "ocr__body" });
  const srcCol = h("div", { class: "ocr__col" });
  srcCol.append(h("h3", { class: "ocr__col-title", text: tr("识别原文") }));
  sourceEl = h("pre", { class: "ocr__text" });
  srcCol.append(sourceEl);

  const dstCol = h("div", { class: "ocr__col" });
  dstCol.append(h("h3", { class: "ocr__col-title", text: tr("译文") }));
  translationEl = h("pre", { class: "ocr__text" });
  dstCol.append(translationEl);

  body.append(srcCol, dstCol);
  pane.append(body);

  pane.append(
    h("p", { class: "privacy-note", text: "隐私：截图只在本机内存中送入 OCR；只有选择云翻译时，识别出的文字才会发送给对应服务。" }),
  );

  return pane;
}
