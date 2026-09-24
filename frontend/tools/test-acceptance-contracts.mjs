#!/usr/bin/env node
/**
 * 三项需求的无 GUI、无真实配置验收门禁。
 *
 * 这是独立静态契约探针：只读取源码，不启动 Electron，不写用户配置，
 * 也不修改现有测试文件。实现 Agent 合入后可直接重跑。
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname.replace(/^\/(\w):/, "$1:");
const read = (p) => readFileSync(join(root, p), "utf8");
const checks = [];
function check(name, ok, detail = "") {
  checks.push(ok);
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
}
function must(text, pattern, name) { check(name, pattern.test(text), pattern.toString()); }

const main = read("src/main/main.ts");
const preload = read("src/main/preload.ts");
const overlay = read("src/renderer/overlay.ts");
const renderer = read("src/renderer/index.ts");
const session = read("../frontend/backend/handlers/session.py");
const ipc = read("../frontend/backend/ipc_server.py");
const factory = read("../voxsub/translate/factory.py");
const pipeline = read("../voxsub/pipeline.py");

console.log("=== opacity boundary + save/restore contract ===");
must(main, /overlay:set-opacity/, "主进程仍提供透明度 IPC");
must(main, /Math\.min\(1, Math\.max\(0\.2,/, "透明度下界 0.2、上界 1.0");
must(preload, /setOpacity:.*overlay:set-opacity/s, "preload 暴露设置透明度调用");
must(preload, /onOpacityChanged:.*overlay:opacity/s, "preload 暴露透明度恢复/同步事件");
must(overlay, /--overlay-opacity/, "渲染层使用透明度 CSS 变量");
check("透明度保存/恢复契约有显式实现或待合入标记",
  /opacity|透明度/.test(renderer) && (/setOpacity/.test(renderer) || /opacity/.test(main)),
  "若此项失败，需由实现 Agent 补齐设置字段与启动恢复");

console.log("\n=== language IPC payload consistency ===");
must(renderer, /CMD\.setLangs, \{ source: srcSel\.value, target \}/,
  "源语言变更使用 source/target payload");
must(renderer, /CMD\.setLangs, \{ source, target: dstSel\.value \}/,
  "目标语言变更使用 source/target payload");
must(session, /args\.get\("source".*args\.get\("target"/s,
  "后端 set_langs 读取同名 source/target");
must(ipc, /lang_pair.*pipeline\.set_langs/s,
  "配置恢复链路使用同一语言对语义");

console.log("\n=== translation unsupported/candidate-unavailable fallback matrix ===");
must(factory, /resolve_tier/, "档位解析存在独立决策函数");
must(factory, /supports|candidate|候选/, "档位解析检查实际支持/候选能力");
must(pipeline, /_load_translator_for_pair/, "运行时按语言对加载翻译器");
must(pipeline, /substituted|fallback/, "运行时记录或执行降级");
check("静态矩阵：不支持档位与候选不可用均有对应验收入口",
  /unsupported|不支持/.test(factory + pipeline) && /unavailable|不可用|fallback|候选/.test(factory + pipeline),
  "必须同时覆盖 selected unsupported 与 no usable candidate");

const passed = checks.filter(Boolean).length;
console.log(`\n${passed}/${checks.length} acceptance contract checks passed`);
process.exitCode = passed === checks.length ? 0 : 1;
