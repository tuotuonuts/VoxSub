/**
 * 设置页 —— 对应原 Qt 版 settings_window.py（1905 行，6 个分页）。
 *
 * 分页：翻译 / 语音 / 设备 / 识别调优 / 外观 / 关于
 * 纪律：调优参数使用显式保存（保存/放弃事务），其余项即时生效。
 */
import { h, on } from "../dom";
import { call, store } from "../store";
import { CMD, type AsrTuningMeta, type AudioDevice, type CaptureTarget, type HardwareProfile, type ModelEntry } from "../protocol";
import { tr, setLanguage, currentLanguage } from "../i18n";
import { reopenWizard } from "./migration";

type Config = Record<string, unknown>;

let config: Config = {};
let microphones: AudioDevice[] = [];
let loopbacks: AudioDevice[] = [];
/** 模型清单：设置页要按用途列出可选的本地模型（识别/翻译/朗读）。 */
let models: ModelEntry[] = [];
/** 可捕获的可见窗口（B 模式按应用隔离用）。 */
let captureTargets: CaptureTarget[] = [];
/** 配置是否已成功取回。用于决定设置页是否需要补一次重绘。 */
let configReady = false;
/** 调优草稿：只有点保存才写回，避免逐项写配置造成抖动。 */
let tuningDraft: Record<string, unknown> = {};
let tuningDirty = false;
/**
 * 识别调优的元数据（后端提供：预设值 / 各档可改键 / 当前生效值）。
 *
 * 拿不到时退化为"全部可改"，不阻断界面 —— 灰化是提示，不该让设置页打不开。
 */
let tuningMeta: AsrTuningMeta | null = null;

export async function loadConfig(): Promise<void> {
  const result = await call<Config>(CMD.getConfig);
  config = result ?? {};

  // 模型清单：识别/翻译分页要用它列出可选的本地模型。
  // 与配置一起取，保证下拉里的"已安装模型"和真实情况一致。
  const catalog = await call<{ models: ModelEntry[] }>(CMD.listModels, { models_root: null });
  models = catalog?.models ?? [];

  // 调优元数据：界面据此决定哪些项要置灰、以及该显示哪个值。
  // 失败时不抛错 —— 拿不到就退化为"全部可改"，灰化只是提示。
  try {
    tuningMeta = await call<AsrTuningMeta>(CMD.asrTuningMeta);
  } catch {
    tuningMeta = null;
  }

  // 更新日志与配置一起加载：设置页「关于」要用（后端默认只回最近一版）
  const notes = await call<{ notes: Array<{ version: string; title: string; body: string }> }>(
    CMD.releaseNotes,
    { include_history: true, language: currentLanguage() },
  );
  if (notes?.notes) {
    store.patch({ releaseNotes: notes.notes.map((n) => ({ version: n.version, body: n.body })) });
  }
  tuningDraft = {
    asr_tuning_profile: normalizeProfile(config["asr_tuning_profile"]),
    asr_vad_threshold: config["asr_vad_threshold"] ?? 0.32,
    asr_silence_ms: config["asr_silence_ms"] ?? 500,
    asr_max_utterance_ms: config["asr_max_utterance_ms"] ?? 18000,
    asr_beam_paths: config["asr_beam_paths"] ?? 6,
    asr_max_new_tokens: config["asr_max_new_tokens"] ?? 512,
    asr_hotwords: config["asr_hotwords"] ?? "",
    asr_context_hold_ms: config["asr_context_hold_ms"] ?? 1800,
    asr_live_draft_enabled: config["asr_live_draft_enabled"] ?? true,
    asr_context_correction: config["asr_context_correction"] ?? true,
    asr_filler_mode: config["asr_filler_mode"] ?? "light",
  };
  tuningDirty = false;
  configReady = true;
}

/**
 * 配置是否已就绪。设置页据此判断要不要补一次重绘：
 * 未就绪时首屏用的是兜底值，取回后必须重绘；
 * 已就绪时不再重绘 —— 那会把用户正在输入的内容清掉。
 */
export function isConfigReady(): boolean {
  return configReady;
}

/**
 * 档位归一化。
 *
 * 下拉只列五个档，但历史配置里可能存着 "auto"（旧默认）或前端此前写错的
 * "accurate"。这两种值在下拉里没有对应项，select 会显示成空白，
 * 用户会以为"配置丢了"。统一映射到语义最接近的档：
 *   auto     → context（都是"自动帮你挑"的语义，且 context 是当前默认）
 *   accurate → accuracy（后端只认 accuracy）
 */
function normalizeProfile(raw: unknown): string {
  const value = String(raw ?? "").trim();
  if (value === "" || value === "auto") return "context";
  if (value === "accurate") return "accuracy";
  return value;
}

async function saveConfig(updates: Config): Promise<void> {
  const merged = await call<Config>(CMD.setConfig, { updates });
  if (merged) config = merged;
}

/* ------------------------------------------------------------ 通用控件 */

/**
 * 字段容器。
 *
 * `disabledBy` 非空时表示"这一项当前不可改"，并给出被谁接管 ——
 * 只把控件置灰而不说原因，用户会以为界面坏了；说清"由预设决定"才可理解。
 */
function field(label: string, control: HTMLElement, hint?: string, disabledBy?: string): HTMLElement {
  const wrap = h("div", { class: disabledBy ? "field is-locked" : "field" });
  wrap.append(h("label", { class: "field__label", text: label }));
  wrap.append(control);
  const notes: string[] = [];
  if (disabledBy) notes.push(disabledBy);
  if (hint) notes.push(hint);
  for (const note of notes) {
    wrap.append(h("p", { class: "field__hint", text: note }));
  }
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

/** 按用途取**已安装**的本地模型（设置页下拉只列能真正跑起来的）。 */
function installedLocalModels(task: string): ModelEntry[] {
  return models.filter((m) => m.task === task && m.installed);
}

/** 本地模型下拉：没有可选模型时给出明确的下一步，而不是留一个空框。 */
function localModelField(
  label: string,
  task: string,
  currentId: string,
  onPick: (id: string) => void,
  emptyHint: string,
): HTMLElement {
  const available = installedLocalModels(task);
  if (available.length === 0) {
    return h("p", { class: "field__hint field__hint--warn", text: emptyHint });
  }
  const value = available.some((m) => m.id === currentId) ? currentId : available[0]!.id;
  return field(
    label,
    select<string>(
      value,
      available.map((m) => [m.id, m.name] as const),
      onPick,
    ),
    tr("只列出已下载到本机的模型"),
  );
}

function translationTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });

  const sttProvider = String(config["stt_provider"] ?? "local");
  const tier = String(config["translate_tier"] ?? "fast");

  // 本地与云端是两套完全不同的配置：本地要挑模型文件，云端要填地址和密钥。
  // 之前两组字段始终堆在一起 —— 选"本地识别"却只看到 API 密钥输入框，
  // 既找不到本地模型，也分不清哪些项真的生效。现在只显示所选来源那一组。
  const sttChildren: Array<HTMLElement | null> = [
    radioGroup<"local" | "cloud">(
      sttProvider === "cloud" ? "cloud" : "local",
      [["local", tr("本地识别")], ["cloud", tr("云端识别")]],
      (v) => {
        void saveConfig({ stt_provider: v }).then(() => {
          window.dispatchEvent(new Event("voxsub:settings"));
        });
      },
    ),
  ];

  if (sttProvider === "cloud") {
    sttChildren.push(
      field(tr("模型名"), textInput(String(config["stt_model"] ?? ""), (v) => void saveConfig({ stt_model: v })), tr("云端识别使用的模型")),
      field(tr("API 地址"), textInput(String(config["stt_base_url"] ?? ""), (v) => void saveConfig({ stt_base_url: v }))),
      field(tr("API 密钥"), textInput(String(config["stt_api_key"] ?? ""), (v) => void saveConfig({ stt_api_key: v }), { type: "password" })),
    );
  } else {
    sttChildren.push(
      localModelField(
        tr("识别模型"),
        "asr",
        String(config["asr_model_id"] ?? ""),
        (v) => {
          void saveConfig({ asr_model_id: v }).then(() => call(CMD.setAsrModel, { model_id: v }));
        },
        tr("还没有下载本地识别模型。请到「模型」页下载后回到这里选择。"),
      ),
    );
  }

  page.append(card(tr("语音识别"), sttChildren));

  const tierChildren: Array<HTMLElement | null> = [
    radioGroup<"fast" | "quality" | "cloud">(
      tier as "fast" | "quality" | "cloud",
      [["fast", tr("快档")], ["quality", tr("质量档")], ["cloud", tr("云端")]],
      (v) => {
        void saveConfig({ translate_tier: v }).then(() => {
          const kind = v === "fast" ? "opus-fast" : v === "quality" ? "qwen-quality" : "cloud";
          void call(CMD.setTranslator, { kind, config: {} });
          window.dispatchEvent(new Event("voxsub:settings"));
        });
      },
    ),
  ];

  if (tier === "cloud") {
    tierChildren.push(
      field(tr("模型名"), textInput(String(config["translate_model"] ?? ""), (v) => void saveConfig({ translate_model: v }))),
      field(tr("API 地址"), textInput(String(config["translate_base_url"] ?? ""), (v) => void saveConfig({ translate_base_url: v }))),
      field(tr("API 密钥"), textInput(String(config["translate_api_key"] ?? ""), (v) => void saveConfig({ translate_api_key: v }), { type: "password" })),
    );
  } else {
    tierChildren.push(
      localModelField(
        tr("翻译模型"),
        "translate",
        String(config["translate_model_id"] ?? ""),
        (v) => void saveConfig({ translate_model_id: v }),
        tr("还没有下载本地翻译模型。请到「模型」页下载后回到这里选择。"),
      ),
    );
  }

  page.append(card(tr("翻译档位"), tierChildren));

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

  // 按应用隔离：这里必须能**挑**应用，只显示一行"未选择应用"等于没有这个功能。
  // 列表来自后端 list_capture_targets（枚举当前有可见窗口的进程）。
  const currentPid = String(config["capture_process_id"] ?? "");
  const targetOptions: Array<readonly [string, string]> = [["", tr("不隔离（捕获全部系统声音）")]];
  for (const target of captureTargets) {
    targetOptions.push([String(target.pid), target.label || `${target.processName} — ${target.windowTitle}`]);
  }

  const captureField =
    captureTargets.length === 0
      ? field(
          tr("应用声音隔离"),
          h("span", { class: "readonly-value", text: tr("没有检测到正在发声的应用窗口") }),
          tr("先打开要捕获声音的应用，再回到这里刷新"),
        )
      : field(
          tr("应用声音隔离"),
          select<string>(currentPid, targetOptions, (v) => {
            const picked = captureTargets.find((t) => String(t.pid) === v);
            const title = picked ? picked.windowTitle : "";
            void saveConfig({
              capture_process_id: v,
              capture_window_title: title,
            }).then(() => call(CMD.setCaptureProcess, { pid: Number(v) || 0, title }));
          }),
          tr("只在 B 模式下生效：选择后只捕获该应用的声音，其它声音不会被识别"),
        );

  const refreshBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: tr("刷新应用列表") });
  on(refreshBtn, "click", () => {
    void loadCaptureTargets().then(() => window.dispatchEvent(new Event("voxsub:settings")));
  });

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
      captureField,
      h("div", { class: "tuning-actions" }, [h("span", { class: "catalog__spacer" }), refreshBtn]),
    ]),
  );
  return page;
}

/**
 * 识别调优分页。
 *
 * ## 为什么有些项要置灰
 *
 * 后端只有部分参数在任何档位下都生效。逐条核对过后端调用点：
 *
 *   · vad_threshold / silence_ms / max_utterance_ms / beam_paths
 *     在预设档下由 ASR_TUNING_PRESETS 覆盖，**只有"自定义"档才读用户值** ——
 *     选了"智能上下文"还去调"语音灵敏度"，改了等于没改。
 *   · context_hold_ms / context_correction / live_draft_enabled / filler_mode
 *     只作用于 ContextualTextProcessor，而它**仅在"智能上下文"档创建**。
 *   · hotwords / max_new_tokens 有非上下文的生效路径，任何时候都可改。
 *
 * 所以本分页按当前档位置灰不可改的项。规则与预设值都来自后端元数据
 * （asr_tuning_meta 命令），前端不硬编码 —— 否则改了一处忘另一处，又会出现
 * "界面显示的值和实际跑的值对不上"。
 *
 * 置灰项显示的是**实际生效值**（预设值），而不是用户存的值：显示一个不生效
 * 的数字比置灰更糟。用户存的值仍保留在草稿里，切回"自定义"就会回来。
 */
function tuningTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });

  // 档位就是这五个。不提供"自动"：它是一个隐藏的自适应档，用户选了
  // 也不知道实际跑什么参数，反而让"我调过优了"变成错觉。
  //
  // 键名必须与后端 APP_CONFIG_SCHEMA.choices 一致 —— 是 accuracy 不是
  // accurate。后端对非法值**不报错**，而是静默回退到默认值（实测
  // normalize("accurate") == "context"），所以拼错的表现是"选了准确优先，
  // 下次打开又变回智能上下文"，从界面上完全看不出原因。
  // tests/test_config_defaults.py 守住了这一点。
  const profiles: ReadonlyArray<readonly [string, string]> = [
    ["responsive", tr("快档")],
    ["balanced", "均衡"],
    ["accuracy", "准确优先"],
    ["context", "智能上下文"],
    ["custom", "自定义"],
  ];

  // 切换档位会改变"哪些项可改"与"置灰项显示什么值"，整体重建最不容易漏。
  // 草稿在模块级变量里，重建不会丢未保存的编辑。
  const rebuild = (): void => {
    page.replaceChildren(...buildTuningContent(page, profiles, rebuild));
  };
  rebuild();
  return page;
}

/** 识别调优分页的全部内容。抽出来是为了档位切换时能整体重建。 */
function buildTuningContent(
  page: HTMLElement,
  profiles: ReadonlyArray<readonly [string, string]>,
  rebuild: () => void,
): HTMLElement[] {
  const profile = normalizeProfile(tuningDraft["asr_tuning_profile"]);
  const editable = new Set(tuningMeta?.editable?.[profile] ?? []);
  const controlled = new Set(tuningMeta?.controlled ?? []);

  /**
   * 该键在当前档位下是否可改。
   *
   * 两层判断，缺一不可：
   *   · 不受档位控制的键（hotwords、max_new_tokens）任何档位都可改；
   *   · 受控制的键只在当前档位的 editable 列表里才可改。
   *
   * 元数据拿不到时一律视为可改 —— 置灰只是提示，不该把界面锁死。
   */
  const canEdit = (key: string): boolean => {
    if (!tuningMeta) return true;
    if (!controlled.has(key)) return true;
    return editable.has(key);
  };

  /** 该键显示的值。被预设管理的项显示**实际生效值**，其余显示草稿值。 */
  const displayValue = (key: string): unknown => {
    if (!canEdit(key)) {
      const preset = tuningMeta?.presets?.[profile]?.[key];
      if (preset !== undefined) return preset;
    }
    return tuningDraft[key];
  };

  /** 置灰原因。按"这个键在哪些档位可改"生成，避免一句笼统的"不可用"。 */
  const lockedReason = (key: string): string => {
    const where = Object.entries(tuningMeta?.editable ?? {})
      .filter(([, keys]) => keys.includes(key))
      .map(([name]) => name);
    if (where.length === 1 && where[0] === "context") {
      return tr("仅「智能上下文」档可用");
    }
    if (where.length === 1 && where[0] === "custom") {
      return tr("由当前预设决定；切到「自定义」可自行调整");
    }
    return tr("当前档位下不可调整");
  };

  const markDirty = (): void => {
    tuningDirty = true;
    const bar = page.querySelector<HTMLElement>(".tuning-actions__state");
    if (bar) bar.textContent = "有未保存的更改";
  };

  const numField = (key: string, label: string, min: number, max: number, step: number, hint?: string): HTMLElement => {
    const locked = !canEdit(key);
    const input = h("input", {
      class: "input input--num",
      type: "number",
      value: String(displayValue(key) ?? ""),
      min: String(min),
      max: String(max),
      step: String(step),
    });
    if (locked) input.disabled = true;
    on(input, "change", () => {
      tuningDraft[key] = Number(input.value);
      markDirty();
    });
    return field(label, input, hint, locked ? lockedReason(key) : undefined);
  };

  const boolField = (key: string, label: string): HTMLElement => {
    const locked = !canEdit(key);
    const wrap = h("label", { class: "switch" });
    const input = h("input", { type: "checkbox" });
    input.checked = Boolean(displayValue(key));
    if (locked) input.disabled = true;
    on(input, "change", () => {
      tuningDraft[key] = input.checked;
      markDirty();
    });
    wrap.append(input, h("span", { class: "switch__track" }), h("span", { class: "switch__label", text: label }));
    if (!locked) return wrap;
    // 置灰时包一层容器放原因。不复用 field()：开关自己已经带标签，
    // 再套一层会出现两个标题。
    const box = h("div", { class: "field is-locked" });
    box.append(wrap, h("p", { class: "field__hint", text: lockedReason(key) }));
    return box;
  };

  const selectField = (
    key: string,
    label: string,
    options: ReadonlyArray<readonly [string, string]>,
  ): HTMLElement => {
    const locked = !canEdit(key);
    const sel = select(String(displayValue(key) ?? ""), options, (v) => {
      tuningDraft[key] = v;
      markDirty();
    });
    if (locked) sel.disabled = true;
    return field(label, sel, undefined, locked ? lockedReason(key) : undefined);
  };

  const tuningCard = card(tr("识别调优"), [
    h("p", { class: "field__hint", text: "这里调整的是模型如何听、何时断句，不会重新训练模型。该设置同时用于 A/B/C 三种模式。" }),
    field(tr("调优预设"), select(profile, profiles, (v) => {
      tuningDraft["asr_tuning_profile"] = v;
      markDirty();
      // 换档位会改变哪些项可改，立即重建界面（草稿保留，不丢未保存的编辑）
      rebuild();
    })),
    numField("asr_vad_threshold", "语音灵敏度", 0, 1, 0.01, "越高越不容易把背景噪声当成说话"),
    numField("asr_silence_ms", "停顿多久断句", 100, 3000, 50, "说完后静音多久算一句话结束"),
    numField("asr_max_utterance_ms", "单句最长时长", 2000, 60000, 500, "超过这个时长会强制断句"),
    numField("asr_beam_paths", "识别候选数", 1, 12, 1, "越大越准，但更慢"),
    numField("asr_max_new_tokens", "单句最大文字量", 64, 4096, 64),
    field(tr("常用词 / 专有名词"), textInput(String(displayValue("asr_hotwords") ?? ""), (v) => {
      tuningDraft["asr_hotwords"] = v;
      markDirty();
    }, { placeholder: "用逗号分隔" })),
  ]);

  const contextCard = card("智能上下文", [
    h("p", { class: "field__hint", text: "下面几项只在「智能上下文」档生效；其他档位下它们不会参与识别。" }),
    numField("asr_context_hold_ms", "上下文最长等待", 0, 4000, 100, "句子可能没说完时，最多多等多久"),
    boolField("asr_live_draft_enabled", "实时双语草稿"),
    boolField("asr_context_correction", "上下文保守纠偏"),
    selectField("asr_filler_mode", "语气词清理", [
      ["off", "关闭（保留原话）"],
      ["light", "轻度（仅独立语气词）"],
    ]),
  ]);

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

  return [tuningCard, contextCard, actions];
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

  // 这里只放用户关心的事实（版本、来源）。
  // 不写"前端 Electron / 后端 Python"这类实现细节：对使用者没有意义，
  // 而且换技术栈就会过期，属于会腐烂的信息。
  page.append(
    card(tr("关于"), [
      field("版本", h("span", { class: "readonly-value", text: state.version || "—" })),
      field("GitHub", h("a", {
        class: "link",
        href: "https://github.com/tuotuonuts/VoxSub",
        text: "github.com/tuotuonuts/VoxSub",
        target: "_blank",
        rel: "noreferrer",
      })),
    ]),
  );

  // 更新日志：默认只显示最近一版，可展开历史（与原 Qt 版一致）
  page.append(buildReleaseNotes());

  return page;
}

/**
 * 「存储与模型」分页 —— 对应原 Qt settings_window.py 的同名分页。
 *
 * 三块内容：模型目录 / OCR 缓存（位置 + 每类保留张数）/ 迁移已有模型。
 */
function storageTab(): HTMLElement {
  const page = h("div", { class: "tab-page" });

  const modelsRoot = String(config["models_root"] ?? "");
  const modelsMode = String(config["models_root_mode"] ?? "default");

  const pathValue = h("span", {
    class: "readonly-value",
    text: modelsRoot || "使用默认位置",
  });
  const modeNote = h("p", {
    class: "field__hint",
    text:
      modelsMode === "custom"
        ? tr("模型保存在自定义位置。更新软件不会清空这个文件夹。")
        : tr("模型保存在默认位置。可改到其它磁盘以避免占用系统盘。"),
  });

  const openFolder = h("button", { class: "btn btn--ghost", type: "button", text: tr("打开文件夹") });
  on(openFolder, "click", () => {
    const target = modelsRoot || store.get().modelsRoot || "";
    // 传原始路径给主进程，不要再拼 file:/// —— 主进程的 open-external 只放行
    // http/https，file:// 会被直接拒掉，表现就是"点了没反应"。
    if (target) void window.voxsub?.dialog.openPath(target);
  });

  const changeFolder = h("button", {
    class: "btn btn--ghost",
    type: "button",
    text: tr("更改保存位置"),
  });
  on(changeFolder, "click", async () => {
    const api = window.voxsub;
    if (!api) return;
    const picked = await api.dialog.pickDirectory();
    if (!picked) return;
    await saveConfig({ models_root: picked, models_root_mode: "custom" });
    window.dispatchEvent(new Event("voxsub:settings"));
  });

  page.append(
    card(tr("模型目录"), [
      h("p", {
        class: "field__hint",
        text: tr("识别、翻译、语音模型会按用途整理在这里。更新软件不会清空这个文件夹。"),
      }),
      field(tr("当前位置"), pathValue, modeNote.textContent ?? undefined),
      h("div", { class: "tuning-actions" }, [openFolder, changeFolder]),
    ]),
  );

  // ---- OCR 缓存 ----
  const cacheRoot = String(config["ocr_cache_root"] ?? "");
  const cacheLimit = Number(config["ocr_cache_limit"] ?? 15);

  const cachePathValue = h("span", {
    class: "readonly-value",
    text: cacheRoot || tr("使用默认位置"),
  });

  const limitInput = h("input", {
    class: "input",
    type: "number",
    min: "0",
    max: "999",
    value: String(cacheLimit),
    style: "max-width: 120px",
  }) as HTMLInputElement;
  on(limitInput, "change", () => {
    const value = Math.max(0, Math.min(999, Math.round(Number(limitInput.value) || 0)));
    limitInput.value = String(value);
    void saveConfig({ ocr_cache_limit: value });
  });

  const openCache = h("button", { class: "btn btn--ghost", type: "button", text: tr("打开缓存") });
  on(openCache, "click", () => {
    // 同「打开文件夹」：必须走 openPath，openExternal 只认 http/https
    if (cacheRoot) void window.voxsub?.dialog.openPath(cacheRoot);
  });

  const changeCache = h("button", {
    class: "btn btn--ghost",
    type: "button",
    text: tr("更改缓存位置"),
  });
  on(changeCache, "click", async () => {
    const api = window.voxsub;
    if (!api) return;
    const picked = await api.dialog.pickDirectory();
    if (!picked) return;
    await saveConfig({ ocr_cache_root: picked });
    window.dispatchEvent(new Event("voxsub:settings"));
  });

  page.append(
    card(tr("OCR 缓存"), [
      h("p", {
        class: "field__hint",
        text: tr("上传/截图原图与译后覆盖图分开保存，绝不写入 C 盘。默认每类保留最近 15 张；设为 0 表示无限保留。"),
      }),
      field(tr("当前位置"), cachePathValue),
      field(tr("每类保留"), limitInput, tr("设为 0 表示无限保留。")),
      h("div", { class: "tuning-actions" }, [openCache, changeCache]),
    ]),
  );

  // ---- 迁移已有模型 ----
  const importBtn = h("button", {
    class: "btn btn--ghost",
    type: "button",
    text: tr("迁移已有模型"),
  });
  const importState = h("span", { class: "tuning-actions__state", text: "" });
  on(importBtn, "click", async () => {
    const api = window.voxsub;
    if (!api) return;
    const picked = await api.dialog.pickDirectory();
    if (!picked) return;
    importState.textContent = tr("正在扫描…");
    // 迁移是多 GB 的文件搬动，期间必须阻止退出（否则留下半个模型库）
    void window.voxsub?.app.setBusy(true, tr("模型仍在后台迁移，请等待完成后再退出应用。"));
    const result = await call<{ moved: number; skipped: number }>(CMD.importModels, {
      source: picked,
    });
    void window.voxsub?.app.setBusy(false);
    importState.textContent = result
      ? tr("已并入 {n} 项，跳过 {m} 项").replace("{n}", String(result.moved)).replace("{m}", String(result.skipped))
      : tr("迁移失败，详见日志");
  });

  page.append(
    card(tr("迁移已有模型"), [
      h("p", {
        class: "field__hint",
        text: tr("如果以前把模型放在其他磁盘或手动复制过模型，可从这里把它们并入当前位置。"),
      }),
      h("div", { class: "tuning-actions" }, [importBtn, importState]),
    ]),
  );

  // ---- 旧版数据检查 ----
  // 用户跳过首次向导后反悔的入口。放在这里而不是"关于"：它与数据位置同类。
  const legacyState = h("span", { class: "tuning-actions__state", text: "" });
  const legacyBtn = h("button", {
    class: "btn btn--ghost",
    type: "button",
    text: tr("检查旧版数据"),
  });
  on(legacyBtn, "click", async () => {
    legacyState.textContent = tr("正在检查…");
    const result = await call<{
      legacy: { found: boolean; version: string; install_location: string };
      overallRisk: string;
      storage: Array<{ key: string; risk: string; path: string; bytes: number }>;
    }>(CMD.detectLegacy);
    if (!result) {
      legacyState.textContent = tr("检查失败，详见日志");
      return;
    }
    if (!result.legacy.found) {
      legacyState.textContent = tr("未检测到旧版");
      return;
    }
    const risky = result.storage.filter((s) => s.risk !== "safe");
    legacyState.textContent = risky.length
      ? `${tr("旧版")} ${result.legacy.version} · ${risky.length} ${tr("项需注意")}`
      : `${tr("旧版")} ${result.legacy.version} · ${tr("数据位置安全")}`;
  });

  const legacyOpen = h("button", {
    class: "btn btn--ghost",
    type: "button",
    text: tr("打开迁移向导"),
  });
  on(legacyOpen, "click", () => {
    const host = document.querySelector<HTMLElement>(".page-layer");
    if (host) void reopenWizard(host);
  });

  page.append(
    card(tr("旧版数据"), [
      h("p", {
        class: "field__hint",
        text: tr("检查旧版（Qt 版）留下的数据位置，必要时迁移到独立目录。卸载旧版前建议先做这一步。"),
      }),
      h("div", { class: "tuning-actions" }, [legacyBtn, legacyOpen, legacyState]),
    ]),
  );

  return page;
}

/** 更新日志卡片：默认折叠到最近一版（原 Qt 版行为一致）。 */
function buildReleaseNotes(): HTMLElement {
  const notes = store.get().releaseNotes;
  const card = h("div", { class: "card" });
  card.append(h("h3", { class: "card__title", text: tr("更新日志") }));

  const body = h("div", { class: "card__body" });

  if (!notes || notes.length === 0) {
    body.append(h("p", { class: "field__hint", text: tr("暂无更新日志") }));
    card.append(body);
    return card;
  }

  const latest = notes[0];
  const render = (item: (typeof notes)[number]): HTMLElement => {
    const block = h("div", { class: "release-item" });
    block.append(
      h("p", { class: "release-item__head" }, [
        h("strong", { text: item.version }),
        h("span", { class: "release-item__date", text: item.date ?? "" }),
      ]),
      h("p", { class: "release-item__body", text: item.body }),
    );
    return block;
  };

  if (latest) body.append(render(latest));

  const older = notes.slice(1);
  if (older.length > 0) {
    const history = h("div", { class: "release-history", hidden: true });
    older.forEach((item) => history.append(render(item)));

    const toggle = h("button", {
      class: "btn btn--ghost btn--sm",
      type: "button",
      text: tr("展开历史更新日志（还有 {n} 版）").replace("{n}", String(older.length)),
    });
    let expanded = false;
    on(toggle, "click", () => {
      expanded = !expanded;
      history.hidden = !expanded;
      toggle.textContent = expanded
        ? tr("收起历史更新日志")
        : tr("展开历史更新日志（还有 {n} 版）").replace("{n}", String(older.length));
    });

    body.append(toggle, history);
  }

  card.append(body);
  return card;
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
    [tr("存储与模型"), storageTab],
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

  // 配置与设备列表可能在 boot 之后才到齐，这里补一次刷新。
  // 只在配置**尚未就绪**时重绘：已就绪还重绘会把用户正在输入的内容清掉。
  const needsConfigRefresh = !isConfigReady();
  void Promise.all([loadDevices(), loadCaptureTargets(), needsConfigRefresh ? loadConfig() : null]).then(
    () => {
      if (needsConfigRefresh || current === 2) renderPane();
    },
  );

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

/**
 * 枚举可捕获的应用窗口（B 模式按应用隔离的目标）。
 *
 * 单独加载且不阻塞渲染：枚举要遍历所有顶层窗口，慢的时候要几百毫秒。
 * 失败时不抛错 —— 拿不到列表只是"这一项不可选"，不该让整个设置页挂掉。
 */
export async function loadCaptureTargets(): Promise<void> {
  try {
    const result = await call<{ targets: CaptureTarget[] }>(CMD.listCaptureTargets);
    captureTargets = result?.targets ?? [];
  } catch {
    captureTargets = [];
  }
}

export async function loadHardware(): Promise<HardwareProfile | null> {
  return call<HardwareProfile>(CMD.hardwareProfile);
}
