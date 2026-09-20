#!/usr/bin/env node
/**
 * 字幕时间轴的会话事件归约（src/shared/session-timeline.ts）—— 缺陷 #2 的离线守护。
 *
 * ## 守的是什么
 *
 * 用户报告的形态：**会话一停，字幕全没了，导出什么也导不出。**
 * 根因不在导出按钮，而在 store 收到 `{"event":"session","action":"stop"}` 时
 * 无视 action、一律 `subtitles: []`；接着 `exportSession()` 因为字幕为空直接
 * return，表现成"点了导出没反应"。
 *
 * 这个测试用**真实事件序列**（start → utterance×N → stop）断言：
 *   · stop 之后字幕**必须还在**（数量、内容、时间戳都不变）；
 *   · 导出前置条件 `hasExportableSubtitles` 为真；
 *   · start（以及只有 start）才重置；
 *   · 认不出的 action **不得**清空字幕（方向必须是"宁可少做，不可误删"）。
 *
 * 用法：node tools/test-session-timeline.mjs
 */
import { importShared, createReporter } from "./esbuild-ts.mjs";

const { check, finish } = createReporter("会话事件 → 字幕时间轴（缺陷 #2）");
const { applySessionEvent, reduceSessionEvent, normalizeSessionAction, hasExportableSubtitles } =
  await importShared("src/shared/session-timeline.ts");

/** 模拟 store：订阅者、事件序列都按真实顺序走。 */
function simulate(events, { addUtterances = 0 } = {}) {
  let state = { subtitles: [], sessionStartedAt: null, draft: null };
  let clock = 1000;
  for (const event of events) {
    state = applySessionEvent(state, event.action, clock);
    if (event.action === "start") {
      for (let i = 0; i < addUtterances; i += 1) {
        clock += 500;
        state = {
          ...state,
          subtitles: [
            ...state.subtitles,
            { source: `原文${i + 1}`, translation: `译文${i + 1}`, tsMs: i * 500 },
          ],
        };
      }
      state = { ...state, draft: null };
    }
  }
  return state;
}

console.log("=== start 才重置 ===\n");
{
  const patch = reduceSessionEvent("start", 4200);
  check("start 重置字幕为空", Array.isArray(patch.subtitles) && patch.subtitles.length === 0);
  check("start 重置时间基准", patch.sessionStartedAt === 4200, String(patch.sessionStartedAt));
  check("start 丢弃草稿", patch.draft === null);

  // 上一场的字幕必须被清掉，导出时间轴才从零算起
  const before = { subtitles: [{ source: "旧", translation: "old", tsMs: 0 }], sessionStartedAt: 1, draft: null };
  const after = applySessionEvent(before, "start", 9000);
  check("start 清掉上一场字幕", after.subtitles.length === 0);
  check("start 后时间基准为新时刻", after.sessionStartedAt === 9000);
}

console.log("\n=== stop 保留字幕（缺陷 #2 的核心）===\n");
{
  const patch = reduceSessionEvent("stop", 7000);
  check("stop 不产生 subtitles 键（不动字幕）", !("subtitles" in patch), JSON.stringify(patch));
  check("stop 只清草稿", patch.draft === null && !("sessionStartedAt" in patch));

  // 真实序列：开始 → 3 句 → 停止
  const state = simulate([{ action: "start" }, { action: "stop" }], { addUtterances: 3 });
  check("停止后字幕仍在（3 句）", state.subtitles.length === 3, `实际 ${state.subtitles.length} 句`);
  check("停止后字幕内容不变", state.subtitles.map((l) => l.source).join(",") === "原文1,原文2,原文3");
  check("停止后时间戳不变", state.subtitles.map((l) => l.tsMs).join(",") === "0,500,1000");
  check("停止后仍可导出", hasExportableSubtitles(state) === true);

  // 用户能看到的最终结果：导出的行数与字幕数一致（非空）
  const exportLines = state.subtitles.map((line, index, all) => {
    const next = all[index + 1];
    return {
      source: line.source,
      translation: line.translation,
      startMs: line.tsMs,
      endMs: next ? Math.max(line.tsMs + 400, next.tsMs) : line.tsMs + 3000,
    };
  });
  check("导出内容非空", exportLines.length === 3, `${exportLines.length} 行`);
  check("每行都有起止时间", exportLines.every((l) => l.endMs > l.startMs));

  // 反向对照：旧行为（stop 也清空）会让导出变成 0 行 —— 这就是被修掉的那个 bug
  const legacyState = { ...state, subtitles: [] };
  check(
    "旧行为（stop 也清空）确实会让导出为空",
    hasExportableSubtitles(legacyState) === false,
    "对照用，说明修的是真问题",
  );
}

console.log("\n=== 认不出的 action 不得清空 ===\n");
{
  for (const action of [undefined, null, "pause", "paused", "STOP", "", 0, 123, {}]) {
    const patch = reduceSessionEvent(action, 5000);
    check(
      `action=${JSON.stringify(action)} 时不清空字幕`,
      !("subtitles" in patch),
      JSON.stringify(patch),
    );
  }
  check("normalizeSessionAction('start') === 'start'", normalizeSessionAction("start") === "start");
  for (const bad of [undefined, "Start", "resume", ""]) {
    check(
      `normalizeSessionAction(${JSON.stringify(bad)}) 归一到 'stop'（保守方向）`,
      normalizeSessionAction(bad) === "stop",
    );
  }
}

console.log("\n=== 边界 ===\n");
{
  const state = simulate([{ action: "start" }, { action: "stop" }, { action: "stop" }], {
    addUtterances: 1,
  });
  check("重复 stop 幂等（字幕不被二次处理）", state.subtitles.length === 1);

  check("空字幕不可导出", hasExportableSubtitles({ subtitles: [] }) === false);

  // 会话期间草稿不影响已确认字幕
  let live = { subtitles: [], sessionStartedAt: 100, draft: { source: "半句", translation: "" } };
  live = applySessionEvent(live, "stop", 200);
  check("stop 丢弃未确认的草稿行", live.draft === null);
}

finish();
