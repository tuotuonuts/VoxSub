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
  const radius = Math.min(OVERLAY_FRAME_RADIUS * zoom, w / 2, h / 2);
  const rows: ShapeRect[] = [];
  // Only corner bands need scanlines; the middle stays one rectangle at any height.
  const band = Math.ceil(radius);
  function add(y: number, count: number, dx: number): void {
    const row = { x: inset + dx, y: inset + y, width: w - 2 * dx, height: count };
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
