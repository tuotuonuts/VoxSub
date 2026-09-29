/** Backend-confirmed WAV-saving control. No capture/session commands belong here. */
export interface RecordingState {
  recordingEnabled: boolean;
  recordingActive: boolean;
  recordingSupported: boolean;
  recordingCanChange: boolean;
}
export interface RecordingView {
  checked: boolean;
  active: boolean;
  known: boolean;
  disabled: boolean;
  pending: boolean;
  notice: "" | "pending" | "unknown" | "disconnected" | "rejected" | "unsupported" | "idle-only";
}
type Dependencies = {
  read: () => Promise<unknown>;
  set: (enabled: boolean) => Promise<unknown>;
  changed?: (view: RecordingView) => void;
};

function parse(value: unknown): RecordingState | null {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  const keys = ["recordingEnabled", "recordingActive", "recordingSupported", "recordingCanChange"];
  if (!keys.every(key => typeof data[key] === "boolean")) return null;
  return { recordingEnabled: data.recordingEnabled as boolean,
    recordingActive: data.recordingActive as boolean,
    recordingSupported: data.recordingSupported as boolean,
    recordingCanChange: data.recordingCanChange as boolean };
}

export class RecordingControl {
  private state: RecordingState | null = null;
  private online = true;
  private issue: RecordingView["notice"] = "unknown";
  private wanted: boolean | null = null;
  private work: Promise<void> | null = null;
  private epoch = 0;
  private revision = 0;

  constructor(private readonly deps: Dependencies) {}

  get view(): RecordingView {
    const s = this.state;
    let notice = this.issue;
    if (!this.online) notice = "disconnected";
    else if (this.work) notice = "pending";
    else if (!s) notice = "unknown";
    else if (!s.recordingSupported) notice = "unsupported";
    else if (!s.recordingCanChange) notice = "idle-only";
    return { checked: s?.recordingEnabled ?? false, active: s?.recordingActive ?? false,
      known: s !== null, disabled: !this.online || !s?.recordingCanChange || !s?.recordingSupported,
      pending: this.work !== null, notice };
  }

  private publish(): void { this.deps.changed?.(this.view); }

  /** Use authoritative state events; pending intents are not state. */
  observe(value: unknown): void {
    if (!this.online) return;
    this.revision++;
    this.state = parse(value);
    this.issue = this.state ? "" : "unknown";
    this.publish();
  }

  async refresh(): Promise<void> {
    if (!this.online) return;
    const epoch = this.epoch, revision = this.revision;
    let result: unknown = null;
    try { result = await this.deps.read(); } catch { /* No response means unknown, not success. */ }
    if (epoch !== this.epoch || revision !== this.revision) return;
    this.observe(result);
  }

  disconnect(): void {
    this.epoch++;
    this.online = false;
    this.state = null;
    this.wanted = null;
    this.work = null;
    this.publish();
  }

  async reconnect(): Promise<void> {
    this.online = true;
    await this.refresh();
  }

  toggle(): Promise<void> {
    return this.request(!(this.wanted ?? this.state?.recordingEnabled ?? false));
  }

  request(enabled: boolean): Promise<void> {
    if (this.view.disabled) return Promise.resolve();
    this.wanted = enabled;
    if (this.work) return this.work;
    this.revision++; // In-flight initial reads cannot overwrite this operation.
    const epoch = this.epoch;
    this.work = Promise.resolve().then(() => this.drain(epoch)).finally(() => {
      if (epoch !== this.epoch) return;
      this.work = null;
      this.wanted = null;
      this.publish();
    });
    this.publish();
    return this.work;
  }

  private async drain(epoch: number): Promise<void> {
    while (this.wanted !== null && this.wanted !== this.state?.recordingEnabled) {
      const target = this.wanted;
      const revision = this.revision;
      let result: unknown = null;
      try { result = await this.deps.set(target); } catch { /* Transport outcome is unknown. */ }
      if (epoch !== this.epoch || revision !== this.revision) return;
      this.state = parse(result);
      this.revision++;
      if (!this.state) { this.issue = "unknown"; return; }
      if (this.state.recordingEnabled !== target) { this.issue = "rejected"; return; }
      this.issue = "";
      this.publish();
      if (!this.state.recordingCanChange || !this.state.recordingSupported) return;
    }
  }
}
