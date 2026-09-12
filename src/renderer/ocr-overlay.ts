/**
 * OCR 覆盖层：把译文画回原文的坐标上。
 *
 * 坐标换算：后端返回的 box 是相对截图区域的像素坐标；覆盖窗与截图区域
 * 同尺寸，因此可以直接使用。
 *
 * 防重叠：按原框高度排序后，逐块检查是否与已放置的块相交，相交则跳过。
 * 保守策略的理由 —— 覆盖框重叠会让两块译文互相遮挡，比少画一块更糟。
 */

interface OcrLine {
  text: string;
  box: number[];
}

const container = document.getElementById("blocks");
let lastFrame: OcrLine[] = [];

/** box 可能是 [x1,y1,x2,y2] 或 [[x,y],...] 两种形态，这里统一成矩形。 */
function toRect(box: number[]): { x: number; y: number; w: number; h: number } | null {
  if (!Array.isArray(box) || box.length < 4) return null;

  if (typeof box[0] === "number" && box.length === 4) {
    const [x1, y1, x2, y2] = box as [number, number, number, number];
    return {
      x: Math.min(x1, x2),
      y: Math.min(y1, y2),
      w: Math.abs(x2 - x1),
      h: Math.abs(y2 - y1),
    };
  }

  if (Array.isArray(box[0])) {
    const flat = (box as unknown as number[][]).flat();
    const xs = flat.filter((_, i) => i % 2 === 0);
    const ys = flat.filter((_, i) => i % 2 === 1);
    if (xs.length === 0 || ys.length === 0) return null;
    const x = Math.min(...xs);
    const y = Math.min(...ys);
    return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
  }

  return null;
}

function render(lines: OcrLine[]): void {
  if (!container) return;

  if (lines.length === 0) {
    // 空结果保留旧画面并标记为过期，避免闪白造成"字幕消失"的错觉
    container.querySelectorAll(".block").forEach((n) => n.classList.add("is-stale"));
    return;
  }

  const placed: Array<{ x: number; y: number; w: number; h: number }> = [];
  const nodes: HTMLElement[] = [];

  for (const line of lines) {
    const rect = toRect(line.box);
    if (!rect || rect.w < 4 || rect.h < 4) continue;

    const overlaps = placed.some(
      (p) =>
        rect.x < p.x + p.w &&
        rect.x + rect.w > p.x &&
        rect.y < p.y + p.h &&
        rect.y + rect.h > p.y,
    );
    if (overlaps) continue;

    placed.push(rect);

    const block = document.createElement("div");
    block.className = "block";
    block.style.left = `${rect.x}px`;
    block.style.top = `${rect.y}px`;
    block.style.width = `${rect.w}px`;
    block.style.minHeight = `${rect.h}px`;

    // 字号以原框高度为基准；译文比原文长时逐级缩小，避免溢出到相邻块
    let size = Math.max(9, Math.min(rect.h * 0.62, 26));
    if (line.text.length > rect.w / (size * 0.62)) size = Math.max(9, size * 0.82);
    block.style.fontSize = `${size.toFixed(1)}px`;
    block.textContent = line.text;

    nodes.push(block);
  }

  container.replaceChildren(...nodes);
  lastFrame = lines;
}

function boot(): void {
  const api = window.voxsub;
  if (!api) return;

  api.ocrOverlay.onRegionReady(() => {
    container?.replaceChildren();
    lastFrame = [];
  });

  api.ocrOverlay.onTranslated((payload) => {
    const data = payload as { lines?: OcrLine[] } | null;
    render(data?.lines ?? []);
  });

  // 单帧失败不清空：保留上一帧可读内容，比闪成空白好
  api.ocrOverlay.onFrameFailed(() => {
    lastFrame.forEach(() => undefined);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
