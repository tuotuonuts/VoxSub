#!/usr/bin/env python
"""Qt 前端 → Electron 前端的功能对照（只读，不修改任何文件）。

用途：回答"原来前端有的功能是不是都实现了"，用枚举而不是印象。

做法：
  1. 从 VoxSub/voxsub/ui/*.py 提取功能点：
     - setObjectName(...)   —— Qt 里每个具名控件都是功能面的证据
     - QPushButton/QLabel/QToolButton 等的可见文案
     - def <name>(             —— 公开方法名（能力清单）
  2. 在 voxsub-electron/src/**、backend/** 里搜索对应痕迹
  3. 报告缺失项，按文件分组

局限（必须如实说明）：
  · 名字对不上不等于功能缺失（我用的是 web 命名习惯）
  · 所以脚本输出是"待人工确认清单"，不是最终结论
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

QT_UI = Path(r"D:/OneDrive/app_dve/VoxSub/voxsub/ui")
NEW_ROOT = Path(r"D:/OneDrive/app_dve/voxsub-electron")

# 取反：这些是 Qt 内部实现细节，不是功能面
SKIP_METHODS = {
    "__init__", "_build_ui", "paintEvent", "resizeEvent", "moveEvent", "closeEvent",
    "showEvent", "hideEvent", "enterEvent", "leaveEvent", "mousePressEvent",
    "mouseMoveEvent", "mouseReleaseEvent", "wheelEvent", "keyPressEvent",
    "eventFilter", "changeEvent", "focusInEvent", "focusOutEvent", "sizeHint",
    "minimumSizeHint", "heightForWidth", "hasHeightForWidth", "dragEnterEvent",
    "dropEvent", "contextMenuEvent", "tabletEvent", "nativeEvent", "initStyleOption",
}

DISPLAY_CLASSES = (
    "QPushButton", "QLabel", "QToolButton", "QCheckBox", "QRadioButton",
    "ToggleSwitch", "QGroupBox", "QTabWidget", "QMenu", "QAction", "QComboBox",
    "SwitchButton", "PushButton", "PrimaryPushButton", "CheckBox", "RadioButton",
)

# 中文/英文可见文案：至少含 2 个连续汉字，或首字母大写的英文词组
TEXT_RE = re.compile(r'"([^"\n]{2,60})"|\'([^\'\n]{2,60})\'')
CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def read_new_frontend() -> str:
    """把新前端所有源码拼成一份文本，用于关键词命中判断。"""
    parts: list[str] = []
    for pattern in ("src/**/*.ts", "src/**/*.css", "src/**/*.html", "backend/*.py"):
        for path in NEW_ROOT.glob(pattern):
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def extract_qt_features(source: str) -> tuple[set[str], set[str], set[str]]:
    """返回 (具名控件, 可见文案, 公开方法)。"""
    object_names = set(re.findall(r"setObjectName\(\s*[\"']([A-Za-z0-9_]+)[\"']", source))

    visible: set[str] = set()
    for cls in DISPLAY_CLASSES:
        # 类名后面紧跟的字符串字面量，通常是可见文案
        for match in re.finditer(rf"\b{cls}\(\s*(?:f?[\"']([^\"']{{2,60}})[\"'])", source):
            visible.add(match.group(1))
    # 常见的 setText / setToolTip / setWindowTitle
    for match in re.finditer(r"set(?:Text|ToolTip|WindowTitle|PlaceholderText)\(\s*f?[\"']([^\"']{2,60})[\"']", source):
        visible.add(match.group(1))

    methods = set(re.findall(r"^    def ([a-z][a-z0-9_]+)\(", source, re.MULTILINE))
    return object_names, visible, methods


def has_trace(needle: str, haystack: str) -> bool:
    """判断新前端里是否有这个功能点的痕迹。

    中文文案要求整串出现；标识符按拆词后的片段判断（容忍 snake_case → camelCase
    的命名变化）。
    """
    if CJK_RE.search(needle):
        # 中文：允许去掉标点后再比一次
        core = re.sub(r"[\s·，。：、（）()「」\-—]+", "", needle)
        if needle in haystack:
            return True
        return bool(core) and core in re.sub(r"[\s·，。：、（）()「」\-—]+", "", haystack)

    # 标识符：目标词 + 各片段
    if needle in haystack:
        return True
    parts = [p for p in re.split(r"[_\-]", needle) if len(p) > 2]
    if not parts:
        return False
    lowered = haystack.lower()
    return all(part.lower() in lowered for part in parts)


def main() -> int:
    new_text = read_new_frontend()
    if not new_text:
        print("读不到新前端源码", file=sys.stderr)
        return 2

    groups: dict[str, tuple[list[str], list[str], list[str]]] = {}

    for py in sorted(QT_UI.glob("*.py")):
        source = py.read_text(encoding="utf-8", errors="replace")
        names, texts, methods = extract_qt_features(source)

        miss_names = sorted(n for n in names if not has_trace(n, new_text))
        miss_texts = sorted(t for t in texts if not has_trace(t, new_text))
        miss_methods = sorted(
            m for m in methods
            if m not in SKIP_METHODS
            and not m.startswith("_on_")
            and not has_trace(m, new_text)
        )
        groups[py.name] = (miss_names, miss_texts, miss_methods)

    print("=" * 78)
    print("Qt 前端 → Electron 前端 功能对照（待人工确认清单）")
    print("=" * 78)

    total = 0
    for name, (mn, mt, mm) in groups.items():
        if not (mn or mt or mm):
            print(f"\n## {name}  —— 无明显缺口")
            continue
        print(f"\n## {name}")
        if mn:
            print(f"  具名控件未命中 ({len(mn)}):")
            for item in mn:
                print(f"    · {item}")
        if mt:
            print(f"  可见文案未命中 ({len(mt)}):")
            for item in mt:
                print(f"    · {item}")
        if mm:
            print(f"  方法未命中 ({len(mm)}):")
            for item in mm:
                print(f"    · {item}")
        total += len(mn) + len(mt) + len(mm)

    print("\n" + "=" * 78)
    print(f"合计 {total} 项待人工确认（名字对不上 ≠ 功能缺失）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
