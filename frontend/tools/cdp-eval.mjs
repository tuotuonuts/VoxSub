/**
 * 通过 CDP 在渲染进程里执行 JS，用于诊断"界面没渲染出数据"这类问题。
 *
 * 为什么需要它：截图只能看到"结果为空"，看不到原因（数据没到 / 渲染抛错 /
 * 元素被布局压成 0 高度）。真实 DOM 状态 + 控制台报错才是证据。
 *
 * 用法：node tools/cdp-eval.mjs "<js 表达式>" [页面标题关键字]
 */
const expression = process.argv[2] ?? "document.title";
const titleFilter = process.argv[3] ?? "语幕";

const list = await (await fetch("http://127.0.0.1:9222/json/list")).json();
const page = list.find((t) => t.type === "page" && (t.title ?? "").includes(titleFilter));
if (!page) {
  console.error(`未找到标题包含「${titleFilter}」的页面。现有：`);
  for (const t of list) console.error(`  ${t.type}  ${t.title}`);
  process.exit(1);
}

const socket = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map();
let nextId = 1;

function send(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

socket.addEventListener("message", (event) => {
  const payload = JSON.parse(event.data);
  if (payload.id === undefined) return;
  const slot = pending.get(payload.id);
  if (!slot) return;
  pending.delete(payload.id);
  if (payload.error) slot.reject(new Error(JSON.stringify(payload.error)));
  else slot.resolve(payload.result);
});

await new Promise((resolve) => socket.addEventListener("open", resolve));

try {
  const result = await send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (result.exceptionDetails) {
    console.error("执行出错:", result.exceptionDetails.text);
    const desc = result.exceptionDetails.exception?.description;
    if (desc) console.error(desc);
    process.exitCode = 1;
  } else {
    console.log(JSON.stringify(result.result.value, null, 2));
  }
} finally {
  socket.close();
}
