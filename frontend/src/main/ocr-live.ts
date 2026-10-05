/** Native orchestration only: one capture+backend request, stale-session fencing.
 * No Electron import, so the production scheduler is testable without a window.
 */
import type { SelectionArea, CaptureResult } from "./capture";
import type { CommandResult } from "./backend";
export interface LiveOcrPayload {
  lines: Array<{text: string; box: number[]}>;
  width: number; height: number;
  failedLines?: number;
  ocrElapsedMs?: number;
  translateElapsedMs?: number;
}
export interface LiveOcrDeps {
  capture(area: SelectionArea): Promise<CaptureResult | null>;
  command(name: string, args: unknown): Promise<CommandResult>;
  remove(file: string): void;
  translated(payload: LiveOcrPayload): void;
  status(state: string, details?: Record<string, number>): void;
  failed(): void;
}
export class LiveOcrSession {
  private epoch = 0;
  private area: SelectionArea | null = null;
  private busy = false;
  private retryAt = 0;
  private failures = 0;
  private languages: {source: string; target: string} | null = null;
  constructor(private readonly deps: LiveOcrDeps, private readonly clock = () => Date.now()) {}
  start(area: SelectionArea, languages: {source: string; target: string}): void { this.epoch++; this.area = area; this.languages = languages; this.retryAt = 0; this.failures = 0; }
  configure(languages: {source: string; target: string}): void { this.epoch++; this.languages = languages; this.retryAt = 0; this.failures = 0; }
  invalidate(): void { this.epoch++; }
  stop(): void { this.epoch++; this.area = null; } // Never reset an in-flight lock.
  async tick(): Promise<void> {
    if (this.busy || !this.area || this.clock() < this.retryAt) return;
    this.busy = true;
    const epoch = this.epoch, area = this.area;
    let shot: CaptureResult | null = null;
    const active = (): boolean => epoch === this.epoch && this.area !== null;
    try {
      shot = await this.deps.capture(area);
      if (!active()) return;
      if (!shot) throw new Error("Capture unavailable");
      this.deps.status("recognizing");
      const response = await this.deps.command("ocr_recognize", {path: shot.path, live: true, translate: true, ...this.languages});
      if (!active()) return;
      if (!response.ok || response.timedOut) throw new Error("OCR unavailable");
      const data = response.data as {lines?: Array<{translation?: string; box: number[]}>; width?: number; height?: number; failedLines?: number; ocrElapsedMs?: number; translateElapsedMs?: number};
      if (!data || !Array.isArray(data.lines) || !(Number(data.width) > 0) || !(Number(data.height) > 0)) throw new Error("Invalid OCR payload");
      this.deps.translated({lines: data.lines.filter(line => line.translation?.trim()).map(line => ({text: line.translation!, box: line.box})),
        width: data.width!, height: data.height!});
      this.failures = data.failedLines ? this.failures + 1 : 0;
      this.retryAt = data.failedLines ? this.clock() + Math.min(8000, 1000 * 2 ** Math.min(3, this.failures - 1)) : 0;
      this.deps.status(data.failedLines ? "partial" : !data.lines.length ? "no-text" : !data.lines.some(line => line.translation?.trim()) ? "no-translation" : "running",
        {lines: data.lines.length, failedLines: data.failedLines ?? 0,
          ocrMs: data.ocrElapsedMs ?? 0, translationMs: data.translateElapsedMs ?? 0});
    } catch {
      if (active()) {
        this.failures++;
        this.retryAt = this.clock() + Math.min(8000, 1000 * 2 ** Math.min(3, this.failures - 1));
        this.deps.failed(); this.deps.status("error");
      }
    } finally {
      if (shot) { try { this.deps.remove(shot.path); } catch { /* Retry cleanup on application exit. */ } }
      this.busy = false;
    }
  }
}
