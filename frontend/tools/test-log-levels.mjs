#!/usr/bin/env node
/**
 * 日志级别识别的测试（src/shared/log-levels.ts）。
 *
 * ## 覆盖的用户报告
 *
 * 「报错很多而且还有很多不是错误的日志被识别成 ERROR」
 *
 * 两个真实缺陷，各修一处，都在这里守住：
 *
 *   1. 主进程把后端 stderr **一律**标成 level="error"，而 Python 侧会把整个
 *      日志流也写到 stderr → 界面上每条 INFO 都显示成 ERROR。
 *   2. 渲染层读磁盘日志时用 /(ERROR|CRITICAL)/ 匹配**整行**，于是正文里出现
 *      "error=" 的 INFO 行（例如翻译失败那条）也被标成错误级别。
 *
 * 用例里的每一行都取自真实日志（见 data 段注释），不是编造的。
 *
 * 做法：用 esbuild 把该模块单独编译成临时 .mjs 再 import —— 它是纯函数、
 * 无 Electron 依赖，所以能直接在 node 里跑，不必启动应用。
 *
 * 用法：node tools/test-log-levels.mjs
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { tmpdir } from "node:os";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const SRC = join(ROOT, "src", "shared", "log-levels.ts");

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

/* ------------------------------------------------------------ 编译 */

const esbuild = join(ROOT, "node_modules", ".bin", process.platform === "win32" ? "esbuild.cmd" : "esbuild");
if (!existsSync(esbuild)) {
  console.error("找不到 esbuild，请先 npm install");
  process.exit(1);
}

const outDir = mkdtempSync(join(tmpdir(), "voxsub-log-levels-"));
const outFile = join(outDir, "log-levels.mjs");
const built = spawnSync(esbuild, [SRC, "--format=esm", `--outfile=${outFile}`, "--log-level=warning"], {
  cwd: ROOT, stdio: "pipe", encoding: "utf-8", shell: process.platform === "win32",
});
if (built.status !== 0) {
  console.error("esbuild 编译失败：", (built.stderr ?? "").slice(0, 400));
  process.exit(1);
}

const { guessStderrLevel, localIsoNow, splitStderrLines } = await import(pathToFileURL(outFile).href);

/* ------------------------------------------------------------ 真实日志行 */

// 取自 voxsub.log 的真实行（格式见 logging_setup.py 的 Formatter）
const FILE_INFO =
  "2026-09-13 19:00:55 INFO     [voxsub.diagnostics] [session=-] 自检结果[VAD 冒烟] status=ok detail=模型加载通过, 触发 54 个语音窗口";
const FILE_ERROR =
  "2026-09-13 19:06:38 ERROR    [voxsub.pipeline] [session=-] 翻译失败: src_chars=5 error=快档不支持语言对 ('ja', 'zh')";
const FILE_WARNING =
  "2026-09-13 19:06:30 WARNING  [voxsub.pipeline] [session=-] 音频已连接但持续静音: source=麦克风";
/** INFO 行，但正文里提到了 ERROR —— 旧的整行匹配会把它误判成错误级别。 */
const FILE_INFO_MENTIONS_ERROR =
  "2026-09-13 19:00:55 INFO     [voxsub] [session=-] 日志级别设为 ERROR（仅记录错误）";

// stderr 控制台格式（无日期前缀）
const STDERR_INFO = "INFO     [voxsub.pipeline] [session=-] Pipeline 生命周期: idle -> starting";
const STDERR_WARNING = "WARNING  [voxsub.hardware] [session=-] NPU 探测来源=none detail=WMI";
const STDERR_ERROR = "ERROR    [voxsub.pipeline] [session=-] 翻译失败: src_chars=5";

console.log("=== 应识别为 INFO ===\n");
for (const [label, line] of [
  ["文件格式的 INFO 行", FILE_INFO],
  ["stderr 格式的 INFO 行", STDERR_INFO],
  ["正文提到 ERROR 的 INFO 行（旧整行匹配会误判）", FILE_INFO_MENTIONS_ERROR],
]) {
  const level = guessStderrLevel(line);
  check(label, level === "INFO", `得到 ${level}`);
}

console.log("\n=== 应识别为 WARNING ===\n");
for (const [label, line] of [
  ["文件格式的 WARNING 行", FILE_WARNING],
  ["stderr 格式的 WARNING 行", STDERR_WARNING],
]) {
  const level = guessStderrLevel(line);
  check(label, level === "WARNING", `得到 ${level}`);
}

console.log("\n=== 应识别为 ERROR ===\n");
for (const [label, line] of [
  ["stderr 格式的 ERROR 行", STDERR_ERROR],
  ["线程异常回溯的首行", "Exception in thread Thread-9 (_readerthread):"],
  ["回溯的 Traceback 行", "Traceback (most recent call last):"],
  ["回溯的异常末行", "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xb9 in position 14"],
]) {
  const level = guessStderrLevel(line);
  check(label, level === "ERROR", `得到 ${level}`);
}

console.log("\n=== 兜底行为 ===\n");
{
  // 认不出的普通文本 → INFO（**不是** ERROR）。
  // 这条是缺陷 1 的核心：兜底成 error 会把所有非日志输出都变成红色错误。
  const plain = "  File \"D:\\Python 3.12.1\\Lib\\subprocess.py\", line 1599, in _readerthread";
  check("回溯中间的帧行（认不出级别）兜底为 INFO", guessStderrLevel(plain) === "INFO",
        guessStderrLevel(plain));
  check("普通文本兜底为 INFO", guessStderrLevel("hello world") === "INFO");
  check(
    "正文提到 ERROR 但不带日志格式的行不被判为 ERROR",
    guessStderrLevel("parsed 3 items, 0 errors, level=ERROR threshold") === "INFO",
    guessStderrLevel("parsed 3 items, 0 errors, level=ERROR threshold"),
  );
}

console.log("\n=== 拆行 ===\n");
{
  const chunk = [
    "INFO     [voxsub] [session=-] 第一行",
    "WARNING  [voxsub] [session=-] 第二行",
    "",
    "Traceback (most recent call last):",
    "  File \"x.py\", line 1, in <module>",
  ].join("\r\n");

  const lines = splitStderrLines(chunk);
  // 空行被过滤掉（日志区不该出现空行占位），所以是 4 行而非 5
  check("按 CRLF 拆行并过滤空行", lines.length === 4, `${lines.length} 行`);
  check("过滤空行", !lines.includes(""), JSON.stringify(lines));
  check("去掉行尾空白", lines.every((l) => l === l.trimEnd()));
  check("首行级别正确", guessStderrLevel(lines[0]) === "INFO", lines[0]);
  check("次行级别正确", guessStderrLevel(lines[1]) === "WARNING", lines[1]);

  // 整块回溯（线程异常就是这样到达的）：必须拆开，否则级别无法逐行判断
  const tracebackChunk = [
    "Exception in thread Thread-9 (_readerthread):",
    "Traceback (most recent call last):",
    "  File \"D:\\Python 3.12.1\\Lib\\threading.py\", line 1073, in _bootstrap_inner",
    "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xb9 in position 14",
  ].join("\n");
  const tb = splitStderrLines(tracebackChunk);
  check("回溯被拆成 4 行", tb.length === 4, `${tb.length} 行`);
  check("回溯首行判为 ERROR", guessStderrLevel(tb[0]) === "ERROR");
  check("回溯末行判为 ERROR", guessStderrLevel(tb[3]) === "ERROR");

  check("空块返回空数组", splitStderrLines("").length === 0);
  check("纯空白块返回空数组", splitStderrLines("  \n \n").length === 0);
}

/* ------------------------------------------------------------ 时间戳 */

console.log("\n=== 时间戳格式（须与 Python 侧 _now_iso 一致）===\n");
{
  const ts = localIsoNow();
  // 格式：2026-09-13T19:24:38 —— 本地时间、秒精度、无 Z 后缀
  check("形如 YYYY-MM-DDTHH:MM:SS", /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(ts), ts);

  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const expectedLocal =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}` +
    `T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  check("用本地时间（不是 UTC）", ts.slice(0, 13) === expectedLocal.slice(0, 13), `${ts} vs ${expectedLocal}`);

  // 关键回归：带 Z 的 UTC 时间戳会和 Python 侧并排显示成两个时刻
  check("不带 Z 后缀（否则与 Python 的本地时间并排显示成两个时刻）", !ts.endsWith("Z"), ts);
  check("秒精度，无毫秒", !ts.includes("."), ts);

  // 界面上的显示逻辑：ts.length > 11 ? ts.slice(11) : ts
  check("切片后显示为 HH:MM:SS", ts.slice(11) === expectedLocal.slice(11), ts.slice(11));
}

/* ------------------------------------------------------------ 清理 */

try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 忽略 */ }

const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
if (failed.length) {
  console.log("\n失败项：");
  for (const f of failed) console.log(`  ${f.name}`);
}
process.exit(failed.length ? 1 : 0);
