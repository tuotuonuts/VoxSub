/** Independent compact multi-select filters. No store, IPC or global listeners. */
import { h, on } from "../dom";
import { buildFilterChip } from "./button";

export interface MultiFilterOptions<T extends string> {
  label: string;
  allLabel: string;
  onChange: (values: readonly T[]) => void;
}

export function buildMultiFilter<T extends string>(
  choices: ReadonlyArray<readonly [T, string]>,
  initial: readonly T[],
  options: MultiFilterOptions<T>,
): HTMLElement {
  const values = [...new Set(choices.map(([value]) => value))];
  const selected = new Set(initial.filter(value => values.includes(value)));
  const group = h("div", { class: "filter-bar", role: "group", "aria-label": options.label });
  const all = buildFilterChip(options.allLabel);
  const buttons = new Map<T, HTMLButtonElement>();
  const refresh = (): void => {
    const mark = (button: HTMLButtonElement, active: boolean): void => {
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    };
    mark(all, values.length > 0 && selected.size === values.length);
    for (const [value, button] of buttons) mark(button, selected.has(value));
  };
  const changed = (): void => {
    refresh();
    // Emit a fresh ordered array: callbacks cannot mutate this component's state.
    options.onChange(values.filter(value => selected.has(value)));
  };
  on(all, "click", () => {
    if (!values.length || selected.size === values.length) return;
    for (const value of values) selected.add(value);
    changed();
  });
  group.append(h("span", { class: "hint", text: options.label }), all);
  for (const [value, label] of choices) {
    if (buttons.has(value)) continue;
    const button = buildFilterChip(label);
    buttons.set(value, button);
    on(button, "click", () => {
      if (selected.has(value)) selected.delete(value);
      else selected.add(value);
      changed();
    });
    group.append(button);
  }
  refresh();
  return group;
}
