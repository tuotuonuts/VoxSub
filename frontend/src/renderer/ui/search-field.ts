/** Shared IME-safe search input. Local listeners only; no global hotkeys, store, timers or IPC. */
import { h, on } from "../dom";
import { buildTextInput } from "./controls";
import { buildButton } from "./button";
export function buildSearchField(value: string, labels: { label: string; placeholder: string; clear: string },
  onQuery: (value: string) => void): {
    element: HTMLElement; input: HTMLInputElement; setValue: (value: string) => void; dispose: () => void;
  } {
  const input = buildTextInput(value, undefined, { type: "search", placeholder: labels.placeholder });
  input.setAttribute("aria-label", labels.label);
  input.setAttribute("autocomplete", "off"); input.setAttribute("spellcheck", "false");
  input.setAttribute("maxlength", "160");
  const clear = buildButton("×", { small: true, title: labels.clear });
  clear.setAttribute("aria-label", labels.clear); clear.classList.add("search-field__clear");
  let composing = false; let alive = true; let emitted = value;
  const update = (): void => { clear.hidden = input.value.length === 0; };
  const emit = (): void => {
    update();
    if (!alive || input.value === emitted) return;
    emitted = input.value; onQuery(input.value);
  };
  const clearQuery = (): void => {
    if (!alive) return;
    composing = false; input.value = ""; emit(); input.focus();
  };
  on(input, "compositionstart", () => { if (alive) composing = true; });
  on(input, "compositionend", () => { if (alive) { composing = false; emit(); } });
  on(input, "input", event => {
    update(); if (!composing && !(event as InputEvent).isComposing) emit();
  });
  on(input, "keydown", event => {
    const key = event as KeyboardEvent;
    if (!alive || key.key !== "Escape") return;
    if (composing || key.isComposing) { event.stopPropagation(); return; }
    if (input.value) { event.preventDefault(); event.stopPropagation(); clearQuery(); }
  });
  on(clear, "click", clearQuery);
  update();
  return { element: h("div", { class: "search-field" }, [input, clear]), input,
    setValue: next => { if (alive) { input.value = next; emitted = next; update(); } },
    dispose: () => { alive = false; composing = false; },
  };
}
