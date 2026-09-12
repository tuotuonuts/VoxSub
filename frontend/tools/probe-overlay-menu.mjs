#!/usr/bin/env node
/**
 * 实测浮窗的下拉菜单为什么关不掉。
 *
 * 浮窗是独立的 BrowserWindow（自己的 CDP target），所以要单独连。
 * 关键怀疑：菜单 DOM 超出了浮窗窗口的可视区域 —— 超出部分既看不见也
 * 点不到，用户点"外面"其实是点在别的应用上，浮窗根本收不到事件。
 *
 * 用法：node tools/probe-overlay-menu.mjs
 */
const PORT = 9222;

const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const overlay = targets.find((t) => (t.title ?? "").includes("浮窗"));
if (!overlay) {
  console.error("找不到浮窗 target");
  process.exit(1);
}

const socket = new WebSocket(overlay.webSocketDebuggerUrl);
let id = 1;
const pending = new Map();
socket.addEventListener("message", (e) => {
  const p = JSON.parse(e.data);
  const s = pending.get(p.id);
  if (s) { pending.delete(p.id); s(p.result); }
});
await new Promise((r) => socket.addEventListener("open", r));
const send = (m, pa) =>
  new Promise((r) => { const i = id++; pending.set(i, r); socket.send(JSON.stringify({ id: i, method: m, params: pa })); });

const ev = async (expr) => {
  const res = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description);
  return res.result.value;
};

console.log("=== 浮窗初始状态 ===");
const initial = await ev(`JSON.stringify({
  vw: innerWidth, vh: innerHeight,
  dpr: devicePixelRatio,
  menuHidden: document.getElementById('display-menu')?.hidden,
  btnRect: (() => { const r = document.getElementById('display-btn')?.getBoundingClientRect(); return r ? {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)} : null; })(),
  controlsHidden: document.getElementById('controls')?.hidden,
})`);
console.log(initial);

console.log("\n=== 点开菜单 ===");
const opened = await ev(`(async () => {
  document.getElementById('display-btn').click();
  await new Promise(r => setTimeout(r, 400));
  const menu = document.getElementById('display-menu');
  const mr = menu.getBoundingClientRect();
  return JSON.stringify({
    menuHidden: menu.hidden,
    menuRect: { x: Math.round(mr.x), y: Math.round(mr.y), w: Math.round(mr.width), h: Math.round(mr.height) },
    viewportH: innerHeight,
    // 菜单底部是否超出可视区 —— 超出的部分点不到
    overflowsBottom: Math.round(mr.bottom - innerHeight),
    overflowsRight: Math.round(mr.right - innerWidth),
    menuStyle: (() => { const cs = getComputedStyle(menu); return { position: cs.position, bottom: cs.bottom, top: cs.top, zIndex: cs.zIndex, maxHeight: cs.maxHeight }; })(),
  });
})()`);
console.log(opened);

console.log("\n=== 尝试点空白处关闭 ===");
const afterOutside = await ev(`(async () => {
  const menu = document.getElementById('display-menu');
  // 在浮窗可视区内找一个不在菜单/按钮上的点
  const target = document.elementFromPoint(innerWidth / 2, innerHeight - 8);
  const desc = target ? (target.id || target.className || target.tagName) : 'null';
  target?.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  await new Promise(r => setTimeout(r, 300));
  return JSON.stringify({ clickedEl: String(desc), menuHiddenAfter: menu.hidden });
})()`);
console.log(afterOutside);

console.log("\n=== 再点一次按钮（应关闭）===");
const afterToggle = await ev(`(async () => {
  const menu = document.getElementById('display-menu');
  document.getElementById('display-btn').click();
  await new Promise(r => setTimeout(r, 300));
  const mid = menu.hidden;
  document.getElementById('display-btn').click();
  await new Promise(r => setTimeout(r, 300));
  return JSON.stringify({ afterOneClick: mid, afterTwoClicks: menu.hidden });
})()`);
console.log(afterToggle);

console.log("\n=== 选中一个模式后是否关闭 ===");
const afterSelect = await ev(`(async () => {
  document.getElementById('display-btn').click();
  await new Promise(r => setTimeout(r, 300));
  const before = document.getElementById('display-menu').hidden;
  document.querySelector('#display-menu .menu__item[data-mode="translation"]')?.click();
  await new Promise(r => setTimeout(r, 300));
  return JSON.stringify({ beforeOpen: before, afterSelectHidden: document.getElementById('display-menu').hidden });
})()`);
console.log(afterSelect);

socket.close();
