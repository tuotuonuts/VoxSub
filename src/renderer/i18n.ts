/**
 * i18n —— 中文词条 + 英文映射。
 *
 * 与 Qt 版一致的纪律（DESIGN.md「UI 语言契约」）：
 *   · 所有用户可见静态文案必须经 tr()
 *   · 字幕正文、设备真实名称、文件名、日志内容属于用户/系统数据，不得翻译
 */
import { store } from "./store";

type Dict = Record<string, string>;

const EN: Dict = {
  // 通用
  "语幕": "VoxSub",
  "让对话、会议和视频，落成清晰的双语文幕。":
    "Live bilingual captions for conversations, meetings, and video.",
  "开始": "Start",
  "暂停": "Pause",
  "继续": "Resume",
  "结束": "Stop",
  "清空": "Clear",
  "取消": "Cancel",
  "保存": "Save",
  "关闭": "Close",
  "下载": "Download",
  "卸载": "Uninstall",
  "使用中": "In use",
  "已下载": "Installed",
  "内置": "Built-in",
  "刷新": "Refresh",
  "返回": "Back",
  "导出": "Export",

  // 模式
  "模式": "Mode",
  "麦克风同传": "Microphone",
  "系统声音": "System audio",
  "音视频文件": "Media file",
  "屏幕 OCR": "Screen OCR",
  "边说边出双语字幕": "Live bilingual subtitles",
  "会议 / 网课 / 视频": "Meetings, classes, video",
  "导入并导出 SRT": "Import and export SRT",
  "框选后原位覆盖译文": "Select area, overlay translation",

  // 字幕
  "字幕": "Subtitles",
  "待机": "Idle",
  "开始后，原文与译文会并排出现在这里": "Source and translation appear here side by side once you start",
  "导出会话": "Export session",
  "同时录音": "Record audio",
  "录音中": "Recording",
  "尚未选择文件": "No file selected",
  "选择文件": "Choose file",
  "已选择文件": "File selected",

  // 语言
  "识别语言": "Recognition language",
  "翻译为": "Translate to",
  "自动识别": "Auto-detect",
  "中文": "Chinese",
  "英文": "English",
  "日文": "Japanese",
  "韩文": "Korean",

  // 导航
  "设置": "Settings",
  "模型目录": "Model catalog",
  "诊断": "Diagnostics",
  "外观": "Appearance",
  "翻译": "Translation",
  "语音": "Voice",
  "设备": "Devices",
  "识别调优": "Recognition tuning",
  "关于": "About",
  "存储与模型": "Storage & models",
  "更新日志": "What's new",

  // 设置项
  "本地识别": "Local recognition",
  "云端识别": "Cloud recognition",
  "本地翻译": "Local translation",
  "云端翻译": "Cloud translation",
  "翻译档位": "Translation tier",
  "快档": "Fast",
  "质量档": "Quality",
  "云端": "Cloud",
  "API 地址": "API endpoint",
  "API 密钥": "API key",
  "模型名": "Model name",
  "朗读译文": "Read translations aloud",
  "麦克风": "Microphone",
  "系统输出": "System output",
  "应用声音隔离": "App audio isolation",
  "主题": "Theme",
  "深色": "Dark",
  "浅色": "Light",
  "跟随系统": "Follow system",
  "界面语言": "Interface language",
  "模型存储位置": "Model storage location",
  "更改位置": "Change location",
  "打开目录": "Open folder",

  // 模型目录
  "全部": "All",
  "识别": "Speech",
  "朗读": "Speech output",
  "质量分": "Quality",
  "大小": "Size",
  "许可证": "License",
  "语言": "Languages",
  "硬件": "Hardware",
  "NPU 已验证": "NPU verified",
  "NPU 待验证": "NPU unverified",
  "不支持 NPU": "No NPU",
  "按任务筛选": "Filter by task",

  // 诊断
  "自检结果": "Self-check results",
  "重新检查": "Run again",
  "导出报告": "Export report",
  "实时日志": "Live logs",
  "状态": "Status",
  "检查项": "Check",
  "详情": "Detail",
  "硬件画像": "Hardware profile",
  "运行设备": "Runtime devices",

  // OCR
  "截图翻译": "Screenshot",
  "上传图片": "Upload image",
  "框选屏幕": "Select screen area",
  "识别原文": "Recognized source",
  "译文": "Translation",
  "导出译后图片": "Export translated image",
  "等待框选": "Waiting for selection",
  "正在识别…": "Recognizing...",

  // 主题词
  "深色科技": "Dark tech",
  "同人展目录": "Event catalog",
};

export function tr(zh: string): string {
  const state = store.get();
  if (state.theme && (state as { lang?: string }).lang === "en") {
    return EN[zh] ?? zh;
  }
  return zh;
}

/** 英文界面模式：由设置页切换。 */
export function setLanguage(lang: "zh" | "en"): void {
  document.documentElement.dataset["lang"] = lang;
  (store.get() as { lang?: string }).lang = lang;
  window.dispatchEvent(new Event("voxsub:lang"));
}

export function currentLanguage(): string {
  return document.documentElement.dataset["lang"] ?? "zh";
}
