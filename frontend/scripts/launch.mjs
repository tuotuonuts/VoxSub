/**
 * 开发启动器 —— 一键构建并启动 Electron 前端。
 *
 * 存在理由：手动流程有四个步骤（检查依赖 → 构建 → 设环境变量 → 启动），
 * 而且其中两个容易踩坑：
 *   · npm 会拦截 postinstall，electron/esbuild 的二进制不会被下载
 *   · 忘了设 VOXSUB_ROOT 就找不到 Python 后端
 * 这个脚本把四步合成一步，并且把两个坑都自动处理掉。
 *
 * 用法（也可直接双击 启动Electron版.bat）：
 *   node scripts/launch.mjs              增量构建后启动
 *   node scripts/launch.mjs --clean      强制全量重建
 *   node scripts/launch.mjs --no-build   跳过构建，直接启动
 *   node scripts/launch.mjs --dev        启动后自动打开开发者工具
 *   node scripts/launch.mjs --debug      开启远程调试端口 9222（冒烟测试用）
 *   node scripts/launch.mjs --silent     静默模式：窗口不显示、不抢焦点（自动化测试用）
 *   node scripts/launch.mjs --keep       不杀掉已在运行的实例
 *
 * 输出一律用 ASCII：Windows 控制台在 CP936 下会把 UTF-8 中文显示成乱码，
 * 而这个脚本经常在双击的 cmd 窗口里跑，没法保证代码页。
 */
import { spawn, spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readdirSync, statSync } from "node:fs";
import { dirname, join, extname } from "node:path";
import { fileURLToPath } from "node:url";

import { buildCleanupScript } from "./lib/electron-instances.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const args = new Set(process.argv.slice(2));

/* ------------------------------------------------------------------ 自写日志 */

/**
 * 启动器自带日志文件。
 *
 * 为什么不让调用方用 shell 重定向：实测在某些托管环境里
 * `node launch.mjs > log 2>&1` 会让包装器的消息混进日志、而 node 自身的
 * 输出反而丢失（表现为日志只有一行无关报错、应用没起来）。
 * 自己写日志就没有这层依赖，后台跑也能事后回看。
 */
const LOG_DIR = join(ROOT, "logs");
const LOG_FILE = join(LOG_DIR, "launcher.log");

function logLine(text) {
  try {
    mkdirSync(LOG_DIR, { recursive: true });
    appendFileSync(LOG_FILE, text + "\n", "utf-8");
  } catch {
    // 日志写不进去不该阻断启动
  }
}

const step = (n, text) => {
  console.log(`[${n}] ${text}`);
  logLine(`[${n}] ${text}`);
};
const ok = (text) => {
  console.log(`    OK   ${text}`);
  logLine(`    OK   ${text}`);
};
const warn = (text) => {
  console.log(`    WARN ${text}`);
  logLine(`    WARN ${text}`);
};
const fail = (text) => {
  console.error(`    FAIL ${text}`);
  logLine(`    FAIL ${text}`);
};

/* -------------------------------------------------------------- 依赖检查 */

/** npm 在本机拦截 postinstall，二进制要手动补 —— 这是最容易踩的坑。 */
function ensureBinaries() {
  const targets = [
    {
      name: "electron",
      binary: join(ROOT, "node_modules", "electron", "dist", "electron.exe"),
      installer: join(ROOT, "node_modules", "electron", "install.js"),
    },
    {
      name: "esbuild",
      binary: join(ROOT, "node_modules", "@esbuild", "win32-x64", "esbuild.exe"),
      installer: join(ROOT, "node_modules", "esbuild", "install.js"),
    },
  ];

  let repaired = 0;
  for (const target of targets) {
    if (existsSync(target.binary)) continue;
    if (!existsSync(target.installer)) {
      fail(`${target.name}: missing installer (run npm install first)`);
      continue;
    }
    warn(`${target.name} binary missing - repairing`);
    const result = spawnSync(process.execPath, [target.installer], {
      cwd: ROOT,
      // stdin 用 ignore：非 tty 环境下子进程会报 "stdin is not a tty"
      stdio: ["ignore", "inherit", "inherit"],
      windowsHide: true,
    });
    if (result.status === 0 && existsSync(target.binary)) {
      ok(`${target.name} repaired`);
      repaired += 1;
    } else {
      fail(`${target.name} repair failed`);
    }
  }
  return repaired;
}

/* -------------------------------------------------------------- 增量构建 */

const SOURCE_EXT = new Set([".ts", ".css", ".html", ".mjs", ".json"]);
const SOURCE_DIRS = ["src", "scripts", "tools"];
const SKIP_DIRS = new Set(["node_modules", "dist", ".git", "VoxSub"]);

/** 递归取目录里最新的修改时间。 */
function newestMtime(dir, filter) {
  let newest = 0;
  const walk = (current) => {
    let entries;
    try {
      entries = readdirSync(current, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (SKIP_DIRS.has(entry.name)) continue;
      const full = join(current, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (filter && !filter(entry.name)) continue;
      try {
        const stat = statSync(full);
        if (stat.mtimeMs > newest) newest = stat.mtimeMs;
      } catch {
        // 读不到就跳过，不影响判断
      }
    }
  };
  walk(dir);
  return newest;
}

function needsBuild() {
  const distMain = join(ROOT, "dist", "main", "main.js");
  const distRenderer = join(ROOT, "dist", "renderer", "index.js");
  const distCss = join(ROOT, "dist", "renderer", "app.css");
  if (!existsSync(distMain) || !existsSync(distRenderer) || !existsSync(distCss)) {
    return { needed: true, reason: "dist is incomplete" };
  }

  const sourceTime = Math.max(
    ...SOURCE_DIRS.map((d) => newestMtime(join(ROOT, d), (name) => SOURCE_EXT.has(extname(name)))),
    existsSync(join(ROOT, "package.json")) ? statSync(join(ROOT, "package.json")).mtimeMs : 0,
  );
  const distTime = Math.min(
    statSync(distMain).mtimeMs,
    statSync(distRenderer).mtimeMs,
    statSync(distCss).mtimeMs,
  );

  if (sourceTime > distTime) {
    const delta = Math.round((sourceTime - distTime) / 1000);
    return { needed: true, reason: `sources are ${delta}s newer than build` };
  }
  return { needed: false, reason: "build is up to date" };
}

function build() {
  // npm run build 会先跑 prebuild（零宽字符清洗），不能绕过它
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const result = spawnSync(npm, ["run", "build"], {
    cwd: ROOT,
    stdio: ["ignore", "inherit", "inherit"],
    shell: process.platform === "win32",
    windowsHide: true,
  });
  if (result.status !== 0) {
    fail("build failed");
    process.exit(1);
  }
  ok("build finished");
}

/* ---------------------------------------------------------------- 清理 */

/**
 * 杀掉已在运行的实例。
 *
 * 为什么必须做：Electron 有单实例锁（app.requestSingleInstanceLock），
 * 已有实例在跑时新实例会静默退出 —— 用户看到的是"双击没反应"，
 * 极难自查。
 *
 * 这里踩过三次坑，教训都写下来别再犯：
 *
 *   1. 最早写死 '*voxsub-electron*'（迁移前的目录名）。迁入 <repo>/frontend 后
 *      该条件永远匹配不到 —— 旧实例杀不掉、新实例被锁挡住。
 *      教训：不要写死目录名，要用从自身位置推导出的路径。
 *
 *   2. 改成"只匹配本项目 node_modules"后仍然不够：工作区里还留着迁移前的旧
 *      检出（voxsub-electron/），从那里启动的实例与本项目**共用同一份
 *      userData**，照样抢锁 —— 用户双击旧目录里的启动器时新实例会立刻退出。
 *      教训：要清的是"会争抢同一 userData 的实例"，不只是"自己这一个"。
 *
 *   3. 拼接 PowerShell 时不要用 $var++ 自增、不要嵌套单引号，并且必须用
 *      -EncodedCommand 传参 —— 否则经 cmd.exe 一层后解析失败（exit 255）。
 *
 * 过滤规则：electron 进程，且满足以下任一
 *   · 路径在本项目 node_modules 下（当前检出）
 *   · 路径在同一工作区父目录下且含 node_modules\electron（同工作区的其它检出）
 * 第二条用 dirname(ROOT) 推导而非写死目录名，项目搬家后依然成立；又因为要求
 * 路径里带 node_modules\electron，不会误杀正式安装的 Electron 应用。
 */
function killRunning() {
  if (args.has("--keep")) return;

  // 判定规则与理由见 scripts/lib/electron-instances.mjs
  const ownMarker = join(ROOT, "node_modules");

  const script = buildCleanupScript({ ownMarker });

  const encoded = Buffer.from(script, "utf16le").toString("base64");
  const result = spawnSync(
    "powershell",
    ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
    // windowsHide: 不加的话每次都会在屏幕上闪一个黑框控制台窗口
    { stdio: "pipe", windowsHide: true, encoding: "utf-8" },
  );

  const output = (result.stdout ?? "").trim();
  if (result.status === 0) {
    // output 可能是多行：第一行是 stopped=N,M，后面是 from: <目录>
    const lines = output.split(String.fromCharCode(10)).map((l) => l.trim()).filter(Boolean);
    const counts = lines.find((l) => l.startsWith("stopped=")) ?? "stopped=0,0";
    const sources = lines.filter((l) => l.startsWith("from:"));
    ok(`previous instances cleared (${counts})`);
    // 来源目录单独打：出现"清理了别的检出"时能立刻看出来
    for (const src of sources) ok(src);
  } else {
    // 清理失败不该阻断启动：可能是权限或竞态，继续尝试启动
    warn(`could not stop previous instances (exit ${result.status})`);
  }
}

/* ---------------------------------------------------------------- 启动 */

function resolveVoxSubRoot() {
  const fromEnv = process.env.VOXSUB_ROOT;
  if (fromEnv && existsSync(join(fromEnv, "voxsub", "__init__.py"))) return fromEnv;

  // Electron 前端已并入 VoxSub 仓库，仓库根就是 frontend 的上一级。
  // 逐级向上找含 voxsub/__init__.py 的目录，不写死层级。
  let probe = ROOT;
  for (let depth = 0; depth < 4; depth += 1) {
    if (existsSync(join(probe, "voxsub", "__init__.py"))) return probe;
    const parent = dirname(probe);
    if (parent === probe) break;
    probe = parent;
  }

  // 兜底：同级目录（前端尚未并入仓库时的旧布局）
  const sibling = join(ROOT, "..", "VoxSub");
  if (existsSync(join(sibling, "voxsub", "__init__.py"))) return sibling;

  return "";
}

function launch() {
  const voxsubRoot = resolveVoxSubRoot();
  if (!voxsubRoot) {
    fail("cannot locate VoxSub backend");
    console.error("    Set it explicitly:  set VOXSUB_ROOT=D:\\path\\to\\VoxSub");
    process.exit(1);
  }
  ok(`backend: ${voxsubRoot}`);

  const python = join(voxsubRoot, ".venv", "Scripts", "python.exe");
  if (!existsSync(python)) {
    warn(`python venv not found at ${python}`);
    console.error("    The app will start but the backend cannot launch.");
  }

  const electron = join(ROOT, "node_modules", "electron", "dist", "electron.exe");
  const cliArgs = [];
  // Electron CLI switches must precede the app path. If placed after ROOT,
  // Electron treats them as application arguments and remote debugging never starts.
  if (args.has("--debug")) {
    cliArgs.push("--remote-debugging-port=9222");
    ok("remote debugging on port 9222");
  }
  cliArgs.push(ROOT);

  const debugSwitchIndex = args.has("--debug") ? cliArgs.indexOf("--remote-debugging-port=9222") : -1;
  if (args.has("--debug") && (debugSwitchIndex < 0 || debugSwitchIndex > cliArgs.indexOf(ROOT))) {
    fail("debug switch must precede the Electron app path");
    process.exit(1);
  }

  const child = spawn(electron, cliArgs, {
    cwd: ROOT,
    // stdin 用 ignore 而不是 inherit：Electron 在无终端环境（后台任务、CI、
    // 被管道接住）继承到非 tty 的 stdin 时会报 "stdin is not a tty" 并直接退出。
    // stdout/stderr 仍要 inherit，否则看不到应用日志。
    stdio: ["ignore", "inherit", "inherit"],
    env: {
      ...process.env,
      VOXSUB_ROOT: voxsubRoot,
      // 这两个必须清掉，否则 Python 侧会 import 到错误的包
      PYTHONPATH: "",
      PYTHONHOME: "",
      // 通过环境变量让主进程自己开 DevTools。
      // 不用 SendKeys 发 F12：那会把按键发给"当前焦点窗口"，
      // 用户此刻可能正对着编辑器，等于往别人窗口里打字。
      ...(args.has("--dev") ? { VOXSUB_DEVTOOLS: "1" } : {}),
      // 静默模式：窗口不显示、不抢焦点、不建托盘。
      // 自动化测试与后台验证一律走这个模式 —— 用户桌面上不该出现任何东西。
      ...(args.has("--silent") ? { VOXSUB_HEADLESS: "1" } : {}),
    },
  });

  if (args.has("--silent")) {
    ok("silent mode: no window will appear on the desktop");
  }

  child.on("exit", (code, signal) => {
    // 区分"用户正常关闭"与"被强杀/崩溃"：只把退出码原样传给调用方的话，
    // 每次 Stop-Process 都会让启动器看起来像失败了。写清原因才能分辨。
    const killed = signal != null || (code != null && code !== 0);
    const reason = signal
      ? `terminated by signal ${signal}`
      : code === 0
        ? "closed normally"
        : `exited with code ${code}`;
    console.log(`\n[exit] Electron ${reason}`);
    logLine(`[exit] Electron ${reason}${killed ? " (expected if force-stopped)" : ""}`);
    process.exit(code ?? (killed ? 1 : 0));
  });
}

/* ---------------------------------------------------------------- 主流程 */

logLine("");
logLine(`--- launch ${new Date().toISOString()} args=${[...args].join(" ") || "(none)"} ---`);
console.log("========================================");
console.log("  VoxSub Electron - dev launcher");
console.log("========================================\n");

step(1, "checking dependencies");
if (!existsSync(join(ROOT, "node_modules"))) {
  warn("node_modules missing - running npm install (first run, may take a while)");
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  spawnSync(npm, ["install"], {
    cwd: ROOT,
    stdio: ["ignore", "inherit", "inherit"],
    shell: process.platform === "win32",
    windowsHide: true,
  });
}
ok("node_modules present");
ensureBinaries();

step(2, "checking build");
if (args.has("--no-build")) {
  ok("build skipped (--no-build)");
} else if (args.has("--clean")) {
  ok("forced rebuild (--clean)");
  build();
} else {
  const verdict = needsBuild();
  if (verdict.needed) {
    ok(`rebuilding: ${verdict.reason}`);
    build();
  } else {
    ok(verdict.reason);
  }
}

step(3, "preparing launch");
killRunning();

step(4, "starting");
launch();
