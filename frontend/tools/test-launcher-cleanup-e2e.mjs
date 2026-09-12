#!/usr/bin/env node
/**
 * 启动器清理逻辑的端到端测试 —— 用**真实进程**验证。
 *
 * 为什么不能只靠路径判定的单元测试（test-launcher-cleanup.mjs）：
 * 那个测试验证的是"判定表达式对合成路径返回什么"，而用户遇到的是
 * "旧检出的进程真的还在跑、真的没被杀掉"。中间还隔着
 * Get-CimInstance 取不取得到 ExecutablePath、Stop-Process 有没有权限、
 * 脚本有没有被 cmd.exe 转义破坏 —— 这些只有真跑一遍才知道。
 *
 * 做法：在临时目录建一个最小的 Electron 应用（不建窗口、静默），
 * 用**旧检出的 electron.exe** 启动它 —— 这样进程的路径与命令行都符合
 * "另一个 VoxSub 检出"的特征（命令行含 voxsub-electron），但不占用我们的
 * userData、也不会在桌面上弹窗。然后跑生产的清理脚本，断言它被清掉。
 *
 * 用法：node tools/test-launcher-cleanup-e2e.mjs
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";

import { buildCleanupScript } from "../scripts/lib/electron-instances.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const ownMarker = join(ROOT, "node_modules");
const WORKSPACE = dirname(dirname(ROOT));

/** 旧检出目录（迁移前的残留）。不存在则跳过该测试。 */
const LEGACY_DIR = join(WORKSPACE, "voxsub-electron");
const LEGACY_ELECTRON = join(LEGACY_DIR, "node_modules", "electron", "dist", "electron.exe");

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

/* ------------------------------------------------------------ 工具 */

const ps = (script) => {
  const encoded = Buffer.from(script, "utf16le").toString("base64");
  return spawnSync("powershell", ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
    { stdio: "pipe", windowsHide: true, encoding: "utf-8" });
};

/** 列出当前所有 electron 进程的 (pid, path, cmdline)。 */
function listElectron() {
  const r = ps([
    "Get-CimInstance Win32_Process -Filter \"Name='electron.exe'\" -ErrorAction SilentlyContinue |",
    "  ForEach-Object { Write-Output ($_.ProcessId.ToString() + ' ||| ' + $_.ExecutablePath + ' ||| ' + $_.CommandLine) }",
  ].join("\n"));
  return (r.stdout ?? "")
    .split(String.fromCharCode(10))
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const [pid, path, cmd] = l.split(" ||| ");
      return { pid: Number(pid), path: path ?? "", cmd: cmd ?? "" };
    })
    .filter((p) => Number.isFinite(p.pid) && p.pid > 0);
}

/* ------------------------------------------------------------ 前置 */

if (!existsSync(LEGACY_ELECTRON)) {
  console.log("旧检出不存在，跳过端到端测试：");
  console.log(`  ${LEGACY_ELECTRON}`);
  console.log("（该目录已被清理时属于正常情况）");
  process.exit(0);
}

console.log("环境");
console.log(`  own      = ${ownMarker}`);
console.log(`  旧检出   = ${LEGACY_DIR}`);
console.log("");

// 先清干净，避免既有实例干扰断言
ps("Get-Process electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue");
await new Promise((r) => setTimeout(r, 1500));

/* ------------------------------------------------------------ 造一个静默的"旧检出实例" */

const sandbox = join(tmpdir(), `voxsub-e2e-${Date.now()}`);
mkdirSync(sandbox, { recursive: true });
writeFileSync(join(sandbox, "package.json"), JSON.stringify({
  name: "voxsub-e2e-probe",
  version: "0.0.0",
  main: "main.js",
}, null, 2));
writeFileSync(join(sandbox, "main.js"), [
  "// 最小 Electron 应用：不建窗口，只保持存活，供清理逻辑测试用。",
  "const { app } = require('electron');",
  "app.whenReady().then(() => {",
  "  // 不建窗口 → 不会在桌面上出现任何东西",
  "  setInterval(() => {}, 1000);",
  "});",
].join(String.fromCharCode(10)));

console.log("=== 启动一个来自旧检出路径的进程 ===\n");

const child = spawn(LEGACY_ELECTRON, [sandbox], {
  detached: true,
  stdio: "ignore",
  windowsHide: true,
});
child.unref();

// 等它起来
let legacyProcs = [];
for (let i = 0; i < 20; i += 1) {
  await new Promise((r) => setTimeout(r, 500));
  legacyProcs = listElectron().filter((p) =>
    p.path.toLowerCase().startsWith(LEGACY_DIR.toLowerCase()));
  if (legacyProcs.length > 0) break;
}

check(
  "旧检出进程已启动（真实进程）",
  legacyProcs.length > 0,
  `${legacyProcs.length} 个进程`,
);

if (legacyProcs.length === 0) {
  // 起不来就没法验证清理，直接失败退出
  console.log("\n无法造出旧检出进程，测试不成立");
  try { rmSync(sandbox, { recursive: true, force: true }); } catch { /* 忽略 */ }
  process.exit(1);
}

const mainProc = legacyProcs.find((p) => !p.cmd.includes("--type="));
console.log(`  主进程 pid=${mainProc?.pid} 路径=${mainProc?.path}`);
console.log(`  命令行=${(mainProc?.cmd ?? "").slice(0, 120)}`);

// 关键前提：这条进程的路径**不在**本项目 node_modules 下 ——
// 所以只靠"匹配自己"的旧修法一定清不掉它（这正是用户第二次报障的场景）
check(
  "该进程路径不属于本项目（复现用户的失败场景）",
  !mainProc.path.toLowerCase().startsWith(ownMarker.toLowerCase()),
  "路径不在本项目 node_modules 下",
);

/* ------------------------------------------------------------ 跑生产的清理脚本 */

console.log("\n=== 跑生产清理脚本 ===\n");

const script = buildCleanupScript({ ownMarker });
const cleanup = ps(script);
const output = (cleanup.stdout ?? "").trim();
console.log(`  exit=${cleanup.status}`);
for (const line of output.split(String.fromCharCode(10))) {
  if (line.trim()) console.log(`  ${line.trim()}`);
}
console.log("");

check("清理脚本执行成功", cleanup.status === 0, `exit=${cleanup.status}`);

const stopped = Number((output.match(/stopped=(\d+),/) ?? [])[1] ?? 0);
check("报告清理了实例", stopped > 0, `stopped=${stopped}`);
check(
  "报告了实例来源目录",
  /from:\s*.*voxsub-electron/i.test(output),
  (output.match(/from:\s*.+/i) ?? [""])[0].trim(),
);

/* ------------------------------------------------------------ 断言进程真的没了 */

await new Promise((r) => setTimeout(r, 1500));

const stillAlive = listElectron().filter((p) =>
  p.path.toLowerCase().startsWith(LEGACY_DIR.toLowerCase()));

check(
  "旧检出进程已被真正杀掉",
  stillAlive.length === 0,
  stillAlive.length === 0 ? "0 个残留" : `仍有 ${stillAlive.length} 个`,
);

/* ------------------------------------------------------------ 清理现场 */

try { rmSync(sandbox, { recursive: true, force: true }); } catch { /* 忽略 */ }

const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
process.exit(failed.length ? 1 : 0);
