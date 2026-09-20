/**
 * 设置类字段（工作单 §3.7 的 SettingsField）。
 *
 * ## 为什么单独成组件
 *
 * "标签 + 控件 + 说明 + 禁用原因" 这套表现在设置页出现了 26 次，结构完全一致，
 * 而其中**禁用原因是唯一有行为的部分**：控件置灰时必须说清"被谁接管"，
 * 只把控件变灰会让用户以为界面坏了（见设置页"识别调优"一节的注释）。
 * 这段逻辑原先只能靠起 Electron 手工点开设置页验证，抽到这里之后由
 * `tools/test-ui-components.mjs` 在 node 里直接断言渲染结果。
 *
 * ## 边界
 *
 * 只认参数：标签文案、控件节点、说明、禁用原因。不导入任何业务页面、
 * 不读配置、不调后端 —— 文案一律由调用方经 `tr()` 传入（本文件里没有
 * 用户可见字符串，DESIGN.md「UI 语言契约」要求静态文案必须过 tr()）。
 *
 * DOM 形状与抽取前逐字节一致：
 *   div.field[.is-locked] > label.field__label + 控件 + p.field__hint ×N
 * 禁用原因排在说明之前（先讲"为什么不能改"，再讲"这项是什么"）。
 */
import { h } from "../dom";

export interface FieldOptions {
  /** 字段标签。省略时不渲染 label —— 组合控件（如自带标签的开关）已自带标签，再套一层会出现两个标题。 */
  label?: string | undefined;
  /** 受控的输入控件本体（input / select / 自定义组）。 */
  control: HTMLElement;
  /** 说明文字。 */
  hint?: string | undefined;
  /** 禁用原因：非空表示这一项当前不可改，并给出被谁接管。 */
  lockedBy?: string | undefined;
}

export function buildField(options: FieldOptions): HTMLElement {
  const wrap = h("div", { class: options.lockedBy ? "field is-locked" : "field" });
  if (options.label) wrap.append(h("label", { class: "field__label", text: options.label }));
  wrap.append(options.control);

  const notes: string[] = [];
  if (options.lockedBy) notes.push(options.lockedBy);
  if (options.hint) notes.push(options.hint);
  for (const note of notes) wrap.append(h("p", { class: "field__hint", text: note }));

  return wrap;
}
