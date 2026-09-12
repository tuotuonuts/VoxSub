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
/** 历史区上次渲染的内容签名。没变就跳过 DOM 重建（见 renderHistory）。 */
let historySignature = "";
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

/**
 * 应用显示模式的**渲染**：按钮文案、原文/译文显隐、菜单选中态。
 *
 * 这里刻意不发 IPC 通知主进程 —— 那是 setDisplayMode（用户动作）的职责。
 * 曾经把通知写在这里，形成了 renderer → main → renderer 的无限往返：
 *
 *   用户点菜单 → setDisplayMode → applyDisplayMode → IPC 通知主进程
 *     → 主进程原样回传 overlay:display-mode
 *       → onDisplayModeChanged 处理器 → applyDisplayMode → 再发 IPC → …
 *
 * 实测后果：切换一次显示模式，3 秒内 DOM 重建 181,252 次（约 6 万次/秒）
 * 且持续增长，用户看到的就是浮窗不停闪烁抽动。
 */
function applyDisplayMode(): void {
  if (displayBtn) displayBtn.textContent = MODE_LABEL[displayMode];
  if (srcEl) srcEl.hidden = displayMode === "translation";
  if (dstEl) dstEl.hidden = displayMode === "source";
  if (historyEl) historyEl.hidden = displayMode === "source";

  displayMenu?.querySelectorAll<HTMLElement>(".menu__item").forEach((item) => {
    item.classList.toggle("is-active", item.dataset["mode"] === displayMode);
  });
}

/**
 * 渲染历史区（仅在回溯时才有内容）。
 *
 * 内容签名没变就不碰 DOM。paint() 在每句草稿与定稿时都会跑，而非回溯状态下
 * 历史区永远是空的 —— 无条件 replaceChildren 等于每次字幕更新都让浮窗重排
 * 一次，是"抽动"的来源之一。
 */
function renderHistory(): void {
  if (!historyEl) return;

  const browsing = historyIndex >= 0 && historyIndex < history.length - 1;
  if (historyLiveBtn) historyLiveBtn.hidden = !browsing;

  // 只显示当前查看条目之前的若干条，避免浮窗被撑爆
  const rows: string[] = [];
  if (browsing) {
    const from = Math.max(0, historyIndex - 4);
    for (let i = from; i < historyIndex; i += 1) {
      const item = history[i];
      if (!item) continue;
      rows.push(displayMode === "translation" ? item.dst : item.src);
    }
  }

  // \u0000 作分隔符：正文里不会出现，避免 ["a","b"] 与 ["a\u0000b"] 撞签名
  const signature = rows.join("\u0000");
  if (signature === historySignature) return;
  historySignature = signature;

  historyEl.replaceChildren();
  for (const text of rows) {
    const row = document.createElement("p");
    row.className = "history__row";
    row.textContent = text;
    historyEl.append(row);
  }
}

/** 只在文字真的变了才写 DOM —— 同值赋值也会产生一次变更记录与重排。 */
function setText(el: HTMLElement | null, value: string): void {
  if (el && el.textContent !== value) el.textContent = value;
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
  setText(srcEl, view.src || "等待识别…");
  setText(dstEl, view.dst);
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

const MODES: readonly DisplayMode[] = ["source", "translation", "bilingual"];

function isDisplayMode(value: unknown): value is DisplayMode {
  return typeof value === "string" && (MODES as readonly string[]).includes(value);
}

/**
 * 从后端配置恢复显示模式。
 *
 * Qt 版把这个选择存在 config 的 overlay_display_mode 里
 * （subtitle_overlay.py:152 读、:536 写），重启后沿用；Electron 版原先既没读
 * 也没写，用户每次启动都要重新选一遍。
 */
async function restoreDisplayMode(): Promise<void> {
  try {
    const result = await window.voxsub?.backend.command("get_config", null);
    const saved = (result?.data as Record<string, unknown> | undefined)?.["overlay_display_mode"];
    if (!isDisplayMode(saved) || saved === displayMode) return;
    displayMode = saved;
    applyDisplayMode();
    paint();
  } catch {
    // 读不到就保持默认值，不影响浮窗工作
  }
}

/** 把显示模式写回配置，下次启动沿用。 */
async function persistDisplayMode(mode: DisplayMode): Promise<void> {
  try {
    await window.voxsub?.backend.command("set_config", {
      updates: { overlay_display_mode: mode },
    });
  } catch {
    // 写失败不打断用户操作：本次切换照常生效，只是下次启动回到默认
  }
}

/**
 * 用户主动切换显示模式（点菜单项）。
 *
 * 三件事各归其位：
 *   · 渲染 —— applyDisplayMode()
 *   · 持久化 —— persistDisplayMode()，写 config（与 Qt 版同一个键）
 *   · 通知主进程 —— 保持主进程侧的状态同步
 *
 * 通知为什么必须放在这里、而不是 applyDisplayMode 里：主进程收到通知后会把
 * 模式回传给浮窗，若渲染函数也发通知就会形成无限往返 —— 详见 applyDisplayMode。
 */
function setDisplayMode(mode: DisplayMode): void {
  displayMode = mode;
  applyDisplayMode();
  paint();
  if (displayMenu) displayMenu.hidden = true;
  void persistDisplayMode(mode);
  void window.voxsub?.overlay.setDisplayMode(mode);
}

/* -------------------------------------------------------------- 事件 */

/** 收起浮窗上的所有弹出层（显示模式菜单、间距面板）。 */
function closePopups(): void {
  if (displayMenu) displayMenu.hidden = true;
  if (spacingControls) spacingControls.hidden = true;
}

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

  // 浮窗失焦时收起浮层。
  //
  // 为什么必须加：浮窗是个独立的小窗口（860×140），上面那条"点空白处"
  // 只在**窗口内部**的点击才触发。用户点开菜单后往往直接把鼠标移回文档
  // 或浏览器去点，那些点击浮窗根本收不到 —— 菜单就永远挂在屏幕上，
  // 看起来像"关不掉"。窗口失焦是唯一可靠的信号。
  window.addEventListener("blur", closePopups);

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

  // Esc：先收弹出层，再解锁（锁定时本窗口收不到键盘事件，解锁这条只在解锁态生效）
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const popupOpen = Boolean(
      (displayMenu && !displayMenu.hidden) || (spacingControls && !spacingControls.hidden),
    );
    if (popupOpen) {
      closePopups();
      return;
    }
    if (locked) setLocked(false);
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
    const next = value as DisplayMode;
    // 主进程回传的模式常常就是我们刚发出去的那个（它在 IPC 处理里原样广播）。
    // 值没变就不重绘：paint() 会重建历史区 DOM，无谓的重排正是"闪烁抽动"的来源。
    // 这道去重是第二层保险 —— 第一层是 applyDisplayMode 不再发通知。
    if (next === displayMode) return;
    displayMode = next;
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

  // 恢复上次选的显示模式。异步做：读配置要一次 IPC 往返，不该卡住浮窗首帧
  // （浮窗是要长期压在别的应用上的，晚出现几百毫秒很显眼）。
  void restoreDisplayMode();

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
