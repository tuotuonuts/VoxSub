/**
 * 响应式布局实测 —— 用 CDP 视口模拟，量出各宽度下关键区域的真实几何。
 *
 * 为什么用 Emulation.setDeviceMetricsOverride 而不是 window.resizeTo：
 *   · resizeTo 在 Electron 里不可靠（实测只生效一次，之后窗口卡住）
 *   · 而 CSS 媒体查询与弹性布局响应的是"视口尺寸"，视口模拟正是为此设计
 *   · 每次覆盖都是独立的，不会互相污染
 *
 * 用法：node tools/probe-resize.mjs   （应用需带 --remote-debugging-port=9222 运行）
 */
const PORT = 9222;
const SIZES = [
  [900, 660],    // 低于 minWidth，测极窄
  [1000, 660],   // 主窗允许的最小尺寸
  [1240, 820],   // 默认
  [1600, 900],
  [1920, 1080],
  [2560, 1440],  // 大屏 / 最大化
];

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find((t) => t.type === "page" && (t.title ?? "").includes("语幕"));
  if (!page) {
    console.error("找不到主窗。现有：");
    for (const t of list) console.error(`  ${t.type} ${t.title}`);
    process.exit(1);
  }

  const socket = new WebSocket(page.webSocketDebuggerUrl);
  const pending = new Map();
  let nextId = 1;
  socket.addEventListener("message", (e) => {
    const p = JSON.parse(e.data);
    const s = pending.get(p.id);
    if (s) {
      pending.delete(p.id);
      s(p.result);
    }
  });
  await new Promise((r) => socket.addEventListener("open", r));
  const send = (method, params) =>
    new Promise((r) => {
      const id = nextId++;
      pending.set(id, r);
      socket.send(JSON.stringify({ id, method, params }));
    });

  const evaluate = async (expression, timeoutMs = 20000) => {
    const res = await Promise.race([
      send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }),
      new Promise((_, j) => setTimeout(() => j(new Error("求值超时")), timeoutMs)),
    ]);
    if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description);
    return res.result.value;
  };

  const MEASURE = `(() => {
    const ws = document.querySelector('.workspace');
    const side = document.querySelector('.mode-index');
    const topbar = document.querySelector('.topbar__actions');
    const sheet = document.querySelector('.catalog__sheet');
    const doc = document.documentElement;

    let columns = 0;
    if (sheet) {
      const tpl = getComputedStyle(sheet).gridTemplateColumns;
      columns = tpl && tpl !== 'none' ? tpl.split(' ').filter(Boolean).length : 0;
    }

    let wrapped = false;
    if (topbar) {
      const tops = [...topbar.children].map(c => Math.round(c.getBoundingClientRect().top));
      wrapped = new Set(tops).size > 1;
    }

    // 正文实际字号：用于判断大屏上文字是否跟着放大
    const body = getComputedStyle(document.body);

    return {
      viewport: [innerWidth, innerHeight],
      sideWidth: side ? Math.round(side.getBoundingClientRect().width) : 0,
      ws: ws ? [Math.round(ws.getBoundingClientRect().width), Math.round(ws.getBoundingClientRect().height)] : [0, 0],
      columns,
      wrapped,
      overflowX: doc.scrollWidth > doc.clientWidth,
      rootFont: body.fontSize,
      lineHeight: body.lineHeight,
    };
  })()`;

  console.log("宽度    视口        左栏  字幕区(w×h)     目录列数  顶栏换行  横向溢出  根字号");
  console.log("-".repeat(86));

  const rows = [];
  for (const [width, height] of SIZES) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await new Promise((r) => setTimeout(r, 600));

    const data = await evaluate(MEASURE);
    const applied = data.viewport[0] === width;
    rows.push({ width, applied, ...data });

    console.log(
      `${String(width).padEnd(7)} ` +
      `${String(data.viewport.join("×")).padEnd(11)} ` +
      `${String(data.sideWidth).padEnd(5)} ` +
      `${String(data.ws[0] + "×" + data.ws[1]).padEnd(15)} ` +
      `${String(data.columns).padEnd(9)} ` +
      `${String(data.wrapped ? "是" : "否").padEnd(9)} ` +
      `${String(data.overflowX ? "是 <-问题" : "否").padEnd(10)} ` +
      `${data.rootFont}${applied ? "" : "  <- 未生效"}`
    );
  }

  await send("Emulation.clearDeviceMetricsOverride", {});

  // ---- 自动判读 ----
  console.log("\n判读：");
  const problems = [];

  const overflows = rows.filter((r) => r.overflowX);
  if (overflows.length) {
    problems.push(`横向溢出出现在宽度 ${overflows.map((r) => r.width).join(", ")}`);
  }

  const wsWidths = [...new Set(rows.map((r) => r.ws[0]))];
  if (wsWidths.length === 1) {
    problems.push(`字幕区宽度恒定 ${wsWidths[0]}px，未随视口变化`);
  }

  const fonts = [...new Set(rows.map((r) => r.rootFont))];
  if (fonts.length === 1) {
    problems.push(`根字号恒定 ${fonts[0]}，大屏上界面不会放大（2560 宽时文字仍为 ${fonts[0]}）`);
  }

  const sideWidths = [...new Set(rows.map((r) => r.sideWidth))];
  if (sideWidths.length === 1 && rows.length > 2) {
    problems.push(`左栏宽度恒定 ${sideWidths[0]}px，未随视口收窄/放宽`);
  }

  if (problems.length === 0) {
    console.log("  未发现布局问题");
  } else {
    for (const p of problems) console.log(`  · ${p}`);
  }

  socket.close();
  process.exit(problems.length ? 1 : 0);
}

main().catch((error) => {
  console.error("探针失败:", error.message);
  process.exit(2);
});
