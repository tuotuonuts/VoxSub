/** Shared secondary-page frame: fixed compact header, separately scrollable body.
 * The router owns focus, routing and content disposal; this component only builds UI.
 */
import { h, on } from "../dom";
import { tr } from "../i18n";
import { buildButton } from "./button";

export function buildPageFrame(title: string, content: HTMLElement, onBack: () => void): HTMLElement {
  const frame = h("div", { class: "page" });
  const bar = h("header", { class: "page__bar" });
  const back = buildButton("", { title: tr("返回（Esc）") });
  back.classList.add("page__back");
  back.setAttribute("aria-label", tr("返回"));
  back.append(h("span", { class: "page__back-icon", text: "←", "aria-hidden": "true" }),
    h("span", { class: "page__back-text", text: tr("返回") }));
  on(back, "click", onBack);
  bar.append(back, h("span", { class: "page__title", text: title }));
  const body = h("div", { class: "page__content" });
  body.append(content);
  frame.append(bar, body);
  return frame;
}
