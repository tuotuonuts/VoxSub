#!/usr/bin/env node
/**
 * 浮窗实时/事实显示当前句子的行为测试。
 *
 * 覆盖用户需求：
 *   "2. 浮窗应该事实显示当前的句子"
 *
 * 核心断言：
 *   1. 初始状态显示 "等待识别…"；
 *   2. 收到 partial 时，实时呈现正在说的当前句原文；
 *   3. 收到 draft 时，实时呈现当前句原文与草稿译文；
 *   4. 收到 utterance（定稿）时，**事实保留显示该最新定稿句子**，绝不在定稿后闪退变白！
 *   5. 新一句话开始（收到下一句的 partial）时，立即无缝过渡到新一句；
 *   6. 收到 session start 时重置。
 */
const PORT = 9222;

const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const overlay = targets.find((t) => (t.title ?? "").includes("浮窗"));
if (!overlay) {
  console.error("找不到浮窗（应用需以 --debug 启动）");
  process.exit(1);
}

const socket = new WebSocket(overlay.webSocketDebuggerUrl);
let id = 1;
const pending = new Map();
socket.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  const r = pending.get(m.id);
  if (r) {
    pending.delete(m.id);
    m.error ? r.reject(new Error(JSON.stringify(m.error))) : r.resolve(m.result);
  }
});
await new Promise((r) => socket.addEventListener("open", r));
const send = (method, params) =>
  new Promise((resolve, reject) => {
    const mid = id++;
    pending.set(mid, { resolve, reject });
    socket.send(JSON.stringify({ id: mid, method, params }));
  });

const ev = async (expr) => {
  const res = await send("Runtime.evaluate", {
    expression: expr,
    awaitPromise: true,
    returnByValue: true,
  });
  if (res.exceptionDetails) {
    throw new Error(res.exceptionDetails.exception?.description ?? "eval 失败");
  }
  return res.result.value;
};

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

const readDom = async () => {
  return ev(`(() => {
    return {
      src: document.getElementById('src')?.textContent ?? '',
      dst: document.getElementById('dst')?.textContent ?? '',
      state: window.__overlayState ? window.__overlayState() : null,
    };
  })()`);
};

const feed = async (event) => {
  return ev(`(() => {
    if (typeof window.__feedOverlayEvent === 'function') {
      window.__feedOverlayEvent(${JSON.stringify(event)});
      return true;
    }
    return false;
  })()`);
};

console.log("=== 浮窗当前句子显示行为测试 ===\n");

// 1. 初始化会话重置
await feed({ type: "session", action: "start" });
const initial = await readDom();
check("初始状态显示等待识别", initial.src === "等待识别…" && initial.dst === "", JSON.stringify(initial));

// 2. 模拟正在说第一句话：收到 partial
await feed({ type: "partial", text: "こんにちは" });
const partial1 = await readDom();
check("正在说第一句时实时显示 partial", partial1.src === "こんにちは", JSON.stringify(partial1));

// 3. 收到 draft：实时更新草稿与翻译
await feed({ type: "draft", source: "こんにちは、元気ですか", translation: "你好，你好吗" });
const draft1 = await readDom();
check("草稿到达时实时显示原文", draft1.src === "こんにちは、元気ですか", draft1.src);
check("草稿到达时实时显示译文", draft1.dst === "你好，你好吗", draft1.dst);

// 4. 定稿 utterance：关键回归！定稿后必须保留显示当前句子，绝不能消失变回"等待识别…"！
await feed({
  type: "utterance",
  source: "こんにちは、元気ですか？",
  translation: "你好，你好吗？",
});
const settled1 = await readDom();
check("定稿后事实保留显示当前句子原文", settled1.src === "こんにちは、元気ですか？", settled1.src);
check("定稿后事实保留显示当前句子译文", settled1.dst === "你好，你好吗？", settled1.dst);
check("定稿后绝不闪退变回「等待识别…」", settled1.src !== "等待识别…", settled1.src);

// 5. 第二句话开始说：收到新 partial，无缝过渡到新一句
await feed({ type: "partial", text: "会議は" });
const partial2 = await readDom();
check("新一句话开始时实时切换为新句子原文", partial2.src === "会議は", partial2.src);

// 6. 第二句话定稿
await feed({
  type: "utterance",
  source: "会議は三時から始まります。",
  translation: "会议从三点开始。",
});
const settled2 = await readDom();
check("第二句定稿后事实保留显示第二句原文", settled2.src === "会議は三時から始まります。", settled2.src);
check("第二句定稿后事实保留显示第二句译文", settled2.dst === "会议从三点开始。", settled2.dst);

// 7. 新会话开启：恢复干净状态
await feed({ type: "session", action: "start" });
const resetState = await readDom();
check("新会话开启时重置状态", resetState.src === "等待识别…" && resetState.dst === "", JSON.stringify(resetState));

socket.close();
const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
if (failed.length) {
  console.log("\n失败项：");
  for (const f of failed) console.log(`  ${f.name}`);
}
process.exit(failed.length ? 1 : 0);
