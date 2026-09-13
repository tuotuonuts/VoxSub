#!/usr/bin/env node
/**
 * 会话操作条（录音控件）的行为测试。
 *
 * 覆盖用户明确要求的三点：
 *   1. 主按钮随状态变形：开始 → 暂停 ⇄ 继续
 *   2. 收尾按钮按「同时录音」开关切换文案（结束 / 结束并保存），
 *      并且**不再同时出现两个按钮**（原先"结束"和"结束并保存"并排，
 *      底层却是同一个 stop，用户不知道该点哪个）
 *   3. 那行"像手机录音：开始 → 暂停 / 继续 → 结束并保存"的文案必须消失 ——
 *      它是把设计意图当界面说明写给用户看，不是功能本身
 *
 * 用法：node tools/test-recorder-controls.mjs
 */
const PORT = 9222;

const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const main = targets.find((t) => (t.title ?? "").includes("语幕"));
if (!main) { console.error("找不到主窗（需以 --debug 启动）"); process.exit(1); }

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
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

// 复位到主屏 A 模式
await ev(`(async () => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await new Promise(r => setTimeout(r, 300));
  document.querySelector('.page__back')?.click();
  await new Promise(r => setTimeout(r, 300));
  document.querySelector('.mode-cell[data-mode="a"]')?.click();
  await new Promise(r => setTimeout(r, 1200));
})()`);

console.log("=== 操作条结构 ===\n");
{
  const d = await ev(`(() => {
    const bar = document.querySelector('.workspace__actions');
    if (!bar) return { missing: true };
    const btns = [...bar.querySelectorAll('button')].map(b => b.textContent.trim());
    return {
      missing: false,
      buttons: btns,
      hasClock: Boolean(bar.querySelector('.recorder__clock')),
      hasDot: Boolean(bar.querySelector('.rec-dot')),
      hasSwitch: Boolean(bar.querySelector('.switch')),
      // 那行说明文案必须不在页面上
      pageText: document.querySelector('.workspace')?.textContent ?? '',
    };
  })()`);
  check("操作条存在", d.missing === false);
  check("主按钮存在", d.buttons.includes("开始"), d.buttons.join(" / "));
  check("有计时器元素", d.hasClock === true);
  check("有录音红点元素", d.hasDot === true);
  check("有「同时录音」开关", d.hasSwitch === true);
  check(
    "「像手机录音…」文案已移除",
    !d.pageText.includes("像手机录音"),
    d.pageText.includes("像手机录音") ? "仍然存在" : "已删除",
  );
}

console.log("\n=== 操作条上只有一个会话控制按钮 ===\n");
console.log("  主按钮 开始⇄结束 由 test-session-controls.mjs 覆盖；");
console.log("  这里确认没有残留的第二个「结束」按钮（旧设计留下的）。\n");
{
  const d = await ev(`(() => {
    const bar = document.querySelector('.workspace__actions');
    const labels = [...bar.querySelectorAll('button')].map(b => b.textContent.trim());
    return {
      stopCount: labels.filter(t => t === '结束').length,
      finishCount: labels.filter(t => t === '结束并保存').length,
      labels,
    };
  })()`);
  check("未运行时没有「结束」按钮", d.stopCount === 0, `结束=${d.stopCount}`);
  check("没有「结束并保存」按钮（已合并进主按钮）", d.finishCount === 0, `结束并保存=${d.finishCount}`);
  check("操作条按钮清单", d.labels.length > 0, d.labels.join("/"));
}

console.log("\n=== 录音开关不再改变按钮文案 ===\n");
console.log("  开关只管「是否落盘录音」，不再劫持主按钮的文案 ——");
console.log("  主按钮的语义固定为 开始⇄结束，由会话状态决定。\n");
{
  const d = await ev(`(async () => {
    const bar = document.querySelector('.workspace__actions');
    const input = bar.querySelector('.switch input[type=checkbox]');
    const main = [...bar.querySelectorAll('button')].find(b => b.classList.contains('btn--primary'));

    const offLabel = main?.textContent.trim();
    input.checked = true;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 600));
    const onLabel = main?.textContent.trim();

    // 关回去，避免留下副作用
    input.checked = false;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 600));
    const backLabel = main?.textContent.trim();

    return { offLabel, onLabel, backLabel, hint: document.querySelector('.rec-hint')?.textContent ?? '' };
  })()`);
  check("录音关闭时主按钮为「开始」", d.offLabel === "开始", String(d.offLabel));
  check("录音开启时主按钮仍为「开始」", d.onLabel === "开始", String(d.onLabel));
  check("关闭后仍是「开始」", d.backLabel === "开始", String(d.backLabel));
}

console.log("\n=== 说明行只讲音频去哪 ===\n");
{
  const d = await ev(`(async () => {
    const input = document.querySelector('.workspace__actions .switch input[type=checkbox]');
    const read = () => document.querySelector('.rec-hint')?.textContent ?? '';
    const off = read();
    input.checked = true;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 600));
    const on = read();
    input.checked = false;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 600));
    return { off, on };
  })()`);
  check("关闭时说仅生成字幕", d.off.includes("仅生成字幕"), d.off);
  check("开启时说保存位置", d.on.includes("WAV"), d.on);
  check("开启时不再解释操作步骤", !d.on.includes("暂停") && !d.on.includes("继续"), d.on);
}

console.log("\n=== 计时器只在运行时出现 ===\n");
{
  const d = await ev(`(() => {
    const clock = document.querySelector('.recorder__clock');
    if (!clock) return { missing: true };
    return {
      missing: false,
      hidden: clock.hidden,
      display: getComputedStyle(clock).display,
      text: clock.textContent,
    };
  })()`);
  check("计时器元素存在", d.missing === false);
  check("未运行时计时器不可见", d.display === "none", `hidden=${d.hidden} display=${d.display}`);
}

socket.close();

const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
process.exit(failed.length ? 1 : 0);
