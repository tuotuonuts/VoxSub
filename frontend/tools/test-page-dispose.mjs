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
  ["迁移向导", "tools/test-migration-entry.ts", (m) => m.buildMigrationWizard(DETECT_FIXTURE)],
];

const loaded = [];
for (const [name, path, build] of VIEWS) {
  const mod = await importShared(path, { bundle: true });
  loaded.push({ name, module: mod, build: () => build(mod) });
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

  const migrationSrc = readFileSync(join(ROOT, "src/renderer/views/migration.ts"), "utf-8");
  check(
    "迁移向导句柄 dispose 捕获自己的 lifecycle/host（旧句柄不得释放新向导）",
    /releaseWizard\(lifecycle, host\)/.test(migrationSrc) &&
      /expectedLifecycle && wizardLifecycle !== expectedLifecycle[\s\S]{0,160}expectedLifecycle\.dispose\(\)/.test(migrationSrc),
  );

  const migrationView = loaded[5];
  const migration = migrationView.module;
  const oldWizardHandle = migrationView.build();
  const currentWizardHandle = migrationView.build();
  const currentWizardBody = currentWizardHandle.element.querySelector(".wiz__body");
  let currentWizardLeaving = 0;
  currentWizardBody.addEventListener("voxsub:leaving", () => { currentWizardLeaving += 1; });
  oldWizardHandle.dispose();
  check("旧迁移句柄迟到 dispose 不会向新向导广播 leaving", currentWizardLeaving === 0, `${currentWizardLeaving} 次`);
  currentWizardHandle.dispose();
  check("当前迁移句柄 dispose 才广播一次 leaving", currentWizardLeaving === 1, `${currentWizardLeaving} 次`);

  // 迁移请求在途时替换向导；旧请求迟到不得导航或改写新向导。
  const busyChanges = [];
  const backendListeners = new Set();
  const commandLog = [];
  const migrationJobId = "migration-job-old-page";
  let migrationArgs = null;
  let completeBeforeUnavailableReceipt = false;
  let completeBeforeFailedReceipt = false;
  let timeoutReceipt = false;
  const planFixture = {
    targetRoot: "C:/NewVoxSub",
    steps: [{ key: "models", source: "C:/Old/models", target: "C:/New/models", bytes: 1, file_count: 1, purpose: "", mode: "copy", note: "", rebuildable: false }],
    totalBytes: 1,
    freeBytes: 1024,
  };
  const completedReport = {
    done: [{ key: "models", mode: "copy", target: "C:/New/models", verify: { ok: true } }],
    failed: [],
    elapsedMs: 1,
    ok: true,
  };
  window.voxsub = {
    app: { setBusy: (busy) => busyChanges.push(busy) },
    backend: {
      onEvent: (handler) => { backendListeners.add(handler); return () => backendListeners.delete(handler); },
      start: async () => ({ ok: true }),
      command: async (command, args) => {
        commandLog.push(command);
        if (command === migration.CMD.state) return { ok: true, data: { running: false, paused: false, mode: "a" } };
        if (command === migration.CMD.planMigration) return { ok: true, data: planFixture };
        if (command === migration.CMD.startMigration) {
          migrationArgs = args;
          if (timeoutReceipt) {
            timeoutReceipt = false;
            for (const [status, sequence] of [["queued", 1], ["running", 2]]) {
              for (const listener of [...backendListeners]) listener({
                type: "job", jobId: "migration-job-timeout", command: migration.CMD.startMigration,
                clientMigrationId: args?.clientMigrationId, status, sequence,
              });
            }
            return { ok: false, timedOut: true, error: "simulated hard timeout" };
          }
          if (completeBeforeFailedReceipt) {
            completeBeforeFailedReceipt = false;
            for (const listener of [...backendListeners]) listener({
              type: "job", jobId: migrationJobId, command: migration.CMD.startMigration,
              clientMigrationId: args?.clientMigrationId, status: "succeeded", sequence: 3,
              result: completedReport,
            });
            return { ok: false, error: "simulated failed receipt after terminal" };
          }
          if (completeBeforeUnavailableReceipt) {
            completeBeforeUnavailableReceipt = false;
            const terminal = {
              type: "job", jobId: migrationJobId, command: migration.CMD.startMigration,
              clientMigrationId: args?.clientMigrationId, status: "succeeded", sequence: 3,
              result: completedReport,
            };
            for (const listener of [...backendListeners]) listener(terminal);
            for (const listener of [...backendListeners]) listener({ type: "disconnected" });
            return { ok: false, unavailable: true, error: "simulated disconnect before receipt" };
          }
          return { ok: true, data: { accepted: true, jobId: migrationJobId, async: args?.async } };
        }
        return { ok: false, error: `unexpected test command: ${command}` };
      },
    },
  };
  const disconnectBackend = migration.connectBackend();
  check("store 已订阅测试后端事件", backendListeners.size > 0, `${backendListeners.size} 个`);
  for (const listener of backendListeners) listener({ type: "ready" });
  await settle();

  const oldLayer = newLayer();
  const oldHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  oldLayer.append(oldHandle.element);
  oldHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  oldHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  check("迁移清单页已加载", Boolean(oldHandle.element.querySelector("#wiz-proceed")), oldHandle.element.textContent);
  check("规划命令已到达测试后端", commandLog.includes(migration.CMD.planMigration), JSON.stringify(commandLog));
  oldHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  check("迁移命令已到达测试后端", commandLog.includes(migration.CMD.startMigration), JSON.stringify(commandLog));
  check("迁移期间退出保护仍有效", busyChanges.at(-1) === true, JSON.stringify(busyChanges));
  check("迁移提交显式走可跟踪的异步 job 模式", migrationArgs?.async === true && typeof migrationArgs?.clientMigrationId === "string" && migrationArgs.clientMigrationId.length > 0, JSON.stringify(migrationArgs));
  check("收到 job 终态前不会误进报告页", oldHandle.element.querySelector(".wiz__title")?.textContent === "正在迁移");

  oldHandle.dispose();
  oldLayer.remove();
  const newPageLayer = newLayer();
  const newHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  newPageLayer.append(newHandle.element);
  const titleBeforeLateResult = newHandle.element.querySelector(".wiz__title")?.textContent;
  for (const listener of [...backendListeners]) {
    listener({
      type: "job", jobId: migrationJobId, command: migration.CMD.startMigration,
      clientMigrationId: migrationArgs?.clientMigrationId,
      status: "succeeded", sequence: 3, result: completedReport,
    });
  }
  await settle();
  check(
    "旧向导迁移迟到成功不导航/写入新向导",
    newHandle.element.querySelector(".wiz__title")?.textContent === titleBeforeLateResult &&
      titleBeforeLateResult === "检测到旧版 VoxSub",
    `${titleBeforeLateResult} → ${newHandle.element.querySelector(".wiz__title")?.textContent}`,
  );
  check("旧迁移终态后退出保护解除", busyChanges.at(-1) === false, JSON.stringify(busyChanges));

  // 当前向导收到异步终态后，应读取事件中的报告并正常进入结果页。
  newHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  newHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  newHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  newHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  check("当前迁移任务仍由退出保护持有", busyChanges.at(-1) === true, JSON.stringify(busyChanges));
  for (const listener of [...backendListeners]) {
    listener({
      type: "job", jobId: migrationJobId, command: migration.CMD.startMigration,
      clientMigrationId: migrationArgs?.clientMigrationId,
      status: "succeeded", sequence: 3, result: completedReport,
    });
  }
  await settle();
  check("当前向导收到成功终态后展示迁移结果", newHandle.element.querySelector(".wiz__body")?.textContent?.includes("C:/New/models"));
  check("当前迁移真实终态后解除退出保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));

  newHandle.dispose();
  newPageLayer.remove();
  const failedLayer = newLayer();
  const failedHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  failedLayer.append(failedHandle.element);
  failedHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  failedHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  failedHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  failedHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  for (const listener of [...backendListeners]) {
    listener({
      type: "job", jobId: migrationJobId, command: migration.CMD.startMigration,
      clientMigrationId: migrationArgs?.clientMigrationId,
      status: "failed", sequence: 3, error: "disk full",
    });
  }
  await settle();
  check("失败终态报告保留后端原因", failedHandle.element.querySelector(".wiz__body")?.textContent?.includes("disk full"));
  check("失败也只有到真实终态后解除退出保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));
  failedHandle.dispose();
  failedLayer.remove();

  const timeoutLayer = newLayer();
  const timeoutHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  timeoutLayer.append(timeoutHandle.element);
  timeoutHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  timeoutHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  await settle();
  timeoutReceipt = true;
  timeoutHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  check("回执硬超时不释放仍在运行任务的退出保护", busyChanges.at(-1) === true, JSON.stringify(busyChanges));
  for (const listener of [...backendListeners]) listener({
    type: "job", jobId: "migration-job-timeout", command: migration.CMD.startMigration,
    clientMigrationId: migrationArgs?.clientMigrationId, status: "succeeded", sequence: 3,
    result: completedReport,
  });
  await settle();
  check("回执超时后仍能按 clientMigrationId 接收报告", timeoutHandle.element.querySelector(".wiz__body")?.textContent?.includes("C:/New/models"), timeoutHandle.element.querySelector(".wiz__body")?.textContent);
  check("回执超时任务真实终态后解除退出保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));
  timeoutHandle.dispose();
  timeoutLayer.remove();

  const failedReceiptLayer = newLayer();
  const failedReceiptHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  failedReceiptLayer.append(failedReceiptHandle.element);
  failedReceiptHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  failedReceiptHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  await settle();
  completeBeforeFailedReceipt = true;
  failedReceiptHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  check("failed 回执前已缓存的成功终态仍展示迁移报告", failedReceiptHandle.element.querySelector(".wiz__body")?.textContent?.includes("C:/New/models"), failedReceiptHandle.element.querySelector(".wiz__body")?.textContent);
  check("failed 回执前已到达终态后解除退出保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));
  failedReceiptHandle.dispose();
  failedReceiptLayer.remove();

  const fastLayer = newLayer();
  const fastHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  fastLayer.append(fastHandle.element);
  fastHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  fastHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  await settle();
  completeBeforeUnavailableReceipt = true;
  fastHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  check("回执前已缓存的成功报告优先于随后断连", fastHandle.element.querySelector(".wiz__body")?.textContent?.includes("C:/New/models"), fastHandle.element.querySelector(".wiz__body")?.textContent);
  check("回执前终态+断连组合仍能安全解除退出保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));
  fastHandle.dispose();
  fastLayer.remove();
  disconnectBackend();
  newHandle.dispose();
  newPageLayer.remove();
  delete window.voxsub;

  const catalogSrc = readFileSync(join(ROOT, "src/renderer/views/catalog.ts"), "utf-8");
  check(
    "模型目录迟到响应写入前校验请求代次，旧页面 dispose 不会覆盖新页",
    /const requestId = \+\+loadRequestId;/.test(catalogSrc) &&
      (catalogSrc.match(/if \(!isCurrent\(\)\) return;/g) ?? []).length >= 2 &&
      /if \(activePageId !== pageId\) return;[\s\S]{0,100}activePageId = \+\+nextPageId;/.test(catalogSrc),
  );

  const catalog = loaded[2];
  const oldCatalogHandle = catalog.build();
  const currentCatalogHandle = catalog.build();
  oldCatalogHandle.dispose();
  const asrChip = [...currentCatalogHandle.element.querySelectorAll(".filter-chip")]
    .find((chip) => chip.textContent === "识别");
  asrChip?.click();
  const activeAsrChip = [...currentCatalogHandle.element.querySelectorAll(".filter-chip")]
    .find((chip) => chip.textContent === "识别");
  check(
    "旧目录句柄迟到 dispose 后，新目录仍可更新筛选 DOM",
    activeAsrChip?.classList.contains("is-active") === true && activeAsrChip.getAttribute("aria-pressed") === "true",
  );
  currentCatalogHandle.dispose();

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
