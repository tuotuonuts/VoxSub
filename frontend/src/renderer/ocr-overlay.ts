/** Pixel coordinates -> overlay DIP. Failed/empty frames expire, never stick indefinitely. */
import { overlayRect } from "../shared/ocr-geometry";
interface OcrLine { text: string; box: number[] }
const container = document.getElementById("blocks");
let expires: ReturnType<typeof setTimeout> | null = null;
function clear(): void {
  if (expires !== null) clearTimeout(expires);
  expires = null;
  container?.replaceChildren();
}
function stale(): void {
  container?.querySelectorAll(".block").forEach(node => node.classList.add("is-stale"));
  // Keep the original deadline: repeated empty/failing frames cannot extend it.
}
function render(lines: OcrLine[], width: number, height: number): void {
  if (!container) return;
  if (!lines.length) { stale(); return; }
  const nodes: HTMLElement[] = [];
  const placed: Array<{x: number; y: number; width: number; height: number}> = [];
  for (const line of lines) {
    if (!line.text?.trim()) continue;
    const rect = overlayRect(line.box, {width, height}, {width: window.innerWidth, height: window.innerHeight});
    if (!rect || rect.width < 4 || rect.height < 4) continue;
    if (placed.some(p => rect.x < p.x + p.width && rect.x + rect.width > p.x && rect.y < p.y + p.height && rect.y + rect.height > p.y)) continue;
    placed.push(rect);
    const block = document.createElement("div");
    block.className = "block";
    block.style.left = `${rect.x}px`; block.style.top = `${rect.y}px`;
    block.style.width = `${rect.width}px`; block.style.height = `${rect.height}px`;
    const capacity = Math.max(1, (rect.width - 8) * (rect.height - 4) / Math.max(1, line.text.length));
    block.style.fontSize = `${Math.max(7, Math.min(rect.height * .62, Math.sqrt(capacity / .78), 26)).toFixed(1)}px`;
    block.textContent = line.text;
    nodes.push(block);
  }
  if (!nodes.length) { stale(); return; }
  clear(); container.replaceChildren(...nodes);
  expires = setTimeout(clear, 2500);
}
function boot(): void {
  const api = window.voxsub;
  if (!api) return;
  api.ocrOverlay.onRegionReady(clear);
  api.ocrOverlay.onTranslated(payload => {
    const data = payload as {lines?: OcrLine[]; width?: number; height?: number} | null;
    render(data?.lines ?? [], data?.width ?? 0, data?.height ?? 0);
  });
  api.ocrOverlay.onFrameFailed(stale);
  window.addEventListener("beforeunload", clear, {once: true});
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
