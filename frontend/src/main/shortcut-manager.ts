/** Transactional registration: acquire new keys before releasing old keys or writing settings. */
import { emptyBindings, SHORTCUT_ACTIONS, validateBindings, type ShortcutAction, type ShortcutBindings, type ShortcutIssue, type ShortcutResult, type ShortcutSnapshot } from "../shared/shortcuts";
export interface ShortcutDriver { register(key: string, callback: () => void): boolean; unregister(key: string): void; }
export class ShortcutManager {
  private bindings = emptyBindings();
  private readonly owned = new Set<string>();
  private issues: ShortcutIssue[] = [];
  private captureToken: string | null = null;
  private disposed = false;
  private suppressUntil = 0;
  private lastFire = 0;
  constructor(private readonly driver: ShortcutDriver, private readonly persist: (bindings: ShortcutBindings) => void,
    private readonly execute: (action: ShortcutAction) => void, private readonly changed: () => void = () => {}) {}
  get snapshot(): ShortcutSnapshot {
    return { bindings: { ...this.bindings }, active: SHORTCUT_ACTIONS.filter(a => !!this.bindings[a] && this.owned.has(this.bindings[a])), issues: [...this.issues], capturing: this.captureToken !== null };
  }
  load(raw: unknown, storageError = false): void {
    const parsed = validateBindings(raw);
    if (storageError || parsed.issues.length) { this.issues = [{ code: "storage" }]; this.changed(); return; }
    this.bindings = parsed.bindings; this.restore();
  }
  private acquire(key: string): boolean {
    try {
      if (!this.driver.register(key, () => this.fire(key))) return false;
      this.owned.add(key); return true;
    } catch { return false; }
  }
  private release(key: string): void { this.driver.unregister(key); this.owned.delete(key); }
  private fire(key: string): void {
    const now = Date.now();
    if (this.disposed || this.captureToken || now < this.suppressUntil || now - this.lastFire < 300 || !this.owned.has(key)) return;
    const action = SHORTCUT_ACTIONS.find(a => this.bindings[a] === key);
    if (action) { this.lastFire = now; this.execute(action); }
  }
  private restore(): void {
    if (this.disposed) return;
    this.issues = [];
    for (const action of SHORTCUT_ACTIONS) {
      const key = this.bindings[action];
      if (key && !this.owned.has(key) && !this.acquire(key)) this.issues.push({ code: "occupied", action, key });
    }
    this.changed();
  }
  check(raw: unknown, save = false): ShortcutResult {
    const parsed = validateBindings(raw), added: string[] = [];
    const issues = [...parsed.issues];
    if (this.disposed || this.captureToken) issues.push({ code: "capturing" });
    if (issues.length) return { ok: false, snapshot: this.snapshot, issues };
    for (const action of SHORTCUT_ACTIONS) {
      const key = parsed.bindings[action];
      if (!key || this.owned.has(key)) continue;
      if (this.acquire(key)) added.push(key);
      else issues.push({ code: "occupied", action, key });
    }
    if (issues.length || !save) {
      added.forEach(key => this.release(key));
      return { ok: issues.length === 0, snapshot: this.snapshot, issues };
    }
    try { this.persist(parsed.bindings); }
    catch { added.forEach(key => this.release(key)); return { ok: false, snapshot: this.snapshot, issues: [{ code: "storage" }] }; }
    this.bindings = parsed.bindings; this.issues = [];
    const desired = new Set(Object.values(this.bindings).filter(Boolean));
    for (const key of this.owned) if (!desired.has(key)) this.release(key);
    this.changed(); return { ok: true, snapshot: this.snapshot, issues: [] };
  }
  beginCapture(token: string): boolean {
    if (this.disposed || (this.captureToken && this.captureToken !== token)) return false;
    this.captureToken = token;
    for (const key of this.owned) this.release(key);
    this.changed(); return true;
  }
  endCapture(token?: string): void {
    if (!this.captureToken || (token && token !== this.captureToken)) return;
    this.captureToken = null; this.suppressUntil = Date.now() + 600; this.restore();
  }
  dispose(): void { this.disposed = true; this.captureToken = null; for (const key of this.owned) this.release(key); }
}
