/**
 * 会话事件 → 字幕时间轴补丁 —— 纯逻辑，渲染层与单测共用。
 *
 * ## 为什么要单独成模块
 *
 * `store.ts` 在**导入时**就会读 `window.matchMedia`（主题初值），在 node 里没法
 * 直接 import 它。把"事件 → 状态补丁"这一段抽成纯函数，就能离线验证
 * （见 `tools/test-session-timeline.mjs`）—— 与 `shared/log-levels.ts` 同一模式。
 *
 * ## 缺陷背景（工作单 #2：会话停止不能清空字幕）
 *
 * `store.ts` 的 `case "session"` 原先**忽略 `event.action`**，任何 session 事件都
 * 执行 `subtitles: []`。而停止会话时后端恰好会发一条
 * `{"event":"session","action":"stop"}`（见 frontend/backend/ipc_server.py:511），
 * 于是用户一点"结束"，整场字幕被清空；接着点"导出会话"什么也导不出来 ——
 * 因为 `workspace.ts` 在 `subtitles.length === 0` 时直接 return，
 * 表现出来就是"点了导出没反应"，用户在会话里攒的内容全没了。
 *
 * 正确的口径：**只有 start 才重置字幕与时间基准；stop 必须保留**。
 */

/** 后端会话事件里的动作。类型来源：protocol.ts 的 `{ type: "session"; action: "start" | "stop" }`。 */
export type SessionAction = "start" | "stop";

export interface SubtitleLineRecord {
  source: string;
  translation: string;
  /** 相对会话开始的毫秒数（导出 SRT/VTT 的时间轴用）。 */
  tsMs: number;
  endMs?: number;
}

export interface TimelineState {
  subtitles: SubtitleLineRecord[];
  /** 会话开始的单调时刻（performance.now()）；null 表示还没开始过。 */
  sessionStartedAt: number | null;
  draft: { source: string; translation: string } | null;
}

export type TimelinePatch = Partial<TimelineState>;

/**
 * 归一化会话动作。
 *
 * **认不出的动作一律按 `stop` 处理** —— 方向是刻意选的：把未知动作当 start 会
 * 静默清掉用户的字幕（就是缺陷 #2 那个后果），当 stop 最多是"该重置的没重置"，
 * 用户再点一次开始即可。宁可少做，不可误删。
 */
export function normalizeSessionAction(action: unknown): SessionAction {
  return action === "start" ? "start" : "stop";
}

/**
 * 会话事件产生的状态补丁。
 *
 * - `start`：时间基准归零 + 清空上一场字幕与草稿（新一场从零算起，导出时间轴才对得上）
 * - `stop`：**保留 subtitles**，只丢弃未完成的草稿行（草稿不属于已确认字幕）
 */
export function reduceSessionEvent(action: unknown, now: number): TimelinePatch {
  if (normalizeSessionAction(action) === "start") {
    return { sessionStartedAt: now, subtitles: [], draft: null };
  }
  return { draft: null };
}

/** 把补丁应用到时间轴上（store 用 patch 合并，单测用这个算子模拟同一过程）。 */
export function applySessionEvent<T extends TimelineState>(
  state: T,
  action: unknown,
  now: number,
): T {
  return { ...state, ...reduceSessionEvent(action, now) };
}

/**
 * 有没有可导出的内容。
 *
 * `workspace.ts` 的 `exportSession()` 用这个作为前置条件；把它提出来是为了让
 * "停止后还能不能导出"这一条能被离线断言（而不是只看代码里那行 return）。
 */
export function hasExportableSubtitles(state: { subtitles: readonly unknown[] }): boolean {
  return state.subtitles.length > 0;
}
