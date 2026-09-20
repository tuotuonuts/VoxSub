#!/usr/bin/env node
/**
 * 页面销毁统一与残留量测量（缺陷 #10 / 工作单 §3.7 的 B 部分）。
 *
 * ## 这个测试回答两个问题
 *
 *   1. **所有页面是否都返回 `{ element, dispose }`，且换页时真的被 dispose？**
 *      —— 六个视图逐个构建/销毁，断言句柄形状、dispose 幂等、订阅成对释放；
 *   2. **切页 N 次之后还残留多少监听与定时器？改造前 vs 改造后各是多少？**
 *      —— 在同一套真实视图代码上跑两遍：一遍换个页就 dispose，一遍**只**
 *      replaceChildren（这正是改造前渲染层的全部动作）。两个数字都打印出来。
 *
 * ## 为什么"改造前"是用同一份代码跑出来的
 *
 * 改造前的源码已经不存在（就是这次改动）。但改造前的行为只有一句话：
 * **换页时只 replaceChildren，从不释放**（工作单原文：渲染层原先没有任何
 * dispose / removeEventListener / AbortController）。所以把同一批构建函数
 * 在"不调 dispose"的模式下跑一遍，得到的就是改造前的残留量；
 * 两边的差异完全来自 dispose，而不是来自两段不同的代码。
 *
 * 驱动的是**真实视图构建函数**（settings/diagnostics/catalog/workspace/ocr/
 * migration wizard），只有 window/document/计时器被换成可计数的假实现
 * （tools/mini-dom.mjs）。数字是量出来的，不是估的。
 *
 * 不启动 Electron、不弹窗、不抢焦点、不播音频、不占用音频设备。
 *
 * 用法：node tools/test-page-dispose.mjs
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { installMiniDom } from "./mini-dom.mjs";
import { importShared, createReporter, ROOT } from "./esbuild-ts.mjs";

const dom = installMiniDom();
const { document, window } = dom;
const { check, finish } = createReporter("页面销毁统一与残留量（§3.7 / 缺陷 #10）");

const DETECT_FIXTURE = {
  legacy: {
    found: true,
    install_location: "C:/OldVoxSub",
    version: "0.2.0",
    display_name: "语幕 旧版",
    uninstall_string: "",
    registry_key: "",
    source: "registry",
    notes: [],
  },
  storage: [],
  overallRisk: "conditional",
};

const VIEWS = [
  ["设置页", "src/renderer/views/settings.ts", (m) => m.buildSettings()],
  ["诊断页", "src/renderer/views/diagnostics.ts", (m) => m.buildDiagnostics()],
  ["模型目录", "src/renderer/views/catalog.ts", (m) => m.buildModelCatalog()],
  ["字幕工作区", "src/renderer/views/workspace.ts", (m) => m.buildWorkspace()],
  ["OCR 工作区", "src/renderer/views/ocr.ts", (m) => m.buildOcrWorkspace()],
  ["迁移向导", "src/renderer/views/migration.ts", (m) => m.buildMigrationWizard(DETECT_FIXTURE)],
];

const loaded = [];
for (const [name, path, build] of VIEWS) {
  const mod = await importShared(path, { bundle: true });
  loaded.push({ name, build: () => build(mod) });
}

/** 把在途 promise 放完：未启动后端时命令会立刻以 unavailable 返回。 */
const settle = () => dom.flushAsync(6);

const liveNow = () => dom.residualListeners().live;
const timersLive = () => dom.timers.created - dom.timers.cleared;

function newLayer() {
  const layer = document.createElement("div");
  layer.className = "page-layer";
  document.body.append(layer);
  return layer;
}

/* ============================================================ 1. 句柄形状 */

console.log("=== 每个视图都返回 { element, dispose } ===\n");
{
  for (const view of loaded) {
    const handle = view.build();
    const shaped =
      Boolean(handle) &&
      typeof handle.dispose === "function" &&
      handle.element !== null &&
      typeof handle.element === "object" &&
      "nodeName" in handle.element;
    check(`${view.name}：构建返回 PageHandle`, shaped, Object.keys(handle ?? {}).join(","));
    check(`${view.name}：dispose 幂等（重复调用不抛错）`, (() => {
      handle.dispose();
      handle.dispose();
      return true;
    })());
    handle.element.remove();
  }
  await settle();

  const indexSrc = readFileSync(join(ROOT, "src/renderer/index.ts"), "utf-8");
  check("路由不再用 staticPage 兜底（目录页也有了真正的释放动作）", !indexSrc.includes("staticPage("));
  check(
    "路由的 builders 覆盖三个二级页面且都取 handle",
    /settings: buildSettings[\s\S]*diagnostics: buildDiagnostics[\s\S]*catalog: buildModelCatalog/.test(indexSrc),
  );
  check(
    "换页与关页都先 dispose 上一页",
    (indexSrc.match(/disposePageContent\(\);/g) ?? []).length >= 3,
    `${(indexSrc.match(/disposePageContent\(\);/g) ?? []).length} 处`,
  );
}

/* ============================================================ 2. 订阅成对释放 */

console.log("\n=== 订阅成对释放 ===\n");
{
  const winAddsBase = dom.counters.window.adds.length;
  const winRemovesBase = dom.counters.window.removes.length;

  const settings = loaded[0];
  const handle = settings.build();
  handle.dispose();

  const added = dom.counters.window.adds.slice(winAddsBase).filter((c) => c.type === "voxsub:settings");
  const removed = dom.counters.window.removes.slice(winRemovesBase).filter((c) => c.type === "voxsub:settings");
  check("设置页订阅 voxsub:settings 一次", added.length === 1, `${added.length} 次`);
  check("dispose 时移除一次", removed.length === 1, `${removed.length} 次`);
  check(
    "移除用的是同一个 handler 引用与 options（否则监听器移不掉）",
    Boolean(added[0]) && added[0].handler === removed[0]?.handler && added[0].options === removed[0]?.options,
  );

  const stateBase = dom.counters.window.adds.length;
  const stateRemoveBase = dom.counters.window.removes.length;
  const ws = loaded[3];
  const wsHandle = ws.build();
  wsHandle.dispose();
  const stateAdded = dom.counters.window.adds.slice(stateBase).filter((c) => c.type === "voxsub:state");
  const stateRemoved = dom.counters.window.removes.slice(stateRemoveBase).filter((c) => c.type === "voxsub:state");
  check("工作区订阅 voxsub:state 一次", stateAdded.length === 1, `${stateAdded.length} 次`);
  check("切模式释放时成对移除", stateRemoved.length === 1, `${stateRemoved.length} 次`);
  check("工作区还清掉了自己的会话计时器", timersLive() === 0, `残留 ${timersLive()} 个`);
}

/* ============================================================ 3. 异步回调失效 */

console.log("\n=== 失效页面不再被写 ===\n");
{
  // 设置页：dispose 之后，页面上挂着的刷新入口（window 事件）必须停下来
  const settings = loaded[0];
  const layer = newLayer();
  const handle = settings.build();
  layer.append(handle.element);
  await settle();

  const panes = handle.element.querySelector(".settings__panes");
  const marker = document.createElement("span");
  marker.className = "marker";
  marker.textContent = "用户此刻正在输入的内容";
  panes.append(marker);

  handle.dispose();
  window.dispatchEvent(dom.makeEvent("voxsub:settings"));
  await settle();
  check(
    "dispose 之后收到 voxsub:settings 不再重建页面（用户正在输入的内容不被清掉）",
    panes.querySelector(".marker") === marker,
    panes.querySelector(".marker") ? "内容仍在" : "已被重建覆盖",
  );

  // 对照：没 dispose 时同一个事件会触发重建 —— 证明上面那条不是恒真
  const layer2 = newLayer();
  const handle2 = settings.build();
  layer2.append(handle2.element);
  await settle();
  const panes2 = handle2.element.querySelector(".settings__panes");
  const marker2 = document.createElement("span");
  marker2.className = "marker";
  panes2.append(marker2);
  window.dispatchEvent(dom.makeEvent("voxsub:settings"));
  await settle();
  check(
    "对照：没有 dispose 时同一个事件会重建页面（说明上一条断言有效）",
    panes2.querySelector(".marker") === null,
    panes2.querySelector(".marker") ? "仍在（事件未生效）" : "已被重建覆盖",
  );
  handle2.dispose();
}

{
  // OCR：选图对话框在途时关页 → 结果回来不得写进任何页面（包括之后新开的页面）
  let resolvePick = null;
  let pickCalls = 0;
  window.voxsub = {
    dialog: {
      pickImage: () => {
        pickCalls += 1;
        return new Promise((resolve) => { resolvePick = resolve; });
      },
      saveImage: async () => null,
      revealInFolder: async () => undefined,
      openPath: async () => undefined,
    },
    ocr: { selectArea: async () => null, startLiveRegion: async () => null, stopLiveRegion: async () => undefined },
    app: { setBusy: () => undefined, onOpenPage: () => undefined, onBlockingTask: () => undefined },
    backend: { command: async () => ({ ok: false, error: "测试里不允许真的发命令" }) },
  };

  const ocr = loaded[4];
  const layer = newLayer();
  const handle = ocr.build();
  layer.append(handle.element);
  await settle();

  const upload = [...handle.element.querySelectorAll("button")].find((b) => b.textContent.includes("上传图片"));
  check("找到「上传图片并翻译」按钮", Boolean(upload), upload?.textContent);
  upload?.click();
  await settle();
  check("选图对话框已唤起", pickCalls === 1, `${pickCalls} 次`);

  // 关页（换模式会走同一条 dispose），然后**新开一页**，最后对话框才返回
  handle.dispose();
  const layer2 = newLayer();
  const handle2 = ocr.build();
  layer2.append(handle2.element);
  await settle();
  const status2 = handle2.element.querySelector(".ocr__status")?.textContent;

  resolvePick("C:/shots/x.png");
  await settle();
  check(
    "关页后对话框才返回：陈旧结果不会写进新开的 OCR 页面（状态行仍未变）",
    handle2.element.querySelector(".ocr__status")?.textContent === status2,
    `${status2} → ${handle2.element.querySelector(".ocr__status")?.textContent}`,
  );

  // 同一页面内切子页：在途结果同样不得写进新的子页
  const upload2 = [...handle2.element.querySelectorAll("button")].find((b) => b.textContent.includes("上传图片"));
  upload2?.click();
  await settle();
  check("切子页前已唤起第二次选图", pickCalls === 2, `${pickCalls} 次`);

  const liveTab = [...handle2.element.querySelectorAll(".filter-chip")].find((b) => b.textContent === "实时区域");
  check("找到 OCR 子页「实时区域」", Boolean(liveTab), liveTab?.textContent);
  liveTab?.click();
  await settle();
  const statusAfterSwitch = handle2.element.querySelector(".ocr__status")?.textContent;

  resolvePick("C:/shots/y.png");
  await settle();
  check(
    "切到「实时区域」之后，旧子页的选图结果不再写进新子页",
    handle2.element.querySelector(".ocr__status")?.textContent === statusAfterSwitch,
    `${statusAfterSwitch} → ${handle2.element.querySelector(".ocr__status")?.textContent}`,
  );
  handle2.dispose();
  delete window.voxsub;
}

/* ============================================================ 4. 残留量测量 */

console.log("\n=== 切页残留：改造前 vs 改造后 ===\n");

const ROUNDS = 10;

/** 每轮按顺序打开全部六个视图（等价于用户来回切页）。 */
async function switchAll(rounds, disposeEach) {
  const layer = newLayer();
  for (let i = 0; i < rounds; i += 1) {
    for (const view of loaded) {
      const handle = view.build();
      layer.replaceChildren(handle.element);
      if (disposeEach) handle.dispose();
      await settle();
    }
  }
  layer.remove();
  await settle();
}

// 先量"改造后"（环境干净，可以用绝对值断言）
{
  const before = liveNow();
  const timersBefore = { created: dom.timers.created, cleared: dom.timers.cleared };

  await switchAll(ROUNDS, true);

  const after = liveNow();
  const created = dom.timers.created - timersBefore.created;
  const cleared = dom.timers.cleared - timersBefore.cleared;

  console.log(`改造后（每轮换页都 dispose）：`);
  console.log(`  window 活跃监听 ${after.window} 个（基线 ${before.window}）`);
  console.log(`  document 活跃监听 ${after.document} 个（基线 ${before.document}）`);
  console.log(`  定时器：创建 ${created} 个 / 清除 ${cleared} 个 → 残留 ${created - cleared} 个\n`);

  check(
    `改造后：切页 ${ROUNDS} 轮（共 ${ROUNDS * loaded.length} 次）后 window 上无残留监听`,
    after.window === before.window,
    `${before.window} → ${after.window}`,
  );
  check("改造后：document 上无残留监听", after.document === before.document, `${before.document} → ${after.document}`);
  check("改造后：会话计时器（1 秒）全部被清掉", created - cleared === 0, `残留 ${created - cleared} 个`);
  check(`改造后：每轮都真的建过计时器（说明上面那条不是「没建所以不残留」）`, created >= ROUNDS, `创建 ${created} 个`);
}

// 再量"改造前"（同一个构建函数，只是不 dispose）
{
  const before = liveNow();
  const timersBefore = { created: dom.timers.created, cleared: dom.timers.cleared };

  await switchAll(ROUNDS, false);

  const after = liveNow();
  const created = dom.timers.created - timersBefore.created;
  const cleared = dom.timers.cleared - timersBefore.cleared;

  console.log(`改造前（每轮只 replaceChildren，从不 dispose）：`);
  console.log(`  window 活跃监听 ${before.window} → ${after.window}（多出 ${after.window - before.window} 个）`);
  console.log(`  document 活跃监听 ${before.document} → ${after.document}`);
  console.log(`  定时器：创建 ${created} 个 / 清除 ${cleared} 个 → 仍存活 ${created - cleared} 个`);
  console.log(`  结论：每轮泄漏 ${(after.window - before.window) / ROUNDS} 个 window 监听；`);
  console.log(`       计时器不再随轮次增长（新的 startClock 会顺手停掉上一个），但切页结束后始终有 1 个在跑，\n       且它的回调指向已被换走的时钟节点\n`);

  check(
    `改造前：切页 ${ROUNDS} 轮后 window 监听只增不减（残留 ${after.window - before.window} 个）`,
    after.window - before.window === ROUNDS * 2,
    `多出 ${after.window - before.window} 个`,
  );
  check(
    `改造前：切页结束后仍有 ${created - cleared} 个 1 秒计时器在跑（新做法为 0）`,
    created - cleared === 1,
    `创建 ${created} / 清除 ${cleared} → 残留 ${created - cleared}`,
  );

  // 逐视图泄漏量（同一件事的明细，便于定位"谁在漏"）
  console.log("逐视图泄漏明细（每次构建不释放时多出来的 window 监听）：");
  for (const view of loaded) {
    const base = liveNow().window;
    const handle = view.build();
    void handle;
    const added = liveNow().window - base;
    console.log(`  ${view.name}：${added} 个`);
  }
}

{
  const before = liveNow();
  const handle = loaded[3].build();
  handle.dispose();
  check(
    "复测：只剩上面那一轮故意不释放的账；正常释放的视图不增加残留",
    liveNow().window === before.window,
    `${before.window} → ${liveNow().window}`,
  );
}

dom.restore();
finish();
