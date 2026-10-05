/** Backend-owned language matrix. Source intersection and directional targets. */
import { h, on } from "./dom";
import { CMD } from "./protocol";
import { callWithOutcome, setLanguageModelListener, store } from "./store";
import { persistLanguagePair } from "./language-selection";
import { tr, currentLanguage } from "./i18n";

export interface LanguageCapabilities {
  sources: string[];
  targets: Record<string, string[]>;
  compatible: boolean;
  reason: string;
  labels?: Record<string, { zh: string; en: string }>;
  sourceLanguageHint?: string;
}
export function languageLabel(code: string): string {
  if (code === "auto") return tr("自动识别");
  const locale = currentLanguage() === "en" ? "en" : "zh";
  const label = matrix?.labels?.[code]?.[locale];
  if (label) return label;
  try { return new Intl.DisplayNames([locale], { type: "language" }).of(code) ?? code; }
  catch { return code; }
}
let matrix: LanguageCapabilities | null = null;
let generation = 0;
let sourceSelect: HTMLSelectElement | null = null;
let targetSelect: HTMLSelectElement | null = null;
let hint: HTMLElement | null = null;
let initialized = false;
let modelChanges = 0;
let cleanup: (() => void) | null = null;

export function chooseLanguagePair(meta: LanguageCapabilities, source: string, target: string): [string, string] | null {
  const legal = meta.sources.filter(src => meta.targets[src]?.length);
  if (!legal.length) return null;
  const src = legal.includes(source) ? source : (legal.find(code => meta.targets[code]!.includes(target)) ?? legal[0]!);
  const targets = meta.targets[src]!;
  return [src, targets.includes(target) ? target : targets[0]!];
}

function renderOptions(select: HTMLSelectElement | null, codes: string[], selected: string): void {
  if (!select) return;
  // Keep DOM/focus untouched for irrelevant store updates (logs, subtitles, etc.).
  const key = JSON.stringify(codes.map(code => [code, languageLabel(code)]));
  if (select.dataset["languageOptions"] !== key) {
    select.replaceChildren(...codes.map(code => h("option", { value: code, text: languageLabel(code) })));
    select.dataset["languageOptions"] = key;
  }
  select.value = selected;
  select.disabled = store.get().languagePending || !codes.length;
}

function syncControls(): void {
  const state = store.get();
  renderOptions(sourceSelect, matrix?.sources ?? [], state.sourceLang);
  renderOptions(targetSelect, matrix?.targets[state.sourceLang] ?? [], state.targetLang);
  if (sourceSelect && state.running && matrix?.sourceLanguageHint === "load_time") sourceSelect.disabled = true;
  if (state.running && state.mode === "c") {
    if (sourceSelect) sourceSelect.disabled = true;
    if (targetSelect) targetSelect.disabled = true;
  }
  if (hint) hint.textContent = state.languagePending ? tr("正在确认模型支持的语言…") : state.languageNotice ||
    (state.running && matrix?.sourceLanguageHint === "load_time" ? tr("此模型需停止会话后切换识别语言") :
      matrix?.sourceLanguageHint === "unavailable" ? tr("此模型自动判断语种，不支持强制指定识别语言") : "");
}

async function savePair(source: string, target: string): Promise<void> {
  const epoch = generation;
  const isCurrent = () => epoch === generation && store.get().sourceLang === source && store.get().targetLang === target;
  store.patch({ languagePending: true, languageCompatible: false });
  try {
    await persistLanguagePair(source, target, (command, args) => {
      // OCR owns its text routing; never validate D against an audio ASR owner.
      if (command === CMD.setLangs && store.get().mode === "d") return Promise.resolve({ outcome: "ok" });
      return callWithOutcome(command as typeof CMD.setLangs, args);
    },
      message => store.pushLog({ ts: new Date().toISOString(), level: "ERROR", message }), isCurrent);
    if (isCurrent()) store.patch({ languagePending: false, languageCompatible: true });
  } catch {
    if (!isCurrent()) return;
    store.patch({ languagePending: false, languageCompatible: false, languageNotice: tr("语言设置未确认成功，请重新选择或刷新模型设置") });
  }
  syncControls();
}

function reconcile(notify: boolean): void {
  if (!matrix) { syncControls(); return; }
  const state = store.get();
  const pair = chooseLanguagePair(matrix, state.sourceLang, state.targetLang);
  if (!pair) {
    store.patch({ languageCompatible: false, languageNotice: tr(matrix.reason || "当前识别模型与翻译模型不兼容") });
  } else if (pair[0] !== state.sourceLang || pair[1] !== state.targetLang) {
    const message = tr("原语言组合不受当前模型支持，已调整为 {source} → {target}")
      .replace("{source}", languageLabel(pair[0])).replace("{target}", languageLabel(pair[1]));
    store.patch({ sourceLang: pair[0], targetLang: pair[1], languageCompatible: false, languagePending: true, languageNotice: message });
    void savePair(pair[0], pair[1]);
    if (notify) store.pushLog({ ts: new Date().toISOString(), level: "WARNING", message });
  } else {
    store.patch({ languageNotice: "" });
    void savePair(pair[0], pair[1]);
  }
  syncControls();
}

export async function refreshLanguageCapabilities(): Promise<void> {
  if (modelChanges) return;
  initialized = true;
  const epoch = ++generation;
  store.patch({ languagePending: true, languageCompatible: false });
  try {
    const result = await callWithOutcome<LanguageCapabilities>(CMD.languageCapabilities, { mode: store.get().mode });
    if (epoch !== generation) return;
    if (result.outcome !== "ok" || !result.data || !Array.isArray(result.data.sources) || !result.data.targets) throw Error("missing matrix");
    matrix = result.data;
    store.patch({ languagePending: false });
    reconcile(true);
  } catch {
    if (epoch !== generation) return;
    matrix = null;
    store.patch({ languagePending: false, languageCompatible: false, languageNotice: tr("无法确认模型支持的语言，请检查后端连接") });
    syncControls();
  }
}

export function initializeLanguageCapabilities(): () => void {
  cleanup?.();
  store.patch({ languagePending: true, languageCompatible: false });
  let mode = store.get().mode;
  let phase = store.get().backendPhase;
  const unsubscribe = store.subscribe(() => {
    const state = store.get();
    const shouldRefresh = state.mode !== mode || (state.backendPhase === "ready" && phase !== "ready");
    const disconnected = state.backendPhase !== phase && state.backendPhase !== "ready";
    mode = state.mode; phase = state.backendPhase;
    if (disconnected) {
      generation++;
      store.patch({ languagePending: true, languageCompatible: false, languageNotice: tr("无法确认模型支持的语言，请检查后端连接") });
    }
    if (initialized && shouldRefresh) void refreshLanguageCapabilities();
    syncControls();
  });
  setLanguageModelListener(stage => {
    if (stage === "begin") {
      modelChanges++;
      generation++;
      store.patch({ languagePending: true, languageCompatible: false });
    } else {
      modelChanges = Math.max(0, modelChanges - 1);
      if (!modelChanges) void refreshLanguageCapabilities();
    }
  });
  cleanup = () => { unsubscribe(); setLanguageModelListener(null); generation++; initialized = false; modelChanges = 0; };
  return cleanup;
}

export function buildLanguageControls(): HTMLElement {
  const box = h("div", { class: "lang-box" });
  sourceSelect = h("select", { class: "select select--sm", "aria-label": tr("识别语言"), "data-language-source": "" });
  targetSelect = h("select", { class: "select select--sm", "aria-label": tr("翻译为"), "data-language-target": "" });
  hint = h("p", { class: "field__hint", role: "status", "data-language-hint": "" });
  on(sourceSelect, "change", () => {
    if (!matrix || store.get().languagePending || !matrix.sources.includes(sourceSelect!.value)) return;
    const pair = chooseLanguagePair(matrix, sourceSelect!.value, store.get().targetLang);
    if (!pair) return;
    store.patch({ sourceLang: pair[0], targetLang: pair[1], languageNotice: "" });
    syncControls(); void savePair(pair[0], pair[1]);
  });
  on(targetSelect, "change", () => {
    const state = store.get();
    const target = targetSelect!.value;
    if (!matrix || state.languagePending || !matrix.targets[state.sourceLang]?.includes(target)) return;
    store.patch({ targetLang: target, languageNotice: "" });
    syncControls(); void savePair(state.sourceLang, target);
  });
  box.append(h("span", { class: "lang-box__label", text: tr("识别语言") }), sourceSelect,
    h("span", { class: "lang-box__label", text: tr("翻译为") }), targetSelect, hint);
  syncControls(); return box;
}
