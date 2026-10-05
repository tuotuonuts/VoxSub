/** All sizes are Electron DIP: Windows display scaling is already applied to workArea. */
export interface WorkArea { x: number; y: number; width: number; height: number }
export function initialMainWindowBounds(area: WorkArea): WorkArea & { minWidth: number; minHeight: number } {
  const dimension = (value: number, fallback: number): number =>
    Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
  const availableWidth = dimension(area.width, 1280);
  const availableHeight = dimension(area.height, 900);
  const inset = Math.min(16, Math.floor(Math.min(availableWidth, availableHeight) / 10));
  const width = Math.min(1240, Math.max(1, availableWidth - inset * 2));
  const height = Math.min(820, Math.max(1, availableHeight - inset * 2));
  return {
    x: (Number.isFinite(area.x) ? area.x : 0) + Math.floor((availableWidth - width) / 2),
    y: (Number.isFinite(area.y) ? area.y : 0) + Math.floor((availableHeight - height) / 2),
    width, height, minWidth: Math.min(640, width), minHeight: Math.min(420, height),
  };
}
