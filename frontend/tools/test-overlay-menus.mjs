#!/usr/bin/env node
/**
 * 浮窗弹出层的可见性测试。
 *
 * 为什么必须查**计算样式**而不是 hidden 属性：
 *   `hidden` 属性靠 UA 样式表的 `[hidden] { display: none }` 生效，
 *   但只要作者 CSS 里给同一个元素写了 `display: flex`，就把它盖掉了 ——
 *   此时 `el.hidden === true` 但元素**照样显示在屏幕上**。
 *
 * 之前就是这么误判的：测试读 el.hidden 报"已关闭"，用户看到的却是菜单
 * 一直挂在浮窗上关不掉。断言必须落到 getComputedStyle().display。
 *
 * 用法：node tools/test-overlay-menus.mjs
 */
const PORT = 9222;

const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const overlay = targets.find((t) => (t.title ?? "").includes("浮窗"));
if (!overlay) {
  console.error("找不到浮窗 target（应用需以 --debug 启动）");
  process.exit(1);
}

const socket = new WebSocket(overlay.webSocketDebuggerUrl);
let id = 1;
const pending = new Map();
socket.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  const r = pending.get(m.id);
  if (r) { pending.delete(m.id); r(m.result); }
});
await new Promise((r) => socket.addEventListener("open", r));
const send = (method, params) =>
  new Promise((r) => { const i = id++; pending.set(i, r); socket.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr) => {
  const res = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description);
  return res.result.value;
};

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

// 在页面里注入探针：既能读 hidden 属性，也能读真实可见性
await ev(`(() => {
  window.__probe = (id) => {
    const el = document.getElementById(id);
    if (!el) return { missing: true };
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return {
      hidden: el.hidden,
      display: cs.display,
      visibility: cs.visibility,
      height: Math.round(r.height),
      // 真正"用户看得见"的判据
      visible: cs.display !== 'none' && cs.visibility !== 'hidden' && r.height > 0,
    };
  };
  return 'ok';
})()`);

console.log("=== 初始状态：两个弹出层都应收起 ===\n");
{
  const d = await ev(`JSON.stringify({ menu: __probe('display-menu'), sub: __probe('spacing-controls') })`);
  const { menu, sub } = JSON.parse(d);
  check("显示菜单初始不可见", menu.visible === false, `display=${menu.display} height=${menu.height}`);
  check("间距面板初始不可见", sub.visible === false, `display=${sub.display} height=${sub.height}`);
}

console.log("\n=== 点开显示菜单 ===\n");
{
  const d = await ev(`(async () => {
    document.getElementById('display-btn').click();
    await new Promise(r => setTimeout(r, 300));
    return JSON.stringify(__probe('display-menu'));
  })()`);
  const m = JSON.parse(d);
  check("点击后菜单可见", m.visible === true, `display=${m.display} height=${m.height}`);
}

console.log("\n=== 再点一次按钮：应收起 ===\n");
{
  const d = await ev(`(async () => {
    document.getElementById('display-btn').click();
    await new Promise(r => setTimeout(r, 300));
    return JSON.stringify(__probe('display-menu'));
  })()`);
  const m = JSON.parse(d);
  check("再点一次后菜单不可见", m.visible === false, `hidden=${m.hidden} display=${m.display}`);
}

console.log("\n=== 选中一项：应收起 ===\n");
{
  const d = await ev(`(async () => {
    document.getElementById('display-btn').click();
    await new Promise(r => setTimeout(r, 250));
    document.querySelector('#display-menu .menu__item[data-mode="translation"]').click();
    await new Promise(r => setTimeout(r, 300));
    return JSON.stringify(__probe('display-menu'));
  })()`);
  const m = JSON.parse(d);
  check("选中后菜单不可见", m.visible === false, `hidden=${m.hidden} display=${m.display}`);
}

console.log("\n=== 浮窗失焦：应收起 ===\n");
{
  const d = await ev(`(async () => {
    document.getElementById('display-btn').click();
    await new Promise(r => setTimeout(r, 250));
    const opened = __probe('display-menu').visible;
    window.dispatchEvent(new Event('blur'));
    await new Promise(r => setTimeout(r, 300));
    return JSON.stringify({ opened, after: __probe('display-menu') });
  })()`);
  const r = JSON.parse(d);
  check("失焦前确实打开了", r.opened === true);
  check("失焦后菜单不可见", r.after.visible === false, `display=${r.after.display}`);
}

console.log("\n=== 间距面板：开→关 ===\n");
{
  const d = await ev(`(async () => {
    document.getElementById('spacing-toggle').click();
    await new Promise(r => setTimeout(r, 250));
    const opened = __probe('spacing-controls').visible;
    document.getElementById('spacing-toggle').click();
    await new Promise(r => setTimeout(r, 250));
    return JSON.stringify({ opened, after: __probe('spacing-controls') });
  })()`);
  const r = JSON.parse(d);
  check("间距面板可打开", r.opened === true);
  check("再点一次可收起", r.after.visible === false, `display=${r.after.display}`);
}

console.log("\n=== 打开菜单后点浮窗内空白：应收起 ===\n");
{
  const d = await ev(`(async () => {
    document.getElementById('display-btn').click();
    await new Promise(r => setTimeout(r, 250));
    const opened = __probe('display-menu').visible;
    const target = document.elementFromPoint(innerWidth / 2, innerHeight - 6);
    target?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 300));
    return JSON.stringify({ opened, after: __probe('display-menu') });
  })()`);
  const r = JSON.parse(d);
  check("点空白前确实打开了", r.opened === true);
  check("点空白后菜单不可见", r.after.visible === false, `display=${r.after.display}`);
}

socket.close();

const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
process.exit(failed.length ? 1 : 0);
