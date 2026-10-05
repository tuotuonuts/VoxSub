/** Actual renderer card bounds, in CSS pixels. No HWND or native pixels from renderer. */
export interface OverlayFrame {
  x: number; y: number; width: number; height: number; radius: number;
  viewportWidth: number; viewportHeight: number;
}
export function validOverlayFrame(value: unknown): value is OverlayFrame {
  if (!value || typeof value !== "object") return false;
  const f = value as OverlayFrame;
  return [f.x,f.y,f.width,f.height,f.radius,f.viewportWidth,f.viewportHeight].every(n => typeof n === "number" && Number.isFinite(n) && n >= 0 && n <= 20000)
    && f.width > 0 && f.height > 0 && f.viewportWidth > 0 && f.viewportHeight > 0
    && f.x + f.width <= f.viewportWidth + 1 && f.y + f.height <= f.viewportHeight + 1;
}
