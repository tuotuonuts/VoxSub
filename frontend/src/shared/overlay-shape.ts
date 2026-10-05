import { validOverlayFrame, type OverlayFrame } from "./overlay-frame";
/** CSS pixels; native shape scales these by the renderer zoom (not monitor DPI). */
export const OVERLAY_FRAME_INSET = 6;
export const OVERLAY_FRAME_RADIUS = 16;
export interface ShapeRect { x: number; y: number; width: number; height: number }

/** Integer scanlines, merged vertically. null is unsafe; [] would reset the native region. */
export function overlayShape(width: number, height: number, zoom = 1): ShapeRect[] | null {
  if (![width, height, zoom].every(Number.isFinite) || zoom <= 0 || zoom > 5) return null;
  const inset = Math.ceil(OVERLAY_FRAME_INSET * zoom);
  const w = Math.floor(width) - inset * 2, h = Math.floor(height) - inset * 2;
  if (w < 4 || h < 4) return null;
  return roundedShape(inset, inset, w, h, Math.min(OVERLAY_FRAME_RADIUS * zoom, w / 2, h / 2));
}

export function frameShape(width: number, height: number, zoom: number, frame: OverlayFrame): ShapeRect[] | null {
  if (![width, height, zoom].every(Number.isFinite) || !validOverlayFrame(frame) || zoom <= 0 || zoom > 5) return null;
  if (Math.abs(frame.viewportWidth * zoom - width) > 2 || Math.abs(frame.viewportHeight * zoom - height) > 2) return null;
  const x=Math.ceil(frame.x*zoom), y=Math.ceil(frame.y*zoom);
  const w=Math.floor((frame.x+frame.width)*zoom)-x, h=Math.floor((frame.y+frame.height)*zoom)-y;
  if (w<4 || h<4 || x+w>width || y+h>height) return null;
  return roundedShape(x,y,w,h,Math.min(frame.radius*zoom,w/2,h/2));
}

function roundedShape(left: number, top: number, w: number, h: number, radius: number): ShapeRect[] {
  const rows: ShapeRect[] = [];
  // Only corner bands need scanlines; the middle stays one rectangle at any height.
  const band = Math.ceil(radius);
  function add(y: number, count: number, dx: number): void {
    const row = { x: left + dx, y: top + y, width: w - 2 * dx, height: count };
    const previous = rows.at(-1);
    if (previous && previous.x === row.x && previous.width === row.width && previous.y + previous.height === row.y) previous.height += count;
    else rows.push(row);
  }
  for (let y = 0; y < h;) {
    if (y >= band && y < h - band) { add(y, h - band - y, 0); y = h - band; continue; }
    const dy = Math.max(0, radius - Math.min(y + .5, h - y - .5));
    const dx = Math.ceil(radius - Math.sqrt(Math.max(0, radius * radius - dy * dy)));
    add(y++, 1, dx);
  }
  return rows;
}
