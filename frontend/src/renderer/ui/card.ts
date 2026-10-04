/** Shared title/body card. Children and incremental updates remain caller-owned. */
import { h } from "../dom";

export function buildCardFrame(title: string, tag: "section" | "div" = "section"): { element: HTMLElement; body: HTMLDivElement } {
  const element = h(tag, { class: "card" });
  if (title) element.append(h("h3", { class: "card__title", text: title }));
  const body = h("div", { class: "card__body" });
  element.append(body);
  return { element, body };
}

export function buildCard(title: string, children: Array<HTMLElement | null>, tag: "section" | "div" = "section"): HTMLElement {
  const { element, body } = buildCardFrame(title, tag);
  for (const child of children) if (child) body.append(child);
  return element;
}
