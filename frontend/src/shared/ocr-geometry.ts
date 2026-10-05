/** Screen DIP, thumbnail pixels and overlay CSS pixels are distinct spaces. */
export interface Rect { x: number; y: number; width: number; height: number }
export function intersect(a: Rect, b: Rect): Rect | null {
  if (![...Object.values(a), ...Object.values(b)].every(Number.isFinite)) return null;
  const x = Math.max(a.x, b.x), y = Math.max(a.y, b.y);
  const width = Math.min(a.x + a.width, b.x + b.width) - x;
  const height = Math.min(a.y + a.height, b.y + b.height) - y;
  return width > 0 && height > 0 ? { x, y, width, height } : null;
}
export function virtualBounds(displays: Rect[]): Rect {
  const x = Math.min(...displays.map(d => d.x)), y = Math.min(...displays.map(d => d.y));
  return {x, y, width: Math.max(...displays.map(d => d.x + d.width)) - x,
    height: Math.max(...displays.map(d => d.y + d.height)) - y};
}
export function bestDisplay<T extends { bounds: Rect }>(area: Rect, displays: T[]): T | null {
  return displays.map(display => ({display, size: intersect(area, display.bounds)}))
    .sort((a, b) => (b.size ? b.size.width * b.size.height : 0) - (a.size ? a.size.width * a.size.height : 0))
    .find(row => row.size)?.display ?? null;
}
export function thumbnailRect(area: Rect, bounds: Rect, size: {width: number; height: number}): Rect | null {
  const clipped = intersect(area, bounds);
  if (!clipped || size.width <= 0 || size.height <= 0) return null;
  const sx = size.width / bounds.width, sy = size.height / bounds.height;
  const x = Math.max(0, Math.floor((clipped.x - bounds.x) * sx));
  const y = Math.max(0, Math.floor((clipped.y - bounds.y) * sy));
  const right = Math.min(size.width, Math.ceil((clipped.x + clipped.width - bounds.x) * sx));
  const bottom = Math.min(size.height, Math.ceil((clipped.y + clipped.height - bounds.y) * sy));
  return {x, y, width: right - x, height: bottom - y};
}
export function overlayRect(box: number[], image: {width: number; height: number}, viewport: {width: number; height: number}): Rect | null {
  if (box.length !== 4 || !box.every(Number.isFinite) || image.width <= 0 || image.height <= 0) return null;
  const [left, top, right, bottom] = box as [number, number, number, number];
  const rect = intersect({x: left, y: top, width: right - left, height: bottom - top},
    {x: 0, y: 0, width: image.width, height: image.height});
  return rect ? {x: rect.x * viewport.width / image.width, y: rect.y * viewport.height / image.height,
    width: rect.width * viewport.width / image.width, height: rect.height * viewport.height / image.height} : null;
}
