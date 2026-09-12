#!/usr/bin/env python
"""补齐迁移向导的 i18n 词条。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

I18N = Path(__file__).resolve().parent.parent / "src" / "renderer" / "i18n.ts"

ENTRIES: dict[str, str] = {
    # 向导框架
    "检测旧版": "Detect legacy",
    "数据风险": "Data risk",
    "确认清单": "Review list",
    "正在迁移": "Migrating",
    "结果": "Result",
    "检测旧版 VoxSub": "Legacy VoxSub detected",
    "新版本可以直接沿用旧版的模型与设置，无需重复下载":
        "The new version can reuse your existing models and settings — no re-download needed",
    "只读取信息，不会修改任何文件": "Read-only check; nothing is modified",
    "检测结果": "Detection result",
    "状态": "Status",
    "检测到旧版": "Legacy version found",
    "未检测到旧版": "No legacy version found",
    "版本": "Version",
    "安装位置": "Install location",
    "来源": "Source",
    "系统卸载记录": "System uninstall record",
    "配置文件": "Config file",
    "查看数据风险": "Review data risk",
    "稍后再说": "Later",
    "关闭": "Close",
    "返回": "Back",
    # 风险页
    "你的数据都在安全位置，可以直接使用新版本，无需迁移。":
        "All your data is in a safe location. You can use the new version as-is; no migration needed.",
    "数据与旧版放在同一目录。卸载旧版本身不会删除它，但如果你手动删除整个安装文件夹，数据会一起丢失。建议复制到独立位置。":
        "Your data sits inside the legacy install folder. Uninstalling alone will not remove it, "
        "but deleting the whole folder manually would. Copying it to a separate location is recommended.",
    "有数据位于旧版安装器会删除的目录中。卸载旧版会连同这些数据一起删除，必须先迁移。":
        "Some data lives in directories the legacy uninstaller removes. Uninstalling would delete it — migrate first.",
    "安全": "Safe",
    "需注意": "Attention",
    "会被删除": "Will be deleted",
    "文件": "files",
    "规划迁移": "Plan migration",
    "暂时跳过": "Skip for now",
    "列出旧版留下的数据及其位置": "Lists the data left by the legacy version and where it lives",
    # 清单页
    "确认迁移清单": "Confirm migration list",
    "请核对每一项的来源、去向与用途。迁移过程中不会删除原始数据":
        "Check each item's source, destination, and purpose. Originals are never deleted during migration.",
    "没有需要迁移的项目": "Nothing to migrate",
    "迁移到": "Migrate to",
    "目标磁盘空间不足": "Not enough free space on the target drive",
    "需要": "Need",
    "可用": "Available",
    "程序可自行重建": "Rebuilt automatically",
    "已选": "Selected",
    "项": "items",
    "开始迁移": "Start migration",
    "取消": "Cancel",
    # 进度页
    "迁移进行中，请不要关闭应用。中断可能导致目标目录不完整（原始数据不会被删除）。":
        "Migration in progress — do not close the app. Interrupting may leave the target incomplete "
        "(originals are not deleted).",
    "每项数据有独立进度，完成后会自动校验": "Each item has its own progress bar; verification runs automatically",
    "请勿关闭应用": "Do not close the app",
    "进行中…": "In progress…",
    "完成": "Done",
    "失败": "Failed",
    # 报告页
    "迁移结果": "Migration result",
    "迁移完成": "Migration complete",
    "项已校验通过": "items verified",
    "耗时": "took",
    "迁移未完全成功": "Migration did not fully succeed",
    "项成功": "succeeded",
    "项失败": "failed",
    "失败详情（请复制给开发者）": "Failure details (copy this for the developer)",
    "复制诊断摘要": "Copy diagnostics",
    "已复制": "Copied",
    "复制失败": "Copy failed",
    "打开日志文件夹": "Open log folder",
    "原始数据未被删除，可以修正问题后重新迁移。":
        "Originals were not deleted — fix the issue and retry.",
    "迁移成功": "Migration succeeded",
    "迁移遇到问题": "Migration hit a problem",
    "数据已在新位置并通过校验，原始数据保持不变":
        "Data is in the new location and verified; originals are untouched",
    "已保留现场，未删除任何原始数据": "Scene preserved; no original data was deleted",
    "完成并返回": "Finish and return",
    "返回主界面": "Return to main screen",
    "重试迁移": "Retry migration",
    "迁移未返回结果": "Migration returned no result",
    "数据正在迁移，请等待完成后再退出应用。":
        "Data is migrating. Wait for completion before exiting.",
    # 设置页入口
    "旧版数据": "Legacy data",
    "检查旧版数据": "Check legacy data",
    "正在检查…": "Checking…",
    "检查失败，详见日志": "Check failed — see the logs",
    "打开迁移向导": "Open migration wizard",
    "项需注意": "items need attention",
    "数据位置安全": "data location is safe",
    "检查旧版（Qt 版）留下的数据位置，必要时迁移到独立目录。卸载旧版前建议先做这一步。":
        "Check where the legacy (Qt) version keeps its data, and migrate it to a separate folder "
        "if needed. Recommended before uninstalling the old version.",
}


def main() -> int:
    dry = "--dry" in sys.argv
    source = I18N.read_text(encoding="utf-8")
    existing = set(re.findall(r'^\s*"((?:[^"\\]|\\.)*)":', source, re.MULTILINE))

    missing = {zh: en for zh, en in ENTRIES.items() if zh not in existing}
    if not missing:
        print(f"迁移向导词条已完整（清单 {len(ENTRIES)} 条）")
        return 0

    print(f"待补 {len(missing)} 条（已有 {len(ENTRIES) - len(missing)} 条）")
    if dry:
        for zh in list(missing)[:10]:
            print(f"  · {zh}")
        return 0

    anchor = "const EN: Dict = {\n"
    if anchor not in source:
        print("找不到插入锚点", file=sys.stderr)
        return 1

    lines = "".join(
        f"  {json.dumps(zh, ensure_ascii=False)}: {json.dumps(en, ensure_ascii=False)},\n"
        for zh, en in missing.items()
    )
    I18N.write_text(source.replace(anchor, anchor + lines, 1), encoding="utf-8")
    print(f"已写入 {len(missing)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
