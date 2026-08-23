"""Application UI localization for Simplified Chinese and English.

The app intentionally keeps only two product languages for now.  ``system``
is the persisted default and resolves from the Windows/Qt UI locale.  Static
widgets are refreshed from one table so a language change updates every open
window instead of requiring a restart.
"""
from __future__ import annotations

from PySide6.QtCore import QLocale, QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMenu,
    QTabWidget,
    QWidget,
)

LANGUAGE_SYSTEM = "system"
LANGUAGE_ZH = "zh"
LANGUAGE_EN = "en"
LANGUAGE_VALUES = (LANGUAGE_SYSTEM, LANGUAGE_ZH, LANGUAGE_EN)


# Chinese remains the source text used by the existing product copy.  Keep
# this table centralized so every later UI change has one obvious translation
# home and can be reviewed without hunting through unrelated widgets.
TRANSLATIONS: dict[str, str] = {
    "语幕": "VoxSub",
    "语幕 VoxSub": "VoxSub",
    "设置": "Settings",
    "存储与模型": "Storage & models",
    "设置 — 语幕 VoxSub": "Settings - VoxSub",
    "显示主窗": "Show main window",
    "诊断与实时日志": "Diagnostics & live logs",
    "退出": "Exit",
    "诊断与日志": "Diagnostics & Logs",
    "诊断 — 语幕 VoxSub": "Diagnostics - VoxSub",
    "模型广场": "Model Hub",
    "打开浮窗": "Open overlay",
    "打开字幕浮窗": "Open the subtitle overlay",
    "模型广场 · 语幕 VoxSub": "Model Hub - VoxSub",
    "返回": "Back",
    "VOXSUB  /  PREFERENCES": "VOXSUB  /  PREFERENCES",
    "VOXSUB  /  HEALTH & LOGS": "VOXSUB  /  HEALTH & LOGS",
    "VOXSUB  /  CURATED LOCAL MODELS": "VOXSUB  /  CURATED LOCAL MODELS",
    "VOXSUB  /  LIVE TRANSLATION": "VOXSUB  /  LIVE TRANSLATION",
    "让对话、会议和视频，落成清晰的双语文幕。":
        "Turn conversations, meetings, and videos into clear bilingual subtitles.",
    "把识别、设备和字幕显示调整成适合你的工作方式。":
        "Tune recognition, devices, and subtitle display for the way you work.",
    "查看设备、模型和运行时状态；需要排障时直接打开实时日志。":
        "Inspect devices, models, and runtime health; open live logs when troubleshooting.",
    "模式": "Mode",
    "语言对": "Language pair",
    "中 → 英": "Chinese -> English",
    "英 → 中": "English -> Chinese",
    "设备可在「设置」中选择": "Choose the input device in Settings",
    "输入：设置中选择的麦克风": "Input: microphone selected in Settings",
    "输入：指定应用，或所选系统输出设备":
        "Input: selected app or system output device",
    "支持 MP4 / MKV / MOV / MP3 / WAV 等常见格式":
        "Supports common formats including MP4 / MKV / MOV / MP3 / WAV",
    "实时字幕": "Live subtitles",
    "文件字幕": "File subtitles",
    "原文与译文只保留在本次会话内": "Source and translation stay in this session only",
    "其它应用声音可通过进程隔离排除":
        "Other app audio can be excluded with process isolation",
    "自动提取音频 · 识别翻译 · 导出 SRT":
        "Extract audio · recognize · translate · export SRT",
    "会话": "Session",
    "字幕只保留在当前窗口，可随时导出":
        "Subtitles stay in this window and can be exported at any time",
    "保存": "Save",
    "清空": "Clear",
    "保存当前对话": "Save the current session",
    "导入音频或视频": "Import audio or video",
    "尚未选择文件 · 将自动提取音频并导出同名 SRT":
        "No file selected · audio will be extracted and a matching SRT exported",
    "选择文件": "Choose file",
    "选择文件后，处理结果与导出位置将显示在这里":
        "Processing results and the export path will appear here after you choose a file",
    "同时录音": "Record simultaneously",
    "像手机录音：开始 → 暂停 / 继续 → 结束并保存":
        "Like a phone recorder: Start -> Pause / Resume -> Finish and Save",
    "仅生成字幕，不保存麦克风音频":
        "Create subtitles only; do not save microphone audio",
    "翻译麦克风声音的同时保存本地 WAV；暂停期间不会写入录音":
        "Save a local WAV while translating microphone audio; paused time is not recorded",
    "选择麦克风，说完一句即生成双语字幕":
        "Choose a microphone; each completed utterance becomes bilingual subtitles",
    "麦克风同传": "Microphone interpreting",
    "应用 / 系统声音": "App / system audio",
    "隔离指定应用，或监听所选输出设备":
        "Isolate a selected app or listen to the chosen output device",
    "音视频字幕": "Audio/video subtitles",
    "导入音频或视频，自动提音并导出 SRT":
        "Import audio or video, extract audio automatically, and export SRT",
    "开始": "Start",
    "停止": "Stop",
    "暂停": "Pause",
    "继续": "Resume",
    "正在启动…": "Starting...",
    "启动中…正在加载识别与翻译模型": "Starting... loading recognition and translation models",
    "正在结束…": "Stopping...",
    "正在结束…正在整理剩余音频与字幕": "Stopping... finishing remaining audio and subtitles",
    "结束并保存": "Finish and Save",
    "正在录音并翻译 · 点击“暂停”可暂时停下":
        "Recording and translating · click Pause to temporarily stop",
    "已暂停 · 点击“继续”恢复，或结束并保存":
        "Paused · click Resume, or finish and save",
    "识别中…": "Recognizing...",
    "待机": "Idle",
    "拾音中": "Listening",
    "推理中": "Processing",
    "完成": "Done",
    "已停止": "Stopped",
    "处理中": "Processing",
    "翻译中": "Translating",
    "启动失败": "Start failed",
    "音频设备错误": "Audio device error",
    "识别处理错误": "Recognition error",
    "文件处理失败": "File processing failed",
    "文件不存在": "File not found",
    "正在启动或结束任务，请稍候": "A task is starting or stopping; please wait",
    "请先停止当前任务，再切换模式": "Stop the current task before changing modes",
    "正在结束并整理剩余字幕，完成后再保存":
        "Finishing remaining subtitles; save will be available when done",
    "当前没有可保存的字幕": "There are no subtitles to save",
    "当前对话已清空": "The current session was cleared",
    "已停止": "Stopped",
    "已最小化到托盘": "Minimized to the system tray",
    "选择模式后点击「开始」": "Choose a mode, then click Start",
    "字幕将显示在这里 —— 选择模式后点击「开始」":
        "Subtitles will appear here - choose a mode, then click Start",
    "字幕将显示在这里 —— 说完一句后自动生成":
        "Subtitles will appear here - finish a sentence to generate one",
    "字幕将显示在这里 —— 播放目标应用中的内容":
        "Subtitles will appear here - play content in the target app",
    "选择文件后，处理结果与导出位置将显示在这里":
        "Processing results and the export path will appear here after you choose a file",
    "锁定浮窗": "Lock overlay",
    "解锁浮窗": "Unlock overlay",
    "锁定后鼠标点击会穿过浮窗，作用到下面的软件":
        "When locked, mouse clicks pass through to the app below",
    "点击解锁，恢复拖动、选中文字和浮窗工具栏":
        "Unlock to drag, select text, and use the overlay toolbar",
    "减小字幕浮窗字号": "Decrease overlay font size",
    "增大字幕浮窗字号": "Increase overlay font size",
    "保存失败": "Save failed",
    "对话已保存": "Session saved",
    "录音已保存": "Recording saved",
    "正在保存…": "Saving...",
    "正在后台保存对话…": "Saving session in the background...",
    "诊断中心": "Diagnostics",
    "自检": "Self-check",
    "日志": "Logs",
    "自检结果": "Self-check results",
    "正在准备检查…": "Preparing checks...",
    "重新检查": "Run again",
    "导出报告 (txt)": "Export report (txt)",
    "刷新": "Refresh",
    "导出日志": "Export logs",
    "清空视图": "Clear view",
    "实时 · 自动跟随": "Live · auto-follow",
    "调试模式": "Debug mode",
    "暂无日志": "No logs yet",
    "诊断模块尚未实现": "Diagnostics module is not available",
    "未接入检查模块": "No check module connected",
    "正在导出…": "Exporting...",
    "日志已导出": "Logs exported",
    "日志导出失败": "Log export failed",
    "报告已导出": "Report exported",
    "报告导出失败": "Report export failed",
    "导出诊断报告": "Export diagnostic report",
    "下载源": "Download source",
    "自动测速切换": "Auto benchmark and switch",
    "全球源优先": "Global source first",
    "中国大陆源优先": "Mainland China source first",
    "根据这台电脑实时评估": "Live assessment for this computer",
    "独显→NPU→核显→CPU · 按质量排序 · 目录更新于":
        "Discrete GPU -> NPU -> integrated GPU -> CPU · sorted by quality · catalog updated ",
    "徽章同时考虑内存、计算负载与是否存在更合适的高质量模型":
        "Badges consider memory, compute load, and whether a better quality model exists",
    "全部": "All",
    "语音识别": "Speech recognition",
    "字幕翻译": "Subtitle translation",
    "排序：模型质量 ↓": "Sort: model quality ↓",
    "不推荐": "Not recommended",
    "较为推荐": "Somewhat recommended",
    "推荐": "Recommended",
    "满载": "Full load",
    "独显": "Discrete GPU",
    "核显": "Integrated GPU",
    "未检测到独立显卡": "No discrete GPU detected",
    "未检测到 NPU": "No NPU detected",
    "未检测到核显": "No integrated GPU detected",
    "已检测": "Detected",
    "已检测，等待兼容模型后端": "Detected; waiting for a compatible model backend",
    "运行设备": "Runtime devices",
    "NPU 可用": "NPU available",
    "NPU 待验证": "NPU pending",
    "NPU 不可用": "NPU unavailable",
    "NPU 配置不足": "NPU requirements not met",
    "验证设备": "Validated device",
    "驱动": "Driver",
    "运行时": "Runtime",
    "验证日期": "Validated",
    "质量分": "Quality",
    "随应用内置": "Bundled with the app",
    "下载": "Download",
    "取消": "Cancel",
    "正在取消…": "Cancelling...",
    "使用中": "In use",
    "设为使用": "Use this model",
    "修复安装": "Repair installation",
    "缺少文件": "Missing files",
    "卸载": "Uninstall",
    "模型下载未完成": "Model download incomplete",
    "模型正在使用": "Model is in use",
    "请先切换到同类的其他模型再卸载。":
        "Switch to another model of the same type before uninstalling.",
    "卸载模型": "Uninstall model",
    "无法卸载": "Unable to uninstall",
    "系统默认麦克风": "System default microphone",
    "系统默认输出": "System default output",
    "全部系统声音": "All system audio",
    "启动": "Start",
    "结束": "Finish",
    "可用": "available",
    "显存": "VRAM",
    "核": "cores",
    "线程": "threads",
    "内存": "RAM",
    "个可下载或内置模型": "downloadable or bundled models",
    "仅展示已接通运行时的非淘汰模型": "only supported, current models are shown",
    "刷新设备与窗口": "Refresh devices and windows",
    "翻译": "Translation",
    "识别调优": "Recognition tuning",
    "语音": "Voice",
    "语音朗读": "Text-to-speech",
    "设备": "Devices",
    "实时音频设备": "Live audio devices",
    "外观": "Appearance",
    "关于": "About",
    "语言": "Language",
    "跟随系统": "Follow system",
    "简体中文": "Simplified Chinese",
    "翻译来源": "Translation source",
    "语音识别来源": "Speech recognition source",
    "本地 STT": "Local STT",
    "云 STT": "Cloud STT",
    "本地 STT（使用模型广场中的识别模型）":
        "Local STT (use a recognition model from Model Hub)",
    "云 STT（OpenAI 兼容音频转写）": "Cloud STT (OpenAI-compatible transcription)",
    "打开本地模型广场": "Open local Model Hub",
    "翻译来源": "Translation source",
    "快档（本地 OPUS-MT，<0.5s/句）": "Fast (local OPUS-MT, <0.5s/sentence)",
    "质量档（使用模型广场中选择的专用翻译模型）":
        "Quality (use the selected translation model from Model Hub)",
    "云翻译（OpenAI 兼容文本模型）": "Cloud translation (OpenAI-compatible text model)",
    "云翻译配置（仅云翻译生效）": "Cloud translation (used only for cloud translation)",
    "云 STT 配置（仅云 STT 生效）": "Cloud STT (used only for cloud STT)",
    "本地 STT 不上传音频；云 STT 会把每个语音片段发送到你填写的音频转写接口。两者可以和下面的本地/云翻译自由组合。":
        "Local STT does not upload audio; cloud STT sends each speech segment to the transcription endpoint you provide. Either can be combined with local or cloud translation below.",
    "这里调整的是模型如何听、何时断句以及一次考虑多少候选，不是重新训练模型。没有 AI 背景时保持“自动”即可。":
        "These settings control how the model listens, splits sentences, and considers candidates; they do not retrain the model. If you are new to AI, leave the preset on Automatic.",
    "只把识别后的文字发送到翻译接口；可与本地 STT 组合。":
        "Only recognized text is sent to the translation endpoint; this can be combined with local STT.",
    "当前按 VAD 切出的语音片段调用 /audio/transcriptions，适合实时字幕的句段模式；请确认服务商支持音频转写接口。":
        "VAD-finalized speech segments are sent to /audio/transcriptions for live subtitles; confirm that your provider supports audio transcription.",
    "选择窗口后，只捕获该应用及其子进程的声音；其它系统声音不会进入字幕。选择「全部系统声音」时使用上面的输出设备。":
        "After choosing a window, only that app and its child processes are captured; other system audio will not enter the subtitles. Choose All system audio to use the output device above.",
    "浮窗上的工具条也可以直接调整。这里适合设置一个固定的默认外观，锁定后仍可在浮窗顶部悬停打开解锁控制。":
        "The overlay toolbar can also adjust these values directly. Use this page for a fixed default appearance; even when locked, hover over the top of the overlay to unlock it.",
    "翻译服务 API Key": "Translation service API key",
    "音频转写服务 API Key": "Audio transcription service API key",
    "音频和视频 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.mp4 *.mkv *.mov *.avi *.webm)":
        "Audio and video (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.mp4 *.mkv *.mov *.avi *.webm)",
    "所有文件 (*)": "All files (*)",
    "纯文本 (*.txt)": "Plain text (*.txt)",
    "SRT 字幕 (*.srt)": "SRT subtitles (*.srt)",
    "WebVTT 字幕 (*.vtt)": "WebVTT subtitles (*.vtt)",
    "选择要生成字幕的音频或视频": "Choose an audio or video file for subtitles",
    "正在后台保存对话…": "Saving session in the background...",
    "日志页签初始化失败（详见日志文件）": "The log tab could not be initialized (see the log file)",
    "显示音频电平、队列、设备打开与分句等详细事件":
        "Show detailed audio levels, queues, device-open events, and sentence-splitting events",
    "确定卸载": "Uninstall",
    "模型文件将从本机删除。": "The model files will be removed from this computer.",
    "翻译服务 API Key": "Translation service API key",
    "音频转写服务 API Key": "Audio transcription service API key",
    "模型名": "Model name",
    "只把识别后的文字发送到翻译接口；可与本地 STT 组合。":
        "Only recognized text is sent to the translation endpoint; this can be combined with local STT.",
    "当前按 VAD 切出的语音片段调用 /audio/transcriptions，适合实时字幕的句段模式；请确认服务商支持音频转写接口。":
        "VAD-finalized speech segments are sent to /audio/transcriptions for live subtitles; confirm that your provider supports audio transcription.",
    "混合模式": "Hybrid mode",
    "云端双阶段": "Fully cloud",
    "本地模式": "Fully local",
    "本地质量翻译": "Local quality translation",
    "本地快档翻译": "Local fast translation",
    "当前组合": "Current pipeline",
    "识别模型调优": "Recognition model tuning",
    "调优预设": "Tuning preset",
    "自动（按当前模型选择）": "Automatic (based on the selected model)",
    "响应优先": "Low latency",
    "均衡": "Balanced",
    "准确优先": "Accuracy",
    "智能上下文（动态断句）": "Smart context (dynamic segmentation)",
    "自定义": "Custom",
    "语音灵敏度": "Voice sensitivity",
    "停顿多久断句": "Pause before splitting a sentence",
    "上下文最长等待": "Maximum context wait",
    "单句最长时长": "Maximum sentence length",
    "识别候选数": "Recognition candidates",
    "单句最大文字量": "Maximum text per sentence",
    "常用词 / 专有名词": "Common / technical words",
    "实时双语草稿": "Live bilingual draft",
    "启用当前句原文与译文动态更新":
        "Continuously update the current source and translation",
    "上下文保守纠偏": "Conservative context correction",
    "启用保守纠偏（不自由改写）":
        "Enable conservative correction (no free rewriting)",
    "语气词清理": "Filler-word cleanup",
    "关闭（保留原话）": "Off (preserve original speech)",
    "轻度（仅独立语气词）": "Light (isolated fillers only)",
    "恢复自动": "Restore automatic",
    "保存调优": "Save tuning",
    "放弃更改": "Discard changes",
    "未修改": "Unchanged",
    "已保存": "Saved",
    "有未保存的更改": "Unsaved changes",
    "已保存 · 下次开始时生效": "Saved · takes effect next time you start",
    "朗读译文（本地 TTS；失败自动降级为仅字幕）":
        "Read translations aloud (local TTS; falls back to subtitles if unavailable)",
    "智能上下文和实时双语草稿均可使用朗读；为避免反复抢读，只朗读已经定稿的译文。":
        "Text-to-speech works with Smart Context and live bilingual drafts. To avoid repeated or overlapping audio, only finalized translations are read.",
    "中文朗读模型": "Chinese voice model",
    "英文朗读模型": "English voice model",
    "打开模型广场管理朗读模型": "Manage voice models in Model Hub",
    "没有兼容的朗读模型": "No compatible voice model",
    "尚未安装朗读模型": "No voice model installed",
    "（未安装）": " (not installed)",
    "中文 / 英语 / 中英混读": "Chinese / English / mixed speech",
    "英语（美国）": "English (US)",
    "自然音色": "Natural voice",
    "中英混读": "Mixed Chinese and English",
    "单音色": "Single voice",
    "中文": "Chinese",
    "美式女声": "US female voice",
    "单模型支持中文、英文和中英混读，声音更自然；体积和 CPU 占用高于轻量模型。":
        "One model reads Chinese, English, and mixed text with a more natural voice; it uses more storage and CPU than the lightweight models.",
    "中文低延迟朗读模型，CPU 占用较低；当前使用稳定的默认说话人音色。":
        "A low-latency Mandarin model with modest CPU use; VoxSub currently uses its stable default speaker.",
    "美式英文女声轻量模型，适合实时译文朗读和低资源设备。":
        "A lightweight US English female voice suited to live translation and low-resource devices.",
    "麦克风输入（A 模式）": "Microphone input (Mode A)",
    "系统输出（B 模式监听全部声音时）": "System output (Mode B, all system audio)",
    "应用声音隔离": "Application audio isolation",
    "浮窗显示": "Overlay display",
    "译文字号": "Translation font size",
    "浮窗不透明度": "Overlay opacity",
    "启动时保持锁定并允许点击穿透":
        "Keep locked and click-through on startup",
    "主题": "Theme",
    "浅色": "Light",
    "深色": "Dark",
    "基底": "Base",
    "技术栈": "Technology",
    "隐私": "Privacy",
    "开发与排障": "Development and troubleshooting",
    "模型保存位置": "Model storage location",
    "识别、翻译、语音模型会按用途整理在这里。更新软件不会清空这个文件夹。":
        "Speech, translation, and voice models are organized here. Updating VoxSub will not clear this folder.",
    "当前位置": "Current location",
    "打开文件夹": "Open folder",
    "更改保存位置": "Change location",
    "迁移已有模型": "Move existing models",
    "如果以前把模型放在其他磁盘或手动复制过模型，可从这里把它们并入当前位置。同名文件会保留，避免覆盖已有下载。":
        "If models were stored on another drive or copied manually, move them into the current location here. Existing same-name files are kept to avoid overwriting downloads.",
    "已保留你升级前使用的位置；需要时可迁移到其他磁盘。":
        "Your existing model location was kept; move it to another drive whenever you need.",
    "新安装默认保存到软件目录下的 Models 文件夹。":
        "New installations save models in the app's Models folder by default.",
    "使用你自己选择的模型保存位置。": "Models are stored in the location you selected.",
    "模型位置会在更新后保持不变，直到你主动更改。":
        "The model location stays unchanged across updates until you change it.",
    "迁移失败": "Move failed",
    "模型迁移完成": "Model move complete",
    "更新日志": "What's new",
    "每次更新后，首次打开应用会看到一次简短说明。这里可以随时回看最近版本的变化。":
        "After an update, the first app launch shows a brief summary. Revisit recent changes here anytime.",
    "启用内置调试模式（实时显示详细日志）":
        "Enable built-in debug mode (show detailed live logs)",
    "版本": "Version",
    "核心包": "core package",
    "一款面向大众的实时翻译字幕工具（Windows）":
        "A real-time translation subtitle tool for everyone (Windows)",
    "默认音频仅在内存处理；仅当你打开“同时录音”时保存本地 WAV":
        "Audio is processed in memory by default; a local WAV is saved only when simultaneous recording is enabled",
    "透明度": "Opacity",
    "白": "White",
    "青绿": "Teal",
    "黑": "Black",
    "字色": "Text color",
    "字号 +": "Font size +",
    "字号 -": "Font size -",
    "锁定并允许点击穿透": "Lock and allow click-through",
    "关闭浮窗": "Close overlay",
    "关闭": "Close",
    "锁定": "Lock",
    "解锁": "Unlock",
    "解锁浮窗，恢复拖动和文字选择": "Unlock the overlay to restore dragging and text selection",
    "锁定并让鼠标点击穿过浮窗": "Lock the overlay and let mouse clicks pass through",
    "暂时关闭字幕浮窗": "Temporarily close the subtitle overlay",
    "系统默认麦克风": "System default microphone",
    "系统默认输出": "System default output",
    "全部系统声音": "All system audio",
    "正常": "OK",
    "注意": "Warnings",
    "失败": "Failed",
    "未知检查项": "Unknown check",
    "模型完整性": "Model integrity",
    "磁盘/内存余量": "Disk / memory headroom",
    "ASR 冒烟": "ASR smoke test",
    "VAD 冒烟": "VAD smoke test",
    "TTS 冒烟": "TTS smoke test",
    "ORT providers": "ORT providers",
    "自检执行": "Self-check execution",
    "确定卸载": "Uninstall",
    "模型文件将从本机删除。": "The model files will be removed from this computer.",
    "已下载": "Downloaded",
    "中英日高质量识别，强化方言、口音、远场、噪声与歌曲场景。":
        "High-quality Chinese, English, and Japanese recognition with stronger dialect, accent, far-field, noise, and singing performance.",
    "新一代多语种识别，覆盖 30 种语言、22 种中文方言和中英混说。":
        "Next-generation multilingual recognition covering 30 languages, 22 Chinese dialects, and Chinese-English mixed speech.",
    "轻量多语种识别，覆盖中文、粤语、英语、日语和韩语；适合更重视响应速度的轻薄本。":
        "Lightweight multilingual recognition for Chinese, Cantonese, English, Japanese, and Korean; suitable for thin laptops that prioritize responsiveness.",
    "约百兆的实时低资源兜底；精度较低，但在同体积下仍有独特实时优势。":
        "A roughly 100 MB real-time fallback for low-resource machines; less accurate, but unusually responsive at this size.",
    "高质量专用翻译模型，复杂语境、术语和指令遵循优先。":
        "A dedicated high-quality translation model focused on complex context, terminology, and instruction following.",
    "保留 7B 的复杂语境与术语优势，同时比 Q8 更适合高性能游戏本和主流独显。":
        "Keeps the 7B model's context and terminology strengths while fitting high-performance laptops and mainstream discrete GPUs better than Q8.",
    "7B 的高保真量化档，优先保留术语和复杂句质量；只适合内存、显存充裕的电脑。":
        "A high-fidelity 7B quantization that prioritizes terminology and complex sentences; only suitable for computers with ample RAM and VRAM.",
    "面向端侧的专用翻译模型，在速度、资源占用和质量间更均衡。":
        "An edge-focused translation model with a balanced speed, resource, and quality profile.",
    "端侧翻译的均衡高保真档，比 Q4 多占一些内存，适合希望进一步减少量化损失的用户。":
        "A balanced high-fidelity edge translation model that uses slightly more memory than Q4 to reduce quantization loss.",
    "轻量模型中的高保真档，适合内存充裕但不想运行 7B 的笔记本和台式机。":
        "A high-fidelity lightweight model for laptops and desktops with enough memory that do not need a 7B model.",
    "近乎即时的低资源兜底，适合老旧电脑；长句和口语质量有限。":
        "An almost instant low-resource fallback for older computers; long and conversational sentences are less accurate.",
    "中文 / 英语 / 日语 · 7 种中文方言": "Chinese / English / Japanese · 7 Chinese dialects",
    "30 种语言 · 22 种中文方言": "30 languages · 22 Chinese dialects",
    "中文 / 粤语 / 英语 / 日语 / 韩语": "Chinese / Cantonese / English / Japanese / Korean",
    "中文 / 英语": "Chinese / English",
    "33 种语言": "33 languages",
    "33 语言": "33 languages",
    "方言": "Dialect",
    "中文优先": "Chinese-first",
    "抗噪": "Noise resistant",
    "歌曲": "Singing",
    "多语种": "Multilingual",
    "混合语言": "Mixed language",
    "响应快": "Fast response",
    "低资源": "Low resource",
    "粤语": "Cantonese",
    "低延迟": "Low latency",
    "低内存": "Low memory",
    "内置": "Built-in",
    "最高质量": "Highest quality",
    "复杂语境": "Complex context",
    "高质量": "High quality",
    "高性能设备": "High-performance hardware",
    "术语": "Terminology",
    "均衡高保真": "Balanced high fidelity",
    "平衡": "Balanced",
    "端侧": "On-device",
    "满载级": "Full-load class",
    "高保真": "High fidelity",
    "Qwen3-ASR 0.6B · INT8": "Qwen3-ASR 0.6B · INT8",
    "SenseVoice Small · INT8": "SenseVoice Small · INT8",
    "Zipformer 中英双语 · 极速兼容": "Zipformer Chinese-English · Fast fallback",
    "OPUS-MT · 极速兼容": "OPUS-MT · Fast fallback",
    "Fun-ASR-Nano 2512 · INT8": "Fun-ASR-Nano 2512 · INT8",
    "中文 / 英语 / 日语 · 7 种中文方言": "Chinese / English / Japanese · 7 Chinese dialects",
    "该模型没有可用下载源": "This model has no available download source",
    "该模型正在使用；请先切换到其他模型": "This model is in use; switch to another model first",
}

_REVERSE_TRANSLATIONS = {value: key for key, value in TRANSLATIONS.items()}


def system_language() -> str:
    """Resolve the Windows/Qt UI language to one of the supported languages."""
    try:
        return LANGUAGE_ZH if QLocale.system().language() == QLocale.Language.Chinese else LANGUAGE_EN
    except Exception:  # pragma: no cover - defensive fallback for broken Qt locale data
        return LANGUAGE_EN


def normalize_language(value: object) -> str:
    value = str(value or LANGUAGE_SYSTEM).lower()
    return value if value in LANGUAGE_VALUES else LANGUAGE_SYSTEM


class LanguageManager(QObject):
    language_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._setting = LANGUAGE_SYSTEM
        self._language = system_language()

    @property
    def setting(self) -> str:
        return self._setting

    @property
    def language(self) -> str:
        return self._language

    def set_language(self, value: object) -> str:
        setting = normalize_language(value)
        language = system_language() if setting == LANGUAGE_SYSTEM else setting
        changed = setting != self._setting or language != self._language
        self._setting = setting
        self._language = language
        if changed:
            self.language_changed.emit(language)
        return language


language_manager = LanguageManager()


def tr(chinese: str, english: str | None = None) -> str:
    """Return the current-language version of a source string."""
    if language_manager.language == LANGUAGE_ZH:
        return chinese
    return english if english is not None else TRANSLATIONS.get(chinese, chinese)


def translate_existing(text: str) -> str:
    """Translate an already-rendered static widget string in either direction."""
    if language_manager.language == LANGUAGE_ZH:
        return _REVERSE_TRANSLATIONS.get(text, text)
    return TRANSLATIONS.get(text, text)


def format_text(chinese: str, english: str, **values: object) -> str:
    return tr(chinese, english).format(**values)


def translate_dynamic(text: str) -> str:
    """Translate common generated UI sentences while preserving values."""
    if language_manager.language == LANGUAGE_ZH:
        return text
    replacements = (
        ("独立显卡", "Discrete GPU"),
        ("预计负载", "estimated load"),
        ("至少需要", "requires at least"),
        ("当前约", "currently about"),
        ("可运行，但会接近当前配置上限", "can run, but is near this computer's limit"),
        ("模型能力明显偏低，当前配置可流畅运行", "is noticeably less capable; this computer can run"),
        ("质量较高，但资源占用超过一半", "has good quality but uses more than half of the available resources"),
        ("当前配置还能流畅运行质量更高的", "this computer can also run the higher-quality"),
        ("性能开销与质量较均衡", "has a balanced performance cost and quality"),
        ("内存", "RAM"),
        ("质量分", "Quality"),
        ("运行设备", "Runtime devices"),
        ("已检测，等待兼容模型后端", "detected; waiting for a compatible model backend"),
        ("已检测", "detected"),
        ("条登记全部就绪", " registered entries are ready"),
        ("存在且大小一致", "present with matching sizes"),
        ("模型缺失", "model missing"),
        ("模型加载通过", "model loaded successfully"),
        ("加载/解码异常", "load/decode error"),
        ("加载/检测异常", "load/detection error"),
        ("合成通过", "synthesis succeeded"),
        ("合成异常", "synthesis error"),
        ("耗时", "time"),
        ("语音窗", "speech windows"),
        ("个语音窗口", " speech windows"),
        ("磁盘剩余", "Disk free"),
        ("内存可用", "RAM available"),
        ("内存信息不可用", "RAM information unavailable"),
        ("仅有 CPU", "CPU only"),
    )
    translated = text
    for source, target in replacements:
        translated = translated.replace(source, target)
    return translated


def _translate_combo(widget: QComboBox) -> None:
    current = widget.currentData()
    for index in range(widget.count()):
        widget.setItemText(index, translate_existing(widget.itemText(index)))
    if current is None:
        return
    selected = next(
        (index for index in range(widget.count())
         if widget.itemData(index) == current),
        None,
    )
    if selected is not None:
        widget.setCurrentIndex(selected)


def _translate_metadata(widget: QObject) -> None:
    if hasattr(widget, "windowTitle") and hasattr(widget, "setWindowTitle"):
        title = widget.windowTitle()
        if title:
            widget.setWindowTitle(translate_existing(title))
    if hasattr(widget, "toolTip") and hasattr(widget, "setToolTip"):
        tooltip = widget.toolTip()
        if tooltip and "<div" not in tooltip:
            widget.setToolTip(translate_existing(tooltip))


def _translate_widget(widget: QObject) -> None:
    object_name = str(getattr(widget, "objectName", lambda: "")())
    if object_name in {"srcText", "dstText", "overlaySrc", "overlayDst"}:
        return
    if isinstance(widget, (QLabel, QAbstractButton)):
        widget.setText(translate_existing(widget.text()))
    if isinstance(widget, QLineEdit):
        widget.setPlaceholderText(translate_existing(widget.placeholderText()))
    if isinstance(widget, QComboBox):
        _translate_combo(widget)
    if isinstance(widget, QTabWidget):
        for index in range(widget.count()):
            widget.setTabText(index, translate_existing(widget.tabText(index)))
    if isinstance(widget, QGroupBox):
        widget.setTitle(translate_existing(widget.title()))
    if isinstance(widget, QMenu):
        widget.setTitle(translate_existing(widget.title()))
    _translate_metadata(widget)
    if isinstance(widget, QAction):
        widget.setText(translate_existing(widget.text()))


def retranslate_widget_tree(root: QWidget) -> None:
    """Refresh static text in a window and its menus without rebuilding it."""
    _translate_widget(root)
    for child in root.findChildren(QObject):
        _translate_widget(child)
    for action in root.findChildren(QAction):
        _translate_widget(action)


__all__ = [
    "LANGUAGE_EN",
    "LANGUAGE_SYSTEM",
    "LANGUAGE_VALUES",
    "LANGUAGE_ZH",
    "LanguageManager",
    "TRANSLATIONS",
    "format_text",
    "language_manager",
    "normalize_language",
    "retranslate_widget_tree",
    "system_language",
    "tr",
    "translate_dynamic",
    "translate_existing",
]
