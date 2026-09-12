#!/usr/bin/env node
/**
 * 启动器实例清理逻辑的测试。
 *
 * 为什么需要：用户报"双击 启动Electron版.bat 没反应"报了两次，两次根因都在
 * 这个判定上 ——
 *   第一次：写死 '*voxsub-electron*'（当时的目录名），迁入 <repo>/frontend
 *           后永远匹配不到，旧实例杀不掉，新实例被单实例锁挡住。
 *   第二次：改成"只匹配本项目 node_modules"后，旧检出（app_dve\
 *           voxsub-electron）里跑着的实例仍杀不掉 —— 它与本项目共用同一份
 *           userData（%APPDATA%\voxsub-electron），照样抢锁。
 *
 * 判定逻辑只在一处（scripts/lib/electron-instances.mjs 的 predicateLines），
 * 本测试跑的就是生产用的那段 PowerShell，只是喂给它**合成的 (路径, 命令行)**
 * 而不是真实进程 —— 否则测试本身就得在用户桌面上启动一个旧检出的 Electron。
 *
 * 用例里的路径与命令行都取自实测（见 electron-instances.mjs 顶部注释）。
 *
 * 用法：node tools/test-launcher-cleanup.mjs
 */
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { buildPredicateProbeScript } from "../scripts/lib/electron-instances.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const ownMarker = join(ROOT, "node_modules");
/** 工作区根：frontend 上溯两级（<repo>/frontend → <repo> → 工作区）。 */
const WORKSPACE = dirname(dirname(ROOT));

const ELECTRON_EXE = join("node_modules", "electron", "dist", "electron.exe");
/** 实测的 userData 目录名（取自 package.json 的 name）。 */
const USER_DATA = "C:\\Users\\Zhang Ruiduo\\AppData\\Roaming\\voxsub-electron";

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

/** 构造一个"某检出"的进程样本：主进程与子进程各一条。 */
function sample(exeDir, label) {
  const exe = join(exeDir, ELECTRON_EXE);
  return [
    {
      path: exe,
      // 主进程：命令行 = <electron.exe> <检出目录>
      cmd: `${exe} ${exeDir}`,
      label: `${label} · 主进程`,
    },
    {
      path: exe,
      // 子进程：命令行带 --user-data-dir（实测格式）
      cmd: `"${exe}" --type=gpu-process --user-data-dir="${USER_DATA}"`,
      label: `${label} · 子进程`,
    },
  ];
}

const ownDir = ROOT;
const legacyDir = join(WORKSPACE, "voxsub-electron");
const siblingDir = join(WORKSPACE, "VoxSub-copy", "frontend");

const MATCH_CASES = [
  ...sample(ownDir, "本项目检出"),
  ...sample(legacyDir, "迁移前旧检出（第二次踩的坑）"),
  ...sample(siblingDir, "同工作区的另一份检出"),
];

const SKIP_CASES = [
  {
    path: "C:\\Program Files\\VoxSub\\VoxSub.exe",
    cmd: "C:\\Program Files\\VoxSub\\VoxSub.exe",
    label: "正式安装的 VoxSub（不带 node_modules\\electron）",
  },
  {
    path: "C:\\Program Files\\Microsoft VS Code\\Code.exe",
    cmd: "C:\\Program Files\\Microsoft VS Code\\Code.exe",
    label: "无关应用 VS Code",
  },
  {
    path: "C:\\Program Files\\Slack\\app-4.0\\slack.exe",
    cmd: "C:\\Program Files\\Slack\\app-4.0\\slack.exe --type=renderer",
    label: "无关应用 Slack（Electron 应用，但命令行不含 voxsub）",
  },
  {
    path: join("C:\\", "dev", "other-project", "node_modules", "electron", "dist", "electron.exe"),
    cmd: `"C:\\dev\\other-project\\node_modules\\electron\\dist\\electron.exe" --type=gpu-process --user-data-dir="C:\\Users\\x\\AppData\\Roaming\\other-project"`,
    label: "工作区外的另一个 Electron 开发项目",
  },
  {
    path: "D:\\VoxSub\\VoxSub.exe",
    cmd: "D:\\VoxSub\\VoxSub.exe",
    label: "Qt 版安装目录里的程序",
  },
];

const cases = [...MATCH_CASES, ...SKIP_CASES];

console.log("判定参数");
console.log(`  own       = ${ownMarker}`);
console.log(`  workspace = ${WORKSPACE}`);
console.log(`  用例      = ${MATCH_CASES.length} 应匹配 / ${SKIP_CASES.length} 应排除`);
console.log("");

const script = buildPredicateProbeScript({ ownMarker, cases });
const encoded = Buffer.from(script, "utf16le").toString("base64");
const result = spawnSync(
  "powershell",
  ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
  { stdio: "pipe", windowsHide: true, encoding: "utf-8" },
);

if (result.status !== 0) {
  console.error("PowerShell 执行失败，退出码", result.status);
  console.error((result.stderr ?? "").slice(0, 400));
  process.exit(1);
}

// 输出格式：MATCH=True|False ||| <路径> ||| <命令行>
const got = [];
for (const line of (result.stdout ?? "").split(String.fromCharCode(10))) {
  const m = line.trim().match(/^MATCH=(True|False)\s*\|\|\|\s*(.*?)\s*\|\|\|\s*(.*)$/i);
  if (m) got.push({ match: /^true$/i.test(m[1]), path: m[2], cmd: m[3] });
}

if (got.length !== cases.length) {
  console.error(`返回条目数不符：期望 ${cases.length}，实际 ${got.length}`);
  process.exit(1);
}

console.log("=== 应匹配：会争抢同一 userData 的实例 ===\n");
for (let i = 0; i < MATCH_CASES.length; i += 1) {
  check(MATCH_CASES[i].label, got[i].match === true, `实际 ${got[i].match}`);
}

console.log("\n=== 应排除：不能误杀 ===\n");
for (let i = 0; i < SKIP_CASES.length; i += 1) {
  const idx = MATCH_CASES.length + i;
  check(SKIP_CASES[i].label, got[idx].match === false, `实际 ${got[idx].match}`);
}

// 回归保护：判定不能退化成"只匹配本项目 node_modules"（第一次的修法）。
// 那条修法让旧检出的实例逃过清理，正是用户第二次报障的原因。
console.log("\n=== 回归保护 ===\n");
{
  const legacyExe = join(legacyDir, ELECTRON_EXE);
  check(
    "旧检出路径不被 ownMarker 覆盖（所以必须有第二条规则）",
    !legacyExe.startsWith(ownMarker),
    legacyExe.startsWith(ownMarker) ? "ownMarker 竟覆盖旧检出 —— 第二条规则会成死代码" : "ok",
  );
  const legacyMain = got[MATCH_CASES.findIndex((c) => c.label.includes("旧检出") && c.label.includes("主进程"))];
  check("旧检出主进程被判为匹配", legacyMain?.match === true, `实际 ${legacyMain?.match}`);
  const legacyChild = got[MATCH_CASES.findIndex((c) => c.label.includes("旧检出") && c.label.includes("子进程"))];
  check("旧检出子进程被判为匹配", legacyChild?.match === true, `实际 ${legacyChild?.match}`);
}

const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
process.exit(failed.length ? 1 : 0);
