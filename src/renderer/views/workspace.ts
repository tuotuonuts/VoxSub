/**
 * 字幕工作区 —— A/B/C 模式共用；D 模式时被 OCR 工作区替换。
 *
 * 结构（同人展目录的"展位"语言）：
 *   头部：标题 + 状态灯 + 会话操作
 *   主体：双语对照流，原文与译文并排（对应印刷词典的对照排版）
 *   尾部：C 模式的文件导入与进度
 */
import { h, on, percent } from "../dom";
import { call, store } from "../store";
import { CMD } from "../protocol";
import { tr } from "../i18n";

let streamEl: HTMLElement | null = null;
let statusTextEl: HTMLElement | null = null;
let progressWrap: HTMLElement | null = null;
let fileLabelEl: HTMLElement | null = null;
let recordingLabelEl: HTMLElement | null = null;

/** 把一句话渲染成一行对照。草稿行带 is-draft，原位替换而不是新增。 */
function subtitleRow(source: string, translation: string, draft = false): HTMLElement {
  const row = h("div", { class: draft ? "sub-row is-draft" : "sub-row" });
  row.append(
    h("p", { class: "sub-row__src", text: source || "…" }),
    h("p", { class: "sub-row__dst", text: translation || "" }),
  );
  return row;
}

function scrollToBottom(): void {
  if (streamEl) streamEl.scrollTop = streamEl.scrollHeight;
}

/** 增量更新：只在最后一行变化时重绘最后一行，避免整块重排导致滚动跳动。 */
export function updateStream(): void {
  if (!streamEl) return;
  const state = store.get();
  // 行数没变 → 只刷新草稿行内容
  const lastDraft = streamEl.querySelector<HTMLElement>(".sub-row.is-draft");
  if (state.draft) {
    if (lastDraft) {
      const src = lastDraft.querySelector(".sub-row__src");
      const dst = lastDraft.querySelector(".sub-row__dst");
      if (src) src.textContent = state.draft.source || "…";
      if (dst) dst.textContent = state.draft.translation || "";
      scrollToBottom();
      return;
    }
    streamEl.querySelector(".stream__empty")?.remove();
    streamEl.append(subtitleRow(state.draft.source, state.draft.translation, true));
    scrollToBottom();
    return;
  }

  lastDraft?.remove();
  const committed = streamEl.querySelectorAll(".sub-row:not(.is-draft)").length;
  if (committed === state.subtitles.length) {
    if (state.subtitles.length === 0 && !streamEl.querySelector(".stream__empty")) {
      streamEl.append(h("p", { class: "stream__empty", text: tr("开始后，原文与译文会并排出现在这里") }));
    }
    return;
  }
  // 数量对不上时整体重建（切换模式、清空会话等）
  rebuildStream();
}

function rebuildStream(): void {
  if (!streamEl) return;
  const state = store.get();
  streamEl.replaceChildren();
  if (state.subtitles.length === 0 && !state.draft) {
    streamEl.append(h("p", { class: "stream__empty", text: tr("开始后，原文与译文会并排出现在这里") }));
    return;
  }
  for (const line of state.subtitles) {
    streamEl.append(subtitleRow(line.source, line.translation));
  }
  if (state.draft) {
    streamEl.append(subtitleRow(state.draft.source, state.draft.translation, true));
  }
  scrollToBottom();
}

export function updateStatus(): void {
  const state = store.get();
  if (statusTextEl) statusTextEl.textContent = state.statusText;
  const lamp = document.querySelector(".status-lamp__dot");
  if (lamp instanceof HTMLElement) {
    lamp.classList.toggle("is-live", state.running && !state.paused);
    lamp.classList.toggle("is-paused", state.paused);
  }
  if (recordingLabelEl) {
    recordingLabelEl.textContent = state.recording ? tr("录音中") : "";
    recordingLabelEl.hidden = !state.recording;
  }
}

export function updateProgress(): void {
  const state = store.get();
  if (!progressWrap) return;
  const progress = state.progress;
  // 进度只在 C 模式有意义；其他模式隐藏，避免空白条占位
  if (!progress || state.mode !== "c") {
    progressWrap.hidden = true;
    return;
  }
  progressWrap.hidden = false;
  const bar = progressWrap.querySelector<HTMLElement>(".progress__fill");
  const label = progressWrap.querySelector<HTMLElement>(".progress__label");
  if (bar) bar.style.width = percent(progress.completed, progress.total);
  if (label) label.textContent = `${progress.stage} · ${percent(progress.completed, progress.total)}`;
}

/** 会话导出：把当前双语历史写成 SRT / VTT / TXT。 */
async function exportSession(): Promise<void> {
  const state = store.get();
  if (state.subtitles.length === 0) return;
  const api = window.voxsub;
  if (!api) return;
  // 主进程负责弹保存框，这里只送内容
  const lines = state.subtitles.map((line, index) => ({
    source: line.source,
    translation: line.translation,
    startMs: index * 3000,
    endMs: index * 3000 + 2800,
  }));
  await api.dialog.saveSession({ lines });
}

async function toggleRun(): Promise<void> {
  const state = store.get();
  if (!state.running) {
    await call(CMD.start);
    return;
  }
  if (state.paused) {
    await call(CMD.resume);
    return;
  }
  await call(CMD.pause);
}

async function stopRun(): Promise<void> {
  await call(CMD.stop);
  const result = await call<{ path: string | null }>(CMD.lastRecording);
  if (result?.path) store.pushLog({ ts: new Date().toISOString(), level: "INFO", message: `录音已保存: ${result.path}` });
}

export function buildWorkspace(): HTMLElement {
  const state = store.get();

  const pane = h("section", { class: "workspace" });

  // ---- 头部
  const head = h("header", { class: "workspace__head" });
  head.append(h("h2", { class: "workspace__title", text: tr("字幕") }));

  const lamp = h("div", { class: "status-lamp" });
  lamp.append(h("i", { class: "status-lamp__dot" }), h("span", { class: "status-lamp__text", text: state.statusText }));
  statusTextEl = lamp.querySelector(".status-lamp__text");
  head.append(lamp);
  pane.append(head);

  // ---- 会话操作条
  const actions = h("div", { class: "workspace__actions" });

  const cta = h("button", { class: "btn btn--primary", type: "button" });
  const syncCta = (): void => {
    const s = store.get();
    if (!s.running) cta.textContent = tr("开始");
    else if (s.paused) cta.textContent = tr("继续");
    else cta.textContent = tr("暂停");
    cta.disabled = s.mode === "d";
  };
  on(cta, "click", () => void toggleRun());
  Object.defineProperty(cta, "sync", { value: syncCta });

  const stopBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("结束") });
  on(stopBtn, "click", () => void stopRun());

  const exportBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("导出会话") });
  on(exportBtn, "click", () => void exportSession());

  const clearBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("清空") });
  on(clearBtn, "click", () => store.patch({ subtitles: [], draft: null }));

  const recSwitch = h("label", { class: "switch" });
  const recInput = h("input", { type: "checkbox" });
  recInput.checked = state.recording;
  on(recInput, "change", () => {
    store.patch({ recording: recInput.checked });
    void call(CMD.setRecording, { enabled: recInput.checked });
  });
  recSwitch.append(recInput, h("span", { class: "switch__track" }), h("span", { class: "switch__label", text: tr("同时录音") }));
  recordingLabelEl = h("span", { class: "rec-note", hidden: true });
  recordingLabelEl.hidden = true;
  recSwitch.append(recordingLabelEl);

  actions.append(cta, stopBtn, exportBtn, clearBtn, recSwitch);
  pane.append(actions);

  // ---- C 模式文件区
  const filePanel = h("div", { class: "file-panel" });
  const pickBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("选择文件") });
  on(pickBtn, "click", async () => {
    const api = window.voxsub;
    if (!api) return;
    const picked = await api.dialog.pickMedia();
    if (!picked) return;
    fileLabelEl!.textContent = picked;
    store.patch({ statusText: tr("已选择文件") });
    await call(CMD.setInputFile, { path: picked });
  });
  fileLabelEl = h("span", { class: "file-panel__name", text: tr("尚未选择文件") });
  filePanel.append(pickBtn, fileLabelEl);

  progressWrap = h("div", { class: "progress", hidden: true });
  progressWrap.append(
    h("div", { class: "progress__track" }, [h("div", { class: "progress__fill" })]),
    h("div", { class: "progress__label", text: "" }),
  );
  filePanel.append(progressWrap);
  // 文件区只在 C 模式出现
  filePanel.hidden = state.mode !== "c";
  pane.append(filePanel);

  // ---- 字幕流
  streamEl = h("div", { class: "subtitle-stream" });
  pane.append(streamEl);

  rebuildStream();
  syncCta();
  window.addEventListener("voxsub:state", () => {
    syncCta();
    filePanel.hidden = store.get().mode !== "c";
  });

  return pane;
}

export function setWorkspaceFile(path: string): void {
  if (fileLabelEl) fileLabelEl.textContent = path;
}
