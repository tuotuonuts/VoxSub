import { h } from "../dom";
export type BadgeTone = "neutral" | "positive" | "attention" | "danger";
/** Static label plus semantic tone; business state and translated copy stay with callers. */
export function buildBadge(label: string, tone: BadgeTone = "neutral", hint = ""): HTMLElement {
  return h("span", { class: `badge badge--${tone}`, text: label, title: hint || undefined });
}
