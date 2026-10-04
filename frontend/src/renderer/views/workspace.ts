/**
 * 字幕工作区 —— A/B/C 模式共用；D 模式时被 OCR 工作区替换。
 *
 * 结构（同人展目录的"展位"语言）：
 *   头部：标题 + 状态灯 + 会话操作
 *   主体：双语对照流，原文与译文并排（对应印刷词典的对照排版）
 *   尾部：C 模式的文件导入与进度
 */
import { h, on, percent } from "../dom";
import { call, getSessionRevision, refreshSessionState, requestSessionState, setRecordingState, store } from "../store";
import { CMD } from "../protocol";
import { tr } from "../i18n";
import { hasExportableSubtitles } from "../../shared/session-timeline";
import { PageLifecycle, type PageHandle } from "../../shared/page-lifecycle";
import { buildProgressBar, type ProgressBar } from "../ui/progress";
import { updateFollowing } from "../../shared/subtitle-scroll";
import { RecordingControl } from "../recording-control";

let streamEl: HTMLElement | null = null;
let statusTextEl: HTMLElement | null = null;
let progressWrap: HTMLElement | null = null;
/** 进度条内芯（组件：轨道 + 填充 + 标签）。 */
let progressBar: ProgressBar | null = null;
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
let pauseBtnEl: HTMLButtonElement | null = null;
let recInputEl: HTMLInputElement | null = null;
let recDotEl: HTMLElement | null = null;
let recordingControl: RecordingControl | null = null;
let workspaceLifecycle: PageLifecycle | null = null;

/**
 * 该模式是否支持暂停。
 *
 * 后端 pipeline.pause() 对 C 模式（离线文件）直接返回 —— 文件是一次性读取，
 * 没有"暂停拾音"这回事。D 模式（屏幕 OCR）根本不走会话，主按钮本身就禁用。
 * 不支持的模式必须**隐藏**暂停按钮，而不是留一个点了没反应的按钮。
 */
function supportsPause(mode: string): boolean {
  return mode === "a" || mode === "b";
}

/**
 * 同步操作条上的全部状态：主按钮、暂停按钮、录音红点、说明行。
 *
 * 收在一处是因为这几项互相牵连 —— 拆开写就会出现"按钮说结束、红点却没亮"
 * 这种自相矛盾的界面。
 *
 * 按钮布局（按用户要求）：
 *   · 主按钮   —— 开始 ⇄ 结束：点了开始就变结束，再点就结束会话
 *   · 暂停按钮 —— 暂停 ⇄ 继续：仅会话运行中、且该模式支持暂停时出现
 */
function syncControls(): void {
  const s = store.get();
  const recording = recordingControl?.view;
  const recordingChecked = recording?.checked ?? s.recording;
  const inRecordingMode = s.mode === "a" || s.mode === "b";

  // 主按钮：开始 ⇄ 结束
  if (ctaEl) {
    ctaEl.textContent = s.running ? tr("结束") : tr("开始");
    // D 模式没有会话概念（走 OCR 工作区），主按钮禁用
    ctaEl.disabled = s.mode === "d" || (!s.running && (s.languagePending || !s.languageCompatible));
    ctaEl.title = !s.running && !s.languageCompatible ? s.languageNotice : "";
  }

  // 暂停按钮：只在运行中且模式支持时出现
  if (pauseBtnEl) {
    pauseBtnEl.hidden = !(s.running && supportsPause(s.mode));
    pauseBtnEl.textContent = s.paused ? tr("继续") : tr("暂停");
    pauseBtnEl.classList.toggle("is-paused", s.paused);
  }

  // 红点：真的在录（开关开着 + 会话在跑 + 没暂停 + 是实时音频模式）
  if (recDotEl) recDotEl.hidden = !(recording?.active && inRecordingMode);
  if (recInputEl) {
    recInputEl.checked = recordingChecked;
    recInputEl.indeterminate = !recording?.known;
    recInputEl.disabled = !inRecordingMode || Boolean(recording?.disabled);
    recInputEl.setAttribute("aria-busy", String(Boolean(recording?.pending)));
  }

  if (recordHintEl) {
    const notice = recording?.notice;
    if (notice === "pending") recordHintEl.textContent = tr("正在确认录音保存设置…");
    else if (notice === "unknown") recordHintEl.textContent = tr("录音保存状态未确认，请重新连接后端后重试");
    else if (notice === "disconnected") recordHintEl.textContent = tr("后端已断开，无法确认录音保存状态");
    else if (notice === "rejected") recordHintEl.textContent = tr("后端未接受录音保存设置，已保留原状态");
    else if (recording?.reason) recordHintEl.textContent = recording.reason;
    else if (notice === "idle-only") recordHintEl.textContent = tr("请先结束会话，再更改录音保存设置");
    else if (notice === "unsupported" || !inRecordingMode) recordHintEl.textContent = tr("录音仅在麦克风或系统声音模式可用");
    else if (recordingChecked) recordHintEl.textContent = tr("结束后保存为 WAV，放在本机录音文件夹");
    else recordHintEl.textContent = tr(s.mode === "b"
      ? "仅生成字幕，不保存系统或指定应用音频"
      : "仅生成字幕，不保存麦克风音频");
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

/** 只有更新前就在底部附近才贴底；用户上滚阅读历史时不拉回（见 shared/subtitle-scroll）。 */
function mutateStream(mutate: () => void): void {
  if (streamEl) updateFollowing(streamEl, mutate);
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
      mutateStream(() => {
        if (src) src.textContent = state.draft!.source || "…";
        if (dst) dst.textContent = state.draft!.translation || "";
      });
      return;
    }
    mutateStream(() => {
      streamEl!.querySelector(".stream__empty")?.remove();
      streamEl!.append(subtitleRow(state.draft!.source, state.draft!.translation, true));
    });
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
  const el = streamEl;
  if (state.subtitles.length === 0 && !state.draft) {
    el.replaceChildren(h("p", { class: "stream__empty", text: tr("开始后，原文与译文会并排出现在这里") }));
    el.scrollTop = 0;
    return;
  }
  const committed = el.querySelectorAll(".sub-row:not(.is-draft)").length;
  const appending = committed > 0 && committed < state.subtitles.length && !el.querySelector(".stream__empty");
  mutateStream(() => {
    if (appending) {
      // 增量追加：不重建已有行，历史阅读位置才保得住
      el.querySelector(".sub-row.is-draft")?.remove();
      for (const line of state.subtitles.slice(committed)) el.append(subtitleRow(line.source, line.translation));
    } else {
      el.replaceChildren();
      for (const line of state.subtitles) el.append(subtitleRow(line.source, line.translation));
    }
    if (state.draft) el.append(subtitleRow(state.draft.source, state.draft.translation, true));
  });
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
  if (!progressWrap || !progressBar) return;
  const progress = state.progress;
  // 进度只在 C 模式有意义；其他模式隐藏，避免空白条占位
  if (!progress || state.mode !== "c") {
    progressWrap.hidden = true;
    return;
  }
  progressWrap.hidden = false;
  // 总数为 0 时进度未知：宽度不动，标签显示「—」（与原实现一致）
  progressBar.setPercent(progress.total > 0 ? (progress.completed / progress.total) * 100 : null);
  progressBar.setLabel(`${progress.stage} · ${percent(progress.completed, progress.total)}`);
}

/** 会话导出：把当前双语历史写成 SRT / VTT / TXT。 */
async function exportSession(): Promise<void> {
  const state = store.get();
  // 字幕的去留由 store 的 session 事件决定：**停止会话不再清空字幕**
  // （缺陷 #2，原先停止后这里必然是空的，导出表现得像点了没反应）。
  if (!hasExportableSubtitles(state)) return;
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

/**
 * 主按钮：开始 ⇄ 结束。
 *
 * 按用户要求：点击开始后按钮变成结束，再点就结束会话 —— 不需要在两个按钮
 * 之间判断该点哪个。
 */
async function toggleSession(): Promise<void> {
  if (store.get().running) {
    await finishSession();
    return;
  }
  if (store.get().languagePending || !store.get().languageCompatible) return;
  // 立即用命令返回值更新界面，不等 state 事件（事件是异步的另一条路）
  await requestSessionState(CMD.start);
}

/**
 * 暂停按钮：暂停 ⇄ 继续。
 *
 * 只在运行中且模式支持暂停时可见（见 supportsPause）。后端 pause() 对不支持的
 * 模式会直接返回，所以这里的 `!running` 早退是必要的兜底。
 */
async function togglePause(): Promise<void> {
  const state = store.get();
  if (!state.running) return;
  await requestSessionState(state.paused ? CMD.resume : CMD.pause);
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
  const state = store.get();
  const recording = state.recording && supportsPause(state.mode);
  if (recording) store.patch({ statusText: tr("正在结束录音…") });

  await requestSessionState(CMD.stop);

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

export function buildWorkspace(): PageHandle {
  // Module DOM references have exactly one live page owner, even during replacement.
  workspaceLifecycle?.dispose();
  const state = store.get();

  // 页面级生命周期（缺陷 #10）：这一页注册的监听器与会话计时器都记在这里，
  // 切模式/关页时一次释放 —— 原先监听器与 1 秒定时器只增不减。
  const lifecycle = new PageLifecycle();
  workspaceLifecycle = lifecycle;
  lifecycle.add(() => { if (workspaceLifecycle === lifecycle) workspaceLifecycle = null; });

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
  // 会话操作条（按用户要求的布局）：
  //   · 主按钮   —— 开始 ⇄ 结束：点开始后它变成结束，再点就结束会话
  //   · 暂停按钮 —— 暂停 ⇄ 继续：仅运行中且模式支持暂停时出现
  //   · 运行时显示走字的计时，录音时额外亮一个红点
  //
  // 为什么主按钮不再承担"暂停"：一个按钮同时表达三种动作（开始/暂停/继续）
  // 时，用户点之前无法预判会发生什么。拆成两个按钮后各自的语义是唯一的。
  const actions = h("div", { class: "workspace__actions" });

  ctaEl = h("button", { class: "btn btn--primary", type: "button" });
  on(ctaEl, "click", () => void toggleSession());

  pauseBtnEl = h("button", { class: "btn btn--ghost", type: "button", hidden: true });
  on(pauseBtnEl, "click", () => void togglePause());

  clockEl = h("span", { class: "recorder__clock", hidden: true });

  const exportBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("导出会话") });
  on(exportBtn, "click", () => void exportSession());

  const clearBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("清空") });
  on(clearBtn, "click", () => store.patch({ subtitles: [], draft: null }));

  const recSwitch = h("label", { class: "switch" });
  recInputEl = h("input", { type: "checkbox" });
  recInputEl.checked = state.recording;
  lifecycle.listen(recInputEl, "change", () => {
    void recordingControl?.toggle();
    syncControls();
  });
  recSwitch.append(recInputEl, h("span", { class: "switch__track" }), h("span", { class: "switch__label", text: tr("同时录音") }));
  // 录音指示：红点比文字更接近"正在录"的直觉，也不占宽度
  recDotEl = h("span", { class: "rec-dot", hidden: true });
  recSwitch.append(recDotEl);

  actions.append(
    ctaEl,
    pauseBtnEl,
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

  // 进度条内芯（JobProgress 组件）：轨道 + 填充 + 标签，宽度按百分比写入
  progressBar = buildProgressBar();
  progressWrap = h("div", { class: "progress", hidden: true }, [progressBar.track, progressBar.label]);
  filePanel.append(progressWrap);
  // 文件区只在 C 模式出现
  filePanel.hidden = state.mode !== "c";
  pane.append(filePanel);

  // ---- 字幕流
  streamEl = h("div", { class: "subtitle-stream" });
  pane.append(streamEl);
  let observedRecording = state.recordingState;
  let observedRevision = getSessionRevision();
  let observedPhase = state.backendPhase;
  recordingControl = new RecordingControl({
    read: async () => {
      await refreshSessionState();
      return store.get().recordingState;
    },
    set: enabled => setRecordingState(enabled, snapshot => {
      // This controller consumes its own acknowledgement in drain(), preserving queued clicks.
      // Other/current pages observe the shared publication normally, even after our disposal.
      observedRecording = snapshot;
      observedRevision = getSessionRevision();
    }),
    changed: () => {
      if (lifecycle.disposed) return;
      syncControls();
    },
  });
  lifecycle.add(() => { recordingControl?.disconnect(); recordingControl = null; });
  if (state.backendPhase === "failed" || state.backendPhase === "disconnected") recordingControl.disconnect();
  else recordingControl.observe(state.recordingState);
  if (!state.recordingState && state.backendPhase === "ready") void recordingControl.refresh();

  rebuildStream();
  // 页面刚构建时节点还没进文档（clientHeight 为 0），此时写 scrollTop 无效；
  // 挂载后的下一帧再定位到最新内容。之后是否跟随由 mutateStream 按真实位置判断。
  const initialStream = streamEl;
  const schedule = typeof requestAnimationFrame === "function" ? requestAnimationFrame : (fn: FrameRequestCallback) => setTimeout(fn, 0) as unknown as number;
  schedule(() => {
    if (initialStream && streamEl === initialStream) initialStream.scrollTop = initialStream.scrollHeight;
  });
  syncControls();
  startClock();

  lifecycle.add(store.subscribe(() => {
    const current = store.get();
    if (getSessionRevision() !== observedRevision || current.recordingState !== observedRecording || current.backendPhase !== observedPhase) {
      const phaseChanged = current.backendPhase !== observedPhase;
      observedRevision = getSessionRevision();
      observedRecording = current.recordingState;
      observedPhase = current.backendPhase;
      if (current.backendPhase === "failed" || current.backendPhase === "disconnected") recordingControl?.disconnect();
      else if (phaseChanged && current.backendPhase === "ready") void recordingControl?.reconnect(observedRecording ?? null);
      else recordingControl?.observe(observedRecording);
    }
    filePanel.hidden = current.mode !== "c";
    syncControls();
  }));

  // 释放：停表 + 让所有模块级的节点引用失效。
  // 引用必须置空：index.ts 的刷新循环（updateStatus/updateStream/updateProgress）
  // 是常驻的，不置空就会往已经脱离文档的节点上继续写。
  lifecycle.add(() => {
    stopClock();
    streamEl = null;
    statusTextEl = null;
    progressWrap = null;
    progressBar = null;
    fileLabelEl = null;
    recordHintEl = null;
    clockEl = null;
    ctaEl = null;
    pauseBtnEl = null;
    recInputEl = null;
    recDotEl = null;
  });

  return { element: pane, dispose: () => lifecycle.dispose() };
}

export function setWorkspaceFile(path: string): void {
  if (fileLabelEl) fileLabelEl.textContent = path;
}
