import { buildPageFrame } from "./ui/page-frame";
import { buildButton } from "./ui/button";
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
import { applySessionState, callWithOutcome, connectBackend, refreshSessionState, setUiTranslator, store } from "./store";
import { CMD } from "./protocol";
import { buildLanguageControls, initializeLanguageCapabilities, refreshLanguageCapabilities } from "./language-capabilities";
import { tr } from "./i18n";
import { buildWorkspace, updateProgress, updateStatus, updateStream } from "./views/workspace";
import { buildModelCatalog, refreshDownloads } from "./views/catalog";
import { buildSettings, loadConfig } from "./views/settings";
import { buildDiagnostics, refreshLogView } from "./views/diagnostics";
import { buildOcrWorkspace } from "./views/ocr";
import { buildMigrationWizard, shouldOfferMigration } from "./views/migration";
import { type Disposer, type PageHandle } from "../shared/page-lifecycle";

setUiTranslator(tr);

type Mode = "a" | "b" | "c" | "d";

const MODES: ReadonlyArray<readonly [Mode, string, string, string]> = [
  ["a", "A", tr("麦克风同传"), tr("边说边出双语字幕")],
  ["b", "B", tr("系统声音"), tr("会议 / 网课 / 视频")],
  ["c", "C", tr("音视频文件"), tr("导入并导出 SRT")],
  ["d", "D", tr("屏幕 OCR"), tr("框选后原位覆盖译文")],
];

let workspaceSlot: HTMLElement | null = null;
let pageLayer: HTMLElement | null = null;
type PageName = "settings" | "diagnostics" | "catalog";
let currentPage: "none" | PageName = "none";

/**
 * 当前页面内容的释放句柄。
 *
 * 为什么必须有（缺陷 #10）：页面切换原先只有 `replaceChildren`，注册在
 * window/document 上的监听器（设置页的 `voxsub:settings`、工作区的
 * `voxsub:state`、工作区那个 1 秒计时器）**只增不减** —— 用得越久越慢，
 * 回调还会打到已经移除的节点上。现在每个页面构建时返回 `{element, dispose}`，
 * 关闭/替换/换页三处统一释放。
 */
let pageDispose: Disposer | null = null;
let workspaceDispose: Disposer | null = null;

/** 释放当前页面内容（关页、换页、被向导顶掉都要走这里）。 */
function disposePageContent(): void {
  const dispose = pageDispose;
  pageDispose = null;
  dispose?.();
}

/** 释放当前工作区（切换模式时会替换整块）。 */
function disposeWorkspace(): void {
  const dispose = workspaceDispose;
  workspaceDispose = null;
  dispose?.();
}

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

function openPage(page: PageName): void {
  if (!pageLayer) return;
  // 换页前先释放上一页：监听器、定时器、订阅、在途回调都在这里收掉
  disposePageContent();

  currentPage = page;
  pageLayer.hidden = false;

  // 这里**同步**渲染，不能等配置。
  // 试过先 await loadConfig() 再渲染：点"设置"后会空白约 1 秒才出现，
  // 冒烟测试与用户都判定为"点了没反应"。配置改为在 boot() 阶段预取，
  // 页面内的异步刷新（buildSettings 里）负责补上最新值。
  //
  // 三个页面**都**返回 `{ element, dispose }`：目录页也走同一条释放路径
  // （它原来返回裸元素、被 staticPage 包成空 dispose，关页后异步回调仍会
  // 往已移除的网格上写）。
  const builders: Record<PageName, () => PageHandle> = {
    settings: buildSettings,
    diagnostics: buildDiagnostics,
    catalog: buildModelCatalog,
  };
  const handle = builders[page]();
  pageDispose = handle.dispose;
  pageLayer.replaceChildren(buildPageFrame(tr(PAGE_TITLE[page]), handle.element, closePage));
  // A new page must not inherit the old layer/wizard scroll offset.
  pageLayer.scrollTop = 0;
  // 打开后焦点给返回按钮：键盘用户一按 Enter 就能回去
  pageLayer.querySelector<HTMLButtonElement>(".page__back")?.focus({ preventScroll: true });
}

function closePage(): void {
  if (!pageLayer) return;
  // 释放页面：结束监听器、定时器与订阅，并让在途异步回调停止写 DOM（缺陷 #10）
  disposePageContent();
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

  nav.append(buildLanguageControls());

  // 二级页面入口
  const navBox = h("div", { class: "side-actions" });
  const settingsBtn = buildButton(tr("设置"), { block: true });
  on(settingsBtn, "click", () => openPage("settings"));
  const diagBtn = buildButton(tr("诊断"), { block: true });
  on(diagBtn, "click", () => openPage("diagnostics"));
  const overlayBtn = buildButton("打开浮窗", { block: true });
  on(overlayBtn, "click", () => void window.voxsub?.overlay.toggleVisible());
  navBox.append(overlayBtn, settingsBtn, diagBtn);
  nav.append(navBox);

  return nav;
}

async function switchMode(mode: Mode): Promise<void> {
  // 会话运行中不允许换模式。
  //
  // 后端 Pipeline.set_mode 只在**非运行**状态下生效，运行中切换会被静默忽略 ——
  // 界面于是与后端各说各话（界面显示 C 模式、实际仍在 A 模式拾音）。这会连带
  // 让"暂停按钮是否该出现"判断错（supportsPause 按界面模式算）。
  // 与其让用户以为切成功了，不如明确拒绝并说明。
  if (store.get().running) {
    store.patch({ statusText: tr("会话进行中，请先结束后再切换模式") });
    return;
  }

  // 先落 UI 再通知后端：反向等待会让切换有肉眼可见的延迟（后端要跨进程往返）。
  store.patch({ mode });
  renderWorkspace();
  document.querySelectorAll(".mode-cell").forEach((node) => {
    const isActive = node.getAttribute("data-mode") === mode;
    node.classList.toggle("is-active", isActive);
    node.setAttribute("aria-pressed", String(isActive));
  });

  const result = await callWithOutcome(CMD.setMode, { mode });
  // set_mode has no state event: do not retain the previous mode's recording capability.
  // OCR is renderer-only; reading the audio pipeline mode here would overwrite D.
  // A newer click owns the workspace, not this late acknowledgement.
  if (result.outcome === "ok" && mode !== "d" && store.get().mode === mode) {
    await refreshSessionState();
  }
}

function renderWorkspace(): void {
  if (!workspaceSlot) return;
  // 切模式会整块替换工作区：先释放上一块（监听器 + 会话计时器），
  // 否则每切一次模式就多一个 `voxsub:state` 监听和一个永不停走的 1 秒定时器。
  disposeWorkspace();
  const mode = store.get().mode;
  const handle: PageHandle = mode === "d" ? buildOcrWorkspace() : buildWorkspace();
  workspaceDispose = handle.dispose;
  workspaceSlot.replaceChildren(handle.element);
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

  const catalogBtn = buildButton(tr("模型"));
  on(catalogBtn, "click", () => openPage("catalog"));
  actions.append(catalogBtn);

  const settingsBtn = buildButton(tr("设置"));
  on(settingsBtn, "click", () => openPage("settings"));
  actions.append(settingsBtn);

  const diagnosticsBtn = buildButton(tr("诊断"));
  on(diagnosticsBtn, "click", () => openPage("diagnostics"));
  actions.append(diagnosticsBtn);

  const themeBtn = buildButton(tr("主题"));
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
  initializeLanguageCapabilities();
  render();

  // 预取配置：设置页的控件（模型目录、模型下拉、调优默认值）全都依赖它。
  // 这里异步拉取、不阻塞首屏；buildSettings 打开时若已就绪就直接用上，
  // 未就绪则由它自己的刷新补上。
  void loadConfig().then(() => refreshLanguageCapabilities(), () => refreshLanguageCapabilities());

  // 首次启动：检测旧版数据风险，需要时弹出迁移向导。
  // 放在 render 之后异步执行 —— 检测要读注册表与遍历目录，不能阻塞首屏。
  void shouldOfferMigration().then((result) => {
    if (!result || !pageLayer) return;
    // 异步回调返回后复查页面状态（缺陷 #10 的同一类纪律）：检测期间用户可能
    // 已经打开设置/诊断页 —— 那说明他在做别的事，此时把向导顶上去会把他正在
    // 看的页面换掉。这种情况直接放弃自动弹出（设置页里仍有手动入口）。
    if (currentPage !== "none") return;
    // 打开前先释放可能已有的页面内容（向导会顶掉当前页）
    disposePageContent();
    const wizard = buildMigrationWizard(result);
    pageDispose = wizard.dispose;
    pageLayer.hidden = false;
    pageLayer.replaceChildren(wizard.element);
  });

  // store 变化 → 增量刷新；不做整页重建，避免输入框失焦与滚动跳动
  //
  // 合并策略：rAF 优先（对齐渲染帧，突发更新只刷一次），但**必须有超时兜底**。
  // 实测：从未 show 过的窗口里 rAF 被节流到约 1fps（首帧 474ms、次帧 1000ms），
  // 而 setTimeout 不受影响（0ms）。只靠 rAF 的话按钮状态会滞后整整一秒 ——
  // 表现是"点了开始，按钮过一秒才变成结束"，自动化测试里更明显（读到的
  // 永远是上一次的状态）。所以谁先到就谁刷，另一个取消。
  let pending = false;
  let rafHandle = 0;
  let timerHandle = 0;

  const flush = (): void => {
    if (!pending) return;
    pending = false;
    if (rafHandle) { cancelAnimationFrame(rafHandle); rafHandle = 0; }
    if (timerHandle) { clearTimeout(timerHandle); timerHandle = 0; }

    const state = store.get();
    updateStatus();
    updateStream();
    updateProgress();
    refreshLogView();
    if (Object.keys(state.downloads).length) refreshDownloads();
    document.dispatchEvent(new Event("voxsub:state"));
  };

  store.subscribe(() => {
    if (pending) return;
    pending = true;
    rafHandle = requestAnimationFrame(flush);
    // 50ms 足够容纳正常帧（16ms），又远低于用户能察觉的延迟
    timerHandle = window.setTimeout(flush, 50);
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

  window.voxsub?.shortcuts?.onNotice(key => { store.patch({ statusText: tr(key) }); });

  // 主进程可请求打开设置（例如从托盘）
  window.voxsub?.app.onOpenPage?.((page) => openPage(page as PageName));

  // 退出被拦下：说明原因，并停在设置页让用户看到进度
  window.voxsub?.app.onBlockingTask?.((payload) => {
    window.alert(payload?.reason ?? "有后台任务正在运行，暂时无法退出。");
  });

  // 供自动化验证：模拟一条后端 state 事件。
  //
  // 为什么需要这个入口：会话状态的**真实来源**是后端事件，而要触发它就必须
  // 真的开始一个会话 —— 那会占用用户的麦克风/系统声音，而自动化测试明确
  // 不允许占用音频。所以这里开一个入口，走的是与真实事件**完全相同**的
  // 代码路径（applySessionState → store.patch → syncControls），
  // 因此能真实反映"按钮会不会跟着状态变"。
  //
  // 后端的**发出**那一侧由 tests/test_pipeline_state_events.py 覆盖
  // （用假音频源，同样不碰真实设备）。
  Object.defineProperty(window, "__applySessionState", {
    value: (payload: { running?: boolean; paused?: boolean } | null) => {
      applySessionState(payload ?? null);
    },
    writable: false,
  });
}

document.addEventListener("DOMContentLoaded", boot);
