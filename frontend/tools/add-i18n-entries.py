#!/usr/bin/env python
"""补齐 i18n 英文词条。

写成脚本文件而不是内联 node -e 的原因：
  1) 词条多，内联字符串会被 bash 转义层吃掉（引号、箭头符号）
  2) 用户环境的终端会拦截含可疑转义的内联命令

用法：
    python tools/add-i18n-entries.py        # 只补缺失的
    python tools/add-i18n-entries.py --dry  # 只报告，不写
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
I18N = HERE.parent / "src" / "renderer" / "i18n.ts"

# 中文 → 英文。键必须与源码里 tr("...") 的字面量完全一致。
ENTRIES: dict[str, str] = {
    # 通用
    "语幕": "VoxSub",
    "让对话、会议和视频，落成清晰的双语文幕。":
        "Live bilingual captions for conversations, meetings, and video.",
    # 录音流程
    "结束并保存": "Stop & save",
    "像手机录音：开始 → 暂停 / 继续 → 结束并保存":
        "Like a phone recorder: start, then pause/resume, then stop & save",
    "仅生成字幕，不保存麦克风音频": "Captions only; microphone audio is not saved",
    "录音仅在麦克风同传模式可用": "Recording is only available in microphone mode",
    "录音已保存": "Recording saved",
    "正在结束录音…": "Finishing recording…",
    "录音未保存（可能未开启同时录音）":
        "Recording not saved (recording may be turned off)",
    # OCR 工作区
    "OCR 图片与屏幕翻译": "OCR for images and screen",
    "截图翻译": "Screenshot",
    "实时区域": "Live region",
    "框选屏幕并翻译": "Select area & translate",
    "上传图片并翻译": "Upload image & translate",
    "导出译后图片": "Export translated image",
    "识别结果预览": "Recognition preview",
    "框选或上传图片后，这里显示结果":
        "Select an area or upload an image to see results here",
    "原图": "Original",
    "译后": "Translated",
    "识别原文": "Recognised text",
    "译文": "Translation",
    "复制": "Copy",
    "已复制原文": "Source copied",
    "已复制译文": "Translation copied",
    "复制失败": "Copy failed",
    "（未识别到文字）": "(no text recognised)",
    "（未翻译）": "(not translated)",
    "正在识别…": "Recognising…",
    "正在生成译后图片…": "Rendering translated image…",
    "完成": "Done",
    "行": "lines",
    "识别失败，详见日志": "Recognition failed — see the logs",
    "没有检测到文字，请放大区域或提高对比度":
        "No text detected — enlarge the area or increase contrast",
    "拖动选择区域，Esc 取消": "Drag to select an area · Esc to cancel",
    "已取消框选": "Selection cancelled",
    "拖动选择要持续翻译的区域，Esc 取消":
        "Drag to select the region to keep translating · Esc to cancel",
    "实时 OCR 运行中 · 译文将原位覆盖":
        "Live OCR running · translations overlay in place",
    "实时 OCR 已结束": "Live OCR stopped",
    "当前没有可导出的译后图片": "No translated image to export yet",
    "已导出译后图片": "Translated image exported",
    "导出失败": "Export failed",
    "实时区域 OCR": "Live region OCR",
    "原位覆盖，不重复识别静止画面。选中一块区域后持续识别，译文直接盖在原文位置上。":
        "In-place overlay that skips unchanged frames. Pick a region and "
        "translations are painted straight over the original text.",
    "覆盖窗不会被下一轮截图识别到（已排除捕获），因此不会出现译文被再翻译的回路。":
        "The overlay window is excluded from capture, so translations are "
        "never fed back into recognition.",
    "选择区域并开始": "Select region & start",
    "结束实时 OCR": "Stop live OCR",
    "等待框选": "Waiting for selection",
    "隐私：截图只在本机内存中送入 OCR；只有选择云翻译时，识别出的文字才会发送给对应服务。":
        "Privacy: screenshots are processed in local memory only. Recognised "
        "text leaves your machine only when cloud translation is selected.",
    "隐私：识别只在本机进行；译文的显示位置与原文框一致，不修改屏幕上的原应用。":
        "Privacy: recognition runs locally. Translations are drawn in place "
        "and never modify the original application.",
    # 诊断页日志
    "实时": "Live",
    "历史文件": "Log file",
    "文件": "File",
    "导出日志": "Export log",
    "打开文件夹": "Open folder",
    "清除本机日志": "Clear local logs",
    "将删除本机全部日志文件（不影响模型、配置与已导出的报告）。确定继续？":
        "Delete all local log files? Models, config, and exported reports "
        "are not affected.",
    "正在读取日志文件…": "Reading log file…",
    "日志文件为空或尚未生成": "Log file is empty or has not been created yet",
    "已清除": "Cleared",
    "个文件": "files",
    "已导出": "Exported",
    # 设置页
    "模型": "Models",
    "存储与模型": "Storage & models",
    "模型目录": "Model catalog",
    "OCR 缓存": "OCR cache",
    "使用默认位置": "Using the default location",
    "更改保存位置": "Change location",
    "更改缓存位置": "Change cache location",
    "打开缓存": "Open cache",
    "每类保留": "Keep per category",
    "设为 0 表示无限保留。": "Set to 0 to keep everything.",
    "迁移已有模型": "Adopt existing models",
    "正在扫描…": "Scanning…",
    "已并入 {n} 项，跳过 {m} 项": "Adopted {n}, skipped {m}",
    "迁移失败，详见日志": "Migration failed — see the logs",
    "模型保存在自定义位置。更新软件不会清空这个文件夹。":
        "Models are stored in a custom folder. Updating the app will not "
        "clear it.",
    "模型保存在默认位置。可改到其它磁盘以避免占用系统盘。":
        "Models are stored in the default folder. Move them to another drive "
        "to keep the system disk free.",
    "识别、翻译、语音模型会按用途整理在这里。更新软件不会清空这个文件夹。":
        "Speech, translation, and voice models are organised here by purpose. "
        "Updating the app will not clear this folder.",
    "如果以前把模型放在其他磁盘或手动复制过模型，可从这里把它们并入当前位置。":
        "If your models live on another drive or were copied manually, adopt "
        "them into the current location here.",
    "上传/截图原图与译后覆盖图分开保存，绝不写入 C 盘。默认每类保留最近 15 张；设为 0 表示无限保留。":
        "Original captures and translated overlays are stored separately and "
        "never written to the system drive. Keeps the latest 15 per category "
        "by default; 0 means unlimited.",
    "模型仍在后台迁移，请等待完成后再退出应用。":
        "Models are still moving. Wait for completion before exiting VoxSub.",
    # 模型目录页
    "已占": "Used",
    "全部安装需": "All models need",
    "识别、翻译、语音模型按用途分组。下载后即在本机运行，不上传你的音频。":
        "Speech, translation, and voice models grouped by purpose. They run "
        "locally — your audio is never uploaded.",
    # 更新日志
    "更新日志": "Release notes",
    "暂无更新日志": "No release notes yet",
    "收起历史更新日志": "Hide older release notes",
    "展开历史更新日志（还有 {n} 版）": "Show older release notes ({n} more)",
}


def main() -> int:
    dry = "--dry" in sys.argv
    source = I18N.read_text(encoding="utf-8")

    # 解析既有条目，避免重复插入
    existing = set(re.findall(r'^\s*"((?:[^"\\]|\\.)*)":', source, re.MULTILINE))

    missing = {zh: en for zh, en in ENTRIES.items() if zh not in existing}
    if not missing:
        print(f"i18n 已完整（共 {len(ENTRIES)} 条待补清单）")
        return 0

    print(f"待补 {len(missing)} 条（已有 {len(ENTRIES) - len(missing)} 条）：")
    for zh in list(missing)[:8]:
        print(f"  · {zh}")
    if len(missing) > 8:
        print(f"  … 另有 {len(missing) - 8} 条")

    if dry:
        return 0

    anchor = "const EN: Dict = {\n"
    if anchor not in source:
        print("找不到插入锚点 const EN: Dict = {", file=sys.stderr)
        return 1

    lines = "".join(
        f"  {json.dumps(zh, ensure_ascii=False)}: {json.dumps(en, ensure_ascii=False)},\n"
        for zh, en in missing.items()
    )
    I18N.write_text(source.replace(anchor, anchor + lines, 1), encoding="utf-8")
    print(f"已写入 {len(missing)} 条 → {I18N.relative_to(I18N.parent.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
