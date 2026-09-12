/**
 * 字幕浮窗渲染层。
 *
 * 这里验证本项目的四项硬能力是否真的成立：
 *   1. 半透明        —— CSS 变量 --overlay-opacity 驱动（不是整窗 setOpacity，
 *                        这样文字可以保持全不透明，只有底衬半透明）
 *   2. 点击穿透      —— 通过 IPC 调 setIgnoreMouseEvents(forward:true)
 *   3. 置顶无边框    —— 主进程 BrowserWindow 配置（alwaysOnTop: screen-saver）
 *   4. 捕获排除      —— 主进程 setContentProtection(true)
 */
import { palette, type ThemeName } from "./palette";

const srcEl = document.getElementById("src");
const dstEl = document.getElementById("dst");
const lockBtn = document.getElementById("lock");
const closeBtn = document.getElementById("close");

let locked = false;
let opacity = 0.92;
let fontSize = 20;
let contentPadding = 18;
let lineGap = 6;

function theme(): ThemeName {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** 浮窗在深色档使用 accent 作为译文色，浅色档用深色文字。 */
function applyTextColors(): void {
  const t = palette[theme()];
  if (dstEl) {
    dstEl.style.color = t.accent;
    dstEl.style.textShadow = "0 1px 3px rgba(0, 0, 0, 0.7)";
  }
}

function applyVisuals(): void {
  document.documentElement.style.setProperty("--overlay-opacity", String(opacity));
  document.documentElement.style.setProperty("--font-size", `${fontSize}px`);
  document.documentElement.style.setProperty("--content-padding", `${contentPadding}px`);
  document.documentElement.style.setProperty("--line-gap", `${lineGap}px`);
  applyTextColors();
}

function setLocked(next: boolean): void {
  locked = next;
  document.body.classList.toggle("is-locked", locked);
  if (lockBtn) lockBtn.textContent = locked ? "已锁定" : "锁定";
  void window.voxsub?.overlay.setClickThrough(locked);
}

function setLine(source: string, translation: string): void {
  if (srcEl) srcEl.textContent = source;
  if (dstEl) dstEl.textContent = translation;
}

function boot(): void {
  applyVisuals();

  lockBtn?.addEventListener("click", () => setLocked(!locked));
  closeBtn?.addEventListener("click", () => {
    void window.voxsub?.overlay.toggleVisible();
  });

  // 锁定后无法点击按钮，改用"悬停即提示、点击即解锁"：
  // 由于窗口此时不接收鼠标事件，解锁通过主进程的快捷键或主窗按钮完成。
  // 这里保留提示层，说明当前状态。
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && locked) setLocked(false);
  });

  // 主进程可反向同步穿透状态（例如从主窗或托盘解锁）
  window.voxsub?.overlay.onClickThroughChanged((enabled) => {
    locked = enabled;
    document.body.classList.toggle("is-locked", locked);
    if (lockBtn) lockBtn.textContent = locked ? "已锁定" : "锁定";
  });

  window.voxsub?.overlay.onOpacityChanged((value) => {
    opacity = value;
    applyVisuals();
  });

  // 后端事件 → 字幕。只处理与字幕相关的两类，其余交给主窗。
  window.voxsub?.backend.onEvent((raw) => {
    const event = raw as {
      type?: string;
      source?: string;
      translation?: string;
      text?: string;
    };
    if (event.type === "draft" || event.type === "utterance") {
      setLine(event.source ?? "", event.translation ?? "");
    } else if (event.type === "partial" && typeof event.text === "string") {
      setLine(event.text, "");
    }
  });

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTextColors);
}

document.addEventListener("DOMContentLoaded", boot);
