/**
 * 结果行（§3.7 `ErrorNotice` 的真正重复形态）。
 *
 * ## 为什么是"结果行"而不是"错误通知块"
 *
 * 工作单里的 `ErrorNotice`（统一错误展示）在本项目**没有**稳定的通知块结构：
 * 盘点过五种错误/告警展示，互不兼容 ——
 *   1. 页面状态行：往本页固定节点写一句话（`.ocr__status`、`.log-state`、
 *      `.legacy-state`），节点由各页自己建、位置各异 → 没有可共用的结构；
 *   2. 向导判定块 `.wiz__verdict`：3 处，但三处的内容结构不同（单行 / 带清单 /
 *      带按钮），塞进一个组件就是"把不同业务硬塞进一个组件"；
 *   3. 字段级警告 `.field__hint--warn`：本来就是一行 `<p>`，已被
 *      `buildField({ hint })` 覆盖；
 *   4. 自检徽标 `STATUS_CLASS`：一个类名映射，见下；
 *   5. **结果行 `.check-row`：这一种才是真重复** —— 三处逐字相同的三层结构。
 *
 * ## 三处调用点与三份重复的映射表
 *
 * ```
 * div.check-row.is-ok|is-warn|is-fail
 *   span.check-row__mark    ✓ / ! / ✕
 *   span.check-row__name
 *   span.check-row__detail
 * ```
 *
 *   · `views/diagnostics.ts` 自检结果（自带 STATUS_MARK / STATUS_CLASS 两张表）
 *   · `views/migration.ts` 迁移前的磁盘风险清单（自带 RISK_CLASS + 行内三目）
 *   · `views/migration.ts` 迁移结果的成功项（硬编码 `is-ok` 与 "✓"）
 *
 * 同一个"状态 → 类名 + 标记符号"映射被写了三遍，其中两份是 `Record<string,string>`
 * 查表、一份是三目表达式。合并后只有这一处知道 `✓`/`!`/`✕` 与 `is-ok`/`is-warn`/
 * `is-fail` 的对应关系；调用点只负责给出**状态**与**文案**。
 *
 * ## DOM 与原实现逐字一致
 *
 * 类名、层级、顺序、文本都不变（原来 `STATUS_CLASS[x] ?? ""` 在未知状态下会
 * 留下一个尾随空格，这里同样保留），所以外观与 CSS 命中不受影响。
 *
 * 组件不认识任何业务词汇：风险等级 safe/conditional/exposed 到
 * ok/warn/fail 的翻译留在迁移页里，这里只收 ok/warn/fail。
 */

import { h } from "../dom";

/** 结果行的三种状态。 */
export type StatusLevel = "ok" | "warn" | "fail";

const LEVEL_CLASS: Record<string, string> = {
  ok: "is-ok",
  warn: "is-warn",
  fail: "is-fail",
};

const LEVEL_MARK: Record<string, string> = {
  ok: "✓",
  warn: "!",
  fail: "✕",
};

/** 状态 → 类名后缀；未知状态返回空串（与原先的 `?? ""` 一致）。 */
export function statusLevelClass(level: StatusLevel | string): string {
  return LEVEL_CLASS[level] ?? "";
}

/** 状态 → 标记符号；未知状态返回 `?`（与原先的 `?? "?"` 一致）。 */
export function statusLevelMark(level: StatusLevel | string): string {
  return LEVEL_MARK[level] ?? "?";
}

export interface StatusRowOptions {
  /** ok / warn / fail —— 决定类名与默认标记符号。 */
  level: StatusLevel | string;
  /** 行首名称（检查项名 / 数据项名）。 */
  name: string;
  /**
   * 行尾说明。字符串走 textContent；节点数组则原样插入
   * （迁移页的风险行里有 `<strong>` / `<br>` / `<code>` 混排）。
   */
  detail?: string | Array<Node | string | null | undefined | false>;
  /** 覆盖默认标记符号。省略时按 level 取 `✓` / `!` / `✕`。 */
  mark?: string;
}

/** 构造一条结果行。 */
export function buildStatusRow(options: StatusRowOptions): HTMLElement {
  const { level, name, detail, mark } = options;
  const row = h("div", { class: `check-row ${statusLevelClass(level)}` });

  const detailEl =
    detail === undefined
      ? h("span", { class: "check-row__detail" })
      : Array.isArray(detail)
        ? h("span", { class: "check-row__detail" }, detail)
        : h("span", { class: "check-row__detail", text: detail });

  row.append(
    h("span", { class: "check-row__mark", text: mark ?? statusLevelMark(level) }),
    h("span", { class: "check-row__name", text: name }),
    detailEl,
  );
  return row;
}
