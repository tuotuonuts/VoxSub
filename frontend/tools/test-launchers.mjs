#!/usr/bin/env node
/**
 * 启动器 .bat 的测试。
 *
 * 为什么需要：用户报"双击 启动Electron版.bat 没反应"，前后三次，根因各不相同，
 * 其中两次都不是代码逻辑问题，而是文件本身：
 *
 *   1. 杀进程条件写死了迁移前的目录名 → 旧实例杀不掉、新实例被锁挡住。
 *      （由 test-launcher-cleanup*.mjs 覆盖）
 *
 *   2. **我在 .bat 内容里写了中文路径**。cmd.exe 按 OEM 代码页（本机 CP936）
 *      读 .bat，不是 UTF-8 —— 中文路径被解成乱码，`if exist` 全部失败，
 *      窗口一闪而过、什么都不发生。而原文件顶部早就写着 "ASCII-ONLY on
 *      purpose"，是我没遵守。
 *
 *   3. 项目里同时存在三个同名启动器（仓库根 / frontend / 迁移前旧检出），
 *      点错那个就会因为第 1 条而失败。
 *
 * 所以这个测试盯三件事：内容纯 ASCII、转发目标真实存在、入口唯一实现。
 *
 * 用法：node tools/test-launchers.mjs
 */
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(HERE, "..");
const REPO = dirname(FRONTEND);
const WORKSPACE = dirname(REPO);

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

/**
 * 所有启动器入口。旧检出那份可能已被清理，缺失时跳过而不是失败。
 */
const LAUNCHERS = [
  {
    path: join(REPO, "启动Electron版.bat"),
    label: "仓库根启动器",
    required: true,
    forwards: true,
  },
  {
    path: join(FRONTEND, "启动Electron版.bat"),
    label: "frontend 启动器",
    required: true,
    forwards: true,
  },
  {
    path: join(WORKSPACE, "voxsub-electron", "启动Electron版.bat"),
    label: "旧检出副本（已改为转发）",
    required: false,
    forwards: true,
  },
];

/** 真正的实现文件。 */
const IMPL = join(FRONTEND, "scripts", "run-launcher.bat");

/* ------------------------------------------------------------ 1. 纯 ASCII */

console.log("=== 内容必须是纯 ASCII ===\n");
console.log("  cmd.exe 用 OEM 代码页（本机 CP936）读 .bat，不是 UTF-8。");
console.log("  内容里出现中文路径会被解成乱码，if exist 全部失败、窗口一闪而过。\n");

const existing = [];
for (const l of LAUNCHERS) {
  if (!existsSync(l.path)) {
    if (l.required) {
      check(`${l.label} 存在`, false, l.path);
    } else {
      console.log(`SKIP  ${l.label}（不存在：${l.path}）`);
    }
    continue;
  }
  existing.push(l);
}

const ASCII_TARGETS = [
  ...existing.map((l) => ({ path: l.path, label: l.label })),
  { path: IMPL, label: "启动器实现（run-launcher.bat）" },
];

for (const t of ASCII_TARGETS) {
  if (!existsSync(t.path)) {
    check(`${t.label} 存在`, false, t.path);
    continue;
  }
  const buf = readFileSync(t.path);
  let nonAscii = 0;
  const offenders = [];
  for (let i = 0; i < buf.length; i += 1) {
    if (buf[i] > 127) {
      nonAscii += 1;
      if (offenders.length < 1) {
        // 记下首个越界字节的上下文，便于定位
        const from = Math.max(0, i - 20);
        const to = Math.min(buf.length, i + 20);
        offenders.push(buf.subarray(from, to).toString("latin1").replace(/\r?\n/g, " "));
      }
    }
  }
  check(
    `${t.label} 内容纯 ASCII`,
    nonAscii === 0,
    nonAscii === 0 ? `${buf.length} 字节` : `${nonAscii} 个非 ASCII 字节，首个附近：…${offenders[0]}…`,
  );
}

/* ------------------------------------------------------------ 2. 转发目标存在 */

console.log("\n=== 转发目标必须真实存在 ===\n");
console.log("  转发目标用 %~dp0 相对推导（不能写死盘符），且路径为 ASCII。\n");

/** 从 .bat 内容里取出 set "XXX=%~dp0..." 的值。 */
function extractRelativeTarget(text) {
  const m = text.match(/set\s+"(?:IMPL|REAL)=([^"]+)"/i);
  return m ? m[1] : null;
}

for (const l of existing) {
  const text = readFileSync(l.path, "utf8");
  const raw = extractRelativeTarget(text);

  if (!raw) {
    check(`${l.label} 有转发目标声明`, false, "未找到 set \"IMPL/REAL=...\"");
    continue;
  }
  check(`${l.label} 有转发目标声明`, true, raw);

  // 必须是相对推导，不能写死盘符 —— 否则项目换位置就断
  check(
    `${l.label} 目标用 %~dp0 相对推导`,
    /%~dp0/i.test(raw),
    /%~dp0/i.test(raw) ? "ok" : `写死了路径：${raw}`,
  );

  // 解析 %~dp0 为该 .bat 自身目录，验证目标存在
  const selfDir = dirname(l.path);
  const resolved = resolve(raw.replace(/%~dp0/gi, selfDir + "\\").replace(/\\/g, "/"));
  check(
    `${l.label} 转发目标存在`,
    existsSync(resolved),
    existsSync(resolved) ? resolved : `找不到：${resolved}`,
  );
}

/* ------------------------------------------------------------ 3. 实现唯一且完整 */

console.log("\n=== 实现只有一份，且能真正启动 ===\n");

check("实现文件存在", existsSync(IMPL), IMPL);

if (existsSync(IMPL)) {
  const impl = readFileSync(IMPL, "utf8");

  // 实现文件必须自己调用 launch.mjs（否则转发链是断的）
  const callsLaunch = /launch\.mjs/i.test(impl);
  check("实现调用 scripts\\launch.mjs", callsLaunch, callsLaunch ? "ok" : "未找到 launch.mjs 引用");
  check("launch.mjs 存在", existsSync(join(FRONTEND, "scripts", "launch.mjs")));

  // node 查找要有回退路径：双击时 PATH 可能不含 node
  const hasPathLookup = /%\*\$PATH:I|%\*\$PATH/i.test(impl) || /\$PATH:I/.test(impl);
  check("实现里有 PATH 查找 node", hasPathLookup, hasPathLookup ? "ok" : "未找到 $PATH:I 用法");

  const fallbacks = (impl.match(/node\.exe"/g) ?? []).length;
  check("实现里有 node 回退路径", fallbacks >= 2, `${fallbacks} 处 node.exe 引用`);

  // 失败时要留窗口，否则用户看不到任何信息
  check("失败时 pause 保留窗口", /\bpause\b/i.test(impl));

  // 参数要透传
  check("参数透传 %*", /launch\.mjs"\s+%\*/i.test(impl));
}

/**
 * 剥掉 .bat 里的注释与 echo 文本，只留可执行行。
 *
 * 为什么需要：仓库根启动器的注释里会提到 launch.mjs（"真实实现位于
 * frontend\scripts\launch.mjs"），直接对整个文件做关键词匹配会把注释当成
 * 代码，报出"逻辑重复了"这种假失败。判断"有没有自己实现"必须只看真正会
 * 被执行的命令行。
 */
function executableLines(text) {
  return text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !/^rem\b/i.test(l) && !/^::/.test(l) && !/^echo\b/i.test(l));
}

/* ------------------------------------------------------------ 4. 入口不重复实现 */

console.log("\n=== 各入口不得各自实现一套 ===\n");
console.log("  每个入口只允许转发，真正的逻辑只在 run-launcher.bat 里。");
console.log("  只看可执行行 —— 注释里提到 launch.mjs 不算自己实现。\n");

for (const l of existing) {
  const code = executableLines(readFileSync(l.path, "utf8")).join("\n");
  const callsImpl = /run-launcher\.bat/i.test(code);
  const callsLaunchDirectly = /launch\.mjs/i.test(code);
  check(
    `${l.label} 只转发、不自己实现`,
    callsImpl && !callsLaunchDirectly,
    callsImpl
      ? (callsLaunchDirectly ? "可执行行里直接调用了 launch.mjs —— 逻辑重复了" : "ok")
      : "可执行行里未转发到 run-launcher.bat",
  );
}

/* ------------------------------------------------------------ 汇总 */

const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
if (failed.length) {
  console.log("\n失败项：");
  for (const f of failed) console.log(`  ${f.name}`);
}
process.exit(failed.length ? 1 : 0);
