#!/usr/bin/env node
/**
 * 清理请求构造（src/shared/migration-cleanup.ts）—— 新后端契约的离线守护。
 *
 * ## 守的是什么
 *
 * `cleanup_migrated_source` 的入参契约变了：
 *   · **不再接受 `path`** → 传 path 会得到 `{deleted:false, code:"path_not_accepted"}`；
 *   · 只接受 `record_id` / `record_ids`，且**必须显式带 `confirm: true`**
 *     （缺确认 → detail 含「缺少用户确认」，缺 ID → `missing_record_id`）；
 *   · 返回值新增 `paths` / `cleaned` / `refused` / `ok` / `detail`，
 *     `detail` 是可直接展示的中文原因。
 *
 * 老的调用方式（拼路径去清理）会**永远失败**，而用户只会看到一句看不懂的错。
 * 这个测试把"必须用 recordId、必须带 confirm、detail 必须被展示"钉住。
 *
 * 用法：node tools/test-migration-cleanup.mjs
 */
import { importShared, createReporter } from "./esbuild-ts.mjs";

const { check, finish } = createReporter("清理请求契约（cleanup_migrated_source）");
const { buildCleanupRequest, cleanupNotice, cleanupSucceeded, isCleanupRequest } = await importShared(
  "src/shared/migration-cleanup.ts",
);

console.log("=== 入参形状 ===\n");
{
  const decision = buildCleanupRequest([{ recordId: "rec-1" }, { recordId: "rec-2" }], true);
  check("是请求而不是跳过", isCleanupRequest(decision) === true);
  check("用 record_ids 而不是 path", "record_ids" in decision && !("path" in decision), JSON.stringify(decision));
  check("IDs 原样带上", decision.record_ids.join(",") === "rec-1,rec-2");
  check("confirm 恒为 true", decision.confirm === true);
  check("参数里没有 path 字段（后端已明确拒绝）", !JSON.stringify(decision).includes("path"));
}

console.log("\n=== 没有确认就不删 ===\n");
{
  const decision = buildCleanupRequest([{ recordId: "rec-1" }], false);
  check("未确认 → 跳过", !isCleanupRequest(decision) && decision.skip === true);
  check("原因是缺用户确认", decision.reason === "no_user_confirm", decision.reason);
  // 反向对照：确认之后才允许构造请求（漏传 confirm 会被后端拒绝，且这里就不发）
  check("确认之后才构造请求", isCleanupRequest(buildCleanupRequest([{ recordId: "rec-1" }], true)));
}

console.log("\n=== 没有记录 ID 就不发请求 ===\n");
{
  for (const [label, records] of [
    ["空数组", []],
    ["全都没有 recordId", [{ key: "models" }, { key: "cache" }]],
    ["recordId 是空串", [{ recordId: "" }]],
    ["recordId 是空白", [{ recordId: "   " }]],
    ["recordId 不是字符串", [{ recordId: 42 }]],
  ]) {
    const decision = buildCleanupRequest(records, true);
    check(
      `${label} → 跳过（reason=no_record_id）`,
      !isCleanupRequest(decision) && decision.reason === "no_record_id",
      JSON.stringify(decision),
    );
  }
  // 关键：绝不"退而求其次"用路径
  check(
    "带路径的旧写法不会被当成记录 ID",
    (() => {
      const decision = buildCleanupRequest([{ path: "D:/old/models" }], true);
      return !isCleanupRequest(decision) && !JSON.stringify(decision).includes("old/models");
    })(),
  );
}

console.log("\n=== 去重与混合 ===\n");
{
  const decision = buildCleanupRequest(
    [{ recordId: "rec-1" }, { key: "no-id" }, { recordId: "rec-1" }, { recordId: "rec-2" }],
    true,
  );
  check("重复 ID 被去掉", decision.record_ids.join(",") === "rec-1,rec-2", decision.record_ids.join(","));
  check("没有 ID 的项被忽略而不是造一个", decision.record_ids.length === 2);
}

console.log("\n=== 结果说明必须展示后端的 detail ===\n");
{
  const rejected = { deleted: false, code: "path_not_accepted", detail: "不接受 path，请改用 record_id" };
  check("path_not_accepted 的 detail 被原样展示", cleanupNotice(rejected) === rejected.detail);
  check("该结果不算成功", cleanupSucceeded(rejected) === false);

  const missingConfirm = { deleted: false, code: "missing_confirm", detail: "缺少用户确认，未删除任何目录" };
  check("缺确认的 detail 含「用户确认」且被展示", cleanupNotice(missingConfirm).includes("用户确认"));
  check("缺确认不算成功", cleanupSucceeded(missingConfirm) === false);

  const missingId = { deleted: false, code: "missing_record_id", detail: "缺少 record_id" };
  check("缺 ID 的原因被展示", cleanupNotice(missingId).includes("record_id"));

  const okResult = { ok: true, deleted: true, cleaned: ["rec-1", "rec-2"], refused: [] };
  check("成功时给出已清理数量", cleanupNotice(okResult).includes("已清理 2"), cleanupNotice(okResult));
  check("成功被识别", cleanupSucceeded(okResult) === true);

  const partial = { ok: true, cleaned: ["rec-1"], refused: [{ recordId: "rec-2" }] };
  check("部分被拒时说明被拒数量", cleanupNotice(partial).includes("1 项被拒绝"), cleanupNotice(partial));

  check("拿不到结果时不谎报成功", cleanupNotice(null) === "清理未返回结果" && cleanupSucceeded(null) === false);
}

finish();
