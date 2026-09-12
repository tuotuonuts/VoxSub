#!/usr/bin/env node
/**
 * 浮窗显示模式（原文/译文/双语）的行为测试。
 *
 * 覆盖用户报告的"切换选项后 UI 闪烁抽动"，以及修复过程中发现的特性缺失。
 *
 * ## 闪烁的根因（必须防回归）
 *
 * applyDisplayMode() 里有一句 IPC 通知主进程，而主进程把模式原样回传，
 * 回传的监听器又调用 applyDisplayMode() —— 形成 renderer→main→renderer 的
 * 无限往返。每轮都跑 paint() → renderHistory() → replaceChildren()。
 * 实测：切换一次显示模式，3 秒内 DOM 重建 181,252 次（约 6 万次/秒）且持续增长。
 *
 * 所以本测试的核心断言是**用 MutationObserver 数真实 DOM 变更**，而不是看
 * 某个属性值。之前浮窗菜单那次就是栽在"读属性判断、真实问题在别处"上：
 * 测试绿着、功能坏着。DOM 变更数是用户真正感知到的东西。
 *
 * ## 特性缺失（顺带补上）
 *
 * Qt 版把选择存在 config 的 overlay_display_mode（subtitle_overlay.py:152/536），
 * 重启后沿用；Electron 版原先既没读也没写。这里一并断言持久化与恢复。
 *
 * 用法：node tools/test-overlay-display-mode.mjs
 */
const PORT = 9222;

const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const overlay = targets.find((t) => (t.title ?? "").includes("浮窗"));
if (!overlay) { console.error("找不到浮窗（应用需以 --debug 启动）"); process.exit(1); }

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

/* ------------------------------------------------------------ 前置 */

// 记住配置里**原本的值**，测试结束复原。
// 注意不能读浮窗内存里的 displayMode：那是运行时状态，可能已被上一次测试改过，
// 用它复原会把"原本的值"覆盖成测试留下的值（实测踩到：连跑两次后配置里留下
// 了测试设的 source，而用户原本是默认的 bilingual）。
const original = await ev(`(async () => {
  const r = await window.voxsub.backend.command('get_config', null);
  return String(r?.data?.overlay_display_mode ?? 'bilingual');
})()`);
console.log(`配置里原本的显示模式：${original}\n`);

/* ------------------------------------------------------------ 1. 不能有 DOM 风暴 */

console.log("=== 切换显示模式：DOM 变更次数 ===\n");
console.log("  死循环版本实测 3 秒 18 万次；修复后应接近 0。\n");

for (const [mode, label] of [["source", "仅原文"], ["translation", "仅译文"], ["bilingual", "双语对照"]]) {
  const d = await ev(`(async () => {
    // 每次切换前重新装观察器，只统计这一次的变更
    let count = 0;
    const obs = new MutationObserver((records) => { count += records.length; });
    obs.observe(document.body, {
      childList: true, subtree: true, characterData: true, attributes: true,
    });

    document.getElementById('display-btn').click();
    await new Promise(r => setTimeout(r, 250));
    const item = document.querySelector('#display-menu .menu__item[data-mode="${mode}"]');
    if (!item) { obs.disconnect(); return JSON.stringify({ error: '找不到菜单项' }); }
    item.click();

    await new Promise(r => setTimeout(r, 1500));
    const first = count;
    await new Promise(r => setTimeout(r, 1500));
    const later = count;
    obs.disconnect();

    return JSON.stringify({
      after1_5s: first,
      after3s: later,
      stillGrowing: later > first,
      mode: window.__overlayState().displayMode,
    });
  })()`, 20000);

  const r = JSON.parse(d);
  check(
    `切到「${label}」不产生 DOM 风暴`,
    !r.error && r.after3s < 50,
    r.error ?? `${r.after3s} 次变更（阈值 50）`,
  );
  check(
    `切到「${label}」后不再持续变更`,
    !r.error && r.stillGrowing === false,
    r.error ?? `1.5s→3s：${r.after1_5s} → ${r.after3s}`,
  );
  check(`切到「${label}」模式已生效`, r.mode === mode, `实际 ${r.mode}`);
}

/* ------------------------------------------------------------ 2. 渲染正确性 */

console.log("\n=== 三种模式的显隐是否正确 ===\n");
console.log("  菜单项文案与按钮文案不同：菜单是「仅原文/仅译文/双语对照」，");
console.log("  按钮受宽度限制显示「原文/译文/双语」。\n");

for (const [mode, menuLabel, btnLabel, expect] of [
  ["source", "仅原文", "原文", { src: false, dst: true }],
  ["translation", "仅译文", "译文", { src: true, dst: false }],
  ["bilingual", "双语对照", "双语", { src: false, dst: false }],
]) {
  const d = await ev(`(async () => {
    document.getElementById('display-btn').click();
    await new Promise(r => setTimeout(r, 250));
    document.querySelector('#display-menu .menu__item[data-mode="${mode}"]')?.click();
    await new Promise(r => setTimeout(r, 500));
    const vis = (id) => {
      const el = document.getElementById(id);
      if (!el) return null;
      return getComputedStyle(el).display === 'none';
    };
    return JSON.stringify({
      srcHidden: vis('src'),
      dstHidden: vis('dst'),
      btnLabel: document.getElementById('display-btn')?.textContent,
      activeItems: [...document.querySelectorAll('#display-menu .menu__item.is-active')]
        .map(i => i.dataset.mode),
    });
  })()`, 15000);
  const r = JSON.parse(d);
  check(`「${menuLabel}」原文区显隐正确`, r.srcHidden === expect.src, `hidden=${r.srcHidden}`);
  check(`「${menuLabel}」译文区显隐正确`, r.dstHidden === expect.dst, `hidden=${r.dstHidden}`);
  check(`「${menuLabel}」按钮文案正确`, r.btnLabel === btnLabel, `实际 ${r.btnLabel}`);
  check(
    `「${menuLabel}」菜单选中态唯一`,
    r.activeItems.length === 1 && r.activeItems[0] === mode,
    JSON.stringify(r.activeItems),
  );
}

/* ------------------------------------------------------------ 3. 持久化 */

console.log("\n=== 持久化到配置（Qt 版有此行为）===\n");

{
  const d = await ev(`(async () => {
    // 切到一个明确的值
    document.getElementById('display-btn').click();
    await new Promise(r => setTimeout(r, 250));
    document.querySelector('#display-menu .menu__item[data-mode="translation"]')?.click();
    await new Promise(r => setTimeout(r, 1200));  // 等写配置的 IPC 回来

    const result = await window.voxsub.backend.command('get_config', null);
    return JSON.stringify({
      saved: result?.data?.overlay_display_mode,
      ok: result?.ok,
    });
  })()`, 15000);
  const r = JSON.parse(d);
  check("配置里写入了 overlay_display_mode", r.ok === true && r.saved === "translation", `实际 ${r.saved}`);
}

/* ------------------------------------------------------------ 4. 重启后恢复 */

console.log("\n=== 重载浮窗后恢复上次选择 ===\n");

{
  // 先把配置设成一个非默认值
  await ev(`(async () => {
    await window.voxsub.backend.command('set_config', {
      updates: { overlay_display_mode: 'source' },
    });
    return 'ok';
  })()`);

  // 重载渲染进程（等价于下次启动浮窗）
  await send("Page.enable", {});
  await send("Page.reload", { ignoreCache: true });

  // 等 boot() 跑完并完成 restoreDisplayMode
  let restored = null;
  for (let i = 0; i < 40; i += 1) {
    await new Promise((r) => setTimeout(r, 250));
    try {
      const v = await ev(`(typeof window.__overlayState === 'function')
        ? window.__overlayState().displayMode : null`);
      if (v) { restored = v; break; }
    } catch { /* 重载中，继续等 */ }
  }
  // 再等一会儿让异步的 restoreDisplayMode 落地
  await new Promise((r) => setTimeout(r, 1500));
  const finalMode = await ev(`window.__overlayState?.().displayMode ?? null`);

  check("重载后浮窗可用", restored !== null, String(restored));
  check(
    "恢复为配置里的 source（而非默认 bilingual）",
    finalMode === "source",
    `实际 ${finalMode}`,
  );

  const btn = await ev(`document.getElementById('display-btn')?.textContent`);
  check("按钮文案与恢复的模式一致", btn === "原文", String(btn));
}

/* ------------------------------------------------------------ 复原 */

console.log("\n=== 复原用户原本的选择 ===\n");
{
  await ev(`(async () => {
    await window.voxsub.backend.command('set_config', {
      updates: { overlay_display_mode: '${original}' },
    });
    return 'ok';
  })()`);
  console.log(`  已把 overlay_display_mode 复原为 ${original}`);
}

socket.close();

const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
if (failed.length) {
  console.log("\n失败项：");
  for (const f of failed) console.log(`  ${f.name}`);
}
process.exit(failed.length ? 1 : 0);
