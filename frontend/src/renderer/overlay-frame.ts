/** Renderer-owned geometry observer: no polling, no native coordinate assumptions. */
import type { OverlayFrame } from "../shared/overlay-frame";
export function observeOverlayFrame(card: HTMLElement, send: (frame: OverlayFrame) => void): () => void {
  let pending = 0, last = "", alive = true;
  const report = () => {
    pending = 0; if (!alive) return;
    const r = card.getBoundingClientRect();
    const frame = { x:r.x,y:r.y,width:r.width,height:r.height,
      radius:parseFloat(getComputedStyle(card).borderTopLeftRadius), viewportWidth:innerWidth, viewportHeight:innerHeight };
    const key=JSON.stringify(frame);
    if (key !== last) { last=key;send(frame); }
  };
  const schedule = () => { if (!pending && alive) pending=requestAnimationFrame(report); };
  const observer=new ResizeObserver(schedule);observer.observe(card);
  window.addEventListener("resize",schedule);
  window.visualViewport?.addEventListener("resize",schedule);
  schedule();
  return () => { alive=false;cancelAnimationFrame(pending);observer.disconnect();window.removeEventListener("resize",schedule);window.visualViewport?.removeEventListener("resize",schedule); };
}
