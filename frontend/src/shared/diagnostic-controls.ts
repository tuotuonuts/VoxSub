/** Pure developer gate and bounded IPC metadata; never retain request arguments. */
export class DeveloperGesture {
  private clicks: number[] = [];
  hit(now: number): boolean {
    this.clicks = this.clicks.filter(at => now - at < 5000 && now >= at);
    this.clicks.push(now);
    if (this.clicks.length < 10) return false;
    this.clicks = [];
    return true;
  }
}
let enabled = false;
export const developerEnabled = (): boolean => enabled;
export const setDeveloperEnabled = (value: boolean): void => { enabled = value; };
export interface IPCMetric { command: string; outcome: string; durationMs: number; at: string }
const metrics: IPCMetric[] = [];
export function recordIPC(command: string, outcome: string, durationMs: number): void {
  metrics.push({ command, outcome, durationMs: Math.round(durationMs), at: new Date().toISOString() });
  if (metrics.length > 200) metrics.splice(0, metrics.length - 200);
}
export function ipcSnapshot(): IPCMetric[] { return metrics.map(item => ({ ...item })); }
export function logBudgetMB(value: number, unit: string): number | null {
  if (unit !== "MB" && unit !== "GB") return null;
  const mb = Math.round(value * (unit === "GB" ? 1024 : 1));
  return Number.isFinite(value) && value > 0 && mb >= 10 && mb <= 10240 ? mb : null;
}
export function checkSummary(items: readonly { status: string }[]): "not_run" | "ok" | "attention" {
  if (!items.length) return "not_run";
  return items.every(item => item.status === "ok") ? "ok" : "attention";
}
export function matchesLog(entry: { level: string; message: string; run_id?: string | undefined }, level: string, query: string, runId: string): boolean {
  return (level === "all" || entry.level.toUpperCase() === level) &&
    (!query || entry.message.toLowerCase().includes(query.toLowerCase())) &&
    (!runId || entry.run_id === runId);
}

export function pipelineSession(message: string): string {
  const at = message.indexOf("MODEL_TRACE ");
  if (at < 0) return "";
  try { const event = JSON.parse(message.slice(at + 12));
    return typeof event.pipeline_session_id === "string" && /^[a-f0-9]{12}$/.test(event.pipeline_session_id) ? event.pipeline_session_id : "";
  } catch { return ""; }
}
