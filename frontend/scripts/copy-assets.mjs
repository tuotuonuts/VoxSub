/**
 * 把静态资源复制到 dist/renderer。
 *
 * 为什么不全部内联进 JS：HTML 需要 CSP meta，CSS 需要独立文件以便开发者工具
 * 调试；把它们打成字符串反而让排查变难。
 */
import { cpSync, mkdirSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const from = join(root, "src", "renderer");
const to = join(root, "dist", "renderer");

mkdirSync(to, { recursive: true });

const KEEP = /\.(html|css)$/;
let copied = 0;
for (const name of readdirSync(from)) {
  const full = join(from, name);
  if (!statSync(full).isFile()) continue;
  if (!KEEP.test(name)) continue;
  cpSync(full, join(to, name));
  copied += 1;
}

/**
 * 应用图标也要进 dist。
 *
 * 为什么：主进程要拿它设窗口图标（任务栏）和托盘图标，打包后代码在 app.asar
 * 里，读不到仓库根的 assets/。放进 dist/renderer 后会被 electron-builder 的
 * `dist/renderer/**\/*` 规则一起打进 asar，开发与打包两条路径都能读到。
 *
 * 不复制会怎样：托盘图标是 nativeImage.createEmpty()（空白），任务栏显示
 * Electron 默认图标 —— 用户看到的就是"没有 icon"。
 */
const iconFrom = join(root, "..", "assets");
const iconTo = join(to, "assets");
mkdirSync(iconTo, { recursive: true });
let icons = 0;
for (const name of ["icon.ico", "icon.png"]) {
  const full = join(iconFrom, name);
  try {
    if (!statSync(full).isFile()) continue;
  } catch {
    continue; // 图标缺失不该阻断构建，主进程会退化为无图标
  }
  cpSync(full, join(iconTo, name));
  icons += 1;
}

console.log(`[copy-assets] ${copied} 个静态资源 + ${icons} 个图标 → dist/renderer`);
