/** Reusable empty result state. Caller owns translated text and the optional action. */
import { h } from "../dom";
export function buildEmptyState(title: string, detail: string, action?: HTMLElement): HTMLElement {
  return h("section", { class: "empty-state", role: "status" }, [
    h("p", { class: "empty-state__title", text: title }),
    h("p", { class: "empty-state__detail", text: detail }), action,
  ]);
}
