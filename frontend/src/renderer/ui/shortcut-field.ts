/** Reusable editable/capturable shortcut field. IPC and persistence are injected, not owned. */
import { h, on } from "../dom";
import { buildTextInput } from "./controls";
import { buildButton } from "./button";
import { buildField } from "./field";
import { acceleratorFromKey } from "../../shared/shortcuts";
let nextCapture = 0;
export interface ShortcutFieldOptions {
  label: string; value: string; translate(key: string): string;
  changed(value: string): void;
  beginCapture(token: string): Promise<boolean>;
  endCapture(token: string): Promise<void>;
  captureChanged?(): void;
}
export interface ShortcutField {
  readonly isCapturing: boolean; element: HTMLElement; setValue(value: string): void; setDisabled(disabled: boolean): void; setStatus(text: string): void; cancelCapture(): void; dispose(): void;
}
export function buildShortcutField(options: ShortcutFieldOptions): ShortcutField {
  const tr = options.translate;
  let disposed = false, capturing = false, prepared = false, ending = false, token = "", generation = 0;
  let accepted: string | null = null;
  let previous = options.value;
  const input = buildTextInput(options.value, value => { if (!disposed && !capturing) { generation++; options.changed(value); } }, { placeholder: tr("未设置") });
  input.setAttribute("aria-label", options.label);
  const capture = buildButton(tr("录入快捷键"));
  const clear = buildButton(tr("清空"));
  const status = h("p", { class: "field__hint", "aria-live": "polite" });
  const row = h("div", { class: "tuning-actions shortcut-field__control" }, [input, capture, clear]);
  const element = buildField({ label: options.label, control: row }); element.append(status);
  const stop = async (commit: boolean): Promise<void> => {
    const owned = token, value = commit ? accepted : null;
    token = ""; capturing = false; prepared = false; ending = true; const request = ++generation; accepted = null; input.readOnly = false;
    options.captureChanged?.();
    input.placeholder = tr("未设置"); capture.textContent = tr("录入快捷键");
    if (value === null) input.value = previous;
    try { if (owned) await options.endCapture(owned); }
    catch { if (!disposed) status.textContent = tr("快捷键状态未确认，请重新打开设置核对"); }
    ending = false;
    if (!disposed && request === generation && value !== null) { previous = value; input.value = value; options.changed(value); }
    if (!disposed) options.captureChanged?.();
  };
  on(capture, "click", () => {
    if (disposed || ending) return;
    if (capturing) { void stop(false); return; }
    previous = input.value; accepted = null; capturing = true; prepared = false; input.readOnly = true;
    token = `shortcut-${Date.now()}-${++nextCapture}`; const owned = token, request = ++generation;
    options.captureChanged?.(); capture.textContent = tr("取消录入"); status.textContent = tr("正在准备录入快捷键…");
    void options.beginCapture(owned).then(ok => {
      if (disposed || request !== generation) { void options.endCapture(owned).catch(() => {}); return; }
      if (!ok) { void stop(false); status.textContent = tr("无法录入快捷键，请重试"); return; }
      prepared = true; status.textContent = tr("按下组合键；Esc 取消。录入时全局快捷键暂时停用。"); input.focus();
    }).catch(() => { if (!disposed && request === generation) { void stop(false); status.textContent = tr("无法录入快捷键，请重试"); } });
  });
  on(input, "keydown", raw => {
    if (!capturing || disposed) return;
    const event = raw as KeyboardEvent; event.preventDefault(); event.stopPropagation();
    if (!prepared || event.repeat) return;
    if (event.key === "Escape" && !event.ctrlKey && !event.altKey) { void stop(false); return; }
    if (["Control", "Alt", "Shift", "Meta"].includes(event.key)) return;
    const value = acceleratorFromKey(event);
    if (!value) { status.textContent = tr("请使用 Ctrl 或 Alt 加一个按键"); return; }
    accepted = value; input.value = value;
  });
  on(input, "keyup", raw => {
    if (capturing && accepted && !["Control", "Alt", "Shift", "Meta"].includes((raw as KeyboardEvent).key)) void stop(true);
  });
  on(input, "blur", () => { if (capturing) void stop(accepted !== null); });
  on(clear, "click", () => { if (disposed) return; if (capturing) void stop(false); generation++; input.value = ""; previous = ""; options.changed(""); });
  return {
    element,
    get isCapturing() { return capturing || ending; },
    setValue(value) { previous = value; if (!capturing) input.value = value; },
    setDisabled(disabled) { input.disabled = disabled; capture.disabled = disabled || ending; clear.disabled = disabled; if (disabled && capturing) void stop(false); },
    setStatus(text) { if (!capturing) status.textContent = text; },
    cancelCapture() { if (capturing) void stop(false); },
    dispose() { if (disposed) return; disposed = true; if (capturing) void stop(false); generation++; },
  };
}
