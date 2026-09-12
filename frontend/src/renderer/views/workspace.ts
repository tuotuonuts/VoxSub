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
/** 录音提示行：说明音频存到哪，而不是解释操作步骤。 */
let recordHintEl: HTMLElement | null = null;
/** 会话计时器（mm:ss）。只在运行中走字。 */
let clockEl: HTMLElement | null = null;
let clockTimer: ReturnType<typeof setInterval> | null = null;

/* 操作条控件引用。
   提到模块级是为了让 updateStatus（模块作用域）能驱动它们 —— 原先它们都是
   buildWorkspace 里的局部变量，状态更新只能在页面构建时绑定一次，
   会话中途状态变化（暂停、开始录音）不会反映到按钮上。 */
let ctaEl: HTMLButtonElement | null = null;
let stopBtnEl: HTMLButtonElement | null = null;
let recInputEl: HTMLInputElement | null = null;
let recDotEl: HTMLElement | null = null;

/**
 * 同步操作条上的全部状态：主按钮文案、收尾按钮文案与可见性、录音红点、说明行。
 *
 * 收在一处是因为这四项互相牵连 —— 拆开写就会出现"按钮说结束并保存、
 * 红点却没亮"这种自相矛盾的界面。
 */
function syncControls(): void {
  const s = store.get();
  const recording = Boolean(recInputEl?.checked);
  const inMicMode = s.mode === "a";

  // 主按钮：开始 → 暂停 ⇄ 继续
  if (ctaEl) {
    if (!s.running) ctaEl.textContent = tr("开始");
    else if (s.paused) ctaEl.textContent = tr("继续");
    else ctaEl.textContent = tr("暂停");
    ctaEl.disabled = s.mode === "d";
  }

  // 收尾按钮：会话在跑才出现；开了录音才谈得上"保存"
  if (stopBtnEl) {
    stopBtnEl.textContent = recording ? tr("结束并保存") : tr("结束");
    stopBtnEl.hidden = !s.running;
  }

  // 红点：真的在录（开关开着 + 会话在跑 + 没暂停 + 是麦克风模式）
  if (recDotEl) recDotEl.hidden = !(recording && s.running && !s.paused && inMicMode);
  if (recInputEl) recInputEl.disabled = !inMicMode;

  if (recordHintEl) {
    if (!inMicMode) {
      recordHintEl.textContent = tr("录音仅在麦克风同传模式可用");
    } else if (recording) {
      // 告诉用户文件会落在哪 —— 这才是他们需要知道的信息
      recordHintEl.textContent = tr("结束后保存为 WAV，放在本机录音文件夹");
    } else {
      recordHintEl.textContent = tr("仅生成字幕，不保存麦克风音频");
    }
  }
}

/** 启动秒表。幂等：重复调用不会叠出多个定时器。 */
function startClock(): void {
  stopClock();
  clockTimer = setInterval(syncClock, 1000);
  syncClock();
}

function stopClock(): void {
  if (clockTimer !== null) {
    clearInterval(clockTimer);
    clockTimer = null;
  }
}

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
  // 操作条与计时由 syncControls / syncClock 统一驱动（模块级）。
  // 它们依赖多个字段，散在 updateStatus 里容易和按钮状态不一致。
  syncControls();
}

/**
 * 会话计时。
 *
 * 手机录音在走表，用户靠它判断"到底录了多久"。原先界面上没有任何时间反馈，
 * 用户无法确认会话是否真的在进行。
 *
 * 单独开一个 1 秒定时器而不是挂在 store 订阅上：安静时没有字幕事件，
 * store 不会更新，挂在上面的计时会停住。
 */
function syncClock(): void {
  if (!clockEl) return;
  const state = store.get();
  if (!state.running || state.sessionStartedAt === null) {
    clockEl.hidden = true;
    clockEl.textContent = "";
    return;
  }
  const seconds = Math.max(0, Math.floor((Date.now() - state.sessionStartedAt) / 1000));
  const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
  const ss = String(seconds % 60).padStart(2, "0");
  clockEl.hidden = false;
  clockEl.textContent = `${mm}:${ss}`;
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

  // 用每句真实的相对时间戳。之前是 index * 3000 的假时间轴，
  // 会把长句、停顿、快语速全部拉平，导出的 SRT 与录音对不上。
  const lines = state.subtitles.map((line, index, all) => {
    const startMs = line.tsMs;
    const next = all[index + 1];
    // 结束时间取下一句的开始；最后一句给 3 秒收尾
    const endMs = next ? Math.max(startMs + 400, next.tsMs) : startMs + 3000;
    return {
      source: line.source,
      translation: line.translation,
      tsMs: startMs,
      startMs,
      endMs,
    };
  });
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

/**
 * 会话收尾（操作条上那个按钮）。
 *
 * 合并了原先的「结束」与「结束并保存」两个按钮 —— 它们底层是同一个 stop，
 * 同时摆在条上只会让用户犹豫该点哪个。是否保存由「同时录音」开关决定，
 * 按钮文案跟着开关走（见 syncRecorder）。
 *
 * 开着录音时把落盘路径回报到界面并在资源管理器里定位：用户点了"结束并保存"，
 * 最想知道的下一件事就是"文件在哪"。
 */
async function finishSession(): Promise<void> {
  const recording = store.get().recording;
  if (recording) store.patch({ statusText: tr("正在结束录音…") });

  await call(CMD.stop);

  const result = await call<{ path: string | null }>(CMD.lastRecording);
  if (result?.path) {
    store.patch({ statusText: `${tr("录音已保存")}：${fileName(result.path)}` });
    store.pushLog({ ts: new Date().toISOString(), level: "INFO", message: `录音已保存: ${result.path}` });
    // 让用户在资源管理器里能直接找到
    void window.voxsub?.dialog.revealInFolder(result.path);
  } else if (recording) {
    // 开了录音却没拿到文件：必须说清楚，否则用户以为录到了
    store.patch({ statusText: tr("录音未保存（可能未开启同时录音）") });
  }
}

function fileName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
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
  //
  // 录音按手机录音的操作模型组织：
  //   · 一个主按钮，随状态变形：开始 → 暂停 ⇄ 继续
  //   · 一个收尾按钮：开了「同时录音」就是「结束并保存」，否则是「结束」
  //   · 运行时显示走字的计时，录音时额外亮一个红点
  //
  // 为什么收尾只留一个按钮：原先「结束」与「结束并保存」同时挂在条上，而它们
  // 底层是同一个 stop，用户不知道该点哪个。现在按"是否在录音"决定文案与后续
  // 动作（是否回显保存路径、是否在资源管理器里定位文件）。
  //
  // 同时删掉了原先那行"像手机录音：开始 → 暂停 / 继续 → 结束并保存"的文案：
  // 那是把设计意图当界面说明写给用户看。用户要的是这个流程本身，不是对它的描述。
  const actions = h("div", { class: "workspace__actions" });

  ctaEl = h("button", { class: "btn btn--primary", type: "button" });
  on(ctaEl, "click", () => void toggleRun());

  stopBtnEl = h("button", { class: "btn btn--ghost", type: "button" });
  on(stopBtnEl, "click", () => void finishSession());

  clockEl = h("span", { class: "recorder__clock", hidden: true });

  const exportBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("导出会话") });
  on(exportBtn, "click", () => void exportSession());

  const clearBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("清空") });
  on(clearBtn, "click", () => store.patch({ subtitles: [], draft: null }));

  const recSwitch = h("label", { class: "switch" });
  recInputEl = h("input", { type: "checkbox" });
  recInputEl.checked = state.recording;
  on(recInputEl, "change", () => {
    store.patch({ recording: recInputEl!.checked });
    void call(CMD.setRecording, { enabled: recInputEl!.checked });
    syncControls();
  });
  recSwitch.append(recInputEl, h("span", { class: "switch__track" }), h("span", { class: "switch__label", text: tr("同时录音") }));
  // 录音指示：红点比文字更接近"正在录"的直觉，也不占宽度
  recDotEl = h("span", { class: "rec-dot", hidden: true });
  recSwitch.append(recDotEl);

  actions.append(
    ctaEl,
    stopBtnEl,
    clockEl,
    h("span", { class: "catalog__spacer" }),
    exportBtn,
    clearBtn,
    recSwitch,
  );
  pane.append(actions);

  // 录音说明行：只讲音频去哪，不讲操作步骤
  recordHintEl = h("p", { class: "field__hint rec-hint" });
  pane.append(recordHintEl);

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
  syncControls();
  startClock();

  window.addEventListener("voxsub:state", () => {
    filePanel.hidden = store.get().mode !== "c";
    syncControls();
  });

  return pane;
}

export function setWorkspaceFile(path: string): void {
  if (fileLabelEl) fileLabelEl.textContent = path;
}
