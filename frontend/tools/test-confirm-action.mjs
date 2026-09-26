#!/usr/bin/env node
/**
 * 危险操作的确认闸门（ConfirmAction）—— 离线行为测试。
 *
 * ## 守的是什么
 *
 * 全应用只有两处破坏性操作，都靠同一句手写契约：**没确认就绝不能动**。
 *   · 诊断页「清除本机日志」→ clear_logs（截断活动日志 + 删轮转文件）
 *   · 迁移向导「清理已迁移的原目录」→ cleanup_migrated_source（删掉用户原目录，不可逆）
 *
 * 这个测试用假的 `ask` / `action` 把契约钉死：
 *   · 取消时 `action` **一次都不执行**（不是"执行了但结果被忽略"）；
 *   · 取消时 `onDecline` 一定执行（界面上要能说明"已取消"）；
 *   · 确认时 `onDecline` 不执行；
 *   · `ask` 只被调用一次（不会连弹两次对话框）；
 *   · `action` 抛出的错误照常向上传播 —— 闸门不吞异常，
 *     否则调用点的错误处理会静默失效。
 *
 * 最后一段把闸门接到**真实的** `buildCleanupRequest` 上：确认前不得构造出
 * 带 `confirm: true` 的清理请求，确认后才允许。
 *
 * 用法：node tools/test-confirm-action.mjs
 */
import { importShared, createReporter } from "./esbuild-ts.mjs";
import { createRequire } from "node:module";

const ts = createRequire(import.meta.url)("typescript");

function findNode(root, predicate) {
  let match = null;
  const visit = (node) => {
    if (match) return;
    if (predicate(node)) {
      match = node;
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(root);
  return match;
}

function findCall(root, name) {
  return findNode(root, (node) => ts.isCallExpression(node) &&
    ts.isIdentifier(node.expression) && node.expression.text === name);
}

function hasWindowConfirm(root) {
  return Boolean(findNode(root, (node) => ts.isCallExpression(node) &&
    ts.isPropertyAccessExpression(node.expression) &&
    ts.isIdentifier(node.expression.expression) && node.expression.expression.text === "window" &&
    node.expression.name.text === "confirm"));
}

const { check, finish } = createReporter("危险操作确认闸门（§3.7 ConfirmAction）");
const [confirmMod, cleanupMod] = await importShared(
  ["src/shared/confirm-action.ts", "src/shared/migration-cleanup.ts"],
);
const { runAfterConfirm } = confirmMod;
const { buildCleanupRequest, isCleanupRequest } = cleanupMod;

console.log("=== 确认才执行 ===\n");
{
  let asks = 0;
  let actions = 0;
  let declines = 0;
  const ran = runAfterConfirm(
    () => { asks += 1; return true; },
    () => { actions += 1; },
    () => { declines += 1; },
  );
  check("确认 → 返回 true", ran === true);
  check("确认 → action 执行一次", actions === 1, `${actions} 次`);
  check("确认 → onDecline 不执行", declines === 0, `${declines} 次`);
  check("ask 只被问一次", asks === 1, `${asks} 次`);
}

console.log("\n=== 取消绝不执行 ===\n");
{
  let asks = 0;
  let actions = 0;
  let declines = 0;
  const ran = runAfterConfirm(
    () => { asks += 1; return false; },
    () => { actions += 1; },
    () => { declines += 1; },
  );
  check("取消 → 返回 false", ran === false);
  check("取消 → action **一次都没执行**", actions === 0, `${actions} 次`);
  check("取消 → onDecline 执行一次（界面要说明已取消）", declines === 1, `${declines} 次`);
  check("ask 只被问一次", asks === 1, `${asks} 次`);

  // 省略 onDecline 也不能炸（诊断为早退，没有收尾动作）
  const minimal = runAfterConfirm(() => false, () => { actions += 1; });
  check("省略 onDecline 时安全", minimal === false && actions === 0);
}

console.log("\n=== 闸门不吞异常 ===\n");
{
  let thrown = null;
  try {
    runAfterConfirm(
      () => true,
      () => { throw new Error("后端拒绝"); },
    );
  } catch (error) {
    thrown = error;
  }
  check("action 抛错照常向上传播（调用点的错误处理不被吞）", thrown !== null && thrown.message === "后端拒绝", String(thrown));
}

console.log("\n=== 接到真实的清理请求上 ===\n");
{
  // 诊断页形状：确认后才会调用后端命令
  let clearCalls = 0;
  const clickClearLogs = (userSaysYes) => {
    runAfterConfirm(
      () => userSaysYes,
      () => { clearCalls += 1; },
    );
  };
  clickClearLogs(false);
  check("诊断页：取消时不发 clear_logs", clearCalls === 0, `${clearCalls} 次`);
  clickClearLogs(true);
  check("诊断页：确认后发出 clear_logs", clearCalls === 1, `${clearCalls} 次`);

  // 迁移页形状：确认才允许构造带 confirm:true 的请求
  const records = [{ key: "models", recordId: "rec-1" }];
  const migrationSite = (userSaysYes) => {
    let decision = null;
    runAfterConfirm(
      () => userSaysYes,
      () => { decision = buildCleanupRequest(records, true); },
      () => { decision = buildCleanupRequest(records, false); },
    );
    return decision;
  };

  const cancelled = migrationSite(false);
  check("迁移页：取消 → 不是清理请求", !isCleanupRequest(cancelled), JSON.stringify(cancelled));
  check("迁移页：取消 → 跳过原因是缺确认", cancelled.skip === true && cancelled.reason === "no_user_confirm", JSON.stringify(cancelled));
  check("迁移页：取消 → 请求里没有 record_ids（不会误删）", !("record_ids" in cancelled));

  const confirmed = migrationSite(true);
  check("迁移页：确认 → 是清理请求", isCleanupRequest(confirmed) === true);
  check("迁移页：确认 → 显式带 confirm: true", confirmed.confirm === true);
  check("迁移页：确认 → 用记录 ID 而不是路径", confirmed.record_ids.join(",") === "rec-1" && !JSON.stringify(confirmed).includes("path"));
}

console.log("\n=== 组件边界 ===\n");
{
  const { readFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  const { ROOT } = await import("./esbuild-ts.mjs");
  const raw = readFileSync(join(ROOT, "src/shared/confirm-action.ts"), "utf-8");
  // 去掉注释再查：注释里为了说明用法会提到 window.confirm，那不是依赖
  const code = raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  check("不依赖 DOM / window", !code.includes("document") && !code.includes("window."), code.trim().slice(0, 80));
  check("不导入业务页面与 store", !code.includes("views/") && !code.includes("../store") && !code.includes("../i18n"));

  // 两个真实调用点都在源码里走闸门（防止有人改回手写 if (!confirm) return）
  const diag = readFileSync(join(ROOT, "src/renderer/views/diagnostics.ts"), "utf-8");
  const mig = readFileSync(join(ROOT, "src/renderer/views/migration.ts"), "utf-8");
  check("诊断页：清除日志走闸门", diag.includes("runAfterConfirm("));
  check("诊断页：确认之后才调用清除函数", /runAfterConfirm\([\s\S]{0,200}clearLocalLogs\(\)/.test(diag));
  const migAst = ts.createSourceFile("migration.ts", mig, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const requestCleanup = findNode(migAst, (node) => ts.isFunctionDeclaration(node) &&
    node.name?.text === "requestCleanup");
  const confirmGate = requestCleanup ? findCall(requestCleanup, "runAfterConfirm") : null;
  const cleanupAction = confirmGate?.arguments[1];
  const cleanupCall = cleanupAction ? findCall(cleanupAction, "cleanupMigratedSources") : null;
  check("迁移页：清理原目录走闸门", confirmGate !== null);
  check("迁移页：确认回调才进入 cleanupMigratedSources", Boolean(cleanupAction && cleanupCall &&
    ts.isArrowFunction(cleanupAction) && cleanupCall.arguments[0]?.kind === ts.SyntaxKind.Identifier &&
    cleanupCall.arguments[0].text === "records"));
  check("迁移页：弹出的确认位于闸门 ask 回调", Boolean(confirmGate?.arguments[0] &&
    ts.isArrowFunction(confirmGate.arguments[0]) && hasWindowConfirm(confirmGate.arguments[0])));
  // window.confirm 只应出现在这两个闸门的 ask 里
  const confirms = [...diag.matchAll(/window\.confirm\(/g)].length + [...mig.matchAll(/window\.confirm\(/g)].length;
  check("两处确认弹窗都只剩闸门里的一次调用", confirms === 2, `共 ${confirms} 处 window.confirm`);
}

finish();
