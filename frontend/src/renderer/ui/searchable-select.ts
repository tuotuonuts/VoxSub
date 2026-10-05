/** Editable, controlled combobox. No store, IPC or implicit persistence. */
import { h, on } from "../dom";
import { buildTextInput } from "./controls";
import { buildButton } from "./button";

export interface SearchableOption { value: string; label: string; aliases?: readonly string[] }
export interface SearchableSelect {
  element: HTMLElement;
  input: HTMLInputElement;
  update: (options: readonly SearchableOption[], value: string, disabled: boolean) => void;
  dispose: () => void;
}
let nextId = 0;
const normalized = (text: string) => text.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase().trim();

export function matchSelectOptions(options: readonly SearchableOption[], query: string): SearchableOption[] {
  const words = normalized(query).split(/\s+/).filter(Boolean);
  return options.filter(option => {
    const text = normalized([option.label, option.value, ...(option.aliases ?? [])].join(" "));
    return words.every(word => text.includes(word));
  });
}

export function buildSearchableSelect(
  labels: { label: string; placeholder: string; empty: string; toggle: string },
  onChange: (value: string) => void,
): SearchableSelect {
  const id = `searchable-select-${++nextId}`;
  const input = buildTextInput("", undefined, { placeholder: labels.placeholder });
  input.classList.add("searchable-select__input");
  for (const [name, text] of Object.entries({ role: "combobox", "aria-label": labels.label,
    "aria-autocomplete": "list", "aria-controls": id, "aria-expanded": "false",
    autocomplete: "off", spellcheck: "false", maxlength: "160", title: labels.placeholder })) input.setAttribute(name, text);
  const toggle = buildButton("▾", { small: true, title: labels.toggle });
  toggle.classList.add("searchable-select__toggle");
  for (const [name, text] of Object.entries({ "aria-label": labels.toggle, "aria-controls": id,
    "aria-expanded": "false", tabindex: "-1" })) toggle.setAttribute(name, text);
  const list = h("div", { id, role: "listbox", "aria-label": labels.label });
  const empty = h("div", { class: "searchable-select__empty", role: "status", text: labels.empty });
  const popup = h("div", { class: "searchable-select__popup", popover: "manual", hidden: true }, [list, empty]);
  const element = h("div", { class: "searchable-select" }, [input, toggle, popup]);
  let options: readonly SearchableOption[] = [], value = "", key = "";
  let shown: SearchableOption[] = [], active = -1, opened = false, disposed = false, composing = false;
  const selectedLabel = () => options.find(option => option.value === value)?.label ?? "";

  function position() {
    const rect = input.getBoundingClientRect();
    const width = Math.min(Math.max(rect.width, 220), window.innerWidth - 16);
    const below = window.innerHeight - rect.bottom - 8, above = rect.top - 8;
    const upward = below < 160 && above > below;
    const height = Math.min(288, Math.max(40, upward ? above : below));
    popup.style.width = `${width}px`;
    popup.style.maxHeight = `${height}px`;
    popup.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
    popup.style.top = upward ? "auto" : `${rect.bottom + 4}px`;
    popup.style.bottom = upward ? `${window.innerHeight - rect.top + 4}px` : "auto";
  }
  function close() {
    if (!opened) return;
    opened = false;
    popup.hidePopover?.(); popup.hidden = true;
    input.setAttribute("aria-expanded", "false"); toggle.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant"); input.value = selectedLabel();
    document.removeEventListener("pointerdown", outside, true);
    window.removeEventListener("resize", position);
    window.removeEventListener("scroll", scroll, true);
  }
  function scroll(event: Event) { if (!popup.contains(event.target as Node)) close(); }
  function outside(event: Event) { if (!element.contains(event.target as Node)) close(); }
  function highlight(index: number) {
    active = index;
    Array.from(list.children).forEach((node, i) => {
      node.classList.toggle("is-active", i === active);
    });
    const node = list.children[active] as HTMLElement | undefined;
    if (node) { input.setAttribute("aria-activedescendant", node.id); node.scrollIntoView?.({ block: "nearest" }); }
    else input.removeAttribute("aria-activedescendant");
  }
  function commit(index: number) {
    const option = shown[index];
    if (disposed || input.disabled || !opened || !option) return;
    close(); // Restore controlled value until the owner accepts the requested selection.
    input.focus();
    onChange(option.value);
  }
  function render(query: string) {
    shown = matchSelectOptions(options, query);
    list.replaceChildren(...shown.map((option, index) => {
      const node = h("div", { id: `${id}-${index}`, role: "option", class: "searchable-select__option",
        "data-value": option.value, "aria-selected": String(option.value === value) }, [
        h("span", { text: option.label }), h("span", { class: "searchable-select__code", text: option.value }),
      ]);
      on(node, "pointerdown", event => event.preventDefault());
      on(node, "click", () => commit(index));
      return node;
    }));
    empty.hidden = shown.length > 0;
    highlight(query ? (shown.length ? 0 : -1) : shown.findIndex(option => option.value === value));
  }
  function open(query = "") {
    if (disposed || input.disabled) return;
    if (!opened) {
      opened = true; popup.hidden = false; popup.showPopover?.();
      input.setAttribute("aria-expanded", "true"); toggle.setAttribute("aria-expanded", "true");
      document.addEventListener("pointerdown", outside, true);
      window.addEventListener("resize", position);
      window.addEventListener("scroll", scroll, true);
    }
    position(); render(query);
  }
  function keydown(event: KeyboardEvent) {
    if (composing || event.isComposing) { if (event.key === "Escape") event.stopPropagation(); return; }
    if (input.disabled) return;
    if (event.key === "Escape") { if (opened) { event.preventDefault(); event.stopPropagation(); close(); } return; }
    if (event.key === "Tab") { close(); return; }
    if (event.key === "Enter") { if (opened) { event.preventDefault(); commit(active); } return; }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    if (!opened) { open(); highlight(event.key === "ArrowDown" ? 0 : shown.length - 1); return; }
    if (shown.length) highlight((active + (event.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length);
  }
  on(input, "keydown", event => keydown(event as KeyboardEvent));
  on(input, "click", () => { if (!opened) { open(); input.select?.(); } });
  on(input, "input", () => { if (!composing) open(input.value); });
  on(input, "compositionstart", () => { composing = true; open(); });
  on(input, "compositionend", () => { composing = false; open(input.value); });
  on(input, "blur", close);
  on(toggle, "pointerdown", event => event.preventDefault());
  on(toggle, "click", () => { if (input.disabled) return; input.focus(); if (opened) close(); else { open(); input.select?.(); } });
  return { element, input,
    update(next, selected, disabled) {
      if (disposed) return;
      const nextKey = next === options ? key : JSON.stringify(next);
      const changed = nextKey !== key || selected !== value;
      if (changed || disabled) close();
      options = next; key = nextKey; value = selected;
      input.disabled = toggle.disabled = disabled || !options.length;
      if (!opened) input.value = selectedLabel();
    },
    dispose() { close(); disposed = true; },
  };
}
