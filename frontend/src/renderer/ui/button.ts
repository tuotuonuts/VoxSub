/** Standard action button. Business actions and translations stay with the caller. */
import { h } from "../dom";

export interface ButtonOptions {
  variant?: "primary" | "ghost";
  small?: boolean;
  block?: boolean;
  disabled?: boolean;
  hidden?: boolean;
  title?: string;
}

export function buildButton(label = "", options: ButtonOptions = {}): HTMLButtonElement {
  const variant = options.variant ?? "ghost";
  return h("button", {
    class: `btn btn--${variant}${options.small ? " btn--sm" : ""}${options.block ? " btn--block" : ""}`,
    type: "button",
    text: label,
    disabled: options.disabled,
    hidden: options.hidden,
    title: options.title,
  });
}

/** Compact filter control; the owning page controls selection updates. */
export function buildFilterChip(label: string, active = false): HTMLButtonElement {
  return h("button", {
    class: active ? "filter-chip is-active" : "filter-chip",
    type: "button", text: label, "aria-pressed": String(active),
  });
}
