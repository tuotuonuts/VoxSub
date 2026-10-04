/** Accessible percentage input: preview on input, commit on change; no IPC/config. */
import { h, on } from "../dom";
export function buildPercentageSlider(value: number, label: string, options: {
  min?: number; onInput?: (value: number) => void; onChange?: (value: number) => void;
} = {}): { element: HTMLElement; input: HTMLInputElement; setDisabled: (disabled: boolean) => void } {
  const min = options.min ?? 0;
  const clamp = (v: number): number => Math.round(Math.min(100, Math.max(min, Number.isFinite(v) ? v : min)));
  const input = h("input", { class: "input", type: "range", min: String(min), max: "100", step: "1",
    value: String(clamp(value)), "aria-label": label });
  const output = h("output", { text: `${input.value}%`, "aria-live": "off" });
  const update = (): number => {
    const next = clamp(Number(input.value)); input.value = String(next);
    output.textContent = `${next}%`; input.setAttribute("aria-valuetext", `${next}%`); return next;
  };
  update();
  on(input, "input", () => { if (!input.disabled) options.onInput?.(update()); });
  on(input, "change", () => { if (!input.disabled) options.onChange?.(update()); });
  return { element: h("div", { class: "tuning-actions" }, [input, output]), input,
    setDisabled: disabled => { input.disabled = disabled; } };
}
