#!/usr/bin/env node
/**
 * 会话控制按钮的行为测试（开始/结束 + 暂停/继续）。
 *
 * ## 覆盖的两个用户报告
 *
 *   1.「麦克风同传的暂停和继续功能还没有实现」
 *   2.「所有模式都没有结束按钮……应该当用户点击开始后开始按钮应该变为结束按钮」
 *
 * 两者的根因相同：**会话状态从未传到界面**。渲染层 store 里的 running/paused
 * 只有初始值 false，既没有事件也没查询 —— 主按钮永远显示"开始"、结束按钮
 * 永远隐藏、暂停/继续的代码分支永远走不到。
 *
 * ## 为什么不真的开始一个会话
 *
 * 用户明确要求自动化测试**不得占用音频**。真实的 start 会打开麦克风（A 模式）
 * 或系统声音回环（B 模式）。所以：
 *
 *   · 前端：用 window.__applySessionState 驱动 —— 它走的是与真实 state 事件
 *     **完全相同**的代码路径（applySessionState → store.patch → syncControls）。
 *   · 后端：由 tests/test_pipeline_state_events.py 覆盖（假音频源）。
 *
 * 这里另外验证 IPC 命令在**未启动**时也返回正确的状态形状（安全，不碰设备）。
 *
 * 用法：node tools/test-session-controls.mjs
 */
const PORT = 9222;

const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const main = targets.find((t) => (t.title ?? "").includes("语幕"));
if (!main) { console.error("找不到主窗（应用需以 --debug 启动）"); process.exit(1); }

const socket = new WebSocket(main.webSocketDebuggerUrl);
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
const ev = async (expr, timeoutMs = 30000) => {
  const res = await Promise.race([
    send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }),
    new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), timeoutMs)),
  ]);
  if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description);
  return res.result.value;
};

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

/** 读操作条上两个按钮的真实可见性与文案。 */
const readButtons = () => ev(`(() => {
  const bar = document.querySelector('.workspace__actions');
  if (!bar) return JSON.stringify({ missing: true });
  const btns = [...bar.querySelectorAll('button')];
  const main = btns.find(b => b.classList.contains('btn--primary'));
  // 暂停按钮：主按钮之后第一个 ghost 按钮
  const pause = btns.find(b => b.classList.contains('btn--ghost') && b !== main);
  const info = (el) => el ? {
    text: el.textContent.trim(),
    hidden: el.hidden,
    display: getComputedStyle(el).display,
    visible: el.getBoundingClientRect().height > 0,
    disabled: el.disabled,
    paused: el.classList.contains('is-paused'),
  } : null;
  return JSON.stringify({ main: info(main), pause: info(pause) });
})()`);

/** 复位到主屏 + A 模式 + 未运行。 */
const reset = () => ev(`(async () => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await new Promise(r => setTimeout(r, 300));
  document.querySelector('.page__back')?.click();
  await new Promise(r => setTimeout(r, 300));
  window.__applySessionState?.({ running: false, paused: false });
  await new Promise(r => setTimeout(r, 200));
  document.querySelector('.mode-cell[data-mode="a"]')?.click();
  await new Promise(r => setTimeout(r, 900));
  window.__applySessionState?.({ running: false, paused: false });
  await new Promise(r => setTimeout(r, 300));
})()`);

/* ------------------------------------------------------------ 前置 */

const hasHook = await ev(`typeof window.__applySessionState === 'function'`);
if (!hasHook) {
  console.error("找不到测试钩子 window.__applySessionState");
  process.exit(1);
}

await reset();

console.log("=== 未运行时 ===\n");
{
  const d = JSON.parse(await readButtons());
  check("主按钮存在", Boolean(d.main), JSON.stringify(d.main));
  check("主按钮显示「开始」", d.main?.text === "开始", String(d.main?.text));
  check("暂停按钮存在但隐藏", d.pause !== null && d.pause.visible === false,
        `visible=${d.pause?.visible} display=${d.pause?.display}`);
}

console.log("\n=== 运行中（A 模式）：主按钮变结束、暂停按钮出现 ===\n");
{
  await ev(`(async () => {
    window.__applySessionState({ running: true, paused: false });
    await new Promise(r => setTimeout(r, 400));
  })()`);
  const d = JSON.parse(await readButtons());
  check("主按钮变为「结束」", d.main?.text === "结束", String(d.main?.text));
  check("主按钮可见且可用", d.main?.visible === true && d.main?.disabled === false);
  check("暂停按钮出现", d.pause?.visible === true, `visible=${d.pause?.visible}`);
  check("暂停按钮显示「暂停」", d.pause?.text === "暂停", String(d.pause?.text));
  check("暂停按钮未标记暂停态", d.pause?.paused === false);
}

console.log("\n=== 暂停后：暂停按钮变继续 ===\n");
{
  await ev(`(async () => {
    window.__applySessionState({ running: true, paused: true });
    await new Promise(r => setTimeout(r, 400));
  })()`);
  const d = JSON.parse(await readButtons());
  check("主按钮仍是「结束」", d.main?.text === "结束", String(d.main?.text));
  check("暂停按钮变为「继续」", d.pause?.text === "继续", String(d.pause?.text));
  check("暂停按钮带 is-paused 标记", d.pause?.paused === true);
}

console.log("\n=== 继续后：回到「暂停」 ===\n");
{
  await ev(`(async () => {
    window.__applySessionState({ running: true, paused: false });
    await new Promise(r => setTimeout(r, 400));
  })()`);
  const d = JSON.parse(await readButtons());
  check("暂停按钮回到「暂停」", d.pause?.text === "暂停", String(d.pause?.text));
  check("is-paused 标记已清除", d.pause?.paused === false);
}

console.log("\n=== 结束后：回到「开始」、暂停按钮消失 ===\n");
{
  await ev(`(async () => {
    window.__applySessionState({ running: false, paused: false });
    await new Promise(r => setTimeout(r, 400));
  })()`);
  const d = JSON.parse(await readButtons());
  check("主按钮回到「开始」", d.main?.text === "开始", String(d.main?.text));
  check("暂停按钮再次隐藏", d.pause?.visible === false, `visible=${d.pause?.visible}`);
}

console.log("\n=== C 模式（离线文件）不显示暂停按钮 ===\n");
console.log("  后端 pipeline.pause() 对 C 模式直接返回，留个点了没反应的按钮更糟。\n");
{
  await ev(`(async () => {
    document.querySelector('.mode-cell[data-mode="c"]')?.click();
    await new Promise(r => setTimeout(r, 900));
    window.__applySessionState({ running: true, paused: false });
    await new Promise(r => setTimeout(r, 400));
  })()`);
  const d = JSON.parse(await readButtons());
  check("C 模式运行中主按钮为「结束」", d.main?.text === "结束", String(d.main?.text));
  check("C 模式不显示暂停按钮", d.pause?.visible === false, `visible=${d.pause?.visible}`);

  // 切回 A 模式，暂停按钮应回来
  await ev(`(async () => {
    window.__applySessionState({ running: false, paused: false });
    await new Promise(r => setTimeout(r, 200));
    document.querySelector('.mode-cell[data-mode="a"]')?.click();
    await new Promise(r => setTimeout(r, 900));
    window.__applySessionState({ running: true, paused: false });
    await new Promise(r => setTimeout(r, 400));
  })()`);
  const back = JSON.parse(await readButtons());
  check("切回 A 模式后暂停按钮恢复", back.pause?.visible === true, `visible=${back.pause?.visible}`);
}

console.log("\n=== 运行中禁止切换模式 ===\n");
console.log("  后端 set_mode 只在非运行时生效，运行中切换会被静默忽略 ——");
console.log("  不拦的话界面与后端会各说各话。\n");
{
  const d = await ev(`(async () => {
    // 前置：处于 A 模式且运行中
    const before = document.querySelector('.mode-cell.is-active')?.dataset.mode;
    document.querySelector('.mode-cell[data-mode="c"]')?.click();
    await new Promise(r => setTimeout(r, 700));
    const after = document.querySelector('.mode-cell.is-active')?.dataset.mode;
    return JSON.stringify({ before, after });
  })()`);
  const r = JSON.parse(d);
  check("运行中点击其它模式不生效", r.before === r.after, `${r.before} → ${r.after}`);
}

console.log("\n=== IPC：state 命令在未启动时也返回正确形状 ===\n");
console.log("  只读查询，不碰音频设备。\n");
{
  await ev(`(async () => {
    window.__applySessionState({ running: false, paused: false });
    await new Promise(r => setTimeout(r, 300));
  })()`);
  const d = await ev(`(async () => {
    const r = await window.voxsub.backend.command('state', null);
    return JSON.stringify({ ok: r.ok, data: r.data });
  })()`, 15000);
  const r = JSON.parse(d);
  check("state 命令可用", r.ok === true, String(r.ok));
  check("返回 running 字段", typeof r.data?.running === "boolean", String(r.data?.running));
  check("返回 paused 字段", typeof r.data?.paused === "boolean", String(r.data?.paused));
  check("未启动时 running=false", r.data?.running === false, String(r.data?.running));
}

/* ------------------------------------------------------------ 复原 */

await reset();
console.log("\n  已复位到主屏 / A 模式 / 未运行");

socket.close();

const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
if (failed.length) {
  console.log("\n失败项：");
  for (const f of failed) console.log(`  ${f.name}`);
}
process.exit(failed.length ? 1 : 0);
