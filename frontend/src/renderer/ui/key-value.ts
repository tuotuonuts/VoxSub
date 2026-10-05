/** Reusable key/value row with optional multi-line values. All content is text, no HTML or IPC. */
import { h } from "../dom";
export function buildKeyValueRow(label: string, value: string | readonly string[]): HTMLElement {
  const content = typeof value === "string"
    ? h("span", { class: "kv__value", text: value })
    : h("ul", { class: "kv__value kv__list" }, value.map(text => h("li", { text })));
  return h("div", { class: "kv" }, [h("span", { class: "kv__key", text: label }), content]);
}
