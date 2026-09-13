/**
 * 日志行的解析与格式化工具 —— 纯函数，主进程与渲染层共用。
 *
 * 为什么要共享：两条日志通路都需要"从一行文本里认出级别"——
 *   · 主进程：把后端 stderr 转发成日志事件时
 *   · 渲染层：读磁盘日志文件（历史运行）逐行渲染时
 * 各写一份必然漂移。而这里的判错会让**正常日志显示成错误**：
 *   · 主进程原先写死 `level: "error"`，于是每条 INFO 都成了错误（用户报的）；
 *   · 渲染层原先用 `/(ERROR|CRITICAL)/.test(line)` 匹配**整行**，于是正文里
 *     提到 "ERROR" 的 INFO 行（例如"日志级别设为 ERROR"）也被判成错误级别。
 * 正确做法是读日志格式里的**级别字段**，而不是在整行里找关键词。
 *
 * 单独成模块还有一个好处：它是纯函数、无 Electron 依赖，能直接单元测试
 * （见 tools/test-log-levels.mjs）。
 */

/**
 * 本地时区的 ISO 时间戳（形如 `2026-09-13T19:24:38`）。
 *
 * 必须与 Python 侧 `_now_iso()`（`datetime.now().isoformat(timespec="seconds")`）
 * 格式一致：两边的时间戳会同时出现在日志列表里，一边本地时间、一边 UTC
 * （`toISOString()`）的话，同一时刻会显示成 19:24 与 11:24 两个值 ——
 * 用户会以为日志乱序或时间错乱。
 */
export function localIsoNow(): string {
  const now = new Date();
  const pad = (value: number): string => String(value).padStart(2, "0");
  return (
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}` +
    `T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`
  );
}

/**
 * 把一段原始输出拆成单行。
 *
 * 一个数据块可能含多行（线程异常的回溯就是整块到达），不拆的话整个回溯会挤在
 * 一行里，级别也无法逐行判断。
 */
export function splitStderrLines(chunk: string): string[] {
  return chunk
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter((line) => line.trim() !== "");
}

/**
 * 识别一行日志的级别，返回大写的级别名。
 *
 * 覆盖两种真实格式：
 *
 *   1. Python 的 stderr 控制台格式
 *      `"%(levelname)-8s [%(name)s] [session=...] %(message)s"`
 *      → `INFO     [voxsub.pipeline] [session=-] ...`
 *   2. 磁盘日志文件格式（带日期前缀）
 *      `"%(asctime)s %(levelname)-8s [%(name)s] ..."`
 *      → `2026-09-13 19:00:55 INFO     [voxsub.pipeline] [session=-] ...`
 *
 * 两者都是「级别名 + 若干空格 + `[`」，所以一条正则同时覆盖；**必须要求后面
 * 跟 `[`**，否则正文里出现的 "error=" 之类会被误判成错误级别。
 *
 * 认不出格式的原始输出（线程异常回溯、`warnings` 模块告警）按关键词判断。
 *
 * 兜底是 **INFO 而不是 ERROR**：宁可把真错误显示成 info，也不要把正常日志
 * 显示成错误 —— 后者会淹没真正的问题。
 */
export function guessStderrLevel(line: string): string {
  const exact = /(?:^|\s)(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\[/.exec(line);
  if (exact) return exact[1]!;

  // 线程异常 / 未捕获异常的回溯：
  //   "Exception in thread Thread-9 (_readerthread):"
  //   "Traceback (most recent call last):"
  //   "UnicodeDecodeError: 'utf-8' codec can't decode ..."
  if (
    /^Traceback \(most recent call last\)/.test(line) ||
    /^Exception in thread\b/.test(line) ||
    /^\s*\w*(Error|Exception)\b\s*:/.test(line)
  ) {
    return "ERROR";
  }
  if (/\bWarning\b|\bwarn\b/i.test(line)) return "WARNING";
  return "INFO";
}
