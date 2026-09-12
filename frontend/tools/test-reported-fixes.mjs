#!/usr/bin/env node
/**
 * 回归测试 —— 用户人工测试报告的 11 条问题的修复验证。
 *
 * 为什么要有这个文件：这些问题**不是**靠肉眼看界面能稳定复现的
 * （其中 6 条是"后端未运行"的连带症状，后端一起来就消失）。
 * 必须用真实 DOM 断言，否则下次重构又会悄悄退回去。
 *
 * 用法：
 *   node tools/test-reported-fixes.mjs          # 需要应用已用 --debug 启动
 *   node tools/test-reported-fixes.mjs --json   # 机器可读输出
 *
 * 覆盖：
 *   #1 浮窗显示模式菜单可开可关（含失焦/Esc）
 *   #2 模型广场有模型且筛选可用
 *   #3 设置页本地/云端识别分开，本地能选模型
 *   #4 内容与窗口底部留白 ≥16px
 *   #6 存储页显示真实模型目录（不是"使用默认位置"）
 *   #7 识别调优 5 档、无"自动"、默认智能上下文、默认值已预填
 *   #8 应用声音隔离有程序选择菜单
 *   #9 关于页不出现前后端架构字样
 *   #10 更新日志有内容
 *   #11 硬件信息已渲染
 */
const PORT = 9222;
const JSON_OUT = process.argv.includes("--json");

/* ------------------------------------------------------------ CDP 连接 */

async function connect(titlePart) {
  const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const target = targets.find((t) => (t.title ?? "").includes(titlePart));
  if (!target) throw new Error(`找不到 target: ${titlePart}`);

  const socket = new WebSocket(target.webSocketDebuggerUrl);
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

  const ev = async (expr, timeoutMs = 40000) => {
    const res = await Promise.race([
      send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }),
      new Promise((_, rej) => setTimeout(() => rej(new Error("evaluate timeout")), timeoutMs)),
    ]);
    if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description ?? "eval error");
    return res.result.value;
  };
  return { ev, close: () => socket.close() };
}

/* ------------------------------------------------------------ 断言框架 */

const results = [];
function check(id, name, ok, detail) {
  results.push({ id, name, ok, detail });
  if (!JSON_OUT) console.log(`${ok ? "PASS" : "FAIL"}  [${id}] ${name}${detail ? `  — ${detail}` : ""}`);
}

/* ------------------------------------------------------------ 主流程 */

const main = await connect("语幕");
const overlay = await connect("浮窗");

try {
  if (!JSON_OUT) console.log("=== #1 浮窗显示模式菜单 ===\n");
  {
    const d = await overlay.ev(`(async () => {
      const menu = document.getElementById('display-menu');
      const btn = document.getElementById('display-btn');
      const out = {};
      // 点开
      btn.click();
      await new Promise(r => setTimeout(r, 300));
      out.opensOnFirstClick = !menu.hidden;
      // 点空白关闭
      document.elementFromPoint(innerWidth / 2, innerHeight - 8)
        ?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await new Promise(r => setTimeout(r, 250));
      out.closesOnOutsideClick = menu.hidden;
      // 失焦关闭（浮窗是小窗口，用户点回文档时唯一可靠的信号）
      btn.click();
      await new Promise(r => setTimeout(r, 250));
      out.openedAgain = !menu.hidden;
      window.dispatchEvent(new Event('blur'));
      await new Promise(r => setTimeout(r, 250));
      out.closesOnBlur = menu.hidden;
      // Esc 关闭
      btn.click();
      await new Promise(r => setTimeout(r, 250));
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await new Promise(r => setTimeout(r, 250));
      out.closesOnEscape = menu.hidden;
      // 选中后关闭
      btn.click();
      await new Promise(r => setTimeout(r, 250));
      document.querySelector('#display-menu .menu__item[data-mode="bilingual"]')?.click();
      await new Promise(r => setTimeout(r, 250));
      out.closesAfterPick = menu.hidden;
      return out;
    })()`);
    check("#1", "首次点击即打开菜单", d.opensOnFirstClick === true, JSON.stringify(d.opensOnFirstClick));
    check("#1", "点击浮窗内空白可关闭", d.closesOnOutsideClick === true);
    check("#1", "浮窗失焦时关闭", d.closesOnBlur === true);
    check("#1", "Esc 可关闭", d.closesOnEscape === true);
    check("#1", "选中模式后关闭", d.closesAfterPick === true);
  }

  // 打开设置页（后续多项都在这里查）
  await main.ev(`(async () => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await new Promise(r => setTimeout(r, 300));
    [...document.querySelectorAll('.topbar__actions button')].find(b => b.textContent === '设置')?.click();
    await new Promise(r => setTimeout(r, 4500));
  })()`);

  if (!JSON_OUT) console.log("\n=== #2 模型广场 ===\n");
  {
    const d = await main.ev(`(async () => {
      document.querySelector('.page__back')?.click();
      await new Promise(r => setTimeout(r, 400));
      [...document.querySelectorAll('.topbar__actions button')].find(b => b.textContent === '模型')?.click();
      await new Promise(r => setTimeout(r, 3500));
      const chips = [...document.querySelectorAll('.filter-chip')];
      // 先复位到「全部」：筛选状态是模块级变量，上一轮测试可能留在某个分类上，
      // 不复位就会出现"点了筛选但数量没变"的假失败。
      chips.find(c => c.textContent === '全部')?.click();
      await new Promise(r => setTimeout(r, 800));
      const before = document.querySelectorAll('.cell').length;
      chips.find(c => c.textContent === '识别')?.click();
      await new Promise(r => setTimeout(r, 800));
      const after = document.querySelectorAll('.cell').length;
      chips.find(c => c.textContent === '全部')?.click();
      await new Promise(r => setTimeout(r, 500));
      return { before, after, chips: chips.length };
    })()`);
    check("#2", "模型列表已渲染", d.before > 0, `${d.before} 个`);
    check("#2", "筛选按钮有响应", d.before !== d.after, `${d.before} → ${d.after}`);
  }

  // 回到设置页
  await main.ev(`(async () => {
    document.querySelector('.page__back')?.click();
    await new Promise(r => setTimeout(r, 400));
    [...document.querySelectorAll('.topbar__actions button')].find(b => b.textContent === '设置')?.click();
    await new Promise(r => setTimeout(r, 3000));
  })()`);

  if (!JSON_OUT) console.log("\n=== #3 本地/云端识别分开 ===\n");
  {
    const d = await main.ev(`(async () => {
      const tabs = [...document.querySelectorAll('.settings__tab')];
      tabs.find(t => t.textContent === '翻译')?.click();
      await new Promise(r => setTimeout(r, 1200));
      const pane = document.querySelector('.settings__panes');
      return {
        selects: [...pane.querySelectorAll('select')].map(s => ({ v: s.value, n: s.options.length })),
        apiInputs: [...pane.querySelectorAll('input')].filter(i => i.type === 'password').length,
        textInputs: [...pane.querySelectorAll('input')].length,
      };
    })()`);
    check("#3", "本地模式有模型下拉", d.selects.length >= 2, `${d.selects.length} 个下拉`);
    check("#3", "下拉里确实有模型", d.selects.every((s) => s.n > 0), JSON.stringify(d.selects));
    check("#3", "本地模式不显示云端 API 字段", d.apiInputs === 0, `密码框 ${d.apiInputs} 个`);
  }

  if (!JSON_OUT) console.log("\n=== #6 存储页真实路径 ===\n");
  {
    const d = await main.ev(`(async () => {
      const tabs = [...document.querySelectorAll('.settings__tab')];
      tabs.find(t => t.textContent === '存储与模型')?.click();
      await new Promise(r => setTimeout(r, 1500));
      const pane = document.querySelector('.settings__panes');
      return {
        paths: [...pane.querySelectorAll('.readonly-value')].map(v => v.textContent),
        buttons: [...pane.querySelectorAll('button')].map(b => b.textContent),
      };
    })()`);
    const realPath = d.paths.some((p) => p && p !== "使用默认位置" && p.includes(":"));
    check("#6", "模型目录显示真实路径", realPath, d.paths[0]);
    check("#6", "有「打开文件夹」按钮", d.buttons.includes("打开文件夹"));
  }

  if (!JSON_OUT) console.log("\n=== #7 识别调优 ===\n");
  {
    const d = await main.ev(`(async () => {
      const tabs = [...document.querySelectorAll('.settings__tab')];
      tabs.find(t => t.textContent === '识别调优')?.click();
      await new Promise(r => setTimeout(r, 1500));
      const pane = document.querySelector('.settings__panes');
      const p = pane.querySelector('select');
      // 只统计"应该有值"的控件：数字输入与开关。
      // 热词是自由文本，空字符串本来就是它的正常默认值，不该算未预填。
      const numeric = [...pane.querySelectorAll('input[type=number]')];
      const checks = [...pane.querySelectorAll('input[type=checkbox]')];
      return {
        value: p?.value,
        options: [...(p?.options ?? [])].map(o => o.textContent),
        numericTotal: numeric.length,
        numericFilled: numeric.filter(i => i.value !== '').length,
        checksTotal: checks.length,
        checksFilled: checks.filter(i => i.checked).length,
        hotwordsEmpty: [...pane.querySelectorAll('input[type=text]')].every(i => i.value === ''),
      };
    })()`);
    const want = ["快档", "均衡", "准确优先", "智能上下文", "自定义"];
    check("#7", "档位正好 5 个", d.options.length === 5, d.options.join("/"));
    check("#7", "档位与要求一致", want.every((w) => d.options.includes(w)) && !d.options.includes("自动"));
    check("#7", "默认为智能上下文", d.value === "context", d.value);
    check("#7", "数值项已预填", d.numericFilled === d.numericTotal && d.numericTotal > 0,
          `${d.numericFilled}/${d.numericTotal}`);
    check("#7", "开关项已预填", d.checksFilled === d.checksTotal && d.checksTotal > 0,
          `${d.checksFilled}/${d.checksTotal}`);
  }

  if (!JSON_OUT) console.log("\n=== #8 应用声音隔离 ===\n");
  {
    const d = await main.ev(`(async () => {
      const tabs = [...document.querySelectorAll('.settings__tab')];
      tabs.find(t => t.textContent === '设备')?.click();
      await new Promise(r => setTimeout(r, 3000));
      const pane = document.querySelector('.settings__panes');
      const selects = [...pane.querySelectorAll('select')];
      const capture = selects[selects.length - 1];
      return {
        selectCount: selects.length,
        captureOptions: capture ? capture.options.length : 0,
        firstOption: capture?.options[0]?.textContent,
        hasRefresh: [...pane.querySelectorAll('button')].some(b => b.textContent.includes('刷新')),
      };
    })()`);
    check("#8", "有应用选择下拉", d.selectCount >= 3, `${d.selectCount} 个下拉`);
    check("#8", "下拉里有可捕获的窗口", d.captureOptions > 1, `${d.captureOptions} 项`);
    check("#8", "有「不隔离」选项", String(d.firstOption ?? "").includes("不隔离"), String(d.firstOption));
    check("#8", "有刷新入口", d.hasRefresh);
  }

  if (!JSON_OUT) console.log("\n=== #9 / #10 关于页 ===\n");
  {
    const d = await main.ev(`(async () => {
      const tabs = [...document.querySelectorAll('.settings__tab')];
      tabs.find(t => t.textContent === '关于')?.click();
      await new Promise(r => setTimeout(r, 2000));
      const pane = document.querySelector('.settings__panes');
      const text = pane.textContent;
      return {
        labels: [...pane.querySelectorAll('.field__label')].map(l => l.textContent),
        releaseItems: pane.querySelectorAll('.release-item').length,
        releaseBody: (pane.querySelector('.release-item__body')?.textContent ?? "").slice(0, 30),
        mentionsElectron: text.includes('Electron'),
        mentionsPython: text.includes('Python'),
      };
    })()`);
    check("#9", "关于页无「前端」字段", !d.labels.includes("前端"), d.labels.join("/"));
    check("#9", "关于页无「后端」字段", !d.labels.includes("后端"));
    check("#9", "正文不出现 Electron", !d.mentionsElectron);
    check("#9", "正文不出现 Python", !d.mentionsPython);
    check("#10", "更新日志有条目", d.releaseItems > 0, `${d.releaseItems} 条`);
    check("#10", "更新日志有正文", d.releaseBody.length > 0, d.releaseBody);
  }

  if (!JSON_OUT) console.log("\n=== #11 硬件信息 ===\n");
  {
    const d = await main.ev(`(async () => {
      document.querySelector('.page__back')?.click();
      await new Promise(r => setTimeout(r, 400));
      [...document.querySelectorAll('.topbar__actions button')].find(b => b.textContent === '诊断')?.click();
      await new Promise(r => setTimeout(r, 2000));
      [...document.querySelectorAll('.settings__tab')].find(t => t.textContent === '硬件')?.click();
      await new Promise(r => setTimeout(r, 6000));
      return {
        kv: [...document.querySelectorAll('.kv')].map(k => k.textContent),
      };
    })()`);
    check("#11", "硬件画像已渲染", d.kv.length > 0, `${d.kv.length} 条`);
    const joined = d.kv.join(" ");
    check("#11", "含 CPU 信息", joined.includes("CPU"));
    check("#11", "含显卡信息", joined.includes("显卡") || joined.includes("显"));
  }

  if (!JSON_OUT) console.log("\n=== #4 底部留白 ===\n");
  {
    const d = await main.ev(`(async () => {
      document.querySelector('.page__back')?.click();
      await new Promise(r => setTimeout(r, 600));
      const shell = document.querySelector('.shell');
      const body = document.querySelector('.shell__body');
      const br = body ? body.getBoundingClientRect() : null;
      return {
        padding: getComputedStyle(shell).padding,
        bottomGap: br ? Math.round(innerHeight - br.bottom) : null,
        winH: innerHeight,
      };
    })()`);
    check("#4", "内容与窗口底部留白 ≥16px", (d.bottomGap ?? 0) >= 16, `${d.bottomGap}px`);
  }
} finally {
  main.close();
  overlay.close();
}

const failed = results.filter((r) => !r.ok);
if (JSON_OUT) {
  console.log(JSON.stringify({ total: results.length, failed: failed.length, results }, null, 1));
} else {
  console.log("\n" + "=".repeat(60));
  console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
  if (failed.length) {
    console.log("\n失败项：");
    failed.forEach((f) => console.log(`  [${f.id}] ${f.name}  — ${f.detail ?? ""}`));
  }
}
process.exit(failed.length ? 1 : 0);
