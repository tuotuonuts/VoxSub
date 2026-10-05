/** Native material state is evidence of an API request, not compositor verification. */
export interface OverlayGlassState {
  enabled: boolean;
  strength: number;
  supported: boolean;
  active: boolean;
  clippingCheck?: "not_run" | "checking" | "pass" | "fail";
  materialCheck?: "not_run" | "checking" | "pass" | "fail";
  desktopCheck?: "not_run";
  fallbackReason?: string | null;
  reason: "unsupported" | "unavailable" | null;
}
/** The OS owns the blur radius. Strength exposes more material by reducing the tint. */
export function glassTint(opacity: number, glass: Pick<OverlayGlassState, "active" | "strength">): number {
  return glass.active ? opacity * (1 - glass.strength / 100 * 0.7) : opacity;
}
