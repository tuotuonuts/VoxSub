/**
 * 框选窗逻辑。
 *
 * 只做一件事：把用户拖出的矩形（屏幕绝对坐标）交回主进程。
 *
 * 为什么用 window.voxsub 而不是直接 import electron：
 * 本窗口 contextIsolation 开启，直接 require('electron') 拿不到 ipcRenderer；
 * 走 preload 暴露的收窄 API 是唯一正确路径（也避免打包时误引入 electron 包）。
 */

interface Area {
  x: number;
  y: number;
  width: number;
  height: number;
}

const mask = document.getElementById("mask");
const sel = document.getElementById("sel");

let startX = 0;
let startY = 0;
let dragging = false;

function finish(area: Area | null): void {
  window.voxsub?.selector.finish(area);
}

function show(x1: number, y1: number, x2: number, y2: number): void {
  if (!sel) return;
  const left = Math.min(x1, x2);
  const top = Math.min(y1, y2);
  sel.style.left = `${left}px`;
  sel.style.top = `${top}px`;
  sel.style.width = `${Math.abs(x2 - x1)}px`;
  sel.style.height = `${Math.abs(y2 - y1)}px`;
  sel.classList.add("is-active");
}

function commit(): void {
  if (!sel) return;
  const width = parseFloat(sel.style.width);
  const height = parseFloat(sel.style.height);
  // 太小的选区没有意义，视为误触
  if (!width || !height || width < 8 || height < 8) {
    finish(null);
    return;
  }
  finish({
    x: parseFloat(sel.style.left) + window.screenX,
    y: parseFloat(sel.style.top) + window.screenY,
    width,
    height,
  });
}

mask?.addEventListener("mousedown", (event) => {
  if (event.button !== 0) return;
  dragging = true;
  startX = event.clientX;
  startY = event.clientY;
  show(startX, startY, startX, startY);
});

mask?.addEventListener("mousemove", (event) => {
  if (!dragging) return;
  show(startX, startY, event.clientX, event.clientY);
});

mask?.addEventListener("mouseup", (event) => {
  if (event.button !== 0 || !dragging) return;
  dragging = false;
  show(startX, startY, event.clientX, event.clientY);
  commit();
});

mask?.addEventListener("contextmenu", (event) => {
  event.preventDefault();
  finish(null);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") finish(null);
});
