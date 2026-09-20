/**
 * 旧版目录清理的请求构造 —— 纯逻辑，渲染层与单测共用。
 *
 * ## 后端契约（已变更，渲染层必须配合）
 *
 * `cleanup_migrated_source` **不再接受 `path`**：
 *
 *   · 入参是 `record_id`（单条）或 `record_ids`（数组），并且**必须显式**带
 *     `confirm: true`（删除不可逆，缺确认要能被后端拒绝）；
 *   · 传 `path` → 返回 `{deleted: false, code: "path_not_accepted", detail: "…"}`；
 *   · 缺 record_id → `code: "missing_record_id"`；
 *   · 缺 confirm → `detail` 里含「缺少用户确认」；
 *   · 返回值新增 `paths` / `cleaned` / `refused` / `ok` / `detail`，
 *     `detail` 是**可直接展示给用户的中文原因**。
 *
 * 渲染层原先根本没有调用点（`CMD.cleanupMigratedSource` 只定义没使用），
 * 所以这里的重点是把契约写对，并让"用 path 调用""忘了 confirm"这类改错
 * **在单测里就被拦住** —— 否则表现就是清理永远失败，而用户看到的只是一句
 * 看不懂的后端错误。
 */
import type { CleanupResult } from "../renderer/protocol";

/** 清理请求：新契约的两种入参形状。 */
export interface CleanupRequest {
  record_ids: string[];
  confirm: true;
}

export type CleanupSkipReason =
  /** 用户还没确认（删除不可逆，绝不默认确认） */
  | "no_user_confirm"
  /** 没有可清理的记录 ID（后端只认 record_id，不再认 path） */
  | "no_record_id";

export interface CleanupSkip {
  skip: true;
  reason: CleanupSkipReason;
}

export type CleanupDecision = CleanupRequest | CleanupSkip;

export function isCleanupRequest(decision: CleanupDecision): decision is CleanupRequest {
  return !("skip" in decision);
}

/**
 * 依据迁移结果构造清理请求。
 *
 * 只从 `done[].recordId` 取材 —— 路径不是标识（后端已明确拒绝 `path`），
 * 而且同一路径在不同记录下可能重复。
 */
export function buildCleanupRequest(
  records: ReadonlyArray<{ recordId?: string | undefined }>,
  confirmed: boolean,
): CleanupDecision {
  if (!confirmed) return { skip: true, reason: "no_user_confirm" };
  const ids = records
    .map((record) => record.recordId)
    .filter((id): id is string => typeof id === "string" && id.trim() !== "");
  if (ids.length === 0) return { skip: true, reason: "no_record_id" };
  // 去重：同一个记录被选两次会得到一次"无此记录"的拒绝，没必要
  return { record_ids: [...new Set(ids)], confirm: true };
}

/** 清理结果的用户可见说明。`detail` 是后端给的中文原因，**必须原样展示**。 */
export function cleanupNotice(result: CleanupResult | null | undefined): string {
  if (!result) return "清理未返回结果";
  if (typeof result.detail === "string" && result.detail.trim() !== "") return result.detail;
  const cleaned = result.cleaned?.length ?? 0;
  const refused = result.refused?.length ?? 0;
  if (result.ok === false || result.deleted === false) {
    return refused > 0 ? `清理被拒绝：${refused} 项` : "清理失败（后端未给出原因）";
  }
  return `已清理 ${cleaned} 项${refused > 0 ? `，${refused} 项被拒绝` : ""}`;
}

/** 清理是否成功（供界面决定要不要继续保留"清理"入口）。 */
export function cleanupSucceeded(result: CleanupResult | null | undefined): boolean {
  if (!result) return false;
  if (result.ok === false) return false;
  if (result.deleted === false) return false;
  return (result.cleaned?.length ?? 0) > 0 || result.deleted === true;
}
