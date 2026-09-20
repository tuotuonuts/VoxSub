#!/usr/bin/env node
/**
 * 进度条（JobProgress）—— 离线行为测试。
 *
 * ## 守的是什么
 *
 * 「轨道 + 填充 + 标签」这套内芯在本项目有四处（C 模式文件进度、模型下载、
 * 迁移总进度、迁移每项进度），原先各写一遍"把百分比写进 style.width、
 * 把数字写进标签"。合并成组件后必须守住三件事：
 *
 *   1. **类名不能变**：`.progress__track` / `.progress__fill` / `.progress__label`
 *      是 app.css 与外部自动化（smoke-ui.mjs 的选择器）依赖的契约，
 *      改一个字母进度条就消失。所以这里拿 app.css 逐个核对；
 *   2. **未知进度不瞎写**：`setPercent(null)` 表示"不知道进度"，
 *      不能把宽度写成 0（会被读成"卡住了"），也不能留下非法宽度；
 *   3. **越界夹取**：completed 超过 total 时不能让宽度超出轨道。
 *
 * 另外确认状态只加类名、不写文案 —— 文案必须由调用方经 `tr()` 给出
 * （DESIGN.md 的 UI 语言契约：组件里不许出现写死的中文）。
 *
 * 用法：node tools/test-progress-bar.mjs
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { installMiniDom } from "./mini-dom.mjs";
import { importShared, createReporter, ROOT } from "./esbuild-ts.mjs";

const dom = installMiniDom();
const { document } = dom;
const { check, finish } = createReporter("进度条（§3.7 JobProgress）");
const { buildProgressBar } = await importShared("src/renderer/ui/progress.ts", { bundle: true });

console.log("=== 结构与样式契约 ===\n");
{
  const bar = buildProgressBar();
  document.body.append(bar.track);
  check("轨道是 div.progress__track", bar.track.tagName === "DIV" && bar.track.className === "progress__track", bar.track.className);
  check("填充是 .progress__fill 且在轨道内", bar.fill.className === "progress__fill" && bar.fill.parentNode === bar.track);
  check("标签默认是 span.progress__label", bar.label.tagName === "SPAN" && bar.label.className === "progress__label", bar.label.className);
  check("初始标签为空（文案由调用方给）", bar.label.textContent === "");

  const overridden = buildProgressBar({ labelClass: "cell__progress-label" });
  check("标签类名可覆盖（模型目录用的是小字号）", overridden.label.className === "cell__progress-label");
  const withText = buildProgressBar({ labelText: "0%" });
  check("初始标签可给（迁移页是 0%）", withText.label.textContent === "0%");
}

{
  // 类名与 CSS 的契约：app.css 里必须有对应规则，否则进度条会没有样式
  const css = readFileSync(join(ROOT, "src/renderer/app.css"), "utf-8");
  for (const cls of ["progress__track", "progress__fill", "progress__label", "cell__progress-label"]) {
    check(`app.css 里仍有 .${cls} 规则`, css.includes(`.${cls}`), cls);
  }
}

console.log("\n=== 百分比与文案 ===\n");
{
  const bar = buildProgressBar();
  bar.setPercent(42.4);
  check("百分比四舍五入后写进 width", bar.fill.style.getPropertyValue("width") === "42%", bar.fill.style.getPropertyValue("width"));
  check(
    "同时渲染到 style 属性上（外部工具读的是属性）",
    (bar.fill.getAttribute("style") ?? "").replace(/\s+/g, "").includes("width:42%"),
    bar.fill.getAttribute("style") ?? "",
  );

  bar.setPercent(0);
  check("0% 写 0%（不是空值）", bar.fill.style.getPropertyValue("width") === "0%");

  bar.setPercent(100);
  check("100% 写 100%", bar.fill.style.getPropertyValue("width") === "100%");

  bar.setLabel("转录中 · 42%");
  check("标签文案按原样写入", bar.label.textContent === "转录中 · 42%");

  bar.setLabel("");
  check("标签可清空", bar.label.textContent === "");
}

console.log("\n=== 未知进度与越界 ===\n");
{
  const bar = buildProgressBar();
  bar.setPercent(30);
  const before = bar.fill.style.getPropertyValue("width");
  bar.setPercent(null);
  check("setPercent(null) 不动宽度（未知进度≠0%）", bar.fill.style.getPropertyValue("width") === before, `${before} → ${bar.fill.style.getPropertyValue("width")}`);

  bar.setPercent(Number.NaN);
  check("NaN 同样不动宽度", bar.fill.style.getPropertyValue("width") === before);

  bar.setPercent(-10);
  check("低于 0 夹到 0%", bar.fill.style.getPropertyValue("width") === "0%");

  bar.setPercent(150);
  check("高于 100 夹到 100%（不会超出轨道）", bar.fill.style.getPropertyValue("width") === "100%");

  // 与 percent() 的显示口径一致：总量为 0 时标签是「—」，宽度不动
  bar.setPercent(null);
  bar.setLabel("转录中 · —");
  check("总量为 0 时标签可显示「—」且宽度不变", bar.label.textContent === "转录中 · —" && bar.fill.style.getPropertyValue("width") === "100%");
}

console.log("\n=== 状态只加类名、不写文案 ===\n");
{
  const bar = buildProgressBar({ labelText: "0%" });
  document.body.append(bar.track, bar.label);

  bar.setState("running");
  check("running → is-running", bar.track.classList.contains("is-running"), bar.track.className);
  bar.setState("done");
  check("done → is-done", bar.track.classList.contains("is-done"), bar.track.className);
  check("切换状态时清掉上一个状态类", !bar.track.classList.contains("is-running"));
  bar.setState("failed");
  check("failed → is-failed", bar.track.classList.contains("is-failed"));
  check("failed 时只剩一个状态类", ["is-idle", "is-running", "is-done"].every((c) => !bar.track.classList.contains(c)), bar.track.className);
  bar.setState("idle");
  check("idle → 只留 is-idle", bar.track.className.includes("is-idle") && !bar.track.className.includes("is-failed"), bar.track.className);

  check("setState 不写标签文案（文案必须过 tr()）", bar.label.textContent === "0%", bar.label.textContent);
  check("轨道类名始终保留 progress__track", bar.track.classList.contains("progress__track"));
}

console.log("\n=== 组件源码里没有写死文案 ===\n");
{
  const raw = readFileSync(join(ROOT, "src/renderer/ui/progress.ts"), "utf-8");
  check("没有导入 store/i18n/业务页面", !raw.includes("../store") && !raw.includes("../i18n") && !raw.includes("views/"));

  // 去掉注释后再找中文：注释里可以（也应该）用中文解释，代码里不行 ——
  // 用户可见文案必须经调用方的 tr()，否则英文界面会漏翻。
  const code = raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  const cjk = code.match(/[\u4e00-\u9fa5]/g) ?? [];
  check("去掉注释后代码里没有中文字面量", cjk.length === 0, `发现 ${cjk.length} 个：${cjk.join("")}`);
}

dom.restore();
finish();
