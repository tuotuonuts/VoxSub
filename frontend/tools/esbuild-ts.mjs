#!/usr/bin/env node
/**
 * 纯逻辑测试的公共脚手架：把 `src/shared/*.ts` 用 esbuild 编译成临时 `.mjs` 再 import。
 *
 * 为什么不直接 import：`src/shared/*.ts` 是 TypeScript，node 跑不了；而 `src/renderer/*`
 * 里的模块在导入时就会读 `window`，也不可能在 node 里加载。所以纯逻辑一律放在
 * `src/shared/`，由这里编译后加载 —— 模式与既有的 `tools/test-log-levels.mjs` 一致，
 * 只是把那 20 行样板收敛到一处，避免每个测试各抄一份。
 *
 * 用法（在 tools/ 下的 *.mjs 里）：
 *   import { importShared, cleanupShared } from "./esbuild-ts.mjs";
 *   const mod = await importShared("src/shared/page-lifecycle.ts");
 *   ...
 *   cleanupShared();
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { tmpdir } from "node:os";

const HERE = dirname(fileURLToPath(import.meta.url));
export const ROOT = join(HERE, "..");

let outDir = null;

function ensureOutDir() {
  if (!outDir) outDir = mkdtempSync(join(tmpdir(), "voxsub-ts-"));
  return outDir;
}

/**
 * 编译并加载 `src/` 下的一个或一组 TS 模块。
 *
 * @param {string | string[]} relativePaths 相对 frontend/ 的路径，例如 "src/shared/backend-status.ts"
 * @param {{ bundle?: boolean }} [options] bundle=true 时把相对导入一起打进产物
 *   （用于加载 `src/renderer/**` 这类有内部 import 的模块；纯 `src/shared/*.ts` 单文件不需要）
 * @returns {Promise<any | any[]>} 单个路径 → 模块；数组 → 模块数组（顺序一致）
 */
export async function importShared(relativePaths, options = {}) {
  const single = typeof relativePaths === "string";
  const files = single ? [relativePaths] : relativePaths;
  const bundle = options.bundle === true;

  const esbuild = join(
    ROOT,
    "node_modules",
    ".bin",
    process.platform === "win32" ? "esbuild.cmd" : "esbuild",
  );
  if (!existsSync(esbuild)) {
    throw new Error("找不到 esbuild，请先在 frontend/ 下 npm install");
  }

  const dir = ensureOutDir();
  const modules = [];
  for (const relative of files) {
    const source = join(ROOT, relative);
    if (!existsSync(source)) throw new Error(`源文件不存在：${source}`);
    const outFile = join(dir, `${relative.replace(/[\\/]/g, "_").replace(/\.ts$/, "")}.mjs`);
    const args = [
      source,
      "--format=esm",
      `--outfile=${outFile}`,
      "--log-level=warning",
      ...(bundle ? ["--bundle", "--platform=node", "--target=node20"] : []),
    ];
    const built = spawnSync(esbuild, args, {
        cwd: ROOT,
        stdio: "pipe",
        encoding: "utf-8",
        shell: process.platform === "win32",
      },
    );
    if (built.status !== 0) {
      throw new Error(`esbuild 编译失败 ${relative}：${(built.stderr ?? "").slice(0, 400)}`);
    }
    modules.push(await import(pathToFileURL(outFile).href));
  }

  return single ? modules[0] : modules;
}

/** 删除临时编译产物。测试结束前调用。 */
export function cleanupShared() {
  if (!outDir) return;
  try {
    rmSync(outDir, { recursive: true, force: true });
  } catch {
    // 临时目录清理失败不影响测试结论
  }
  outDir = null;
}

/**
 * 极简断言收集器：与既有测试的输出格式保持一致
 * （`PASS 名称 — 详情` / 结尾汇总 + 失败时退出码 1）。
 */
export function createReporter(title) {
  const results = [];
  if (title) console.log(`=== ${title} ===\n`);

  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
    return ok;
  };

  const finish = () => {
    const failed = results.filter((r) => !r.ok);
    console.log("\n" + "=".repeat(56));
    console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
    if (failed.length) {
      console.log("\n失败项：");
      for (const item of failed) console.log(`  ${item.name}`);
    }
    cleanupShared();
    process.exit(failed.length ? 1 : 0);
  };

  return { check, finish, results };
}
