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
  ocrTranslate: "ocr_translate",
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
  ts: string;
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

export type BackendEvent =
  | { type: "ready"; version: string }
  | { type: "status"; text: string }
  | { type: "session"; action: "start" | "stop" }
  | { type: "utterance"; source: string; translation: string }
  | { type: "draft"; source: string; translation: string }
  | { type: "partial"; text: string }
  | { type: "progress"; completed: number; total: number; stage: string }
  | { type: "download"; modelId: string; completed: number; total: number; stage: string }
  | { type: "log"; ts: string; level: string; message: string }
  | { type: "error"; message: string };
