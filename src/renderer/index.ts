/**
 * 主窗渲染入口。
 *
 * 结构（同人展目录方向）：
 *   展牌（品牌 + 会话控制）
 *   模式索引（左栏，选中被圈出）
 *   工作区（字幕 / OCR，随模式切换）
 *   模型目录（下方密铺网格）
 *   二级页面（设置 / 诊断，覆盖式）
 *
 * 注意：类型来自 src/renderer/api.d.ts 的全局声明，不需要 import。
 */
import { palette, type ThemeName } from "./palette";
import { h, on } from "./dom";
import { call, connectBackend, store } from "./store";
import { CMD } from "./protocol";
import { tr } from "./i18n";
import { buildWorkspace, updateProgress, updateStatus, updateStream } from "./views/workspace";
import { buildModelCatalog, refreshDownloads } from "./views/catalog";
import { buildSettings } from "./views/settings";
import { buildDiagnostics, detachDiagnostics, refreshLogView } from "./views/diagnostics";
import { buildOcrWorkspace } from "./views/ocr";
import { buildMigrationWizard, shouldOfferMigration } from "./views/migration";

type Mode = "a" | "b" | "c" | "d";

const MODES: ReadonlyArray<readonly [Mode, string, string, string]> = [
  ["a", "A", tr("麦克风同传"), tr("边说边出双语字幕")],
  ["b", "B", tr("系统声音"), tr("会议 / 网课 / 视频")],
  ["c", "C", tr("音视频文件"), tr("导入并导出 SRT")],
  ["d", "D", tr("屏幕 OCR"), tr("框选后原位覆盖译文")],
];

const SOURCE_LANGS: ReadonlyArray<readonly [string, string]> = [
  ["auto", tr("自动识别")],
  ["zh", tr("中文")],
  ["en", tr("英文")],
  ["ja", tr("日文")],
  ["ko", tr("韩文")],
];

const TARGET_LANGS: ReadonlyArray<readonly [string, string]> = [
  ["zh", tr("中文")],
  ["en", tr("英文")],
  ["ja", tr("日文")],
  ["ko", tr("韩文")],
];

let workspaceSlot: HTMLElement | null = null;
let pageLayer: HTMLElement | null = null;
type PageName = "settings" | "diagnostics" | "catalog";
let currentPage: "none" | PageName = "none";

/* ------------------------------------------------------------- 令牌应用 */

function applyTheme(theme: ThemeName): void {
  const set = palette[theme];
  const root = document.documentElement;
  for (const [key, value] of Object.entries(set)) {
    root.style.setProperty(`--${key.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`, value);
  }
  root.dataset["theme"] = theme;
}

/* ------------------------------------------------------------- 二级页面 */

/** 页面标题：返回按钮与标题栏需要知道当前在哪一页。 */
const PAGE_TITLE: Record<PageName, string> = {
  catalog: "模型",
  settings: "设置",
  diagnostics: "诊断",
};

/**
 * 二级页面外壳：统一的返回栏 + 内容区。
 *
 * 之前每页直接把自己塞进 pageLayer，返回只能按 Esc —— 不知道这个快捷键的
 * 用户会被困在页面里。返回按钮放在外壳上，三页长得一致、行为一致。
 */
function wrapPage(page: PageName, content: HTMLElement): HTMLElement {
  const frame = h("div", { class: "page" });

  const bar = h("header", { class: "page__bar" });
  const back = h("button", {
    class: "page__back",
    type: "button",
    title: tr("返回（Esc）"),
    "aria-label": tr("返回"),
  });
  back.append(
    h("span", { class: "page__back-icon", text: "←" }),
    h("span", { class: "page__back-text", text: tr("返回") }),
  );
  on(back, "click", () => closePage());

  bar.append(back, h("span", { class: "page__title", text: tr(PAGE_TITLE[page]) }));
  frame.append(bar, content);
  return frame;
}

function openPage(page: PageName): void {
  if (!pageLayer) return;
  currentPage = page;
  pageLayer.hidden = false;
  const builders: Record<PageName, () => HTMLElement> = {
    settings: buildSettings,
    diagnostics: buildDiagnostics,
    catalog: buildModelCatalog,
  };
  pageLayer.replaceChildren(wrapPage(page, builders[page]()));
  // 打开后焦点给返回按钮：键盘用户一按 Enter 就能回去
  pageLayer.querySelector<HTMLButtonElement>(".page__back")?.focus();
}

function closePage(): void {
  if (!pageLayer) return;
  // 通知页面内的在途异步回调停止写 DOM（诊断自检要跑 4-6 秒）
  if (currentPage === "diagnostics") detachDiagnostics();
  currentPage = "none";
  pageLayer.hidden = true;
  pageLayer.replaceChildren();
}

/* ------------------------------------------------------------- 左栏索引 */

function buildModeIndex(): HTMLElement {
  const nav = h("nav", { class: "mode-index", "aria-label": tr("模式") });
  nav.append(h("div", { class: "mode-index__head" }, [h("span", { class: "mode-index__title", text: tr("模式") })]));

  for (const [id, badge, title, desc] of MODES) {
    const active = store.get().mode === id;
    const card = h("button", {
      class: active ? "mode-cell is-active" : "mode-cell",
      type: "button",
      "data-mode": id,
      "aria-pressed": String(active),
    });
    card.append(
      h("span", { class: "mode-cell__badge", text: badge }),
      h("span", { class: "mode-cell__body" }, [
        h("span", { class: "mode-cell__title", text: title }),
        h("span", { class: "mode-cell__desc", text: desc }),
      ]),
    );
    on(card, "click", () => void switchMode(id));
    nav.append(card);
  }

  // 语言选择（识别语言 + 翻译为，与原 Qt 版一致的双下拉）
  const langBox = h("div", { class: "lang-box" });
  const state = store.get();

  const srcSel = h("select", { class: "select select--sm", "aria-label": tr("识别语言") });
  for (const [val, label] of SOURCE_LANGS) {
    const opt = h("option", { value: val, text: label });
    if (val === state.sourceLang) opt.selected = true;
    srcSel.append(opt);
  }
  on(srcSel, "change", () => {
    store.patch({ sourceLang: srcSel.value });
    void call(CMD.setLangs, { source: srcSel.value, target: store.get().targetLang });
  });

  const dstSel = h("select", { class: "select select--sm", "aria-label": tr("翻译为") });
  for (const [val, label] of TARGET_LANGS) {
    const opt = h("option", { value: val, text: label });
    if (val === state.targetLang) opt.selected = true;
    dstSel.append(opt);
  }
  on(dstSel, "change", () => {
    store.patch({ targetLang: dstSel.value });
    void call(CMD.setLangs, { source: store.get().sourceLang, target: dstSel.value });
  });

  langBox.append(
    h("span", { class: "lang-box__label", text: tr("识别语言") }), srcSel,
    h("span", { class: "lang-box__label", text: tr("翻译为") }), dstSel,
  );
  nav.append(langBox);

  // 二级页面入口
  const navBox = h("div", { class: "side-actions" });
  const settingsBtn = h("button", { class: "btn btn--ghost btn--block", type: "button", text: tr("设置") });
  on(settingsBtn, "click", () => openPage("settings"));
  const diagBtn = h("button", { class: "btn btn--ghost btn--block", type: "button", text: tr("诊断") });
  on(diagBtn, "click", () => openPage("diagnostics"));
  const overlayBtn = h("button", { class: "btn btn--ghost btn--block", type: "button", text: "打开浮窗" });
  on(overlayBtn, "click", () => void window.voxsub?.overlay.toggleVisible());
  navBox.append(overlayBtn, settingsBtn, diagBtn);
  nav.append(navBox);

  return nav;
}

async function switchMode(mode: Mode): Promise<void> {
  // 先落 UI 再通知后端：反向等待会让切换有肉眼可见的延迟（后端要跨进程往返）。
  store.patch({ mode });
  renderWorkspace();
  document.querySelectorAll(".mode-cell").forEach((node) => {
    const isActive = node.getAttribute("data-mode") === mode;
    node.classList.toggle("is-active", isActive);
    node.setAttribute("aria-pressed", String(isActive));
  });

  await call(CMD.setMode, { mode });
}

function renderWorkspace(): void {
  if (!workspaceSlot) return;
  const mode = store.get().mode;
  workspaceSlot.replaceChildren(mode === "d" ? buildOcrWorkspace() : buildWorkspace());
}

/* ------------------------------------------------------------- 展牌 */

function buildTopbar(): HTMLElement {
  const bar = h("header", { class: "topbar" });
  const brand = h("div", { class: "topbar__brand" });
  brand.append(
    h("h1", { class: "topbar__title", text: tr("语幕") }),
    h("p", { class: "topbar__sub", text: tr("让对话、会议和视频，落成清晰的双语文幕。") }),
  );
  bar.append(brand);

  const actions = h("div", { class: "topbar__actions" });

  const catalogBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("模型") });
  on(catalogBtn, "click", () => openPage("catalog"));
  actions.append(catalogBtn);

  const settingsBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("设置") });
  on(settingsBtn, "click", () => openPage("settings"));
  actions.append(settingsBtn);

  const diagnosticsBtn = h("button", {
    class: "btn btn--ghost",
    type: "button",
    text: tr("诊断"),
  });
  on(diagnosticsBtn, "click", () => openPage("diagnostics"));
  actions.append(diagnosticsBtn);

  const themeBtn = h("button", { class: "btn btn--ghost", type: "button", text: tr("主题") });
  on(themeBtn, "click", () => {
    const next: ThemeName = store.get().theme === "dark" ? "light" : "dark";
    store.patch({ theme: next });
    applyTheme(next);
  });
  actions.append(themeBtn);

  bar.append(actions);
  return bar;
}

/* ------------------------------------------------------------- 装配 */

function render(): void {
  const root = document.getElementById("app");
  if (!root) return;

  const shell = h("div", { class: "shell" });
  shell.append(buildTopbar());

  // 主体：模式索引 + 工作区。字幕是主角，占满剩余高度；
  // 模型目录这类低频操作移到独立页面（顶栏进入），不再挤压字幕区。
  const body = h("div", { class: "shell__body" });
  body.append(buildModeIndex());

  workspaceSlot = h("div", { class: "workspace-slot" });
  body.append(workspaceSlot);
  shell.append(body);

  pageLayer = h("div", { class: "page-layer", hidden: true });
  shell.append(pageLayer);

  root.replaceChildren(shell);
  renderWorkspace();
}

function boot(): void {
  applyTheme(store.get().theme);

  // 先把后端拉起来再接界面：模型目录、设备列表都在挂载时就要取数据，
  // 晚一步启动会让首屏所有请求落空（见 store.ts 的"后端就绪门"）。
  connectBackend();
  render();

  // 首次启动：检测旧版数据风险，需要时弹出迁移向导。
  // 放在 render 之后异步执行 —— 检测要读注册表与遍历目录，不能阻塞首屏。
  void shouldOfferMigration().then((result) => {
    if (!result || !pageLayer) return;
    pageLayer.hidden = false;
    pageLayer.replaceChildren(buildMigrationWizard(result));
  });

  // store 变化 → 增量刷新；不做整页重建，避免输入框失焦与滚动跳动
  let pending = false;
  store.subscribe(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      const state = store.get();
      updateStatus();
      updateStream();
      updateProgress();
      refreshLogView();
      if (Object.keys(state.downloads).length) refreshDownloads();
      document.dispatchEvent(new Event("voxsub:state"));
    });
  });

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const saved = document.documentElement.dataset["themeChoice"];
    if (!saved || saved === "system") {
      const theme: ThemeName = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      store.patch({ theme });
      applyTheme(theme);
    }
  });

  // Esc 关闭二级页面。
  //
  // 例外：焦点在输入控件里时先让控件处理（比如数字输入框按 Esc 撤销编辑），
  // 否则用户想取消一个输入却把整页关掉，得重新进来。
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || currentPage === "none") return;

    const active = document.activeElement as HTMLElement | null;
    const tag = active?.tagName?.toLowerCase();
    const isEditing =
      tag === "input" || tag === "textarea" || tag === "select" || active?.isContentEditable;
    if (isEditing) {
      active?.blur();
      return;
    }

    // 展开的浮层先收一层（例如显示模式菜单），再关页面
    const openPopup = document.querySelector(".menu:not([hidden])");
    if (openPopup) {
      openPopup.setAttribute("hidden", "");
      return;
    }

    closePage();
  });

  // 主进程可请求打开设置（例如从托盘）
  window.voxsub?.app.onOpenPage?.((page) => openPage(page as PageName));

  // 退出被拦下：说明原因，并停在设置页让用户看到进度
  window.voxsub?.app.onBlockingTask?.((payload) => {
    window.alert(payload?.reason ?? "有后台任务正在运行，暂时无法退出。");
  });
}

document.addEventListener("DOMContentLoaded", boot);
