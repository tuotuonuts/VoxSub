/** Durable download snapshots; rejects stale events without logging or persisting user queries. */
export type ModelDownloadStatus = "queued" | "downloading" | "pausing" | "verifying" | "paused" | "done" | "deleted";
export interface ModelDownloadState {
  completed: number; total: number; stage: string;
  status?: ModelDownloadStatus; modelsRoot?: string; token?: string; revision?: number;
  source?: "auto" | "global" | "china"; error?: string;
}
export type ModelDownloadMap = Record<string, ModelDownloadState>;
const rootKey = (path: string): string => path.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
export function visibleDownload(state: ModelDownloadState | undefined): ModelDownloadState | undefined {
  return state?.status === "done" || state?.status === "deleted" ? undefined : state;
}
export function mergeDownload(current: ModelDownloadMap, modelId: string, incoming: ModelDownloadState,
  root: string): ModelDownloadMap {
  if (incoming.modelsRoot && root && rootKey(incoming.modelsRoot) !== rootKey(root)) return current;
  const old = current[modelId];
  const sameRoot = !old?.modelsRoot || !incoming.modelsRoot || rootKey(old.modelsRoot) === rootKey(incoming.modelsRoot);
  if (sameRoot && old?.revision !== undefined && (incoming.revision ?? -1) <= old.revision) return current;
  return { ...current, [modelId]: incoming };
}

export function downloadsForRoot(current: ModelDownloadMap, root: string): ModelDownloadMap {
  return Object.fromEntries(Object.entries(current).filter(([, state]) =>
    !state.modelsRoot || rootKey(state.modelsRoot) === rootKey(root)));
}
