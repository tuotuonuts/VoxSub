/**
 * 零宽字符清洗 —— 构建前置步骤。
 *
 * 为什么需要它：编辑过程中偶发的 U+200B 会悄悄进入选择器名或模块路径，
 * 后果是「CSS 规则静默失效」「import 无法解析」，而报错信息里的字符
 * 肉眼与正常字符无异。踩过两次（`.shell` 规则整体失效、`./tokens`
 * 无法解析），因此固化为构建门禁而不是依赖人工检查。
 *
 * 行为：就地清除四类零宽字符并报告；发现即视为不合规（退出码 1），
 * 但因为已就地修复，重跑即可通过。
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, extname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");

/** U+200B 零宽空格 / U+200C 非连接符 / U+200D 连接符 / U+2060 词连接符 / U+FEFF BOM */
const ZERO_WIDTH = /[\u200b\u200c\u200d\u2060\ufeff]/g;

const EXTENSIONS = new Set([".ts", ".tsx", ".css", ".html", ".mjs", ".js", ".json", ".py", ".md"]);
const SKIP_DIRS = new Set(["node_modules", "dist", ".git", "release"]);

const offenders = [];

function walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(full);
      continue;
    }
    if (!EXTENSIONS.has(extname(entry.name))) continue;

    const text = readFileSync(full, "utf8");
    const matches = text.match(ZERO_WIDTH);
    if (!matches) continue;

    writeFileSync(full, text.replace(ZERO_WIDTH, ""));
    offenders.push({ file: full.slice(root.length + 1), count: matches.length });
  }
}

walk(root);

if (offenders.length === 0) {
  console.log("[sanitize] 无零宽字符");
} else {
  console.error("[sanitize] 发现并已清除零宽字符：");
  for (const { file, count } of offenders) {
    console.error(`  ${count} 处  ${file}`);
  }
  console.error("[sanitize] 请检查上述文件的选择器名 / 模块路径是否仍符合预期，然后重跑构建。");
  process.exit(1);
}
