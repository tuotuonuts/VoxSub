/** Shared settings-style tab navigation; pane lifecycles stay with the page. */
import { h, on } from "../dom";

export function buildTabNav(labels: readonly string[], onSelect: (index: number) => void): HTMLElement {
  const nav = h("nav", { class: "settings__nav", role: "tablist" });
  const buttons = labels.map((label, index) => {
    const button = h("button", {
      class: index === 0 ? "settings__tab is-active" : "settings__tab",
      type: "button", role: "tab", text: label,
      "aria-selected": String(index === 0),
    });
    on(button, "click", () => {
      buttons.forEach((item, i) => {
        item.classList.toggle("is-active", i === index);
        item.setAttribute("aria-selected", String(i === index));
      });
      onSelect(index);
    });
    return button;
  });
  nav.append(...buttons);
  return nav;
}
