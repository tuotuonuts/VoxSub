#!/usr/bin/env node
/**
 * 命令请求结果分类（src/shared/request-outcome.ts）—— 缺陷 #4 的离线守护。
 *
 * ## 守的是什么
 *
 * 请求层的 ~30 秒兜底（frontend/src/main/backend.ts 的 `command()`）原来把超时
 * 直接判成失败：`resolve({ ok:false, error:"命令超时" })`。而长任务（迁移、模型
 * 下载、OCR）跑到 30 秒是**常态**：
 *
 *   · 界面记一条 ERROR「失败: 命令超时」—— 任务其实还在跑；
 *   · `views/migration.ts:399` 紧接着 `app.setBusy(false)` 并跳到"迁移失败"页 ——
 *     **退出保护被解除**，此时退出会留下搬了一半的目录。
 *
 * 这个测试断言的核心只有一句：**超时 ≠ 失败 ≠ 取消；任务仍在进行，保护必须留着。**
 *
 * 用法：node tools/test-request-outcome.mjs
 */
import { importShared, createReporter } from "./esbuild-ts.mjs";

const { check, finish } = createReporter("请求超时的语义（缺陷 #4）");
const {
  classifyCommandResult,
  taskPhaseAfter,
  isTaskRunning,
  keepsQuitGuard,
  describeOutcome,
  SLOW_REQUEST_NOTICE_MS,
  REQUEST_HARD_DEADLINE_MS,
  parseJobEvent,
  isTerminalJobStatus,
  jobPhase,
  isJobFailure,
} = await importShared("src/shared/request-outcome.ts");

console.log("=== 分类 ===\n");
{
  check("正常返回 → ok", classifyCommandResult({ ok: true, data: {} }) === "ok");
  check(
    "超时（timedOut）→ timeout",
    classifyCommandResult({ ok: false, error: "命令超时", timedOut: true }) === "timeout",
  );
  check("后端报错 → failed", classifyCommandResult({ ok: false, error: "模型不存在" }) === "failed");
  check("后端未运行 → unavailable", classifyCommandResult({ ok: false, unavailable: true }) === "unavailable");
  check("无结果（null）→ failed", classifyCommandResult(null) === "failed");
  check("成功优先于超时标记", classifyCommandResult({ ok: true, timedOut: true }) === "ok");
}

console.log("\n=== 超时后任务仍在进行（核心断言）===\n");
{
  check("timeout 的任务阶段是 running", taskPhaseAfter("timeout") === "running", taskPhaseAfter("timeout"));
  check("timeout 绝不是 failed", taskPhaseAfter("timeout") !== "failed");
  check("timeout 也不是 unknown", taskPhaseAfter("timeout") !== "unknown");
  check("isTaskRunning(timeout) === true", isTaskRunning("timeout") === true);
  check("超时后必须保留退出保护", keepsQuitGuard("timeout") === true);

  check("ok → done", taskPhaseAfter("ok") === "done");
  check("ok 不需要退出保护", keepsQuitGuard("ok") === false);
  check("failed → 任务失败", taskPhaseAfter("failed") === "failed");
  check("失败后不再占用退出保护", keepsQuitGuard("failed") === false);
  check("请求没发出去（后端未运行）不算在进行中", isTaskRunning("unavailable") === false);
}

console.log("\n=== 文案不能说成失败 ===\n");
{
  const timedOut = describeOutcome("timeout", "start_migration");
  check("超时文案说明“尚未返回”", timedOut.includes("尚未返回"), timedOut);
  check("超时文案说明任务可能仍在进行", timedOut.includes("仍在进行"), timedOut);
  check("超时文案明确“不是失败、不代表已取消”", timedOut.includes("不是失败") && timedOut.includes("取消"), timedOut);
  check("超时文案与失败文案不同", timedOut !== describeOutcome("failed", "start_migration"));
  check(
    "失败文案仍明确说“失败”（没有把两者混成一句）",
    describeOutcome("failed", "start_migration") === "start_migration 失败",
  );
  check("后端未运行有单独的文案", describeOutcome("unavailable", "start").includes("后端未运行"));
}

console.log("\n=== 时限常量 ===\n");
{
  check("首次通知在 30 秒", SLOW_REQUEST_NOTICE_MS === 30_000, String(SLOW_REQUEST_NOTICE_MS));
  check(
    "硬性上限远大于首次通知（给长任务留出空间）",
    REQUEST_HARD_DEADLINE_MS > SLOW_REQUEST_NOTICE_MS * 10,
    String(REQUEST_HARD_DEADLINE_MS),
  );
  check("硬性上限是分钟级而不是秒级（多 GB 迁移要能跑完）", REQUEST_HARD_DEADLINE_MS >= 10 * 60_000);

  // 迁移/下载的典型时长远大于 30 秒 —— 这一条说明"30 秒当失败"必然误判
  const typicalMigrationMs = 5 * 60_000;
  check(
    "典型长任务时长（5 分钟）落在通知与上限之间",
    typicalMigrationMs > SLOW_REQUEST_NOTICE_MS && typicalMigrationMs < REQUEST_HARD_DEADLINE_MS,
  );
}

console.log("\n=== 后台任务事件（async 命令的后续事件与终态）===\n");
{
  const running = parseJobEvent({
    type: "job",
    jobId: "job-1",
    command: "start_migration",
    status: "running",
    sequence: 3,
  });
  check("进行中的 job 事件能解析", running !== null && running.jobId === "job-1");
  check("job 的进行中状态不是终态", isTerminalJobStatus(running.status) === false);
  check("job 进行中 → 任务阶段 running", jobPhase(running.status) === "running");
  check("job 进行中不算失败", isJobFailure(running.status) === false);
  check("sequence 被保留", running.sequence === 3);

  // 状态词汇表以后端状态机为准：终态是 succeeded / failed / cancelled
  // （不是自造的 done/error）。用错名字的后果是界面静默不显示结果。
  check("cancelling 仍是进行中（取消中 ≠ 已取消）",
    isTerminalJobStatus("cancelling") === false && jobPhase("cancelling") === "running");
  check("succeeded 是终态", isTerminalJobStatus("succeeded") === true);
  check("succeeded → 任务完成", jobPhase("succeeded") === "done");
  check("自造词 done 不再被当作终态", isTerminalJobStatus("done") === false);

  const failed = parseJobEvent({ jobId: "job-2", command: "install_model", status: "failed", error: "磁盘写满", code: "OSError" });
  check("failed 带出原因（error 字段）", failed.error === "磁盘写满", String(failed.error));
  check("failed 带出错误码", failed.code === "OSError", String(failed.code));
  check("failed 才算失败", isJobFailure("failed") === true && jobPhase("failed") === "failed");

  // 关键：取消**不是**失败（与"超时不是失败"同一条纪律）
  check("cancelled 是终态", isTerminalJobStatus("cancelled") === true);
  check("cancelled **不是**失败", isJobFailure("cancelled") === false);
  check("cancelled 不映射成 failed", jobPhase("cancelled") !== "failed", jobPhase("cancelled"));
  check("cancelled 也不映射成 done", jobPhase("cancelled") !== "done");

  check("字段不全的 job 事件返回 null（不猜）", parseJobEvent({ type: "job", status: "running" }) === null);
  check("status 缺失返回 null", parseJobEvent({ jobId: "x" }) === null);
  check("非对象返回 null", parseJobEvent(null) === null);

  // 与同步路径的一致性：async 受理之后，任务仍在进行 —— 界面不能显示成失败
  const acceptance = { ok: true, data: { jobId: "job-9", accepted: true, status: "running" } };
  check("async 受理是成功结果（不是超时也不是失败）", classifyCommandResult(acceptance) === "ok");
}

finish();
