/** Update native options without replacing the focused control or firing change events. */
import { h } from "../dom";

export function replaceSelectOptions(
  select: HTMLSelectElement, value: string,
  options: ReadonlyArray<readonly [string, string]>,
  placeholder?: string,
): void {
  const nodes = options.map(([id, label]) => {
    const option = h("option", { value: id, text: label });
    option.selected = id === value;
    return option;
  });
  if (!options.some(([id]) => id === value) && placeholder !== undefined) {
    const option = h("option", { value: "", text: placeholder });
    option.disabled = true;
    option.selected = true;
    nodes.unshift(option);
  }
  select.replaceChildren(...nodes);
  select.value = options.some(([id]) => id === value) ? value : (placeholder !== undefined ? "" : options[0]?.[0] ?? "");
}
