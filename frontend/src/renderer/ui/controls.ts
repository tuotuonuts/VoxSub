/** Stateless form primitives. Labels are translated by callers; no store or IPC dependencies. */
import { h, on } from "../dom";
import { replaceSelectOptions } from "./select-options";

export function buildTextInput(value: string, onChange: ((v: string) => void) | undefined, opts?: { type?: string; placeholder?: string }): HTMLInputElement {
  const input = h("input", {
    class: "input",
    type: opts?.type ?? "text",
    value,
    placeholder: opts?.placeholder ?? "",
  });
  if (onChange) on(input, "change", () => onChange(input.value));
  return input;
}

export function buildSelect<T extends string>(
  value: T,
  options: ReadonlyArray<readonly [T, string]>,
  onChange?: (v: T) => void,
): HTMLSelectElement {
  const sel = h("select", { class: "select" });
  replaceSelectOptions(sel, value, options);
  if (onChange) on(sel, "change", () => onChange(sel.value as T));
  return sel;
}

/** 单选组：用圆形指示，避免原生控件在深色档下几何变形。 */
export function buildRadioGroup<T extends string>(
  value: T,
  options: ReadonlyArray<readonly [T, string, string?]>,
  onChange: (v: T) => void,
): HTMLElement {
  const group = h("div", { class: "radio-group", role: "radiogroup" });
  for (const [val, label, badge] of options) {
    const item = h("button", {
      class: val === value ? "radio is-checked" : "radio",
      type: "button",
      role: "radio",
      "aria-checked": String(val === value),
    });
    item.append(h("i", { class: "radio__dot" }), h("span", { text: label }));
    // 徽标用于说明"这一档在当前语言对下不可用"之类的限定条件。
    // 只放在标签旁边而不禁用选项：用户仍可选中它（换语言后就能用），
    // 禁掉会让他以为界面坏了。
    if (badge) item.append(h("span", { class: "radio__badge", text: badge }));
    on(item, "click", () => {
      group.querySelectorAll(".radio").forEach((n) => {
        n.classList.remove("is-checked");
        n.setAttribute("aria-checked", "false");
      });
      item.classList.add("is-checked");
      item.setAttribute("aria-checked", "true");
      onChange(val);
    });
    group.append(item);
  }
  return group;
}

export function buildToggleSwitch(checked: boolean, label: string, onChange?: (v: boolean) => void): HTMLElement {
  const wrap = h("label", { class: "switch" });
  const input = h("input", { type: "checkbox" });
  input.checked = checked;
  if (onChange) on(input, "change", () => onChange(input.checked));
  wrap.append(input, h("span", { class: "switch__track" }), h("span", { class: "switch__label", text: label }));
  return wrap;
}
