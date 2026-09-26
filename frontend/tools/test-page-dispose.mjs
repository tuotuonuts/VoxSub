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

const nodeWarnings = [];
process.on("warning", (warning) => nodeWarnings.push(warning));

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
  ["迁移向导", "tools/test-migration-safety-entry.ts", (m) => m.buildMigrationWizard(DETECT_FIXTURE)],
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
  const busyCalls = [];
  const backendListeners = new Set();
  const commandLog = [];
  const migrationStatusQueries = [];
  const pendingDetectReplies = [];
  const pendingPlanReplies = [];
  const pendingLogPathReplies = [];
  const cleanupRequests = [];
  const revealedLogPaths = [];
  let holdDetectReplies = false;
  let holdPlanReplies = false;
  let holdLogPathReplies = false;
  let holdCleanupReply = false;
  let unknownCleanupReply = false;
  let releaseCleanupReply = null;
  let migrationStatusResponse = "running";
  let exposeExistingMigrationJob = false;
  let rejectAsActiveJob = false;
  const existingMigrationJob = { jobId: "migration-job-preexisting", command: "start_migration", status: "running" };
  let migrationJobId = "migration-job-unsubmitted";
  let migrationSubmissionCount = 0;
  let migrationArgs = null;
  let holdBusyEnable = false;
  let releaseBusyEnable = null;
  let busyEnableResponse = null;
  let completeBeforeUnavailableReceipt = false;
  let completeBeforeFailedReceipt = false;
  let timeoutReceipt = false;
  let holdMigrationReceipt = false;
  let releaseHeldReceipt = null;
  const planFixture = {
    targetRoot: "C:/NewVoxSub",
    steps: [{ key: "models", source: "C:/Old/models", target: "C:/New/models", bytes: 1, file_count: 1, purpose: "", mode: "copy", note: "", rebuildable: false }],
    totalBytes: 1,
    freeBytes: 1024,
  };
  const completedReport = {
    done: [{ key: "models", mode: "copy", target: "C:/New/models", verify: { ok: true }, recordId: "record-test-model" }],
    failed: [],
    elapsedMs: 1,
    ok: true,
  };
  let confirmResult = true;
  window.confirm = () => confirmResult;
  window.voxsub = {
    app: { setBusy: (busy, reason, owner) => {
      busyChanges.push(busy);
      busyCalls.push({ busy, reason, owner });
      const response = busy ? (busyEnableResponse ?? reason ?? "正在执行后台任务") : "";
      if (busy && holdBusyEnable) {
        holdBusyEnable = false;
        return new Promise((resolve) => { releaseBusyEnable = () => resolve(response); });
      }
      return Promise.resolve(response);
    } },
    dialog: { revealInFolder: async (path) => { revealedLogPaths.push(path); } },
    backend: {
      onEvent: (handler) => { backendListeners.add(handler); return () => backendListeners.delete(handler); },
      start: async () => ({ ok: true }),
      command: async (command, args) => {
        commandLog.push(command);
        if (command === migration.CMD.detectLegacy) {
          if (holdDetectReplies) return new Promise((resolve) => pendingDetectReplies.push((data) => resolve({ ok: true, data })));
          return { ok: true, data: DETECT_FIXTURE };
        }
        if (command === migration.CMD.logPath) {
          if (holdLogPathReplies) return new Promise((resolve) => pendingLogPathReplies.push((path) => resolve({ ok: true, data: { path } })));
          return { ok: true, data: { path: "C:/test-logs" } };
        }
        if (command === migration.CMD.jobList) return { ok: true, data: {
          jobs: exposeExistingMigrationJob ? [existingMigrationJob] : [],
        } };
        if (command === migration.CMD.jobStatus) {
          migrationStatusQueries.push(args);
          return { ok: true, data: { ok: true, job: {
            jobId: args?.job_id, command: migration.CMD.startMigration, status: migrationStatusResponse, sequence: 2,
          } } };
        }
        if (command === migration.CMD.cleanupMigratedSource) {
          cleanupRequests.push(args);
          if (unknownCleanupReply) return { ok: false, timedOut: true, delivery: "unknown", error: "simulated hard timeout" };
          if (holdCleanupReply) {
            holdCleanupReply = false;
            return new Promise((resolve) => {
              releaseCleanupReply = () => resolve({ ok: true, data: {
                ok: true, deleted: true, cleaned: ["record-test-model"], detail: "test cleanup completed",
              } });
            });
          }
          return { ok: true, data: { ok: true, deleted: true, cleaned: ["record-test-model"], detail: "test cleanup completed" } };
        }
        if (command === migration.CMD.migrationDecision) return { ok: true, data: { saved: true } };
        if (command === migration.CMD.state) return { ok: true, data: { running: false, paused: false, mode: "a" } };
        if (command === migration.CMD.planMigration) {
          if (holdPlanReplies) {
            return new Promise((resolve) => pendingPlanReplies.push((data) => resolve({ ok: true, data })));
          }
          return { ok: true, data: planFixture };
        }
        if (command === migration.CMD.startMigration) {
          migrationArgs = args;
          if (rejectAsActiveJob) {
            rejectAsActiveJob = false;
            return { ok: false, code: "active_job_exists", error: "已有迁移任务仍在运行", jobId: existingMigrationJob.jobId };
          }
          migrationSubmissionCount += 1;
          migrationJobId = `migration-job-${migrationSubmissionCount}`;
          const submittedJobId = migrationJobId;
          if (holdMigrationReceipt) {
            holdMigrationReceipt = false;
            return new Promise((resolve) => {
              releaseHeldReceipt = (receipt = {
                ok: true,
                data: { accepted: true, jobId: submittedJobId, async: args?.async },
              }) => resolve(receipt);
            });
          }
          if (timeoutReceipt) {
            timeoutReceipt = false;
            for (const [status, sequence] of [["queued", 1], ["running", 2]]) {
              for (const listener of [...backendListeners]) listener({
                type: "job", jobId: submittedJobId, command: migration.CMD.startMigration,
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
          return { ok: true, data: { accepted: true, jobId: submittedJobId, async: args?.async } };
        }
        return { ok: false, error: `unexpected test command: ${command}` };
      },
    },
  };
  const disconnectBackend = migration.connectBackend();
  check("store 已订阅测试后端事件", backendListeners.size > 0, `${backendListeners.size} 个`);
  for (const listener of backendListeners) listener({ type: "ready" });
  await settle();

  holdDetectReplies = true;
  const staleReopenHost = newLayer();
  staleReopenHost.textContent = "old settings page";
  let settingsPageCurrent = true;
  const staleReopen = migration.reopenWizard(staleReopenHost, () => settingsPageCurrent);
  await settle();
  check("设置页迁移检测请求已在途", pendingDetectReplies.length === 1, `${pendingDetectReplies.length} 个`);
  settingsPageCurrent = false;
  const replacementPage = document.createElement("div");
  replacementPage.className = "replacement-page-sentinel";
  replacementPage.textContent = "replacement settings page";
  staleReopenHost.replaceChildren(replacementPage);
  pendingDetectReplies[0](DETECT_FIXTURE);
  await settle();
  const staleReopenHandle = await staleReopen;
  check("设置页失效后迟到的迁移检测不得替换新页面", staleReopenHost.querySelector(".replacement-page-sentinel") !== null && staleReopenHost.querySelector(".wiz-host") === null, staleReopenHost.textContent);
  staleReopenHandle.dispose();
  staleReopenHost.remove();
  holdDetectReplies = false;

  const planFor = (name) => ({
    ...planFixture,
    targetRoot: `C:/plan-${name}`,
    steps: planFixture.steps.map((step) => ({ ...step, key: `models-${name}`, target: `C:/target-${name}` })),
  });
  const submitPlanFromOverview = (handle) => {
    handle.element.querySelector(".wiz__actions .btn--primary")?.click();
    handle.element.querySelector(".wiz__actions .btn--primary")?.click();
  };
  holdPlanReplies = true;
  const planALayer = newLayer();
  const planAHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  planALayer.append(planAHandle.element);
  submitPlanFromOverview(planAHandle);
  await settle();
  const planARequest = pendingPlanReplies.length - 1;
  planAHandle.dispose();
  planALayer.remove();

  const planBLayer = newLayer();
  const planBHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  planBLayer.append(planBHandle.element);
  submitPlanFromOverview(planBHandle);
  await settle();
  const planBRequest = pendingPlanReplies.length - 1;
  pendingPlanReplies[planBRequest](planFor("B"));
  await settle();
  check("B 的规划响应先完成时显示 B 的清单", planBHandle.element.querySelector(".wiz__target code")?.textContent === "C:/plan-B");
  pendingPlanReplies[planARequest](planFor("A"));
  await settle();
  check("A 页面迟到的规划响应不能改写 B 的 DOM 或清单状态", planBHandle.element.querySelector(".wiz__target code")?.textContent === "C:/plan-B" && planBHandle.element.querySelector(".mig-item__head strong")?.textContent === "models-B", planBHandle.element.textContent);

  planBHandle.element.querySelector(".wiz__actions .btn--ghost")?.click();
  await settle();
  const samePageRequestStart = pendingPlanReplies.length;
  const planButton = [...planBHandle.element.querySelectorAll(".wiz__actions button")]
    .find((candidate) => candidate.textContent === "规划迁移");
  planButton?.click();
  planButton?.click();
  await settle();
  const olderSamePageRequest = samePageRequestStart;
  const latestSamePageRequest = samePageRequestStart + 1;
  pendingPlanReplies[latestSamePageRequest](planFor("same-page-latest"));
  await settle();
  pendingPlanReplies[olderSamePageRequest](planFor("same-page-older"));
  await settle();
  check("同一页面多次规划仅最新响应生效", planBHandle.element.querySelector(".wiz__target code")?.textContent === "C:/plan-same-page-latest" && planBHandle.element.querySelector(".mig-item__head strong")?.textContent === "models-same-page-latest", planBHandle.element.textContent);
  holdPlanReplies = false;
  planBHandle.dispose();
  planBLayer.remove();

  const busyAckLayer = newLayer();
  const busyAckHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  busyAckLayer.append(busyAckHandle.element);
  busyAckHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  busyAckHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  const startsBeforeBusyAck = commandLog.filter((item) => item === migration.CMD.startMigration).length;
  busyEnableResponse = "";
  busyAckHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  const busyAckReport = busyAckHandle.element.querySelector(".wiz__body")?.textContent ?? "";
  check("退出保护 IPC 未返回有效确认时不提交迁移", commandLog.filter((item) => item === migration.CMD.startMigration).length === startsBeforeBusyAck && busyAckReport.includes("无法启用退出保护，迁移未启动"), JSON.stringify({ commandLog, busyAckReport }));
  busyEnableResponse = null;
  if (commandLog.filter((item) => item === migration.CMD.startMigration).length > startsBeforeBusyAck) {
    for (const listener of [...backendListeners]) listener({
      type: "job", jobId: migrationJobId, command: migration.CMD.startMigration,
      clientMigrationId: migrationArgs?.clientMigrationId, status: "failed", sequence: 3,
      error: "cleanup after simulated invalid busy acknowledgement",
    });
    await settle();
  }
  busyAckHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  busyAckHandle.dispose();
  busyAckLayer.remove();

  const migrationCountBeforeMainRun = commandLog.filter((item) => item === migration.CMD.startMigration).length;
  const baselineEventListeners = backendListeners.size;
  const oldLayer = newLayer();
  const oldHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  oldLayer.append(oldHandle.element);
  oldHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  oldHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  check("迁移清单页已加载", Boolean(oldHandle.element.querySelector("#wiz-proceed")), oldHandle.element.textContent);
  check("规划命令已到达测试后端", commandLog.includes(migration.CMD.planMigration), JSON.stringify(commandLog));
  holdMigrationReceipt = true;
  const oldProceed = oldHandle.element.querySelector("#wiz-proceed");
  holdBusyEnable = true;
  oldProceed?.click();
  oldProceed?.click();
  await settle();
  check("退出保护确认未返回前迁移命令不得提交", commandLog.filter((item) => item === migration.CMD.startMigration).length === migrationCountBeforeMainRun, JSON.stringify(commandLog));
  check("等待退出保护确认期间仍保持退出保护请求", busyChanges.at(-1) === true, JSON.stringify(busyChanges));

  // 切页发生在主进程确认退出保护之前；旧 continuation 不能重绘替换后的向导。
  migration.closeWizard();
  check("确认待决时关闭页面不释放任务退出保护", busyChanges.at(-1) === true, JSON.stringify(busyChanges));
  check("关闭活动向导保留任务终态监听", backendListeners.size === baselineEventListeners + 1, `${baselineEventListeners} → ${backendListeners.size}`);
  oldHandle.dispose();
  oldLayer.remove();
  let newPageLayer = newLayer();
  let newHandle = await migration.reopenWizard(newPageLayer);
  const newRunningBody = newHandle.element.querySelector(".wiz__body");
  check("确认待决期间重新打开仍展示活动迁移", newHandle.element.querySelector(".wiz__title")?.textContent === "正在迁移");
  check("重新打开时继续保留退出保护", busyChanges.at(-1) === true, JSON.stringify(busyChanges));

  releaseBusyEnable?.();
  await settle();
  const firstClientMigrationId = migrationArgs?.clientMigrationId;
  const firstMigrationJobId = migrationJobId;
  check("退出保护确认后迁移命令恰好提交一次", commandLog.filter((item) => item === migration.CMD.startMigration).length === migrationCountBeforeMainRun + 1, JSON.stringify(commandLog));
  check("迁移提交显式走可跟踪的异步 job 模式", migrationArgs?.async === true && typeof firstClientMigrationId === "string" && firstClientMigrationId.length > 0, JSON.stringify(migrationArgs));
  check("旧页面退出保护 continuation 不重绘新向导", newHandle.element.querySelector(".wiz__body") === newRunningBody, newHandle.element.querySelector(".wiz__title")?.textContent);
  check("收到 job 终态前不会误进报告页", newHandle.element.querySelector(".wiz__title")?.textContent === "正在迁移", newHandle.element.querySelector(".wiz__title")?.textContent);
  oldProceed?.click();
  await settle();
  check("旧页面遗留的提交入口不会创建第二条迁移", commandLog.filter((item) => item === migration.CMD.startMigration).length === migrationCountBeforeMainRun + 1, JSON.stringify(commandLog));

  for (const listener of [...backendListeners]) listener({
    type: "migration", phase: "progress", key: "models", completed: 1, total: 4,
  });
  check("任务仍在运行时显示已收到的真实进度", newHandle.element.querySelector(".mig-progress__row .progress__label")?.textContent === "25%");

  // 关闭页面只释放视图订阅；迁移本身及其终态跟踪仍归应用级任务所有。
  migration.closeWizard();
  check("关闭活动向导不释放迁移退出保护", busyChanges.at(-1) === true, JSON.stringify(busyChanges));
  check("关闭活动向导保留任务终态监听", backendListeners.size === baselineEventListeners + 1, `${baselineEventListeners} → ${backendListeners.size}`);
  newHandle.dispose();
  newPageLayer.remove();
  for (const listener of [...backendListeners]) listener({
    type: "migration", phase: "progress", key: "models", completed: 3, total: 4,
  });

  newPageLayer = newLayer();
  newHandle = await migration.reopenWizard(newPageLayer);
  check("重新打开向导展示仍在运行的迁移", newHandle.element.querySelector(".wiz__title")?.textContent === "正在迁移", newHandle.element.querySelector(".wiz__title")?.textContent);
  check("页面关闭期间的最新进度在重开后恢复", newHandle.element.querySelector(".mig-progress__row .progress__label")?.textContent === "75%", newHandle.element.querySelector(".mig-progress__row .progress__label")?.textContent);
  check("重开页面不会释放原任务退出保护", busyChanges.at(-1) === true, JSON.stringify(busyChanges));

  // 终态可早于快速受理回执。精确 clientMigrationId 的终态必须立即收尾。
  for (const listener of [...backendListeners]) {
    listener({
      type: "job", jobId: firstMigrationJobId, command: migration.CMD.startMigration,
      clientMigrationId: firstClientMigrationId,
      status: "succeeded", sequence: 3, result: completedReport,
    });
  }
  await settle();
  check("回执尚未返回时，匹配的成功终态仍展示报告", newHandle.element.querySelector(".wiz__body")?.textContent?.includes("C:/New/models"), newHandle.element.querySelector(".wiz__body")?.textContent);
  check("回执尚未返回时，真实终态解除退出保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));

  newHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  newHandle.dispose();
  newPageLayer.remove();

  // A 已结束后，开始 B；A 的迟到回执和重复终态不得影响 B。
  const secondLayer = newLayer();
  const secondHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  secondLayer.append(secondHandle.element);
  secondHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  secondHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  secondHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  const secondClientMigrationId = migrationArgs?.clientMigrationId;
  const secondMigrationJobId = migrationJobId;
  check("第二条迁移 B 启动并持有保护", busyChanges.at(-1) === true && secondClientMigrationId !== firstClientMigrationId, JSON.stringify({ busyChanges, secondClientMigrationId }));
  releaseHeldReceipt?.();
  await settle();
  for (const listener of [...backendListeners]) {
    listener({
      type: "job", jobId: firstMigrationJobId, command: migration.CMD.startMigration,
      clientMigrationId: firstClientMigrationId, status: "failed", sequence: 4, error: "late A failure",
    });
    listener({
      type: "job", jobId: "unrelated-job-id", command: migration.CMD.startMigration,
      clientMigrationId: secondClientMigrationId, status: "succeeded", sequence: 3, result: completedReport,
    });
  }
  await settle();
  check("A 的迟到回执/重复终态与 B 的错配 jobId 不解除 B 保护", secondHandle.element.querySelector(".wiz__title")?.textContent === "正在迁移" && busyChanges.at(-1) === true, JSON.stringify({ title: secondHandle.element.querySelector(".wiz__title")?.textContent, busyChanges }));
  for (const listener of [...backendListeners]) {
    listener({
      type: "job", jobId: secondMigrationJobId, command: migration.CMD.startMigration,
      clientMigrationId: secondClientMigrationId, status: "failed", sequence: 4, error: "second migration failed",
    });
  }
  await settle();
  check("B 的精确终态展示 B 自己的结果", secondHandle.element.querySelector(".wiz__body")?.textContent?.includes("second migration failed"));
  check("B 的真实终态解除自己的退出保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));
  secondHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  secondHandle.dispose();
  secondLayer.remove();
  const hiddenRunLayer = newLayer();
  const hiddenRunHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  hiddenRunLayer.append(hiddenRunHandle.element);
  hiddenRunHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  hiddenRunHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  hiddenRunHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  const hiddenRunJobId = migrationJobId;
  const hiddenRunClientId = migrationArgs?.clientMigrationId;
  check("关闭前迁移 C 已受理", typeof hiddenRunClientId === "string" && busyChanges.at(-1) === true);
  migration.closeWizard();
  hiddenRunHandle.dispose();
  hiddenRunLayer.remove();
  for (const listener of [...backendListeners]) listener({
    type: "job", jobId: hiddenRunJobId, command: migration.CMD.startMigration,
    clientMigrationId: hiddenRunClientId, status: "succeeded", sequence: 3,
    result: completedReport,
  });
  await settle();
  check("向导关闭期间收到终态仍由任务 owner 保存报告并解除保护", busyChanges.at(-1) === false);
  const reportLayer = newLayer();
  const detectCountBeforeResume = commandLog.filter((item) => item === migration.CMD.detectLegacy).length;
  const resumedHandle = await migration.reopenWizard(reportLayer);
  check("关闭期间已完成的迁移重开后恢复报告，不重新提交", resumedHandle.element.querySelector(".wiz__body")?.textContent?.includes("C:/New/models") && commandLog.filter((item) => item === migration.CMD.startMigration).length === migrationCountBeforeMainRun + 3);
  check("恢复已缓存报告不重复执行检测", commandLog.filter((item) => item === migration.CMD.detectLegacy).length === detectCountBeforeResume);

  const cleanupButton = [...resumedHandle.element.querySelectorAll("button")]
    .find((candidate) => candidate.textContent === "清理已迁移的原目录");
  const cleanupCountBeforeCancel = cleanupRequests.length;
  confirmResult = false;
  cleanupButton?.click();
  await settle();
  check("用户取消清理时不发送删除请求", cleanupRequests.length === cleanupCountBeforeCancel, JSON.stringify(cleanupRequests));
  check("取消清理后报告页明确显示已取消", resumedHandle.element.querySelector(".wiz__body")?.textContent?.includes("已取消"), resumedHandle.element.textContent);
  confirmResult = true;
  busyEnableResponse = "";
  [...resumedHandle.element.querySelectorAll("button")]
    .find((candidate) => candidate.textContent === "清理已迁移的原目录")?.click();
  await settle();
  check("清理退出保护确认无效时绝不提交删除", cleanupRequests.length === cleanupCountBeforeCancel && resumedHandle.element.textContent.includes("无法启用退出保护，清理未启动"));
  busyEnableResponse = null;
  holdCleanupReply = true;
  const confirmedCleanupButton = [...resumedHandle.element.querySelectorAll("button")]
    .find((candidate) => candidate.textContent === "清理已迁移的原目录");
  holdBusyEnable = true;
  const beforeCleanupGuard = cleanupRequests.length;
  confirmedCleanupButton?.click();
  await settle();
  check("清理退出保护确认之前不得发送删除命令", cleanupRequests.length === beforeCleanupGuard && busyChanges.at(-1) === true);
  holdBusyEnable = false;
  releaseBusyEnable?.();
  await settle();
  const cleanupBusyOwner = busyCalls.at(-1)?.owner;
  check("清理持有独立操作 owner", typeof cleanupBusyOwner === "string" && cleanupBusyOwner !== hiddenRunClientId && busyChanges.at(-1) === true);
  check("用户确认后清理副作用已提交且使用记录 ID", cleanupRequests.at(-1)?.confirm === true && cleanupRequests.at(-1)?.record_ids?.[0] === "record-test-model", JSON.stringify(cleanupRequests));
  migration.closeWizard(true);
  resumedHandle.dispose();
  reportLayer.remove();

  const unrelatedLayer = newLayer();
  const unrelatedHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  unrelatedLayer.append(unrelatedHandle.element);
  const statusBeforeCleanupReply = migration.store.get().statusText;
  releaseCleanupReply?.();
  await settle();
  check("旧报告的清理回执不导航或改写新向导", unrelatedHandle.element.querySelector(".wiz__title")?.textContent === "检测到旧版 VoxSub", unrelatedHandle.element.textContent);
  check("旧报告清理回执不覆盖新页面共享状态文案", migration.store.get().statusText === statusBeforeCleanupReply, migration.store.get().statusText);
  check("已提交清理回执仍被记录，关闭页面不伪装成未执行", migration.store.get().logs.some((entry) => entry.message.includes("test cleanup completed")));
  check("清理真实完成只释放本次清理 owner", busyCalls.at(-1)?.busy === false && busyCalls.at(-1)?.owner === cleanupBusyOwner);
  unrelatedHandle.dispose();
  unrelatedLayer.remove();

  const recoveredCleanupLayer = newLayer();
  const recoveredCleanupHandle = await migration.reopenWizard(recoveredCleanupLayer);
  check("重开原报告可读取旧清理副作用的真实结果", recoveredCleanupHandle.element.querySelector(".wiz__body")?.textContent?.includes("test cleanup completed"), recoveredCleanupHandle.element.querySelector(".wiz__body")?.textContent);
  migration.closeWizard(true);
  recoveredCleanupHandle.dispose();
  recoveredCleanupLayer.remove();

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
  const copyButton = [...failedHandle.element.querySelectorAll("button")]
    .find((candidate) => candidate.textContent === "复制诊断摘要");
  copyButton?.click();
  await settle();
  check("当前报告复制完成后显示成功反馈", copyButton?.textContent === "已复制" && dom.clipboardWrites.at(-1)?.includes("disk full"));
  if (copyButton) copyButton.textContent = "复制诊断摘要";
  const originalClipboardWrite = navigator.clipboard.writeText;
  let releaseClipboardWrite = null;
  navigator.clipboard.writeText = (text) => new Promise((resolve) => {
    releaseClipboardWrite = () => {
      dom.clipboardWrites.push(text);
      resolve();
    };
  });
  copyButton?.click();
  await settle();
  check("报告复制请求已进入可控异步等待", typeof releaseClipboardWrite === "function");
  const openLogButton = [...failedHandle.element.querySelectorAll("button")]
    .find((candidate) => candidate.textContent === "打开日志文件夹");
  openLogButton?.click();
  await settle();
  check("当前报告的日志路径响应仍可正常打开", revealedLogPaths.at(-1) === "C:/test-logs", JSON.stringify(revealedLogPaths));

  holdLogPathReplies = true;
  openLogButton?.click();
  await settle();
  const pendingLogPathIndex = pendingLogPathReplies.length - 1;
  const revealedBeforeStaleLogPath = revealedLogPaths.length;
  failedHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  failedHandle.dispose();
  failedLayer.remove();

  const afterLogCloseLayer = newLayer();
  const afterLogCloseHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  afterLogCloseLayer.append(afterLogCloseHandle.element);
  releaseClipboardWrite?.();
  pendingLogPathReplies[pendingLogPathIndex]("C:/stale-test-logs");
  await settle();
  check("旧报告迟到的复制反馈不写入已释放页面", copyButton?.textContent === "复制诊断摘要", copyButton?.textContent);
  navigator.clipboard.writeText = originalClipboardWrite;
  check("旧报告迟到的日志路径不再触发打开操作或改写新向导", revealedLogPaths.length === revealedBeforeStaleLogPath && afterLogCloseHandle.element.querySelector(".wiz__title")?.textContent === "检测到旧版 VoxSub", JSON.stringify({ revealedLogPaths, title: afterLogCloseHandle.element.querySelector(".wiz__title")?.textContent }));
  afterLogCloseHandle.dispose();
  afterLogCloseLayer.remove();
  holdLogPathReplies = false;

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
  const recoveryButton = timeoutHandle.element.querySelector("#wiz-query-status");
  check("结果未知时提供任务状态查询入口", Boolean(recoveryButton));
  recoveryButton?.click();
  await settle();
  check("状态查询使用后端 job_status 与已关联 jobId", commandLog.includes(migration.CMD.jobStatus) && migrationStatusQueries.at(-1)?.job_id === migrationJobId, JSON.stringify({ commandLog, migrationStatusQueries, migrationJobId }));
  check("查询确认任务仍运行时继续保留退出保护", busyChanges.at(-1) === true, JSON.stringify(busyChanges));
  for (const listener of [...backendListeners]) listener({
    type: "job", jobId: migrationJobId, command: migration.CMD.startMigration,
    clientMigrationId: migrationArgs?.clientMigrationId, status: "succeeded", sequence: 3,
    result: completedReport,
  });
  await settle();
  check("回执超时后仍能按 clientMigrationId 接收报告", timeoutHandle.element.querySelector(".wiz__body")?.textContent?.includes("C:/New/models"), timeoutHandle.element.querySelector(".wiz__body")?.textContent);
  check("回执超时任务真实终态后解除退出保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));
  timeoutHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
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
  failedReceiptHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
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
  fastHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  fastHandle.dispose();
  fastLayer.remove();

  migration.setLanguage("en");
  const migrationSafetyCopy = [
    "上一次原目录清理仍在确认，不能开始新的迁移。",
    "后台确认迁移仍在运行，退出保护保持开启。",
    "后端已断开，迁移结果未知；请重新检测目标目录，不要清理原目录。",
    "已有迁移任务仍在运行，不能重复提交。",
    "已有迁移任务正在运行，但任务编号无法安全匹配；退出保护保持开启，可重新查询。",
    "已有迁移任务正在运行；正在重新关联其状态，退出保护保持开启。",
    "数据迁移",
    "无法启用退出保护，迁移未启动。",
    "无法唯一确认迁移任务状态；退出保护仍保持开启。",
    "暂时无法查询迁移任务状态；退出保护仍保持开启。",
    "查询迁移任务状态",
    "检测到旧版 VoxSub",
    "正在检测旧版安装…",
    "清理结果未知，操作可能已执行；请先核对原目录，不要重复清理。",
    "清理请求已提交，正在等待后端确认；请勿重复执行。",
    "迁移任务仍在队列中，退出保护保持开启。",
    "迁移任务失败",
    "迁移任务已取消",
    "迁移任务已受理，正在等待后台完成。",
    "迁移任务已结束，但未收到有效报告；请重新检测，不要清理原目录。",
    "迁移任务正在取消；等待后台确认终态后再退出。",
    "迁移受理回执不完整或状态未知，任务仍可能正在运行；正在等待后台终态。",
    "迁移回执与终态任务编号不匹配；请重新检测，不要清理原目录。",
    "迁移提交回执状态未知，任务可能仍在运行；正在等待后台终态。",
    "迁移状态仍未知；退出保护保持开启，可再次查询。",
    "迁移状态查询与已跟踪任务不匹配；退出保护仍保持开启。",
    "迁移状态查询与当前任务不匹配；退出保护仍保持开启。",
    "迁移终态尚未确认，仍保留退出保护。",
  ];
  const englishFallbacks = migrationSafetyCopy
    .map((source) => [source, migration.tr(source)])
    .filter(([source, translated]) => translated === source || [...translated].some((char) => {
      const codePoint = char.codePointAt(0) ?? 0;
      return codePoint >= 0x3400 && codePoint <= 0x9fff;
    }));
  check("英文模式下新增迁移安全文案均有实际翻译且不回退中文", englishFallbacks.length === 0, JSON.stringify(englishFallbacks));

  const backendExitLayer = newLayer();
  const backendExitHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  backendExitLayer.append(backendExitHandle.element);
  backendExitHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  backendExitHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  await settle();
  holdMigrationReceipt = true;
  backendExitHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  check("后端退出前尚未收到迁移终态时仍持有保护", busyChanges.at(-1) === true, JSON.stringify(busyChanges));
  for (const listener of [...backendListeners]) listener({ type: "disconnected", reason: "code=1" });
  releaseHeldReceipt?.({ ok: false, unavailable: true, error: "后端已退出", delivery: "unknown" });
  await settle();
  const backendExitBody = backendExitHandle.element.querySelector(".wiz__body")?.textContent ?? "";
  const backendExitMessage = migration.tr("后端已断开，迁移结果未知；请重新检测目标目录，不要清理原目录。");
  check(
    "英文模式下真实后端退出路径保留结果未知、复检目标且禁止清理原目录",
    backendExitBody.includes(backendExitMessage) &&
      !backendExitBody.includes("后端已断开，迁移结果未知") &&
      /unknown/i.test(backendExitMessage) && /do not|don't|never/i.test(backendExitMessage) &&
      !backendExitBody.includes("迁移完成"),
    `${backendExitMessage}: ${backendExitBody}`,
  );
  check("已确认后端进程退出后解除任务保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));
  backendExitHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  backendExitHandle.dispose();
  backendExitLayer.remove();
  migration.setLanguage("zh");
  for (const listener of [...backendListeners]) listener({ type: "ready", version: "test" });
  await settle();

  const queriedTerminalLayer = newLayer();
  const queriedTerminalHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  queriedTerminalLayer.append(queriedTerminalHandle.element);
  queriedTerminalHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  queriedTerminalHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  await settle();
  queriedTerminalHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  migrationStatusResponse = "succeeded";
  queriedTerminalHandle.element.querySelector("#wiz-query-status")?.click();
  await settle();
  await settle();
  const queriedTerminalBody = queriedTerminalHandle.element.querySelector(".wiz__body")?.textContent ?? "";
  check("终态状态查询缺少报告时明确不宣称迁移成功", queriedTerminalBody.includes("迁移任务已结束，但未收到有效报告") && !queriedTerminalBody.includes("迁移完成"), queriedTerminalBody);
  check("任务状态查询确认真实终态后释放退出保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));
  queriedTerminalHandle.dispose();
  queriedTerminalLayer.remove();
  const recoveredTerminalLayer = newLayer();
  const recoveredTerminalHandle = await migration.reopenWizard(recoveredTerminalLayer);
  const recoveredTerminalTitle = recoveredTerminalHandle.element.querySelector(".wiz__title")?.textContent ?? "";
  const recoveredTerminalBody = recoveredTerminalHandle.element.querySelector(".wiz__body")?.textContent ?? "";
  check("查询收尾后的终态重开仍恢复报告，而非伪装仍运行", recoveredTerminalTitle === "迁移遇到问题" && recoveredTerminalBody.includes("迁移任务已结束，但未收到有效报告"), `${recoveredTerminalTitle}: ${recoveredTerminalBody}`);
  for (const listener of [...backendListeners]) listener({
    type: "job", jobId: "unrelated-terminal", command: migration.CMD.startMigration,
    status: "succeeded", sequence: 3, result: completedReport,
  });
  await settle();
  check("无报告终态后的错配事件不能覆盖原报告", !recoveredTerminalHandle.element.querySelector(".wiz__body")?.textContent?.includes("C:/New/models"));
  for (const listener of [...backendListeners]) listener({
    type: "job", jobId: migrationJobId, command: migration.CMD.startMigration,
    clientMigrationId: migrationArgs?.clientMigrationId,
    status: "succeeded", sequence: 3, result: completedReport,
  });
  await settle();
  check("job_status 先到且无报告时，迟到终态恢复真实报告", recoveredTerminalHandle.element.querySelector(".wiz__body")?.textContent?.includes("C:/New/models"), recoveredTerminalHandle.element.textContent);
  check("迟到的有效报告恢复清理记录入口", [...recoveredTerminalHandle.element.querySelectorAll("button")].some((button) => button.textContent === "清理已迁移的原目录"));
  recoveredTerminalHandle.dispose();
  recoveredTerminalLayer.remove();

  migrationStatusResponse = "running";
  exposeExistingMigrationJob = true;
  const acceptedBeforeConflict = migrationSubmissionCount;
  const conflictLayer = newLayer();
  const conflictHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  conflictLayer.append(conflictHandle.element);
  conflictHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  conflictHandle.element.querySelector(".wiz__actions .btn--primary")?.click();
  await settle();
  rejectAsActiveJob = true;
  conflictHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  await settle();
  check("服务端拒绝重复迁移后仍保留旧任务退出保护并按真实 jobId 对账", busyChanges.at(-1) === true && migrationSubmissionCount === acceptedBeforeConflict && migrationStatusQueries.at(-1)?.job_id === existingMigrationJob.jobId, JSON.stringify({ busyChanges, acceptedBeforeConflict, migrationSubmissionCount, migrationStatusQueries }));
  check("接管旧任务期间仍显示运行态而非迁移失败", conflictHandle.element.querySelector(".wiz__title")?.textContent === "正在迁移", conflictHandle.element.querySelector(".wiz__title")?.textContent);
  for (const listener of [...backendListeners]) listener({
    type: "job", jobId: existingMigrationJob.jobId, command: migration.CMD.startMigration,
    clientMigrationId: "migration-owner-from-before-renderer-reload", status: "succeeded", sequence: 3,
    result: completedReport,
  });
  await settle();
  check("按已对账 jobId 接收旧 renderer owner 的权威终态", conflictHandle.element.querySelector(".wiz__body")?.textContent?.includes("C:/New/models"), conflictHandle.element.querySelector(".wiz__body")?.textContent);
  check("被接管的旧迁移只在其终态到达后释放退出保护", busyChanges.at(-1) === false, JSON.stringify(busyChanges));
  migration.closeWizard();
  conflictHandle.dispose();
  conflictLayer.remove();
  exposeExistingMigrationJob = false;

  migration.setLanguage("en");
  const unknownLayer = newLayer();
  const unknownHandle = await migration.reopenWizard(unknownLayer);
  const cleanupLabel = migration.tr("清理已迁移的原目录");
  const findCleanup = (handle) => [...handle.element.querySelectorAll("button")].find((b) => b.textContent === cleanupLabel);
  const logsBeforeUnknown = migration.store.get().logs.length;
  unknownCleanupReply = true;
  findCleanup(unknownHandle)?.click();
  await settle();
  const countAfterUnknown = cleanupRequests.length;
  const unknownButton = findCleanup(unknownHandle);
  check("清理超时保留退出保护并禁用重试", busyChanges.at(-1) === true && unknownButton?.disabled === true);
  const unknownCopy = migration.tr("清理结果未知，操作可能已执行；请先核对原目录，不要重复清理。");
  check("英文清理未知明确禁止重复，而非建议重试", /do not (repeat|retry)|don't (repeat|retry)|never (repeat|retry)/i.test(unknownCopy) && !/before retrying/.test(unknownCopy), unknownCopy);
  const unknownLogs = migration.store.get().logs.slice(logsBeforeUnknown).map((entry) => entry.message);
  check("英文未知回执与清理静态日志不混入中文", unknownLogs.length > 0 && unknownLogs.every((line) => !/[\u3400-\u9fff]/u.test(line)), JSON.stringify(unknownLogs));
  // Force a click past disabled UI: the operation owner itself must reject retries.
  if (unknownButton) unknownButton.disabled = false;
  unknownButton?.click();
  await settle();
  check("未知清理的操作闸门拒绝重复请求", cleanupRequests.length === countAfterUnknown);
  migration.closeWizard();
  unknownHandle.dispose();
  unknownLayer.remove();
  const unknownReopenLayer = newLayer();
  const unknownReopenHandle = await migration.reopenWizard(unknownReopenLayer);
  check("关闭重开后未知清理仍禁止重复且持有退出保护", findCleanup(unknownReopenHandle)?.disabled === true && busyChanges.at(-1) === true);
  unknownReopenHandle.dispose();
  unknownReopenLayer.remove();
  migration.setLanguage("zh");
  const blockedNewLayer = newLayer();
  const blockedNewHandle = migration.buildMigrationWizard(DETECT_FIXTURE);
  blockedNewLayer.append(blockedNewHandle.element);
  submitPlanFromOverview(blockedNewHandle);
  await settle();
  const submissionsBeforeBlocked = migrationSubmissionCount;
  blockedNewHandle.element.querySelector("#wiz-proceed")?.click();
  await settle();
  check("未知清理不得被新迁移 owner 覆盖", migrationSubmissionCount === submissionsBeforeBlocked && migration.store.get().statusText.includes("上一次原目录清理仍在确认"));
  blockedNewHandle.dispose();
  blockedNewLayer.remove();

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

await new Promise((resolve) => setImmediate(resolve));
check(
  "测试辅助编译不触发 Node 的 shell-argument 安全弃用警告",
  !nodeWarnings.some((warning) => warning.code === "DEP0190"),
  nodeWarnings.map((warning) => `${warning.code}: ${warning.message}`).join(" | "),
);
dom.restore();
finish();
