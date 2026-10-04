/** Global shortcut contract and validation. No Electron/DOM dependencies. */
export const SHORTCUT_ACTIONS = [
  "toggle_window", "toggle_session", "pause_session", "stop_session",
  "toggle_recording", "toggle_overlay", "toggle_click_through",
] as const;
export type ShortcutAction = (typeof SHORTCUT_ACTIONS)[number];
export type ShortcutBindings = Record<ShortcutAction, string>;
export interface ShortcutIssue { code: "invalid" | "duplicate" | "reserved" | "occupied" | "storage" | "capturing"; action?: ShortcutAction; other?: ShortcutAction; key?: string; }
export interface ShortcutSnapshot { bindings: ShortcutBindings; active: ShortcutAction[]; issues: ShortcutIssue[]; capturing: boolean; }
export interface ShortcutResult { ok: boolean; snapshot: ShortcutSnapshot; issues: ShortcutIssue[]; }
export const SHORTCUT_LABELS: Record<ShortcutAction, string> = {
  toggle_window: "显示 / 隐藏主窗", toggle_session: "开始 / 结束当前会话",
  pause_session: "暂停 / 继续当前会话", stop_session: "结束当前会话",
  toggle_recording: "切换同时录音", toggle_overlay: "显示 / 隐藏字幕浮窗",
  toggle_click_through: "切换浮窗鼠标穿透",
};
export function emptyBindings(): ShortcutBindings {
  return Object.fromEntries(SHORTCUT_ACTIONS.map(action => [action, ""])) as ShortcutBindings;
}
const MODIFIERS: Record<string, string> = { ctrl: "Ctrl", control: "Ctrl", alt: "Alt", shift: "Shift" };
const KEYS: Record<string, string> = { space: "Space", enter: "Enter", return: "Enter", tab: "Tab", escape: "Escape", esc: "Escape", backspace: "Backspace", delete: "Delete", del: "Delete", insert: "Insert", home: "Home", end: "End", pageup: "PageUp", pagedown: "PageDown", up: "Up", arrowup: "Up", down: "Down", arrowdown: "Down", left: "Left", arrowleft: "Left", right: "Right", arrowright: "Right" };
const RESERVED = new Set(["Alt+F4", "Alt+Tab", "Alt+Escape", "Alt+Space", "Ctrl+Escape", "Ctrl+Alt+Delete", "Ctrl+Alt+Shift+Delete", "Alt+Shift+Tab", "Alt+Shift+Escape", "Ctrl+Shift+Escape"]);
export function normalizeAccelerator(raw: string): string | null {
  if (!raw.trim()) return "";
  if (raw.length > 80) return null;
  const parts = raw.trim().split("+").map(p => p.trim().toLowerCase());
  const last = parts.pop() ?? "";
  const mods = parts.map(p => Object.hasOwn(MODIFIERS, p) ? MODIFIERS[p] : undefined);
  if (!mods.length || mods.some(m => !m) || new Set(mods).size !== mods.length || !mods.some(m => m === "Ctrl" || m === "Alt")) return null;
  const key = /^[a-z0-9]$/.test(last) ? last.toUpperCase() : /^f([1-9]|1[0-9]|2[0-4])$/.test(last) ? last.toUpperCase() : Object.hasOwn(KEYS, last) ? KEYS[last] : undefined;
  if (!key) return null;
  return ["Ctrl", "Alt", "Shift"].filter(m => mods.includes(m)).concat(key).join("+");
}
export function validateBindings(raw: unknown): { bindings: ShortcutBindings; issues: ShortcutIssue[] } {
  const bindings = emptyBindings(), issues: ShortcutIssue[] = [];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { bindings, issues: [{ code: "invalid" }] };
  const input = raw as Record<string, unknown>, seen = new Map<string, ShortcutAction>();
  if (Object.keys(input).some(key => !SHORTCUT_ACTIONS.includes(key as ShortcutAction))) issues.push({ code: "invalid" });
  for (const action of SHORTCUT_ACTIONS) {
    const value = Object.hasOwn(input, action) ? input[action] : "";
    const key = typeof value === "string" ? normalizeAccelerator(value) : null;
    if (key === null) { issues.push({ code: "invalid", action }); continue; }
    bindings[action] = key;
    if (!key) continue;
    if (RESERVED.has(key)) issues.push({ code: "reserved", action, key });
    const other = seen.get(key);
    if (other) issues.push({ code: "duplicate", action, other, key });
    seen.set(key, action);
  }
  return { bindings, issues };
}
/** Keyboard capture uses logical Latin keys, with physical fallback for IME text. */
export function acceleratorFromKey(event: { key: string; code: string; ctrlKey: boolean; altKey: boolean; shiftKey: boolean; metaKey: boolean }): string | null {
  if (event.metaKey) return null;
  const key = event.code === "Space" ? "Space" : /^[a-z0-9]$/i.test(event.key) ? event.key : /^(Key[A-Z]|Digit[0-9])$/.test(event.code) ? event.code.replace(/^(Key|Digit)/, "") : event.key;
  return normalizeAccelerator([event.ctrlKey ? "Ctrl" : "", event.altKey ? "Alt" : "", event.shiftKey ? "Shift" : "", key].filter(Boolean).join("+"));
}
