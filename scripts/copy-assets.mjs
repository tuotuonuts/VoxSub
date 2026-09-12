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

console.log(`[copy-assets] ${copied} 个静态资源 → dist/renderer`);
