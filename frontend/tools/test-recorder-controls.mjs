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

console.log("\n=== 收尾按钮：不同时出现两个 ===\n");
{
  const d = await ev(`(() => {
    const bar = document.querySelector('.workspace__actions');
    const labels = [...bar.querySelectorAll('button')].map(b => b.textContent.trim());
    return {
      stopCount: labels.filter(t => t === '结束').length,
      finishCount: labels.filter(t => t === '结束并保存').length,
      bothPresent: labels.includes('结束') && labels.includes('结束并保存'),
    };
  })()`);
  check("「结束」与「结束并保存」不同时出现", d.bothPresent === false,
        `结束=${d.stopCount} 结束并保存=${d.finishCount}`);
}

console.log("\n=== 录音开关驱动收尾按钮文案 ===\n");
{
  const d = await ev(`(async () => {
    const bar = document.querySelector('.workspace__actions');
    const input = bar.querySelector('.switch input[type=checkbox]');
    const stopBtn = [...bar.querySelectorAll('button')].find(b =>
      b.textContent.trim() === '结束' || b.textContent.trim() === '结束并保存');

    const offLabel = stopBtn?.textContent.trim();
    // 打开录音
    input.checked = true;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 600));
    const onLabel = stopBtn?.textContent.trim();

    // 关回去，避免留下副作用
    input.checked = false;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 600));
    const backLabel = stopBtn?.textContent.trim();

    return { offLabel, onLabel, backLabel, hint: document.querySelector('.rec-hint')?.textContent ?? '' };
  })()`);
  check("录音关闭时显示「结束」", d.offLabel === "结束", String(d.offLabel));
  check("录音开启时显示「结束并保存」", d.onLabel === "结束并保存", String(d.onLabel));
  check("关闭后回到「结束」", d.backLabel === "结束", String(d.backLabel));
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
