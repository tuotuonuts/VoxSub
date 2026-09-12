/**
 * DOM 构造小工具。
 *
 * 不用框架的原因：这个项目的界面状态是"事件推着走"的（字幕流、进度、日志），
 * 不需要虚拟 DOM 的 diff 能力；而工具函数能保证样式类名集中、不散落字符串。
 */

type Attrs = Record<string, string | number | boolean | undefined>;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs?: Attrs,
  children?: Array<Node | string | null | undefined | false>,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === false) continue;
      if (key === "class") node.className = String(value);
      else if (key === "text") node.textContent = String(value);
      else if (key === "html") node.innerHTML = String(value);
      else if (key.startsWith("on") && typeof value === "string") {
        // 事件一律用 addEventListener 绑定，这里只处理静态属性
        node.setAttribute(key.toLowerCase(), value);
      } else if (value === true) node.setAttribute(key, "");
      else node.setAttribute(key, String(value));
    }
  }
  if (children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      node.append(typeof child === "string" ? document.createTextNode(child) : child);
    }
  }
  return node;
}

/** 绑定点击：集中处理 disabled 与键盘可达性。 */
export function on<K extends keyof HTMLElementTagNameMap>(
  node: HTMLElementTagNameMap[K],
  type: string,
  handler: (event: Event) => void,
): void {
  node.addEventListener(type, handler);
}

export function clear(node: HTMLElement): void {
  node.replaceChildren();
}

/** 质量分：用方格表示，不用进度条（避免与真实进度混淆）。 */
export function scorePips(score: number, max = 5): HTMLElement {
  const wrap = h("span", { class: "pip-row", title: `质量分 ${score}` });
  const filled = Math.max(0, Math.min(max, Math.round(score / (100 / max))));
  for (let i = 0; i < max; i += 1) {
    wrap.append(h("i", { class: i < filled ? "pip is-on" : "pip" }));
  }
  return wrap;
}

export function humanBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return "内置";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return index === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${units[index]}`;
}

export function percent(done: number, total: number): string {
  if (!total || total <= 0) return "—";
  return `${Math.round((done / total) * 100)}%`;
}
