#!/usr/bin/env node
/**
 * 极简 DOM —— 让「组件渲染出来的东西」能在 node 里被真实断言。
 *
 * ## 为什么需要它
 *
 * 组件测试要断言的是**实际展示与交互结果**：渲染出的 DOM 结构、文案、
 * 禁用状态，以及点了按钮之后回调有没有按预期触发。这些只能在真实 DOM 上验证，
 * 而用真实浏览器的两条路都走不通：
 *   · 起 Electron 需要窗口/GPU/音频设备，且会弹出窗口、抢焦点 —— 明确禁止；
 *   · 装 jsdom 要给项目加一个运行时依赖，而项目刻意保持零前端依赖。
 *
 * 所以这里手写一个**只覆盖本项目用到的那部分 DOM** 的实现。它是测试用具，
 * 不参与打包、不进 dist、不改变产品行为。
 *
 * ## 覆盖范围（按实际用法逐条加的，不是通用 DOM）
 *
 *   createElement / createTextNode / documentElement / body
 *   className + classList(add/remove/toggle/contains)
 *   setAttribute/getAttribute/hasAttribute/removeAttribute（带属性↔属性名同步：
 *     hidden/disabled/checked/value/selected/id/title/type 等）
 *   textContent（读：递归拼接；写：替换子节点）/ innerHTML（写入即报错，避免
 *     假装支持解析）
 *   append / replaceChildren / replaceWith / remove / prepend
 *   querySelector / querySelectorAll / closest —— 支持 标签、.类、#id、
 *     [属性]、[属性="值"]、:not(...) 与后代组合（"A B"），够本项目用；
 *     不支持的选择器会**抛错**而不是静默返回空，避免测试假通过。
 *   addEventListener / removeEventListener / dispatchEvent / click()
 *   style（属性式 `style.width = "50%"` 与 `setAttribute("style", ...)` 两种写法）
 *   dataset、scrollTop/scrollHeight、hidden/disabled/checked、focus/blur
 *
 * ## 计数
 *
 * 返回的 `stats` 记录 window/document 上的 add/remove 调用与定时器生死 ——
 * 这正是「切页 N 次后残留多少监听」要量的东西。
 *
 * 用法：
 *   import { installMiniDom } from "./mini-dom.mjs";
 *   const dom = installMiniDom();
 *   const { document, window, stats } = dom;
 *   ...
 *   dom.restore();
 */

const WORD = /[A-Za-z0-9_-]/;

class FakeEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.target = null;
    this.currentTarget = null;
    this.defaultPrevented = false;
    this.detail = init.detail;
    this.key = init.key;
    this.button = init.button;
    Object.assign(this, init);
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
  stopPropagation() {
    /* 本项目不依赖冒泡中断 */
  }
}

/* ------------------------------------------------------------ 选择器 */

/** 只解析项目用到的选择器语法；认不出来的部分会被 matches 视为不匹配。 */
function parseSimple(text) {
  const out = { tag: null, id: null, classes: [], attrs: [], nots: [] };
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === ".") {
      let j = i + 1;
      while (j < text.length && WORD.test(text[j])) j += 1;
      if (j === i + 1) throw new Error(`选择器无法解析：${text}`);
      out.classes.push(text.slice(i + 1, j));
      i = j;
    } else if (ch === "#") {
      let j = i + 1;
      while (j < text.length && WORD.test(text[j])) j += 1;
      out.id = text.slice(i + 1, j);
      i = j;
    } else if (ch === "[") {
      const end = text.indexOf("]", i);
      if (end === -1) throw new Error(`选择器无法解析（缺 ]）：${text}`);
      const inner = text.slice(i + 1, end);
      const eq = inner.indexOf("=");
      if (eq === -1) out.attrs.push([inner.trim(), null]);
      else {
        out.attrs.push([
          inner.slice(0, eq).trim(),
          inner.slice(eq + 1).trim().replace(/^["']/, "").replace(/["']$/, ""),
        ]);
      }
      i = end + 1;
    } else if (ch === ":") {
      const open = text.indexOf("(", i);
      const close = text.lastIndexOf(")");
      if (open === -1 || close === -1) throw new Error(`选择器无法解析（伪类需带括号）：${text}`);
      const name = text.slice(i + 1, open);
      if (name !== "not") throw new Error(`不支持的选择器伪类：:${name}`);
      out.nots.push(parseSimple(text.slice(open + 1, close)));
      i = close + 1;
    } else if (WORD.test(ch)) {
      let j = i;
      while (j < text.length && WORD.test(text[j])) j += 1;
      out.tag = text.slice(i, j).toLowerCase();
      i = j;
    } else {
      throw new Error(`选择器无法解析（意外字符 ${ch}）：${text}`);
    }
  }
  return out;
}

function parseSelector(selector) {
  return String(selector)
    .split(",")
    .map((group) => group.trim())
    .filter(Boolean)
    .map((group) => group.split(/\s+/).filter(Boolean).map(parseSimple));
}

function matchesSimple(el, simple) {
  if (simple.tag && el.nodeName.toLowerCase() !== simple.tag) return false;
  if (simple.id && (el.getAttribute("id") ?? "") !== simple.id) return false;
  for (const cls of simple.classes) if (!el.classList.contains(cls)) return false;
  for (const [name, value] of simple.attrs) {
    if (!el.hasAttribute(name)) return false;
    if (value !== null && String(el.getAttribute(name)) !== value) return false;
  }
  for (const not of simple.nots) if (matchesSimple(el, not)) return false;
  return true;
}

function matchesComplex(el, parts) {
  const last = parts[parts.length - 1];
  if (!matchesSimple(el, last)) return false;
  let node = el.parentNode;
  for (let i = parts.length - 2; i >= 0; i -= 1) {
    let found = false;
    while (node) {
      if (node.nodeKind === "element" && matchesSimple(node, parts[i])) {
        found = true;
        node = node.parentNode;
        break;
      }
      node = node.parentNode;
    }
    if (!found) return false;
  }
  return true;
}

/* ------------------------------------------------------------ style */

class Style {
  constructor(element) {
    this._element = element;
    this._props = new Map();
    return new Proxy(this, {
      get(target, key, receiver) {
        if (key in target) return Reflect.get(target, key, receiver);
        return target._props.get(key);
      },
      set(target, key, value, receiver) {
        if (key in target) return Reflect.set(target, key, value, receiver);
        target._props.set(key, String(value));
        return true;
      },
    });
  }
  setProperty(name, value) {
    this._props.set(String(name), String(value));
  }
  getPropertyValue(name) {
    return this._props.get(String(name)) ?? "";
  }
  get cssText() {
    return [...this._props].map(([k, v]) => `${k}: ${v}`).join("; ");
  }
  set cssText(text) {
    this._props.clear();
    for (const chunk of String(text).split(";")) {
      const idx = chunk.indexOf(":");
      if (idx === -1) continue;
      this._props.set(chunk.slice(0, idx).trim(), chunk.slice(idx + 1).trim());
    }
  }
}

class Dataset {
  constructor(element) {
    this._element = element;
    return new Proxy(this, {
      get(target, key) {
        if (key === "_element") return element;
        const attr = `data-${String(key).replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
        return element.getAttribute(attr) ?? undefined;
      },
      set(target, key, value) {
        const attr = `data-${String(key).replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
        element.setAttribute(attr, String(value));
        return true;
      },
      has(target, key) {
        const attr = `data-${String(key).replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
        return element.hasAttribute(attr);
      },
    });
  }
}

class ClassList {
  constructor(element) {
    this._element = element;
  }
  add(...names) {
    for (const name of names) if (name) this._element._classes.add(String(name));
  }
  remove(...names) {
    for (const name of names) this._element._classes.delete(String(name));
  }
  contains(name) {
    return this._element._classes.has(String(name));
  }
  toggle(name, force) {
    const want = force === undefined ? !this.contains(name) : Boolean(force);
    if (want) this.add(name);
    else this.remove(name);
    return want;
  }
  get value() {
    return [...this._element._classes].join(" ");
  }
  toString() {
    return this.value;
  }
}

/* ------------------------------------------------------------ 节点 */

class FakeNode {
  constructor(kind) {
    this.nodeKind = kind;
    this.parentNode = null;
  }
}

class FakeTextNode extends FakeNode {
  constructor(data) {
    super("text");
    this.data = String(data);
    this.nodeName = "#text";
  }
  get textContent() {
    return this.data;
  }
  set textContent(value) {
    this.data = String(value);
  }
}

class FakeElement extends FakeNode {
  constructor(tagName, ownerDocument) {
    super("element");
    this.ownerDocument = ownerDocument;
    this.tagName = String(tagName).toUpperCase();
    this.nodeName = this.tagName;
    this._attrs = new Map();
    this._children = [];
    this._classes = new Set();
    this._listeners = new Map();
    this._value = "";
    this._checked = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.isContentEditable = false;
    this.focused = false;
    this.hidden = false;
    this.disabled = false;
    this.style = new Style(this);
    this.dataset = new Dataset(this);
    this.classList = new ClassList(this);
  }

  /* hidden / disabled：属性与属性名双向同步（选择器 [hidden] 依赖它） */
  get hidden() {
    return this._attrs.has("hidden");
  }
  set hidden(value) {
    if (value) this._attrs.set("hidden", "");
    else this._attrs.delete("hidden");
  }
  get disabled() {
    return this._attrs.has("disabled");
  }
  set disabled(value) {
    if (value) this._attrs.set("disabled", "");
    else this._attrs.delete("disabled");
  }

  /* 属性 */
  setAttribute(name, value) {
    const key = String(name).toLowerCase();
    if (key === "class") {
      this._classes = new Set(String(value).split(/\s+/).filter(Boolean));
      return;
    }
    if (key === "style") {
      this.style.cssText = value;
      return;
    }
    this._attrs.set(key, String(value));
    if (key === "hidden") this.hidden = true;
    if (key === "disabled") this.disabled = true;
    if (key === "checked") this._checked = true;
  }
  getAttribute(name) {
    const key = String(name).toLowerCase();
    if (key === "class") return this.classList.value;
    if (key === "style") return this.style.cssText;
    return this._attrs.has(key) ? this._attrs.get(key) : null;
  }
  hasAttribute(name) {
    return this.getAttribute(name) !== null;
  }
  removeAttribute(name) {
    const key = String(name).toLowerCase();
    this._attrs.delete(key);
    if (key === "hidden") this.hidden = false;
    if (key === "disabled") this.disabled = false;
    if (key === "checked") this._checked = false;
  }

  get className() {
    return this.classList.value;
  }
  set className(value) {
    this._classes = new Set(String(value).split(/\s+/).filter(Boolean));
  }
  get id() {
    return this.getAttribute("id") ?? "";
  }
  set id(value) {
    this.setAttribute("id", value);
  }
  get value() {
    return this._value !== "" ? this._value : this.getAttribute("value") ?? "";
  }
  set value(value) {
    this._value = String(value);
  }
  get checked() {
    return this._checked;
  }
  set checked(value) {
    this._checked = Boolean(value);
  }
  get selected() {
    return this.hasAttribute("selected");
  }
  set selected(value) {
    if (value) this.setAttribute("selected", "");
    else this.removeAttribute("selected");
  }
  get type() {
    return this.getAttribute("type") ?? "";
  }
  set type(value) {
    this.setAttribute("type", value);
  }
  get children() {
    return this._children.filter((n) => n.nodeKind === "element");
  }
  get childNodes() {
    return [...this._children];
  }
  get childElementCount() {
    return this.children.length;
  }
  get firstChild() {
    return this._children[0] ?? null;
  }
  get lastChild() {
    return this._children[this._children.length - 1] ?? null;
  }
  get parentElement() {
    return this.parentNode && this.parentNode.nodeKind === "element" ? this.parentNode : null;
  }

  /* 文本 */
  get textContent() {
    return this._children.map((child) => child.textContent).join("");
  }
  set textContent(value) {
    this._detachAll();
    if (value !== "" && value !== null && value !== undefined) {
      this._append(this.ownerDocument.createTextNode(value));
    }
  }
  get innerHTML() {
    throw new Error("mini-dom 不支持读取 innerHTML");
  }
  set innerHTML(value) {
    throw new Error(`mini-dom 不支持解析 innerHTML（写入：${String(value).slice(0, 40)}…）`);
  }

  /* 树操作 */
  _append(node) {
    if (typeof node === "string") node = this.ownerDocument.createTextNode(node);
    if (!node || node.nodeKind === undefined) {
      throw new Error(`append 收到非节点值：${String(node)}`);
    }
    if (node.parentNode) node.parentNode._children = node.parentNode._children.filter((n) => n !== node);
    node.parentNode = this;
    this._children.push(node);
    return node;
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node === null || node === undefined || node === false) continue;
      this._append(node);
    }
  }
  prepend(...nodes) {
    const added = nodes
      .filter((n) => n !== null && n !== undefined && n !== false)
      .map((n) => (typeof n === "string" ? this.ownerDocument.createTextNode(n) : n));
    for (const node of added) {
      if (node.parentNode) node.parentNode._children = node.parentNode._children.filter((n) => n !== node);
      node.parentNode = this;
    }
    this._children.unshift(...added);
  }
  _detachAll() {
    for (const child of this._children) child.parentNode = null;
    this._children = [];
  }
  replaceChildren(...nodes) {
    this._detachAll();
    this.append(...nodes);
  }
  replaceWith(...nodes) {
    const parent = this.parentNode;
    if (!parent) return;
    const index = parent._children.indexOf(this);
    if (index === -1) return;
    const added = nodes
      .filter((n) => n !== null && n !== undefined && n !== false)
      .map((n) => (typeof n === "string" ? this.ownerDocument.createTextNode(n) : n));
    for (const node of added) node.parentNode = parent;
    parent._children.splice(index, 1, ...added);
    this.parentNode = null;
  }
  remove() {
    const parent = this.parentNode;
    if (!parent) return;
    parent._children = parent._children.filter((n) => n !== this);
    this.parentNode = null;
  }

  /* 查询 */
  _descendants() {
    const out = [];
    const walk = (node) => {
      for (const child of node._children) {
        if (child.nodeKind !== "element") continue;
        out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  querySelectorAll(selector) {
    const groups = parseSelector(selector);
    return this._descendants().filter((el) => groups.some((parts) => matchesComplex(el, parts)));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }
  closest(selector) {
    const groups = parseSelector(selector);
    let node = this;
    while (node && node.nodeKind === "element") {
      if (groups.some((parts) => matchesComplex(node, parts))) return node;
      node = node.parentNode;
    }
    return null;
  }
  matches(selector) {
    const groups = parseSelector(selector);
    return groups.some((parts) => matchesComplex(this, parts));
  }

  /* 事件 */
  addEventListener(type, handler) {
    if (typeof handler !== "function") return;
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type).add(handler);
  }
  removeEventListener(type, handler) {
    this._listeners.get(type)?.delete(handler);
  }
  listenerCount(type) {
    if (type === undefined) {
      let total = 0;
      for (const set of this._listeners.values()) total += set.size;
      return total;
    }
    return this._listeners.get(type)?.size ?? 0;
  }
  dispatchEvent(event) {
    if (!event.target) event.target = this;
    event.currentTarget = this;
    const handlers = [...(this._listeners.get(event.type) ?? [])];
    for (const handler of handlers) handler.call(this, event);
    return !event.defaultPrevented;
  }
  click() {
    return this.dispatchEvent(new FakeEvent("click"));
  }

  focus() {
    this.focused = true;
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }
  blur() {
    this.focused = false;
    if (this.ownerDocument?.activeElement === this) this.ownerDocument.activeElement = null;
  }
}

/* ------------------------------------------------------------ document / window */

function createCounters() {
  return { adds: [], removes: [] };
}

function countingTarget(base, counters) {
  return {
    addEventListener(type, handler, options) {
      counters.adds.push({ type, handler, options, target: base });
      base.addEventListener(type, handler, options);
    },
    removeEventListener(type, handler, options) {
      counters.removes.push({ type, handler, options, target: base });
      base.removeEventListener(type, handler);
    },
    dispatchEvent(event) {
      return base.dispatchEvent(event);
    },
    listenerCount(type) {
      return base.listenerCount(type);
    },
  };
}

export function installMiniDom() {
  const htmlEl = new FakeElement("html", null);
  const bodyEl = new FakeElement("body", null);

  const doc = {
    nodeKind: "document",
    documentElement: htmlEl,
    body: bodyEl,
    activeElement: null,
    createElement(tag) {
      return new FakeElement(tag, doc);
    },
    createTextNode(text) {
      return new FakeTextNode(text);
    },
    getElementById(id) {
      return htmlEl.querySelector(`#${id}`);
    },
    querySelector: (selector) => htmlEl.querySelector(selector) ?? bodyEl.querySelector(selector),
    querySelectorAll: (selector) => [
      ...htmlEl.querySelectorAll(selector),
      ...bodyEl.querySelectorAll(selector),
    ],
  };
  htmlEl.ownerDocument = doc;
  bodyEl.ownerDocument = doc;
  htmlEl._append(bodyEl);

  const docCounters = createCounters();
  const docListeners = new Map();
  doc.add = countingTarget(
    {
      addEventListener(type, handler) {
        if (!docListeners.has(type)) docListeners.set(type, new Set());
        docListeners.get(type).add(handler);
      },
      removeEventListener(type, handler) {
        docListeners.get(type)?.delete(handler);
      },
      dispatchEvent(event) {
        event.target = event.target ?? doc;
        for (const handler of [...(docListeners.get(event.type) ?? [])]) handler.call(doc, event);
        return true;
      },
      listenerCount(type) {
        if (type === undefined) {
          let total = 0;
          for (const set of docListeners.values()) total += set.size;
          return total;
        }
        return docListeners.get(type)?.size ?? 0;
      },
    },
    docCounters,
  );
  doc.addEventListener = doc.add.addEventListener;
  doc.removeEventListener = doc.add.removeEventListener;
  doc.dispatchEvent = doc.add.dispatchEvent;
  doc.listenerCount = doc.add.listenerCount;

  const timerStats = { created: 0, cleared: 0, live: new Set(), delayMs: [] };
  const realSetInterval = globalThis.setInterval;
  const realClearInterval = globalThis.clearInterval;
  const realSetTimeout = globalThis.setTimeout;
  const realClearTimeout = globalThis.clearTimeout;

  const winListeners = new Map();
  const winCounters = createCounters();

  const winTarget = countingTarget(
    {
      addEventListener(type, handler) {
        if (!winListeners.has(type)) winListeners.set(type, new Set());
        winListeners.get(type).add(handler);
      },
      removeEventListener(type, handler) {
        winListeners.get(type)?.delete(handler);
      },
      dispatchEvent(event) {
        event.target = event.target ?? win;
        for (const handler of [...(winListeners.get(event.type) ?? [])]) handler.call(win, event);
        return true;
      },
      listenerCount(type) {
        if (type === undefined) {
          let total = 0;
          for (const set of winListeners.values()) total += set.size;
          return total;
        }
        return winListeners.get(type)?.size ?? 0;
      },
    },
    winCounters,
  );

  const win = {
    document: doc,
    matchMedia: (query) => ({
      media: query,
      matches: false,
      addEventListener() {},
      removeEventListener() {},
    }),
    alert(message) {
      throw new Error(`测试中不允许弹窗（window.alert 被调用）：${message}`);
    },
    focus() {},
    setInterval: (fn, ms) => {
      timerStats.created += 1;
      timerStats.delayMs.push(ms);
      const handle = realSetInterval(fn, ms);
      timerStats.live.add(handle);
      return handle;
    },
    clearInterval: (handle) => {
      timerStats.cleared += 1;
      timerStats.live.delete(handle);
      realClearInterval(handle);
    },
    setTimeout: (fn, ms) => realSetTimeout(fn, ms),
    clearTimeout: (handle) => realClearTimeout(handle),
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
  };
  win.addEventListener = winTarget.addEventListener;
  win.removeEventListener = winTarget.removeEventListener;
  win.dispatchEvent = winTarget.dispatchEvent;
  win.listenerCount = winTarget.listenerCount;
  win.window = win;
  win.self = win;

  const previous = {
    document: globalThis.document,
    window: globalThis.window,
    Event: globalThis.Event,
    HTMLElement: globalThis.HTMLElement,
    CSS: globalThis.CSS,
    navigatorDescriptor: Object.getOwnPropertyDescriptor(globalThis, "navigator"),
  };

  /**
   * navigator 在 node 22 里是只有 getter 的全局（直接赋值会抛 TypeError），
   * 所以用 defineProperty 覆盖，并在 restore() 里还原原始描述符。
   */
  const defineGlobal = (name, value) => {
    Object.defineProperty(globalThis, name, {
      value,
      writable: true,
      configurable: true,
      enumerable: false,
    });
  };

  const clipboardWrites = [];
  defineGlobal("document", doc);
  defineGlobal("window", win);
  /* 视图里用的是裸 setInterval/setTimeout（模块作用域），要连全局一起接管才能计量 */
  defineGlobal("setInterval", win.setInterval);
  defineGlobal("clearInterval", win.clearInterval);
  defineGlobal("setTimeout", win.setTimeout);
  defineGlobal("clearTimeout", win.clearTimeout);
  defineGlobal("Event", FakeEvent);
  defineGlobal("CustomEvent", FakeEvent);
  defineGlobal("HTMLElement", FakeElement);
  defineGlobal("Node", FakeNode);
  defineGlobal("CSS", {
    escape: (value) => String(value).replace(/["\\]/g, "\\$&"),
  });
  defineGlobal("navigator", {
    clipboard: {
      writeText(text) {
        clipboardWrites.push(text);
        return Promise.resolve();
      },
    },
  });

  return {
    document: doc,
    window: win,
    MiniElement: FakeElement,
    makeEvent: (type, init) => new FakeEvent(type, init),
    /** 挂到 document.body 下，这样 document.querySelector 能找到它。 */
    mount(element) {
      bodyEl.replaceChildren(element);
      return element;
    },
    /** 让在途的 promise/定时器跑完（在途回调常在被替换页面上写 DOM，测试要能观察到）。 */
    async flushAsync(turns = 3) {
      for (let i = 0; i < turns; i += 1) await new Promise((resolve) => realSetTimeout(resolve, 0));
    },
    counters: { window: winCounters, document: docCounters },
    /**
     * window + document 上仍活着的监听数。
     *
     * `residual` 是 add 调用数减去 remove 调用数（对应「注册了没释放」）；
     * `live` 是真正还挂在事件表里的数量 —— 两者都给出，避免只看调用次数：
     * 若移除时引用/options 与注册不一致，调用数会显示"已释放"而实际仍活着。
     */
    residualListeners() {
      return {
        residual: {
          window: winCounters.adds.length - winCounters.removes.length,
          document: docCounters.adds.length - docCounters.removes.length,
        },
        live: {
          window: win.listenerCount(),
          document: doc.listenerCount(),
        },
      };
    },
    timers: timerStats,
    clipboardWrites,
    restore() {
      globalThis.document = previous.document;
      globalThis.window = previous.window;
      globalThis.Event = previous.Event;
      globalThis.HTMLElement = previous.HTMLElement;
      globalThis.CSS = previous.CSS;
      if (previous.navigatorDescriptor) {
        Object.defineProperty(globalThis, "navigator", previous.navigatorDescriptor);
      } else {
        delete globalThis.navigator;
      }
      for (const handle of timerStats.live) realClearInterval(handle);
      timerStats.live.clear();
      defineGlobal("setInterval", realSetInterval);
      defineGlobal("clearInterval", realClearInterval);
      defineGlobal("setTimeout", realSetTimeout);
      defineGlobal("clearTimeout", realClearTimeout);
    },
  };
}
