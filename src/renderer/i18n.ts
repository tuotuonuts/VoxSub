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
  "上传/截图原图与译后覆盖图分开保存，绝不写入 C 盘。默认每类保留最近 15 张；设为 0 表示无限保留。": "Original captures and translated overlays are stored separately and never written to the system drive. Keeps the latest 15 per category by default; 0 means unlimited.",
  "更改保存位置": "Change location",
  "更改缓存位置": "Change cache location",
  "打开缓存": "Open cache",
  "设为 0 表示无限保留。": "Set 0 to keep everything.",
  "每类保留": "Keep per category",
  "如果以前把模型放在其他磁盘或手动复制过模型，可从这里把它们并入当前位置。": "If your models live on another drive or were copied manually, adopt them into the current location here.",
  "迁移失败，详见日志": "Migration failed — see logs",
  "已并入 {n} 项，跳过 {m} 项": "Adopted {n}, skipped {m}",
  "正在扫描…": "Scanning…",
  "迁移已有模型": "Adopt existing models",
  "使用默认位置": "Using default location",
  "识别、翻译、语音模型会按用途整理在这里。更新软件不会清空这个文件夹。": "Speech, translation, and voice models are organised here by purpose. Updating the app will not clear this folder.",
  "模型保存在默认位置。可改到其它磁盘以避免占用系统盘。": "Models are stored in the default folder. Move them to another drive to save system disk space.",
  "模型保存在自定义位置。更新软件不会清空这个文件夹。": "Models are stored in a custom folder. Updating the app will not clear it.",
  "OCR 缓存": "OCR cache",
  "识别、翻译、语音模型按用途分组。下载后即在本机运行，不上传你的音频。": "Speech, translation, and voice models grouped by purpose. They run locally — your audio is never uploaded.",
  "展开历史更新日志（还有 {n} 版）": "Show older release notes ({n} more)",
  "收起历史更新日志": "Hide older release notes",
  "暂无更新日志": "No release notes",
  "日志文件为空或尚未生成": "Log file is empty or not created yet",
  "正在读取日志文件…": "Reading log file…",
  "将删除本机全部日志文件（不影响模型、配置与已导出的报告）。确定继续？": "Delete all local log files? Models, config, and exported reports are not affected.",
  "清除本机日志": "Clear local logs",
  "打开文件夹": "Open folder",
  "导出日志": "Export log",
  "导出失败": "Export failed",
  "已导出": "Exported",
  "个文件": "files",
  "已清除": "Cleared",
  "行": "lines",
  "文件": "File",
  "历史文件": "Log file",
  "实时": "Live",
  "全部安装需": "All models need",
  "已占": "Used",
  "模型": "Models",
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
