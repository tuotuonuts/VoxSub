import type { OverlayFrame } from "../shared/overlay-frame";
import { overlayShape, frameShape } from "../shared/overlay-shape";
import type { ShapeRect } from "../shared/overlay-shape";
export interface SurfaceWindow {
  getContentSize(): number[];
  webContents: { getZoomFactor(): number };
  setShape(rects: ShapeRect[]): void;
  setBackgroundMaterial(material: "acrylic" | "none"): void;
  setBackgroundColor(color: string): void;
}
interface SurfaceState {
  material: "none" | "acrylic" | "unknown";
  shape: string;
  clipping: boolean;
  applying: boolean;
}
/** Weak ownership: rebuilt windows never inherit another HWND's material/region cache. */
export class OverlaySurface {
  private readonly owners = new WeakMap<SurfaceWindow, SurfaceState>();
  invalidate(win: SurfaceWindow): void { const state=this.owners.get(win); if(state) { state.shape=""; state.material="unknown"; } }
  apply(win: SurfaceWindow, enabled: boolean, displayScale = 1, frame?: OverlayFrame, keepClip = false): boolean {
    const state: SurfaceState = this.owners.get(win) ?? { material: "unknown", shape: "", clipping: false, applying: false };
    this.owners.set(win, state);
    if (state.applying) return state.material === "acrylic";
    state.applying = true;
    const material = (value: "acrylic" | "none") => {
      // Throwing native calls may still change OS state; never cache them as success.
      state.material = "unknown";
      try { win.setBackgroundMaterial(value); }
      finally {
        // Electron resets the WebContentsView background to opaque white on "none".
        // Restore per-pixel transparency after EVERY material call, including failure.
        // CSS alone cannot make an opaque WebContentsView transparent.
        win.setBackgroundColor("#00000000");
      }
      state.material = value;
    };
    const unclip = () => {
      if (!state.clipping) return;
      state.shape = "";
      win.setShape([]); state.clipping = false;
    };
    try {
      if (!enabled) {
        // Never expand the region while acrylic may still be installed.
        if (state.material !== "none") material("none");
        if (keepClip && frame) {
          const [w,h]=win.getContentSize();
          const shape=frameShape(w!,h!,win.webContents.getZoomFactor(),frame);
          if (shape) { win.setShape(shape);state.clipping=true;state.shape=""; }
        }
        if (!keepClip) unclip();
        return false;
      }
      const [width, height] = win.getContentSize();
      const zoom = win.webContents.getZoomFactor();
      const shape = frame ? frameShape(width!, height!, zoom, frame) : overlayShape(width!, height!, zoom);
      if (!shape) throw new Error("Overlay geometry is unavailable");
      // Native coordinates are DIP; monitor scale invalidates but does not multiply them.
      const key = `${width}:${height}:${zoom}:${displayScale}:${JSON.stringify(frame ?? null)}`;
      if (state.shape !== key) {
        state.clipping = true; state.shape = "";
        win.setShape(shape); state.shape = key;
      }
      if (state.material !== "acrylic") material("acrylic");
      return true;
    } catch (error) {
      // Do not expand until removal succeeds. A later request retries unknown native state.
      try { material("none"); if (!keepClip) unclip(); }
      catch { /* retain clipped bounds; caller reports unavailable */ }
      throw error;
    } finally { state.applying = false; }
  }
}
