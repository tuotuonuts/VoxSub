/**
 * 后端命令名与事件类型的单一来源。
 *
 * 收敛在这里的原因：IPC 是两端唯一的耦合面，命令名散落在 UI 各处时，
 * 后端改名不会有任何编译期错误。集中定义 + TypeScript 类型约束可以把
 * 这类错误变成编译失败。
 */

export const CMD = {
  ping: "ping",
  shutdown: "shutdown",

  // 会话
  start: "start",
  stop: "stop",
  pause: "pause",
  resume: "resume",
  state: "state",

  // 模式与语言
  setMode: "set_mode",
  setLangs: "set_langs",
  languageCapabilities: "language_capabilities",
  setInputFile: "set_input_file",

  // 音频
  listAudioDevices: "list_audio_devices",
  listCaptureTargets: "list_capture_targets",
  setAudioDevices: "set_audio_devices",
  setCaptureProcess: "set_capture_process",

  // STT / 翻译 / TTS
  setStt: "set_stt",
  setTranslator: "set_translator",
  setAsrModel: "set_asr_model",
  setAsrTuning: "set_asr_tuning",
  asrTuningMeta: "asr_tuning_meta",
  translateTiers: "translate_tiers",
  setTts: "set_tts",
  setTtsModels: "set_tts_models",
  setRecording: "set_recording",
  lastRecording: "last_recording",

  // 模型广场
  listModels: "list_models",
  clearLogs: "clear_logs",
  logPath: "log_path",
  importModels: "import_models",
  releaseNotes: "release_notes",
  recentLogs: "recent_logs",
  installModel: "install_model",
  uninstallModel: "uninstall_model",
  modelDir: "model_dir",

  // 配置
  getConfig: "get_config",
  setConfig: "set_config",

  // 诊断
  runSelfCheck: "run_self_check",
  exportDiagnostics: "export_diagnostics",
  listDevices: "list_devices",
  hardwareProfile: "hardware_profile",

  // 文件字幕
  exportSubtitles: "export_subtitles",

  // OCR
  ocrRecognize: "ocr_recognize",
  renderOcrImage: "render_ocr_image",
  copyFile: "copy_file",
  ocrCacheDir: "ocr_cache_dir",

  // 旧版迁移
  detectLegacy: "detect_legacy",
  planMigration: "plan_migration",
  startMigration: "start_migration",
  verifyCopy: "verify_copy",
  writeModelSnapshot: "write_model_snapshot",
  cleanupMigratedSource: "cleanup_migrated_source",
  migrationDecision: "migration_decision",
  ocrTranslate: "ocr_translate",

  // 后台任务（长任务的异步形态：args.async = true 立刻返回 jobId）
  jobList: "job_list",
  jobStatus: "job_status",
  cancelJob: "cancel_job",
} as const;

export type CommandName = (typeof CMD)[keyof typeof CMD];

/* ---------------------------------------------------------------- 数据类型 */

export interface ModelCatalogResult {
  models: ModelEntry[];
  modelsRoot: string;
  lookupRoots: string[];
  diagnostics: string[];
}

export interface ModelEntry {
  id: string;
  name: string;
  task: string;
  quality: number;
  sizeLabel: string;
  sizeBytes: number;
  installedBytes: number;
  installed: boolean;
  builtin: boolean;
  runtime: string;
  license: string;
  languages: string;
  description: string;
  gpuSupported: boolean;
  igpuSupported: boolean;
  npuSupported: boolean;
  minRamGb: number;
}

export interface AudioDevice {
  id: string;
  name: string;
  kind: "mic" | "loopback";
}

/**
 * 可捕获的可见窗口 —— B 模式「按应用隔离」的目标。
 *
 * pid 用来写入 capture_process_id，windowTitle 用来写入 capture_window_title
 * （两个都存，是因为进程可能重启导致 pid 变化，标题可以帮用户认出来选的是谁）。
 */
export interface CaptureTarget {
  pid: number;
  processName: string;
  windowTitle: string;
  label: string;
}

/**
 * 识别调优的元数据（后端 asr_tuning_meta 命令返回）。
 *
 * 三项都由后端提供，前端不硬编码 —— 它们取决于后端实际怎么读配置，
 * 前端各写一份就会出现"界面显示的值和实际跑的值不一致"。
 */
export interface AsrTuningMeta {
  /** 每个预设档位固定的基础参数值（键名带 asr_ 前缀）。 */
  presets: Record<string, Record<string, number>>;
  /** 受档位控制的键。**不在此列表里的键任何档位都可改**。 */
  controlled: string[];
  /** 每个档位下 controlled 里仍可改的子集；其余由预设定，界面应置灰。 */
  editable: Record<string, string[]>;
  /** 当前档位下**实际生效**的值（界面显示这个，而不是用户存的值）。 */
  effective: Record<string, unknown>;
}

/** 单个翻译档位的能力。 */
export interface TranslateTierInfo {
  /** 档位 id：fast | quality | cloud。 */
  id: string;
  /** 后端实际使用的翻译器 kind（质量档在特定配置下其实是 opus-fast）。 */
  kind: string;
  /** 该档位支持的源/目标语言代码。 */
  langs: string[];
  /** 能否翻译**当前**语言对。 */
  supportsPair: boolean;
}

/**
 * 翻译档位 × 当前语言对的能力。
 *
 * 由后端算出来而不是前端硬编码：快档（OPUS-MT）只有 zh↔en 的模型，
 * 界面却允许把语言选成日文/韩文。此前用户选了这个组合，每一句都失败、
 * 只看到原文和满屏报错 —— 界面必须在选择时就告诉他。
 */
export interface TranslateTierMeta {
  tiers: TranslateTierInfo[];
  source: string;
  target: string;
  /** 用户在设置里选的档位。 */
  selected: string;
  /** 实际会用哪个档位（不支持当前语言对时会被替换）。 */
  effective: string;
  /** 被替换掉的档位；None 表示用的就是用户所选。 */
  substitutedFrom: string | null;
}

export interface DeviceEntry {
  provider: string;
  name: string;
  kind: string;
  scoreMs: number | null;
}

export interface SelfCheckItem {
  check: string;
  status: "ok" | "warn" | "fail" | string;
  detail: string;
}

export interface LogEntry {
  /** Unmodified producer timestamp; never inferred for legacy records. */
  ts: string;
  eventTimeMs?: number | null;
  receivedAtMs?: number;
  receiveSequence?: number;
  source?: string;
  raw?: string;
  level: string;
  message: string;
}

export interface HardwareProfile {
  cpu: string;
  physicalCores: number;
  logicalCores: number;
  ramGb: number;
  gpu: string;
  vramGb: number;
  gpuProvider: string;
  npu: string;
}

export interface SubtitleLine {
  source: string;
  translation: string;
  startMs: number;
  endMs: number;
}

export interface OcrLine {
  text: string;
  /** [left, top, right, bottom]（后端已把 OcrBox 对象归一成扁平四元组） */
  box: number[];
  /** 逐行译文；未翻译时为空串 */
  translation: string;
}

export interface OcrResult {
  text: string;
  /** 整段译文（按行拼接，与 lines 顺序一致） */
  translation: string;
  lines: OcrLine[];
  /** 原图尺寸（用于预览缩放校验） */
  width?: number;
  height?: number;
  /** 识别源图路径：预览切到「原图」时用它 */
  sourcePath: string;
  ocrElapsedMs?: number;
  translateElapsedMs?: number;
}

/* ---------------------------------------------------------------- 事件类型 */

/** 后台任务的终态与进行中状态（后端 `job` 事件与 job_status 的 status 字段）。 */
/**
 * 任务状态。**词汇表必须与后端 `job_runner.py` 的状态机逐字一致** ——
 * 权威定义在那边。六个状态：`queued` / `running` / `cancelling` 进行中
 * （`cancelling` 不等于已取消），`succeeded` / `failed` / `cancelled` 终态。
 */
export type JobStatus = "queued" | "running" | "cancelling" | "succeeded" | "failed" | "cancelled";

/** 后端 `job` 事件：长任务的进度/终态（async 命令的结果走这条路送达）。 */
export interface JobEvent {
  type: "job";
  ts?: string;
  jobId: string;
  command: string;
  status: JobStatus;
  /** 事件顺序号；前端据此丢弃迟到/乱序事件。 */
  sequence?: number;
  /** 失败/取消原因（后端 `error` 字段）。 */
  error?: string;
  /** 可识别错误码（后端 `code` 字段），例如 `cancelled`。 */
  code?: string;
  /** 仅异步 start_migration 事件携带，用于受理回执丢失后的终态重关联。 */
  clientMigrationId?: string;
  /** 仅 start_migration 的异步成功终态携带。 */
  result?: unknown;
}

/** `cleanup_migrated_source` 的返回值（新增字段见后端契约）。 */
export interface CleanupResult {
  deleted?: boolean;
  code?: string;
  detail?: string;
  ok?: boolean;
  paths?: string[];
  cleaned?: string[];
  refused?: unknown[];
}

export type BackendEvent =
  | {
      type: "ready";
      version: string;
      /**
       * 后端新增的握手字段（协议版本 / 后端代号 / 就绪快照 / 当前会话）。
       * 全部可选：老后端不发这些字段，渲染层不能因为"多了字段"就报错。
       */
      protocolVersion?: number | string;
      backendGeneration?: number | string;
      frozen?: boolean;
      readiness?: { ready?: boolean; activeJobs?: string[] };
      session?: { running?: boolean; paused?: boolean; mode?: string; state?: string; configGeneration?: number; recordingEnabled?: boolean; recordingActive?: boolean; recordingSupported?: boolean; recordingCanChange?: boolean; recordingReason?: string };
    }
  | { type: "status"; text: string }
  /**
   * 后端进程退出。
   *
   * 独立事件而不是复用 status 文案（缺陷 #11）：断连是一个**状态**，
   * 界面必须把"运行中"收回去、把会话视图归零，只改提示文案是骗人的。
   */
  | { type: "disconnected"; reason?: string }
  /**
   * 请求超过时限未返回 —— **不是失败**（缺陷 #4）。
   *
   * 由主进程在长任务（迁移/模型下载/OCR）超过时限时发出；任务仍在进行，
   * 后续事件与最终结果照常送达。
   */
  | { type: "request-timeout"; command: string; hint?: string }
  /** 后台任务事件（async 命令的进度与终态）。 */
  | JobEvent
  | { type: "session"; action: "start" | "stop" }
  | { type: "state"; running: boolean; paused: boolean; mode: string; state: string; configGeneration?: number; recordingEnabled?: boolean; recordingActive?: boolean; recordingSupported?: boolean; recordingCanChange?: boolean; recordingReason?: string }
  | { type: "migration"; phase: string; key?: string; index?: number; total?: number; error?: string; target?: string }
  | { type: "utterance"; source: string; translation: string }
  | { type: "draft"; source: string; translation: string }
  | { type: "partial"; text: string }
  | { type: "progress"; completed: number; total: number; stage: string }
  | { type: "download"; modelId: string; completed: number; total: number; stage: string }
  | { type: "log"; ts: string; level: string; message: string }
  | { type: "error"; message: string };
