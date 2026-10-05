/** Lifecycle owner for the shared file translation form. */
import { buildFileTranslationForm } from "./ui/file-translation-form";
import { callWithOutcome, store } from "./store";
import { CMD, type ModelEntry } from "./protocol";

export function buildFileTranslationPicker(onSaved?: (config: Record<string, unknown>) => void, initialConfig?: Record<string, unknown>) {
  let disposed = false;
  let epoch = 0;
  let pending = true;
  let failed = false;
  let config: Record<string, unknown> = initialConfig ?? {};
  let models: ModelEntry[] = [];
  const form = buildFileTranslationForm(updates => { void save(updates); }, () => { void refresh(); });
  let observedMode = store.get().mode;
  let observedPhase = store.get().backendPhase;
  let completedDownloads = "";
  const sync = () => {
    if (disposed) return;
    const state = store.get();
    const downloads = Object.values(state.downloads).filter(d => d.status === "done").map(d => d.token).join(",");
    const reload = (observedMode !== state.mode && state.mode === "c") ||
      (observedPhase !== state.backendPhase && state.backendPhase === "ready") || downloads !== completedDownloads;
    observedMode = state.mode; observedPhase = state.backendPhase; completedDownloads = downloads;
    form.update(config, models, pending || state.running, failed);
    if (reload && !pending) void refresh();
  };
  async function refresh() {
    const request = ++epoch;
    pending = true; sync();
    const [c, m] = await Promise.all([
      initialConfig ? Promise.resolve({ outcome: "ok", data: config }) : callWithOutcome<Record<string, unknown>>(CMD.getConfig),
      callWithOutcome<{ models: ModelEntry[] }>(CMD.listModels, { models_root: null }),
    ]);
    if (disposed || request !== epoch) return;
    failed = c.outcome !== "ok" || !c.data || m.outcome !== "ok" || !m.data || !Array.isArray(m.data.models);
    if (!failed) { config = c.data!; models = m.data!.models.filter(item => item.task === "speech"); }
    pending = false; sync();
  }
  async function save(updates: Record<string, string>) {
    if (disposed || pending || store.get().running) { sync(); return; }
    pending = true; sync();
    const result = await callWithOutcome<Record<string, unknown>>(CMD.setConfig, { updates });
    if (disposed) return;
    if (result.outcome === "ok" && result.data) { config = result.data; onSaved?.(config); }
    else { await refresh(); return; }
    pending = false; sync();
  }
  void refresh();
  return { element: form.element, sync, dispose: () => {
    disposed = true; epoch++;
  } };
}
