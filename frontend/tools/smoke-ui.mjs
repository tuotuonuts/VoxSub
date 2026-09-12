/**
 * 界面冒烟验证 —— 通过 CDP 逐项检查，不依赖人工点击。
 *
 * 为什么单独写脚本而不在命令行塞 JS：
 *   1) shell 转义会把中文与引号搞乱（多次踩坑）
 *   2) 需要等待渲染的检查要统一超时，否则会挂住
 *   3) 结果需要汇总成"通过/失败"清单
 *
 * 用法：node tools/smoke-ui.mjs
 */
const PORT = 9222;

async function listPages() {
  const response = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  return response.json();
}

/** 在指定窗口里求值；带超时，避免悬空 Promise 挂住整个脚本。 */
async function evaluate(wsUrl, expression, timeoutMs = 8000) {
  const socket = new WebSocket(wsUrl);
  const pending = new Map();
  let nextId = 1;

  const send = (method, params) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });

  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    const slot = pending.get(payload.id);
    if (!slot) return;
    pending.delete(payload.id);
    if (payload.error) slot.reject(new Error(JSON.stringify(payload.error)));
    else slot.resolve(payload.result);
  });

  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve);
    socket.addEventListener("error", reject);
    setTimeout(() => reject(new Error("WebSocket 连接超时")), timeoutMs);
  });

  try {
    const result = await Promise.race([
      send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }),
      new Promise((_, reject) => setTimeout(() => reject(new Error("求值超时")), timeoutMs)),
    ]);
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description ?? "执行出错");
    }
    return result.result.value;
  } finally {
    socket.close();
  }
}

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
}

const pages = await listPages();
const mainPage = pages.find((p) => p.type === "page" && (p.title ?? "").includes("语幕"));
const overlayPage = pages.find((p) => p.type === "page" && (p.title ?? "").includes("浮窗"));

if (!mainPage) {
  console.error("找不到主窗。现有页面：");
  for (const p of pages) console.error(`  ${p.type}  ${p.title}`);
  process.exit(1);
}

const ev = (expr, timeout) => evaluate(mainPage.webSocketDebuggerUrl, expr, timeout);

console.log("\n=== 主窗：主屏布局 ===");
{
  const info = await ev(`(() => {
    const ws = document.querySelector('.workspace');
    const body = document.querySelector('div[class*=__body]');
    const cells = document.querySelectorAll('.cell').length;
    const modes = document.querySelectorAll('.mode-cell').length;
    const buttons = [...document.querySelectorAll('.topbar__actions button')].map(b => b.textContent);
    return {
      workspaceHeight: Math.round(ws?.getBoundingClientRect().height ?? 0),
      windowHeight: innerHeight,
      bodyHeight: Math.round(body?.getBoundingClientRect().height ?? 0),
      gridOnHome: cells,
      modes,
      buttons,
    };
  })()`);
  check("模式索引 4 项", info.modes === 4, `${info.modes}`);
  check(
    "顶栏含模型入口",
    info.buttons.includes("模型"),
    info.buttons.join(" / "),
  );
  check(
    "字幕区占满剩余高度（≥ 窗口 70%）",
    info.workspaceHeight >= info.windowHeight * 0.7,
    `${info.workspaceHeight}px / ${info.windowHeight}px`,
  );
  check("首屏不再有模型网格", info.gridOnHome === 0, `${info.gridOnHome} 个 .cell`);
}

console.log("\n=== 主窗：模型目录页 ===");
{
  const info = await ev(`(async () => {
    const btn = [...document.querySelectorAll('.topbar__actions button')].find(b => b.textContent === '模型');
    btn?.click();
    await new Promise(r => setTimeout(r, 2200));
    return {
      title: document.querySelector('.page__title')?.textContent ?? null,
      sub: document.querySelector('.catalog-page__sub')?.textContent ?? null,
      count: document.querySelector('.catalog__count')?.textContent ?? null,
      disk: document.querySelector('.catalog__disk')?.textContent ?? null,
      cells: document.querySelectorAll('.cell').length,
      installed: document.querySelectorAll('.cell.is-installed').length,
      filters: [...document.querySelectorAll('.filter-chip')].map(c => c.textContent),
    };
  })()`);
  check("目录页在返回栏里标为「模型」", info.title === "模型", String(info.title));
  check("目录页有说明文字", Boolean(info.sub), String(info.sub).slice(0, 24) + "…");
  check("模型格已渲染", info.cells === 18, `${info.cells} 格`);
  check("已安装标记生效", info.installed > 0, `${info.installed} 个`);
  check("空间提示已显示", Boolean(info.disk?.includes("GB")), String(info.disk));
  check("任务筛选 5 项", info.filters.length === 5, info.filters.join("/"));
}

console.log("\n=== 主窗：诊断页日志能力 ===");
{
  const info = await ev(`(async () => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await new Promise(r => setTimeout(r, 400));
    const btn = [...document.querySelectorAll('.topbar__actions button')].find(b => b.textContent === '诊断');
    btn?.click();
    await new Promise(r => setTimeout(r, 1200));

    // 先等自检跑完（4-6 秒），否则切页时旧页还在渲染
    await new Promise(r => setTimeout(r, 7000));
    const tabs = [...document.querySelectorAll('.settings__tab')];
    tabs[1]?.click();
    await new Promise(r => setTimeout(r, 1800));

    return {
      tabs: tabs.map(t => t.textContent),
      actions: [...document.querySelectorAll('.tuning-actions button')].map(b => b.textContent),
      state: document.querySelector('.tuning-actions__state')?.textContent ?? null,
      hasLogView: Boolean(document.querySelector('.log-view')),
    };
  })()`, 25000);
  check("诊断页 3 个分页", info.tabs.length === 3, info.tabs.join("/"));
  check("日志页有导出日志", info.actions.includes("导出日志"), info.actions.join("/"));
  check("日志页有清除本机日志", info.actions.includes("清除本机日志"), "");
  check("日志页有打开文件夹", info.actions.includes("打开文件夹"), "");
  check("实时/历史切换存在", info.actions.includes("实时") && info.actions.includes("历史文件"), "");
  check("日志视图已挂载", info.hasLogView, "");
}

console.log("\n=== 主窗：设置页 7 分页 ===");
{
  const info = await ev(`(async () => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await new Promise(r => setTimeout(r, 400));
    const btn = [...document.querySelectorAll('.topbar__actions button')].find(b => b.textContent === '设置');
    btn?.click();
    await new Promise(r => setTimeout(r, 1400));
    return {
      tabs: [...document.querySelectorAll('.settings__tab')].map(t => t.textContent),
    };
  })()`);
  check("设置页含「存储与模型」", info.tabs.includes("存储与模型"), info.tabs.join("/"));
  check("设置页 7 个分页", info.tabs.length === 7, `${info.tabs.length}`);

  // 旧版数据入口：用户跳过向导后的反悔通道
  const legacyEntry = await ev(`(async () => {
    const tabs = [...document.querySelectorAll('.settings__tab')];
    const storage = tabs.find(t => t.textContent === '存储与模型');
    storage?.click();
    await new Promise(r => setTimeout(r, 900));
    const cards = [...document.querySelectorAll('.card__title')].map(t => t.textContent);
    const buttons = [...document.querySelectorAll('.settings__panes button')].map(b => b.textContent);
    return { cards, buttons };
  })()`);
  check("设置页有「旧版数据」卡片", legacyEntry.cards.includes("旧版数据"), legacyEntry.cards.join("/"));
  check("有「检查旧版数据」入口", legacyEntry.buttons.includes("检查旧版数据"), "");
  check("有「打开迁移向导」入口", legacyEntry.buttons.includes("打开迁移向导"), "");

  // 逐页点开，确认每页都能渲染出内容
  const perTab = await ev(`(async () => {
    const tabs = [...document.querySelectorAll('.settings__tab')];
    const out = [];
    for (const tab of tabs) {
      tab.click();
      await new Promise(r => setTimeout(r, 450));
      const pane = document.querySelector('.settings__panes');
      out.push({
        tab: tab.textContent,
        cards: pane?.querySelectorAll('.card').length ?? 0,
        fields: pane?.querySelectorAll('.field').length ?? 0,
      });
    }
    return out;
  })()`, 20000);

  for (const item of perTab) {
    check(
      `分页「${item.tab}」有内容`,
      item.cards > 0 || item.fields > 0,
      `${item.cards} 卡片 / ${item.fields} 字段`,
    );
  }
}

console.log("\n=== OCR 工作区 ===");
{
  const info = await ev(`(async () => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await new Promise(r => setTimeout(r, 400));
    const d = [...document.querySelectorAll('.mode-cell')].find(m => m.dataset.mode === 'd');
    d?.click();
    await new Promise(r => setTimeout(r, 1000));

    const tabs = [...document.querySelectorAll('.filter-bar .filter-chip')].map(c => c.textContent);
    // 切到「截图翻译」
    [...document.querySelectorAll('.filter-bar .filter-chip')].find(c => c.textContent === '截图翻译')?.click();
    await new Promise(r => setTimeout(r, 500));

    return {
      title: document.querySelector('.workspace__title')?.textContent ?? null,
      tabs,
      actions: [...document.querySelectorAll('.workspace__actions button')].map(b => b.textContent),
      hasPreview: Boolean(document.querySelector('.ocr-preview')),
      previewToggles: [...document.querySelectorAll('.ocr-preview-bar .filter-chip')].map(b => b.textContent),
      exportDisabled: document.querySelector('.ocr-preview-bar button.btn')?.disabled ?? null,
      cols: [...document.querySelectorAll('.ocr__col-title')].map(t => t.textContent),
      copyButtons: [...document.querySelectorAll('.ocr__col-head button')].map(b => b.textContent),
    };
  })()`, 12000);

  check("OCR 标题", Boolean(info.title?.includes("OCR")), String(info.title));
  check("截图/实时分页", info.tabs.includes("截图翻译") && info.tabs.includes("实时区域"), info.tabs.join("/"));
  check("框选与上传入口", info.actions.includes("框选屏幕并翻译") && info.actions.includes("上传图片并翻译"), info.actions.join("/"));
  check("预览区已挂载", info.hasPreview, "");
  check("原图⇄译后切换", info.previewToggles.includes("原图") && info.previewToggles.includes("译后"), info.previewToggles.join("/"));
  check("无结果时导出禁用", info.exportDisabled === true, String(info.exportDisabled));
  check("原文/译文对照列", info.cols.includes("识别原文") && info.cols.includes("译文"), info.cols.join("/"));
  check("两列都有复制按钮", info.copyButtons.filter((t) => t === "复制").length === 2, info.copyButtons.join("/"));
}

console.log("\n=== 主屏录音与导出 ===");
{
  const info = await ev(`(async () => {
    const a = [...document.querySelectorAll('.mode-cell')].find(m => m.dataset.mode === 'a');
    a?.click();
    await new Promise(r => setTimeout(r, 800));
    return {
      hasFinishRec: [...document.querySelectorAll('.workspace__actions button')].some(b => b.textContent === '结束并保存'),
      recordHint: document.querySelector('.rec-hint')?.textContent ?? null,
      recordSwitch: Boolean(document.querySelector('.switch input')),
      exportBtn: [...document.querySelectorAll('.workspace__actions button')].map(b => b.textContent),
    };
  })()`);
  check("有「结束并保存」按钮", info.hasFinishRec, "");
  check("录音说明文案", Boolean(info.recordHint), String(info.recordHint));
  check("录音开关存在", info.recordSwitch, "");
  check("导出会话入口", info.exportBtn.includes("导出会话"), info.exportBtn.join("/"));
}

console.log("\n=== 二级页面返回栏 ===");
{
  const pages = ["模型", "设置", "诊断"];
  const results = await ev(`(async () => {
    const out = [];
    for (const name of ${JSON.stringify(pages)}) {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
      await new Promise(r => setTimeout(r, 400));
      const btn = [...document.querySelectorAll('.topbar__actions button')].find(b => b.textContent === name);
      btn?.click();
      await new Promise(r => setTimeout(r, 900));
      const back = document.querySelector('.page__back');
      out.push({
        name,
        hasBar: Boolean(document.querySelector('.page__bar')),
        hasBack: Boolean(back),
        backText: back?.textContent ?? null,
        title: document.querySelector('.page__title')?.textContent ?? null,
      });
    }
    return out;
  })()`, 20000);

  for (const item of results) {
    check(`${item.name} 页有返回栏`, item.hasBar && item.hasBack, `${item.backText} | 标题 ${item.title}`);
  }

  // 点返回按钮真的能回到主屏
  const backWorks = await ev(`(async () => {
    document.querySelector('.page__back')?.click();
    await new Promise(r => setTimeout(r, 700));
    const layer = document.querySelector('.page-layer');
    return {
      hidden: layer?.hidden ?? null,
      mainVisible: Boolean(document.querySelector('.workspace')),
    };
  })()`);
  check("点返回回到主屏", backWorks.hidden === true && backWorks.mainVisible, JSON.stringify(backWorks));

  // Esc 也能返回
  const escWorks = await ev(`(async () => {
    const btn = [...document.querySelectorAll('.topbar__actions button')].find(b => b.textContent === '设置');
    btn?.click();
    await new Promise(r => setTimeout(r, 800));
    const opened = !document.querySelector('.page-layer')?.hidden;
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await new Promise(r => setTimeout(r, 600));
    return { opened, closedByEsc: document.querySelector('.page-layer')?.hidden ?? null };
  })()`);
  check("Esc 也能返回", escWorks.opened === true && escWorks.closedByEsc === true, JSON.stringify(escWorks));
}

console.log("\n=== 浮窗 ===");
if (!overlayPage) {
  check("浮窗可见", false, "未找到浮窗");
} else {
  const info = await evaluate(
    overlayPage.webSocketDebuggerUrl,
    `(() => {
      const state = window.__overlayState ? window.__overlayState() : null;
      const shell = document.body.firstElementChild;
      const cs = shell ? getComputedStyle(shell) : null;
      return {
        size: [innerWidth, innerHeight],
        background: cs?.backgroundColor ?? null,
        state,
        controls: [...document.querySelectorAll('#controls button')].map(b => b.textContent),
      };
    })()`,
  );
  check("浮窗尺寸", info.size[0] > 0 && info.size[1] > 0, info.size.join("×"));
  check("半透明生效", String(info.background).includes("0.92"), String(info.background));
  check(
    "工具条含字号/显示/间距/历史/锁定",
    ["A−", "A+", "↑", "↓"].every((t) => info.controls.includes(t)),
    info.controls.join(" "),
  );
  check("内部状态可读", Boolean(info.state), JSON.stringify(info.state));
}

const failed = checks.filter((c) => !c.ok).length;
console.log(`\n${checks.length - failed} 通过 / ${failed} 失败 / 共 ${checks.length}`);
process.exit(failed === 0 ? 0 : 1);
