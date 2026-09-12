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

/** 杀掉已在运行的实例：否则会开出第二个窗口，模型与锁文件也会打架。 */
function killRunning() {
  if (args.has("--keep")) return;
  const result = spawnSync(
    "powershell",
    ["-NoProfile", "-Command",
      "Get-Process electron -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*voxsub-electron*' } | Stop-Process -Force"],
    // windowsHide: 不加的话每次都会在屏幕上闪一个黑框控制台窗口
    { stdio: "pipe", shell: true, windowsHide: true },
  );
  if (result.status === 0) ok("previous instances stopped (if any)");
}

/* ---------------------------------------------------------------- 启动 */

function resolveVoxSubRoot() {
  const fromEnv = process.env.VOXSUB_ROOT;
  if (fromEnv && existsSync(fromEnv)) return fromEnv;

  // 默认找仓库同级目录 —— 本机布局是 app_dve/VoxSub
  const sibling = join(ROOT, "..", "VoxSub");
  if (existsSync(sibling)) return sibling;

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
  const cliArgs = [ROOT];
  if (args.has("--debug")) {
    cliArgs.push("--remote-debugging-port=9222");
    ok("remote debugging on port 9222");
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

  child.on("exit", (code) => {
    console.log(`\n[exit] Electron closed (code ${code ?? "null"})`);
    process.exit(code ?? 0);
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
