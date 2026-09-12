/**
 * 设置页 —— 对应原 Qt 版 settings_window.py（1905 行，6 个分页）。
 *
 * 分页：翻译 / 语音 / 设备 / 识别调优 / 外观 / 关于
 * 纪律：调优参数使用显式保存（保存/放弃事务），其余项即时生效。
 */
import { h, on } from "../dom";
import { call, store } from "../store";
import { CMD, type AudioDevice, type HardwareProfile } from "../protocol";
import { tr, setLanguage, currentLanguage } from "../i18n";

type Config = Record<string, unknown>;

let config: Config = {};
let microphones: AudioDevice[] = [];
let loopbacks: AudioDevice[] = [];
/** 调优草稿：只有点保存才写回，避免逐项写配置造成抖动。 */
let tuningDraft: Record<string, unknown> = {};
let tuningDirty = false;

export async function loadConfig(): Promise<void> {
  const result = await call<Config>(CMD.getConfig);
  config = result ?? {};
  tuningDraft = {
    asr_tuning_profile: config["asr_tuning_profile"] ?? "auto",
    asr_vad_threshold: config["asr_vad_threshold"] ?? 0.35,
    asr_silence_ms: config["asr_silence_ms"] ?? 650,
    asr_max_utterance_ms: config["asr_max_utterance_ms"] ?? 12000,
    asr_beam_paths: config["asr_beam_paths"] ?? 4,
    asr_max_new_tokens: config["asr_max_new_tokens"] ?? 512,
    asr_hotwords: config["asr_hotwords"] ?? "",
    asr_context_hold_ms: config["asr_context_hold_ms"] ?? 1800,
    asr_live_draft_enabled: config["asr_live_draft_enabled"] ?? true,
    asr_context_correction: config["asr_context_correction"] ?? true,
    asr_filler_mode: config["asr_filler_mode"] ?? "light",
  };
  tuningDirty = false;
}

async function saveConfig(updates: Config): Promise<void> {
  const merged = await call<Config>(CMD.setConfig, { updates });
  if (merged) config = merged;
}

/* ------------------------------------------------------------ 通用控件 */

function field(label: string, control: HTMLElement, hint?: string): HTMLElement {
  const wrap = h("div", { class: "field" });
  wrap.append(h("label", { class: "field__label", text: label }));
  wrap.append(control);
  if (hint) wrap.append(h("p", { class: "field__hint", text: hint }));
  return wrap;
}

function textInput(value: string, onChange: (v: string) => void, opts?: { type?: string; placeholder?: string }): HTMLElement {
  const input = h("input", {
    class: "input",
    type: opts?.type ?? "text",
    value,
    placeholder: opts?.placeholder ?? "",
  });
  on(input, "change", () => onChange(input.value));
  return input;
}

function select<T extends string>(
  value: T,
  options: ReadonlyArray<readonly [T, string]>,
  onChange: (v: T) => void,
): HTMLSelectElement {
  const sel = h("select", { class: "select" });
  for (const [val, label] of options) {
    const opt = h("option", { value: val, text: label });
    if (val === value) opt.selected = true;
    sel.append(opt);
  }
  on(sel, "change", () => onChange(sel.value as T));
  return sel;
}

/** 单选组：用圆形指示，避免原生控件在深色档下几何变形。 */
function radioGroup<T extends string>(
  value: T,
  options: ReadonlyArray<readonly [T, string]>,
  onChange: (v: T) => void,
): HTMLElement {
  const group = h("div", { class: "radio-group", role: "radiogroup" });
  for (const [val, label] of options) {
    const item = h("button", {
      class: val === value ? "radio is-checked" : "radio",
      type: "button",
      role: "radio",
      "aria-checked": String(val === value),
    });
    item.append(h("i", { class: "radio__dot" }), h("span", { text: label }));
    on(item, "click", () => {
      group.querySelectorAll(".radio").forEach((n) => {
        n.classList.remove("is-checked");
        n.setAttribute("aria-checked", "false");
      });
      item.classList.add("is-checked");
      item.setAttribute("aria-checked", "true");
      onChange(val);
    });
    group.append(item);
  }
  return group;
}

function toggleSwitch(checked: boolean, label: string, onChange: (v: boolean) => void): HTMLElement {
  const wrap = h("label", { class: "switch" });
  const input = h("input", { type: "checkbox" });
  input.checked = checked;
  on(input, "change", () => onChange(input.checked));
  wrap.append(input, h("span", { class: "switch__track" }), h("span", { class: "switch__label", text: label }));
  return wrap;
}

function card(title: string, children: Array<HTMLElement | null>): HTMLElement {
  const section = h("section", { class: "card" });
  if (title) section.append(h("h3", { class: "card__title", text: title }));
  const body = h("div", { class: "card__body" });
  for (const child of children) if (child) body.append(child);
  section.append(body);
  return section;
}

/* ------------------------------------------------------------ 各分页 */

function translationTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });

  const sttProvider = String(config["stt_provider"] ?? "local");
  const tier = String(config["translate_tier"] ?? "fast");

  page.append(
    card(tr("语音识别"), [
      radioGroup<"local" | "cloud">(
        sttProvider === "cloud" ? "cloud" : "local",
        [["local", tr("本地识别")], ["cloud", tr("云端识别")]],
        (v) => void saveConfig({ stt_provider: v }),
      ),
      field(tr("模型名"), textInput(String(config["stt_model"] ?? ""), (v) => void saveConfig({ stt_model: v })), "云端识别使用的模型"),
      field(tr("API 地址"), textInput(String(config["stt_base_url"] ?? ""), (v) => void saveConfig({ stt_base_url: v }))),
      field(tr("API 密钥"), textInput(String(config["stt_api_key"] ?? ""), (v) => void saveConfig({ stt_api_key: v }), { type: "password" })),
    ]),
  );

  page.append(
    card(tr("翻译档位"), [
      radioGroup<"fast" | "quality" | "cloud">(
        tier as "fast" | "quality" | "cloud",
        [["fast", tr("快档")], ["quality", tr("质量档")], ["cloud", tr("云端")]],
        (v) => {
          void saveConfig({ translate_tier: v });
          const kind = v === "fast" ? "opus-fast" : v === "quality" ? "qwen-quality" : "cloud";
          void call(CMD.setTranslator, { kind, config: {} });
        },
      ),
      field(tr("模型名"), textInput(String(config["translate_model"] ?? ""), (v) => void saveConfig({ translate_model: v }))),
      field(tr("API 地址"), textInput(String(config["translate_base_url"] ?? ""), (v) => void saveConfig({ translate_base_url: v }))),
      field(tr("API 密钥"), textInput(String(config["translate_api_key"] ?? ""), (v) => void saveConfig({ translate_api_key: v }), { type: "password" })),
    ]),
  );

  return page;
}

function voiceTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });
  page.append(
    card(tr("朗读译文"), [
      toggleSwitch(Boolean(config["tts_enabled"]), tr("朗读译文"), (v) => {
        void saveConfig({ tts_enabled: v });
        void call(CMD.setTts, { enabled: v });
      }),
      h("p", { class: "field__hint", text: "朗读失败时只降级为字幕，不会中断识别。" }),
      field(tr("中文朗读模型"), h("span", { class: "readonly-value", text: String(config["tts_model_id_zh"] ?? "—") }), "在模型目录中下载更多音色"),
      field(tr("英文朗读模型"), h("span", { class: "readonly-value", text: String(config["tts_model_id_en"] ?? "—") })),
    ]),
  );
  return page;
}

function devicesTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });

  const micOptions: Array<readonly [string, string]> = [["", "默认设备"]];
  for (const mic of microphones) micOptions.push([mic.id, mic.name]);
  const loopOptions: Array<readonly [string, string]> = [["", "默认设备"]];
  for (const loop of loopbacks) loopOptions.push([loop.id, loop.name]);

  page.append(
    card(tr("设备"), [
      field(tr("麦克风"), select(String(config["mic_device_id"] ?? ""), micOptions, (v) => {
        void saveConfig({ mic_device_id: v });
        void call(CMD.setAudioDevices, {
          microphone: v,
          loopback: String(config["loopback_device_id"] ?? ""),
        });
      })),
      field(tr("系统输出"), select(String(config["loopback_device_id"] ?? ""), loopOptions, (v) => {
        void saveConfig({ loopback_device_id: v });
        void call(CMD.setAudioDevices, {
          microphone: String(config["mic_device_id"] ?? ""),
          loopback: v,
        });
      })),
      field(tr("应用声音隔离"), h("span", { class: "readonly-value", text: String(config["capture_window_title"] || "未选择应用") }), "在 B 模式下可只捕获指定应用的声音"),
    ]),
  );
  return page;
}

function tuningTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });

  const profiles: ReadonlyArray<readonly [string, string]> = [
    ["auto", "自动"],
    ["responsive", tr("快档")],
    ["balanced", "均衡"],
    ["accurate", "准确优先"],
    ["context", "智能上下文"],
    ["custom", "自定义"],
  ];

  const markDirty = (): void => {
    tuningDirty = true;
    const bar = page.querySelector<HTMLElement>(".tuning-actions__state");
    if (bar) bar.textContent = "有未保存的更改";
  };

  const numField = (key: string, label: string, min: number, max: number, step: number, hint?: string): HTMLElement => {
    const input = h("input", {
      class: "input input--num",
      type: "number",
      value: String(tuningDraft[key] ?? ""),
      min: String(min),
      max: String(max),
      step: String(step),
    });
    on(input, "change", () => {
      tuningDraft[key] = Number(input.value);
      markDirty();
    });
    return field(label, input, hint);
  };

  page.append(
    card(tr("识别调优"), [
      h("p", { class: "field__hint", text: "这里调整的是模型如何听、何时断句，不会重新训练模型。该设置同时用于 A/B/C 三种模式。" }),
      field(tr("调优预设"), select(String(tuningDraft["asr_tuning_profile"] ?? "auto"), profiles, (v) => {
        tuningDraft["asr_tuning_profile"] = v;
        markDirty();
      })),
      numField("asr_vad_threshold", "语音灵敏度", 0, 1, 0.01, "越高越不容易把背景噪声当成说话"),
      numField("asr_silence_ms", "停顿多久断句", 100, 3000, 50, "说完后静音多久算一句话结束"),
      numField("asr_max_utterance_ms", "单句最长时长", 2000, 60000, 500, "超过这个时长会强制断句"),
      numField("asr_beam_paths", "识别候选数", 1, 12, 1, "越大越准，但更慢"),
      numField("asr_max_new_tokens", "单句最大文字量", 64, 4096, 64),
      field(tr("常用词 / 专有名词"), textInput(String(tuningDraft["asr_hotwords"] ?? ""), (v) => {
        tuningDraft["asr_hotwords"] = v;
        markDirty();
      }, { placeholder: "用逗号分隔" })),
    ]),
  );

  page.append(
    card("智能上下文", [
      numField("asr_context_hold_ms", "上下文最长等待", 0, 4000, 100, "句子可能没说完时，最多多等多久"),
      toggleSwitch(Boolean(tuningDraft["asr_live_draft_enabled"]), "实时双语草稿", (v) => {
        tuningDraft["asr_live_draft_enabled"] = v;
        markDirty();
      }),
      toggleSwitch(Boolean(tuningDraft["asr_context_correction"]), "上下文保守纠偏", (v) => {
        tuningDraft["asr_context_correction"] = v;
        markDirty();
      }),
      field("语气词清理", select(String(tuningDraft["asr_filler_mode"] ?? "light"), [
        ["off", "关闭（保留原话）"],
        ["light", "轻度（仅独立语气词）"],
      ], (v) => {
        tuningDraft["asr_filler_mode"] = v;
        markDirty();
      })),
    ]),
  );

  const actions = h("div", { class: "tuning-actions" });
  const stateEl = h("span", { class: "tuning-actions__state", text: tuningDirty ? "有未保存的更改" : "未修改" });
  const saveBtn = h("button", { class: "btn btn--primary", type: "button", text: tr("保存") });
  on(saveBtn, "click", async () => {
    await saveConfig(tuningDraft);
    await call(CMD.setAsrTuning, { tuning: tuningDraft });
    tuningDirty = false;
    stateEl.textContent = "已保存 · 下次开始时生效";
  });
  const resetBtn = h("button", { class: "btn btn--ghost", type: "button", text: "放弃更改" });
  on(resetBtn, "click", () => {
    void loadConfig().then(() => window.dispatchEvent(new Event("voxsub:settings")));
  });
  actions.append(stateEl, h("span", { class: "catalog__spacer" }), resetBtn, saveBtn);
  page.append(actions);

  return page;
}

function appearanceTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });
  const theme = String(config["theme"] ?? "system");

  page.append(
    card(tr("外观"), [
      field(tr("主题"), radioGroup<"light" | "dark" | "system">(
        theme as "light" | "dark" | "system",
        [["light", tr("浅色")], ["dark", tr("深色")], ["system", tr("跟随系统")]],
        (v) => {
          void saveConfig({ theme: v });
          applyThemeChoice(v);
        },
      )),
      field(tr("界面语言"), radioGroup<"zh" | "en">(
        currentLanguage() === "en" ? "en" : "zh",
        [["zh", tr("中文")], ["en", tr("英文")]],
        (v) => {
          void saveConfig({ language: v === "en" ? "en" : "system" });
          setLanguage(v);
        },
      )),
    ]),
  );
  return page;
}

function aboutTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });
  const state = store.get();

  page.append(
    card(tr("关于"), [
      field("版本", h("span", { class: "readonly-value", text: state.version || "—" })),
      field("前端", h("span", { class: "readonly-value", text: "Electron + TypeScript" })),
      field("后端", h("span", { class: "readonly-value", text: "Python (voxsub)" })),
      field("GitHub", h("a", {
        class: "link",
        href: "https://github.com/tuotuonuts/VoxSub",
        text: "github.com/tuotuonuts/VoxSub",
        target: "_blank",
        rel: "noreferrer",
      })),
    ]),
  );

  const outputDir = String(config["models_root"] ?? "");
  page.append(
    card(tr("模型存储位置"), [
      field("当前路径", h("span", { class: "readonly-value", text: outputDir || "使用默认位置" })),
      (() => {
        const btn = h("button", { class: "btn btn--ghost", type: "button", text: tr("更改位置") });
        on(btn, "click", async () => {
          const api = window.voxsub;
          if (!api) return;
          const picked = await api.dialog.pickDirectory();
          if (!picked) return;
          await saveConfig({ models_root: picked, models_root_mode: "custom" });
          window.dispatchEvent(new Event("voxsub:settings"));
        });
        return btn;
      })(),
    ]),
  );

  return page;
}

/** 主题选择：写 dataset 让 CSS 变量切换，同时通知主进程（浮窗跟随）。 */
function applyThemeChoice(choice: string): void {
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const resolved = choice === "system" ? (prefersDark ? "dark" : "light") : choice;
  document.documentElement.dataset["theme"] = resolved;
  store.patch({ theme: resolved as "dark" | "light" });
}

export function buildSettings(): HTMLElement {
  const shell = h("div", { class: "settings" });

  const tabs: ReadonlyArray<readonly [string, () => HTMLElement]> = [
    [tr("翻译"), translationTab],
    [tr("语音"), voiceTab],
    [tr("设备"), devicesTab],
    [tr("识别调优"), tuningTab],
    [tr("外观"), appearanceTab],
    [tr("关于"), aboutTab],
  ];

  const nav = h("nav", { class: "settings__nav", role: "tablist" });
  const panes = h("div", { class: "settings__panes" });

  let current = 0;
  const renderPane = (): void => {
    panes.replaceChildren(tabs[current]![1]());
  };

  tabs.forEach(([label], index) => {
    const btn = h("button", {
      class: index === current ? "settings__tab is-active" : "settings__tab",
      type: "button",
      role: "tab",
      text: label,
    });
    on(btn, "click", () => {
      current = index;
      nav.querySelectorAll(".settings__tab").forEach((n, i) => {
        n.classList.toggle("is-active", i === index);
      });
      renderPane();
    });
    nav.append(btn);
  });

  shell.append(nav, panes);
  renderPane();

  void loadDevices().then(() => {
    if (current === 2) renderPane();
  });

  window.addEventListener("voxsub:settings", () => {
    void loadConfig().then(renderPane);
  });

  return shell;
}

/** 设备枚举可能较慢，单独加载，不阻塞设置页渲染。 */
export async function loadDevices(): Promise<void> {
  const result = await call<{ microphones: AudioDevice[]; loopbacks: AudioDevice[] }>(
    CMD.listAudioDevices,
  );
  microphones = result?.microphones ?? [];
  loopbacks = result?.loopbacks ?? [];
}

export async function loadHardware(): Promise<HardwareProfile | null> {
  return call<HardwareProfile>(CMD.hardwareProfile);
}
