/**
 * 路径选择字段（工作单 §3.7 的 PathPicker）。
 *
 * ## 为什么抽
 *
 * 「当前位置（只读路径） + 打开 + 更改位置」这套表现在设置页出现了三处
 * （模型目录 / OCR 缓存位置 / 迁移已有模型），前两处的 DOM、按钮语义与
 * 回调顺序完全一致，只有文案和落库的键不同。真正值得钉住的是那条**取消契约**：
 *
 *   · 用户在系统对话框里点取消（`pick()` 返回空）→ **不写配置、不派发刷新、
 *     不改显示**。这条一旦漏掉，用户点一次取消就会被静默写成"自定义路径"；
 *   · 当前路径为空时「打开」按钮不发命令（发出去只会让用户以为点了没反应）；
 *   · 选中之后才交给调用方落库（`onPicked`）。
 *
 * ## 边界
 *
 * 组件不知道"路径"会存到哪里 —— 落库、派发刷新都由 `onPicked` 回调完成；
 * 文案（标签、说明、按钮、空值占位）全部由调用方经 `tr()` 传入，
 * 本文件里没有用户可见字符串。
 *
 * DOM 形状与抽取前一致：`field`（只读路径）与 `actions`（按钮行）作为两个
 * **独立返回的兄弟节点**，由调用点决定插在哪 —— OCR 缓存那一处的顺序是
 * 「路径字段 → 保留张数字段 → 按钮行」，组件若自作主张替调用点追加到末尾，
 * 就会把顺序改掉（等于改了外观）。
 */
import { h, on } from "../dom";
import { buildField } from "./field";

export interface PathPickerOptions {
  /** 字段标签，例如「当前位置」。 */
  label: string;
  /** 当前路径；空串表示还没设置，显示 `emptyText`。 */
  value: string;
  /** 未设置时的占位文案，例如「使用默认位置」。 */
  emptyText: string;
  /** 字段说明（可选）。 */
  hint?: string | undefined;
  /** 「打开」按钮文案。 */
  openLabel: string;
  /** 「更改」按钮文案。 */
  changeLabel: string;
  /**
   * 取「打开」要定位的目标路径。
   *
   * 与 `value` 分开是必要的：模型目录的显示值可能为空（用默认位置），
   * 但打开的目标来自 store 里后端回传的真实路径。
   */
  openFor: () => string;
  onOpen: (path: string) => void;
  /** 唤起系统选择器；返回空值表示用户取消。 */
  pick: () => Promise<string | null | undefined>;
  /** 只在用户**真的选到了路径**时调用。 */
  onPicked: (path: string) => void | Promise<void>;
}

export interface PathPickerHandle {
  /** 字段节点（只读路径 + 标签 + 说明），由调用点插入。 */
  readonly field: HTMLElement;
  /** 按钮行（打开 / 更改），由调用点插入。 */
  readonly actions: HTMLElement;
  /** 更新显示的路径（空值回落为占位文案）。 */
  setValue(value: string): void;
}

export function buildPathPicker(options: PathPickerOptions): PathPickerHandle {
  const valueEl = h("span", {
    class: "readonly-value",
    text: options.value || options.emptyText,
  });

  const openBtn = h("button", { class: "btn btn--ghost", type: "button", text: options.openLabel });
  on(openBtn, "click", () => {
    // 没有可打开的位置就不发命令：发出去只会表现为"点了没反应"
    const target = options.openFor();
    if (!target) return;
    options.onOpen(target);
  });

  const changeBtn = h("button", { class: "btn btn--ghost", type: "button", text: options.changeLabel });
  on(changeBtn, "click", async () => {
    const picked = await options.pick();
    // 取消：什么都不做（不落库、不刷新、不改显示）
    if (!picked) return;
    await options.onPicked(picked);
  });

  const field = buildField({ label: options.label, control: valueEl, hint: options.hint });
  const actions = h("div", { class: "tuning-actions" }, [openBtn, changeBtn]);

  return {
    field,
    actions,
    setValue(value: string): void {
      valueEl.textContent = value || options.emptyText;
    },
  };
}
