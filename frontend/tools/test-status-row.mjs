#!/usr/bin/env node
/**
 * 结果行（StatusRow）—— 离线行为测试。
 *
 * ## 守的是什么
 *
 * `div.check-row.is-*` + `span.check-row__mark|__name|__detail` 这套三层结构在
 * 三处逐字相同（诊断页自检结果、迁移页磁盘风险清单、迁移页成功项），而
 * 「状态 → 类名 + 标记符号」的映射被写了**三遍**（两张查表 + 一个行内三目）。
 * 合并成组件后必须守住四件事：
 *
 *   1. **类名不能变**：`.check-row` / `__mark` / `__name` / `__detail` 与
 *      `is-ok|is-warn|is-fail` 是 app.css 在命中的契约，改一个字母整行样式就塌。
 *      这里拿 app.css 逐个核对（与 test-progress-bar.mjs 同一把尺子）；
 *   2. **标记符号与状态一一对应**：`✓ / ! / ✕`，且**未知状态退化成 `?`
 *      而不是猜一个**（猜错会把"未知"显示成"通过"，那是最糟的失败方向）；
 *   3. **detail 的两种形态都要对**：字符串走 textContent（不解析 HTML，防注入），
 *      节点数组原样插入（迁移页的风险行里有 strong/br/code 混排）；
 *   4. **组件里不许有写死的中文**（DESIGN.md 的 UI 语言契约）：文案一律由
 *      调用方经 tr() 给出，组件只认 ok/warn/fail 三个词。
 *
 * 用法：node tools/test-status-row.mjs
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { installMiniDom } from "./mini-dom.mjs";
import { importShared, createReporter, ROOT } from "./esbuild-ts.mjs";

const dom = installMiniDom();
const { document } = dom;
const { check, finish } = createReporter("结果行（§3.7 StatusRow）");
const { buildStatusRow, statusLevelClass, statusLevelMark } = await importShared(
  "src/renderer/ui/status-row.ts", { bundle: true });

const css = readFileSync(join(ROOT, "src/renderer/app.css"), "utf-8");

console.log("=== 结构与样式契约 ===\n");
{
  const row = buildStatusRow({ level: "ok", name: "asr", detail: "就绪" });
  document.body.append(row);

  check("行是 div.check-row.is-ok",
    row.tagName === "DIV" && row.className === "check-row is-ok", row.className);
  check("恰好三个子节点（mark / name / detail）", row.childNodes.length === 3, String(row.childNodes.length));

  const [mark, name, detail] = row.childNodes;
  check("第一个是 span.check-row__mark",
    mark.tagName === "SPAN" && mark.className === "check-row__mark", mark.className);
  check("第二个是 span.check-row__name",
    name.tagName === "SPAN" && name.className === "check-row__name", name.className);
  check("第三个是 span.check-row__detail",
    detail.tagName === "SPAN" && detail.className === "check-row__detail", detail.className);
  check("name 文本来自调用方", name.textContent === "asr", name.textContent);
  check("detail 文本来自调用方", detail.textContent === "就绪", detail.textContent);

  for (const cls of [".check-row", ".check-row__mark", ".check-row__name",
                     ".check-row__detail", ".check-row.is-ok .check-row__mark",
                     ".check-row.is-warn .check-row__mark", ".check-row.is-fail .check-row__mark"]) {
    check(`app.css 里有 ${cls}`, css.includes(cls), cls);
  }
}

console.log("\n=== 状态 → 类名与标记 ===\n");
{
  const cases = [["ok", "is-ok", "✓"], ["warn", "is-warn", "!"], ["fail", "is-fail", "✕"]];
  for (const [level, cls, symbol] of cases) {
    const row = buildStatusRow({ level, name: "x" });
    check(`${level} → 类名 ${cls}`, row.className === `check-row ${cls}`, row.className);
    check(`${level} → 标记 ${symbol}`, row.childNodes[0].textContent === symbol, row.childNodes[0].textContent);
  }

  check("未知状态映射到空类名（与旧实现 `?? \"\"` 一致）",
    statusLevelClass("nonsense") === "", JSON.stringify(statusLevelClass("nonsense")));
  // 注意：旧实现在未知状态下 className 会带一个尾随空格（`check-row `）。
  // 那条**在本测试替身里无法断言** —— mini-dom 的 className setter 按空白切分后
  // 用 Set 重建，尾随空格被规范化掉了（真实 DOM 会原样保留）。这是替身的取舍，
  // 不是组件问题；不要为了这条去"修"组件。
  check("未知状态的类名不会多出别的类",
    buildStatusRow({ level: "nonsense", name: "x" }).className === "check-row",
    buildStatusRow({ level: "nonsense", name: "x" }).className);
  check("未知状态退化成 ? 而不是猜成通过",
    statusLevelMark("nonsense") === "?" && buildStatusRow({ level: "nonsense", name: "x" }).childNodes[0].textContent === "?",
    statusLevelMark("nonsense"));

  const overridden = buildStatusRow({ level: "ok", name: "x", mark: "★" });
  check("mark 可覆盖（迁移页某些行需要别的符号）", overridden.childNodes[0].textContent === "★");
}

console.log("\n=== detail 的两种形态 ===\n");
{
  const asText = buildStatusRow({ level: "ok", name: "x", detail: "<b>不是 HTML</b>" });
  const textNode = asText.childNodes[2];
  check("字符串走 textContent：字面量原样保留（被当成文本，不是标记）",
    textNode.textContent === "<b>不是 HTML</b>", textNode.textContent);
  check("字符串 detail 只产生一个子节点，且它不是元素",
    textNode.childNodes.length === 1 && textNode.childNodes[0].tagName === undefined,
    `children=${textNode.childNodes.length} tag=${textNode.childNodes[0]?.tagName}`);
  // 注：这里本来想断言 nodeType === 3，但 mini-dom 的文本节点没实现 nodeType
  // （替身的取舍）。用 tagName === undefined 表达"它不是一个元素"同样到位。
  check("没有把 <b> 解析成元素（不解析 HTML，防注入）",
    textNode.querySelector("b") === null && textNode.childNodes[0].tagName === undefined,
    String(textNode.querySelector("b")));

  const asNodes = buildStatusRow({
    level: "warn", name: "models",
    detail: [document.createElement("strong"), document.createElement("br"), document.createElement("code")],
  });
  const detail = asNodes.childNodes[2];
  check("节点数组原样插入（迁移页的 strong/br/code 混排）",
    detail.childNodes.length === 3 &&
    detail.childNodes[0].tagName === "STRONG" &&
    detail.childNodes[1].tagName === "BR" &&
    detail.childNodes[2].tagName === "CODE",
    detail.childNodes.map((n) => n.tagName).join(","));

  const withoutDetail = buildStatusRow({ level: "ok", name: "x" });
  check("不给 detail 时仍保留空的 __detail 节点（结构与旧的 h(...) 一致）",
    withoutDetail.childNodes[2].className === "check-row__detail" &&
    withoutDetail.childNodes[2].textContent === "");
}

console.log("\n=== 组件里不许有写死的中文 ===\n");
{
  const source = readFileSync(join(ROOT, "src/renderer/ui/status-row.ts"), "utf-8");
  // 去掉注释再找 CJK：注释里可以有中文，代码里的字符串不可以。
  const withoutComments = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/[^\n]*/g, "");
  const cjk = withoutComments.match(/[\u4e00-\u9fff]+/g) ?? [];
  check("代码（非注释）里没有中文字面量", cjk.length === 0, cjk.join(" / "));
  check("只有 ok/warn/fail 三个等级词汇", /"ok" \| "warn" \| "fail"/.test(source));
}

finish();
