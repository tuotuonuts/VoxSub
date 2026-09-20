#!/usr/bin/env node
/**
 * 后端连接状态（src/shared/backend-status.ts）—— 缺陷 #11 的离线守护。
 *
 * ## 守的是什么
 *
 * 后端进程退出时，主进程原来只发一条 `{type:"status", text:"后端已退出（code=…）"}`，
 * 渲染层把它写进 statusText 就完事 —— **状态灯还亮着"运行中"，主按钮还写着"结束"**。
 * 用户看到"后端已退出"却仍以为会话在跑，点什么都只会得到"后端未运行"。
 *
 * 这个测试断言：
 *   · 断连是一个**独立状态**（disconnected），与"启动失败"分开；
 *   · 断连 / 失败时，会话视图一律归零 —— 绝不显示"运行中"；
 *   · 断连后能恢复（ready 可再次进入）；
 *   · 渲染层重载后需要补拉一次状态（needsResync），且模式要被一起恢复（normalizeMode）。
 *
 * 用法：node tools/test-backend-status.mjs
 */
import { importShared, createReporter } from "./esbuild-ts.mjs";

const { check, finish } = createReporter("后端连接状态（缺陷 #11）");
const {
  INITIAL_BACKEND_STATUS,
  reduceBackendStatus,
  sessionViewFor,
  needsResync,
  normalizeMode,
  backendNotice,
  parseReadyPayload,
} = await importShared("src/shared/backend-status.ts");

console.log("=== 状态迁移 ===\n");
{
  const ready = reduceBackendStatus(INITIAL_BACKEND_STATUS, { type: "ready" });
  check("初始态是 connecting", INITIAL_BACKEND_STATUS.phase === "connecting", INITIAL_BACKEND_STATUS.phase);
  check("ready → phase=ready 且无异常原因", ready.phase === "ready" && ready.reason === null);

  const down = reduceBackendStatus(ready, { type: "disconnected", reason: "code=1" });
  check("ready → disconnected 是独立状态", down.phase === "disconnected", down.phase);
  check("断连原因被保留", down.reason === "code=1", String(down.reason));
  check("断连态不等于启动失败态", down.phase !== "failed");

  const failed = reduceBackendStatus(INITIAL_BACKEND_STATUS, { type: "failed", reason: "找不到解释器" });
  check("启动失败 → phase=failed", failed.phase === "failed" && failed.reason === "找不到解释器");

  const recovered = reduceBackendStatus(down, { type: "ready" });
  check("disconnected → ready 可恢复（重新拉起后端）", recovered.phase === "ready" && recovered.reason === null);

  const downAgain = reduceBackendStatus(down, { type: "disconnected" });
  check("重复断连且未给新原因时保留原原因", downAgain.reason === "code=1", String(downAgain.reason));
  check("重复断连仍是 disconnected", downAgain.phase === "disconnected");

  const unknown = reduceBackendStatus(ready, { type: "weird" });
  check("未知事件不改变状态", unknown === ready);
}

console.log("\n=== 断连后不得显示“运行中” ===\n");
{
  const commanded = { running: true, paused: false };
  const view = sessionViewFor("disconnected", commanded);
  check("disconnected + 命令态 running → 界面 running=false", view.running === false, JSON.stringify(view));
  check("disconnected → paused 也归零", view.paused === false);
  check("failed → 同样不得显示运行中", sessionViewFor("failed", commanded).running === false);

  // 反向对照：只改文案而不改状态，界面就还是"运行中" —— 这正是被修掉的旧行为
  const legacyView = { ...commanded, statusText: backendNotice(reduceBackendStatus(INITIAL_BACKEND_STATUS, { type: "disconnected" })) };
  check(
    "旧行为（只改提示文案）仍显示运行中 —— 对照用",
    legacyView.running === true,
    "说明缺陷 #11 的核心不是文案",
  );

  check("ready 时命令态原样透传", sessionViewFor("ready", commanded).running === true);
  check("ready 时暂停态原样透传", sessionViewFor("ready", { running: true, paused: true }).paused === true);
  // 兼容既有的自动化入口 window.__applySessionState（它在 ready 之前就会被调用）
  check("connecting 时命令态原样透传（启动期间的乐观显示）", sessionViewFor("connecting", commanded).running === true);
}

console.log("\n=== 重载后的重新同步 ===\n");
{
  check("ready 需要补拉状态", needsResync("ready") === true);
  check("connecting 不补拉（后端还没起来）", needsResync("connecting") === false);
  check("disconnected 不补拉（没有后端可问）", needsResync("disconnected") === false);
  check("failed 不补拉", needsResync("failed") === false);

  check("合法模式 'b' 被接受", normalizeMode("b") === "b");
  check("合法模式 'd' 被接受", normalizeMode("d") === "d");
  for (const bad of ["z", "", null, undefined, 5, {}, true]) {
    check(
      `模式 ${JSON.stringify(bad)} 认不出 → null（保持原样，不猜默认值）`,
      normalizeMode(bad) === null,
    );
  }
}

console.log("\n=== 提示文案 ===\n");
{
  const down = backendNotice({ phase: "disconnected", reason: "code=1" });
  check("断连文案说明是“已退出”", down.includes("已退出"), down);
  check("断连文案带原因", down.includes("code=1"), down);
  check("启动失败文案说明是“启动失败”", backendNotice({ phase: "failed", reason: "x" }).includes("启动失败"));
  check("connecting 文案是“正在连接”", backendNotice(INITIAL_BACKEND_STATUS).includes("正在连接"));
  check("ready 文案不报错", backendNotice({ phase: "ready", reason: null }) === "后端已连接");
}

console.log("\n=== ready 握手：新增字段不得导致解析失败 ===\n");
{
  // 老后端（只有 version）
  const legacy = parseReadyPayload({ type: "ready", version: "0.9.0-beta" });
  check("老式 ready 仍能解析", legacy !== null && legacy.version === "0.9.0-beta");
  check("老式 ready 的新字段为 null", legacy.protocolVersion === null && legacy.activeJobs === null);

  // 新后端：protocolVersion / backendGeneration / readiness / session
  const modern = parseReadyPayload({
    type: "ready",
    version: "0.9.0-beta",
    protocolVersion: 2,
    backendGeneration: "gen-7",
    readiness: { ready: true, activeJobs: 2 },
    session: { running: true, paused: false, mode: "b" },
    // 后端以后还会加字段：必须被忽略而不是报错
    somethingBrandNew: { nested: true },
  });
  check("新式 ready 解析成功（多出来的字段被忽略）", modern !== null);
  check("protocolVersion 被读到", modern.protocolVersion === "2", String(modern.protocolVersion));
  check("backendGeneration 被读到", modern.backendGeneration === "gen-7");
  check("readiness.activeJobs 被读到", modern.activeJobs === 2, String(modern.activeJobs));
  check(
    "session 被读到（重载后不必再等一次 state 命令）",
    modern.session !== null && modern.session.running === true && modern.session.mode === "b",
  );

  const weird = parseReadyPayload({
    type: "ready",
    version: "x",
    protocolVersion: {},
    readiness: { activeJobs: "many" },
    session: { running: "yes", paused: false },
  });
  check("非法的新字段退化为 null 而不是抛错", weird !== null && weird.protocolVersion === null && weird.activeJobs === null);
  check("载荷形状不对时 session 为 null", weird.session === null);
  check("非对象载荷返回 null", parseReadyPayload(null) === null && parseReadyPayload("ready") === null);
  check("type 不是 ready 时返回 null", parseReadyPayload({ type: "status", text: "x" }) === null);
}

finish();
