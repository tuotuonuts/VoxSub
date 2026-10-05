/** Native material state is evidence of an API request, not compositor verification. */
export interface OverlayGlassState {
  enabled: boolean;
  /** Changes only for settings requests, never for native geometry/status probes. */
  settingsRevision?: number;
  strength: number;
  supported: boolean;
  active: boolean;
  clippingCheck?: "not_run" | "checking" | "pass" | "fail";
  materialCheck?: "not_run" | "checking" | "pass" | "fail";
  desktopCheck?: "not_run";
  fallbackReason?: string | null;
  reason: "unsupported" | "unavailable" | null;
}
/** Background opacity has one owner. Never simulate blur strength by reducing it. */
export function overlayBackgroundOpacity(opacity: number): number {
  return Number.isFinite(opacity) ? Math.min(1, Math.max(0.2, opacity)) : 0.92;
}
