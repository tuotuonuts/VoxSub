import { h } from "../dom";
/** Five-point reference rating; always visibly labelled, never confused with progress. */
export function buildRating(score: number, label: string, hint: string): HTMLElement {
  const valid = Number.isFinite(score) && score >= 0;
  const filled = valid ? Math.round(Math.min(100, score) / 20) : 0;
  const root = h("span", { class: "rating", role: "img", title: `${label} ${valid ? filled : "—"}/5 · ${hint}`,
    "aria-label": `${label} ${valid ? filled : "—"}/5 · ${hint}` });
  root.append(h("span", { class: "rating__label", text: label, "aria-hidden": "true" }));
  const dots = h("span", { class: "pip-row", "aria-hidden": "true" });
  for (let i = 0; i < 5; i++) dots.append(h("i", { class: i < filled ? "pip is-on" : "pip" }));
  root.append(dots);
  return root;
}
