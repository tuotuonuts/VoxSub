/**
 * 字幕浮窗渲染层。
 *
 * 四项硬能力（均由主进程配合）：
 *   1. 半透明        —— CSS 变量 --overlay-opacity（文字保持全不透明）
 *   2. 点击穿透      —— IPC 调 setIgnoreMouseEvents(forward:true)
 *   3. 置顶无边框    —— 主进程 alwaysOnTop: screen-saver
 *   4. 捕获排除      —— 主进程 setContentProtection(true)
 *
 * 功能面与原 Qt 版 subtitle_overlay.py 对齐：
 *   字号调整 / 显示模式（原文·译文·双语）/ 边距与行距 / 历史回溯 /
 *   拖动 / 边缘缩放 / 锁定穿透 / 悬停工具条
 */
import { palette, type ThemeName } from "./palette";

type DisplayMode = "source" | "translation" | "bilingual";

interface HistoryItem {
  src: string;
  dst: string;
}

/* -------------------------------------------------------------- 状态 */

let locked = false;
let opacity = 0.92;
let fontSize = 20;
let contentPadding = 18;
let lineGap = 6;
let displayMode: DisplayMode = "bilingual";

/** 已定稿句子的回溯栈；index 指向当前查看的位置。 */
const history: HistoryItem[] = [];
let historyIndex = -1;
/** 当前草稿（未定稿）内容，滚动历史时暂存，回到最新时恢复。 */
let draft: HistoryItem = { src: "", dst: "" };

const FONT_MIN = 12;
const FONT_MAX = 48;
const PADDING_MIN = 6;
const PADDING_MAX = 48;
const GAP_MIN = 0;
const GAP_MAX = 32;

/* -------------------------------------------------------------- 元素 */

const $ = (id: string) => document.getElementById(id);

const srcEl = $("src");
const dstEl = $("dst");
const historyEl = $("history");
const controlsEl = $("controls");
const lockedPanelEl = $("locked-panel");

const fontValueEl = $("font-value");
const paddingValueEl = $("padding-value");
const gapValueEl = $("gap-value");
const displayBtn = $("display-btn");
const displayMenu = $("display-menu");
const spacingControls = $("spacing-controls");
const historyLiveBtn = $("history-live");

/* -------------------------------------------------------------- 主题 */

function theme(): ThemeName {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTextColors(): void {
  const t = palette[theme()];
  if (dstEl) {
    dstEl.style.color = t.accent;
    dstEl.style.textShadow = "0 1px 3px rgba(0, 0, 0, 0.7)";
  }
}

/* -------------------------------------------------------------- 渲染 */

function applyVisuals(): void {
  const root = document.documentElement.style;
  root.setProperty("--overlay-opacity", String(opacity));
  root.setProperty("--font-size", `${fontSize}px`);
  root.setProperty("--content-padding", `${contentPadding}px`);
  root.setProperty("--line-gap", `${lineGap}px`);

  if (fontValueEl) fontValueEl.textContent = String(fontSize);
  if (paddingValueEl) paddingValueEl.textContent = String(contentPadding);
  if (gapValueEl) gapValueEl.textContent = String(lineGap);

  applyTextColors();
}

const MODE_LABEL: Record<DisplayMode, string> = {
  source: "原文",
  translation: "译文",
  bilingual: "双语",
};

function applyDisplayMode(): void {
  if (displayBtn) displayBtn.textContent = MODE_LABEL[displayMode];
  if (srcEl) srcEl.hidden = displayMode === "translation";
  if (dstEl) dstEl.hidden = displayMode === "source";
  if (historyEl) historyEl.hidden = displayMode === "source";

  displayMenu?.querySelectorAll<HTMLElement>(".menu__item").forEach((item) => {
    item.classList.toggle("is-active", item.dataset["mode"] === displayMode);
  });
  void window.voxsub?.overlay.setDisplayMode(displayMode);
}

/** 把历史项渲染成小字（在光标回到旧条目时才显示）。 */
function renderHistory(): void {
  if (!historyEl) return;

  const browsing = historyIndex >= 0 && historyIndex < history.length - 1;
  if (historyLiveBtn) historyLiveBtn.hidden = !browsing;
  historyEl.replaceChildren();

  if (!browsing) return;

  // 只显示当前查看条目之前的若干条，避免浮窗被撑爆
  const from = Math.max(0, historyIndex - 4);
  for (let i = from; i < historyIndex; i += 1) {
    const item = history[i];
    if (!item) continue;
    const row = document.createElement("p");
    row.className = "history__row";
    row.textContent = displayMode === "translation" ? item.dst : item.src;
    historyEl.append(row);
  }
}

/** 当前应显示的正文：查看历史时显示历史项，否则显示最新草稿。 */
function currentView(): HistoryItem {
  if (historyIndex >= 0 && historyIndex < history.length) {
    const item = history[historyIndex];
    if (item) return item;
  }
  return draft;
}

function paint(): void {
  const view = currentView();
  if (srcEl) srcEl.textContent = view.src || "等待识别…";
  if (dstEl) dstEl.textContent = view.dst;
  // 回溯状态加类名：CSS 据此把当前句压暗，与"正在看历史"一致
  document.body.classList.toggle("is-browsing", isBrowsing());
  renderHistory();
}

/* ---------------------------------------------------------- 历史操作 */

function pushHistory(item: HistoryItem): void {
  history.push(item);
  // 上限保护：长时间会话不应无限增长
  if (history.length > 500) history.splice(0, history.length - 500);
  historyIndex = history.length; // 回到"最新"位置
}

function isBrowsing(): boolean {
  return historyIndex >= 0 && historyIndex < history.length;
}

function stepHistory(delta: number): void {
  if (history.length === 0) return;

  const base = isBrowsing() ? historyIndex : history.length;
  const next = Math.min(history.length, Math.max(0, base + delta));
  historyIndex = next;
  paint();
}

function backToLive(): void {
  historyIndex = history.length;
  paint();
}

/* -------------------------------------------------------------- 交互 */

function setLocked(next: boolean): void {
  locked = next;
  document.body.classList.toggle("is-locked", locked);
  if (lockedPanelEl) lockedPanelEl.hidden = !locked;
  if (controlsEl) controlsEl.hidden = locked;

  const lockBtn = $("lock");
  if (lockBtn) lockBtn.textContent = locked ? "已锁定" : "锁定";

  void window.voxsub?.overlay.setClickThrough(locked);
}

function setFontSize(next: number): void {
  fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, next));
  applyVisuals();
  void window.voxsub?.overlay.setFontSize(fontSize);
}

function setPadding(next: number): void {
  contentPadding = Math.min(PADDING_MAX, Math.max(PADDING_MIN, next));
  applyVisuals();
}

function setLineGap(next: number): void {
  lineGap = Math.min(GAP_MAX, Math.max(GAP_MIN, next));
  applyVisuals();
}

function setDisplayMode(mode: DisplayMode): void {
  displayMode = mode;
  applyDisplayMode();
  paint();
  if (displayMenu) displayMenu.hidden = true;
}

/* -------------------------------------------------------------- 事件 */

function wireControls(): void {
  $("font-down")?.addEventListener("click", () => setFontSize(fontSize - 2));
  $("font-up")?.addEventListener("click", () => setFontSize(fontSize + 2));

  $("padding-down")?.addEventListener("click", () => setPadding(contentPadding - 2));
  $("padding-up")?.addEventListener("click", () => setPadding(contentPadding + 2));
  $("gap-down")?.addEventListener("click", () => setLineGap(lineGap - 2));
  $("gap-up")?.addEventListener("click", () => setLineGap(lineGap + 2));

  displayBtn?.addEventListener("click", (event) => {
    event.stopPropagation();
    if (displayMenu) displayMenu.hidden = !displayMenu.hidden;
  });
  displayMenu?.querySelectorAll<HTMLElement>(".menu__item").forEach((item) => {
    item.addEventListener("click", () => {
      const mode = item.dataset["mode"] as DisplayMode | undefined;
      if (mode) setDisplayMode(mode);
    });
  });

  $("spacing-toggle")?.addEventListener("click", (event) => {
    event.stopPropagation();
    if (spacingControls) spacingControls.hidden = !spacingControls.hidden;
  });

  $("history-prev")?.addEventListener("click", () => stepHistory(-1));
  $("history-next")?.addEventListener("click", () => stepHistory(1));
  historyLiveBtn?.addEventListener("click", backToLive);

  $("lock")?.addEventListener("click", () => setLocked(true));
  $("unlock")?.addEventListener("click", () => setLocked(false));
  $("close")?.addEventListener("click", () => {
    void window.voxsub?.overlay.toggleVisible();
  });

  // 点击空白处收起浮层
  document.addEventListener("click", (event) => {
    const target = event.target as HTMLElement;
    if (!target.closest("#display-menu") && !target.closest("#display-btn")) {
      if (displayMenu) displayMenu.hidden = true;
    }
    if (!target.closest("#spacing-controls") && !target.closest("#spacing-toggle")) {
      if (spacingControls) spacingControls.hidden = true;
    }
  });

  // 滚轮回溯历史（原 Qt 版：锁定时滚轮交给下层软件，这里只在解锁时接管）
  document.addEventListener(
    "wheel",
    (event) => {
      if (locked) return;
      const delta = event.deltaY > 0 ? 1 : -1;
      stepHistory(delta);
      event.preventDefault();
    },
    { passive: false },
  );

  // Esc 解锁（锁定时本窗口收不到键盘事件，这条只在解锁态生效）
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && locked) setLocked(false);
  });

  // 悬停显示工具条：未锁定时鼠标离开一会儿就淡出，避免遮挡画面
  document.addEventListener("mousemove", () => {
    document.body.classList.remove("is-idle");
    window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(() => {
      if (!locked) document.body.classList.add("is-idle");
    }, 2600);
  });
}

let idleTimer = 0;

/** 主进程可反向同步穿透状态（例如从主窗或托盘解锁）。 */
function wireMainProcess(): void {
  window.voxsub?.overlay.onClickThroughChanged((enabled) => {
    locked = enabled;
    document.body.classList.toggle("is-locked", locked);
    if (lockedPanelEl) lockedPanelEl.hidden = !locked;
    if (controlsEl) controlsEl.hidden = locked;
    const lockBtn = $("lock");
    if (lockBtn) lockBtn.textContent = locked ? "已锁定" : "锁定";
  });

  window.voxsub?.overlay.onOpacityChanged((value) => {
    opacity = value;
    applyVisuals();
  });

  window.voxsub?.overlay.onFontSizeChanged((value) => {
    fontSize = value;
    applyVisuals();
  });

  window.voxsub?.overlay.onDisplayModeChanged((value) => {
    displayMode = value as DisplayMode;
    applyDisplayMode();
    paint();
  });
}

/** 后端事件 → 字幕。 */
function wireBackend(): void {
  window.voxsub?.backend.onEvent((raw) => {
    const event = raw as {
      type?: string;
      source?: string;
      translation?: string;
      text?: string;
    };

    if (event.type === "utterance") {
      // 定稿：入历史栈
      pushHistory({ src: event.source ?? "", dst: event.translation ?? "" });
      draft = { src: "", dst: "" };
      paint();
      return;
    }

    if (event.type === "draft") {
      draft = { src: event.source ?? "", dst: event.translation ?? "" };
      if (!isBrowsing()) paint();
      return;
    }

    if (event.type === "partial") {
      draft = { src: event.text ?? "", dst: draft.dst };
      if (!isBrowsing()) paint();
    }
  });
}

/* -------------------------------------------------------------- 启动 */

function boot(): void {
  applyVisuals();
  applyDisplayMode();
  paint();
  wireControls();
  wireMainProcess();
  wireBackend();

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTextColors);

  // 工具条初始隐藏（进入 idle），鼠标移动后出现
  document.body.classList.add("is-idle");

  // 供自动化验证读取内部状态（只读，不提供修改入口）
  Object.defineProperty(window, "__overlayState", {
    value: () => ({
      locked,
      fontSize,
      contentPadding,
      lineGap,
      displayMode,
      historyLength: history.length,
      historyIndex,
    }),
    writable: false,
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
