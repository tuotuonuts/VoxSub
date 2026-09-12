#!/usr/bin/env python
"""把固定 px 尺寸改成 rem，让界面随窗口大小整体缩放。

背景：原先所有尺寸写死 px，根字号恒为 13px。结果是窗口放大到 2560 宽时，
文字仍是 13px、间距不变，只有容器被拉长 —— 看起来"不跟着缩放"。

做法：
  · html 的 font-size 改为随视口流体变化（默认窗口下正好 13px）
  · 把所有"应该跟着缩放"的属性从 px 改成 rem（1rem = 13px 基准）
  · 保留不该缩放的：1px 描边、滚动条厚度、阅读宽度上限、阴影模糊

安全性：默认窗口（1240px）下根字号 = 13px，所以 px→rem 是恒等变换，
默认尺寸的渲染结果与改动前完全一致。只有窗口尺寸变化时才会体现差异。

用法：
    python tools/px-to-rem.py --dry     只报告将要改多少处
    python tools/px-to-rem.py           执行
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RENDERER = Path(__file__).resolve().parent.parent / "src" / "renderer"
TARGETS = ("app.css", "wizard.css")

#: 基准字号：与改动前的 body font-size 一致，保证默认窗口下零视觉变化
BASE_PX = 13.0

#: 需要跟着缩放的属性
CONVERT_PROPS = {
    "font-size",
    "gap", "row-gap", "column-gap",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "border-radius",
    "border-top-left-radius", "border-top-right-radius",
    "border-bottom-left-radius", "border-bottom-right-radius",
    "min-height", "min-width", "max-height",
    "outline-offset",
    "letter-spacing",
    "inset",
    "top", "right", "bottom", "left",
    "grid-template-columns",
    "width", "height",
}

#: 永不缩放：1px 描边、滚动条厚度、阅读宽度上限、阴影与纹理
KEEP_PROPS = {
    "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-left-width", "border-right-width", "border-top-width", "border-bottom-width",
    "outline", "max-width",
    "box-shadow", "background-size",
}

#: 这些块内的 px 一律不动（滚动条厚度是 OS 级的观感，不该随窗口缩放）
SKIP_BLOCK_MARKERS = ("::-webkit-scrollbar",)

#: 小于这个值的 px 不转换（多半是发丝线/描边）
MIN_CONVERT_PX = 2.0


def rem_of(px: float) -> str:
    """px → rem 字符串，去掉多余小数位。"""
    value = px / BASE_PX
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{text}rem"


def convert_value(raw: str, prop: str) -> tuple[str, int]:
    """转换一条声明里的 px 值，返回 (新值, 转换处数)。"""
    if prop in KEEP_PROPS:
        return raw, 0

    # clamp()/calc() 里混着 vw 与 px 的表达式要原样保留：
    # 那些 px 是流体的边界值，转成 rem 会让公式失去意义。
    if "clamp(" in raw or "calc(" in raw:
        return raw, 0

    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        number = float(match.group(1))
        if number < MIN_CONVERT_PX:
            return match.group(0)
        count += 1
        return rem_of(number)

    # 只替换紧跟数字的 px，避免动到 var() 或颜色
    new = re.sub(r"([0-9]*\.?[0-9]+)px\b", replace, raw)
    return new, count


def process(path: Path, dry: bool) -> tuple[int, int]:
    """处理一个文件，返回 (改动处数, 涉及规则数)。"""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")

    total = 0
    rules = 0
    out: list[str] = []

    # 跟踪当前是否在跳过的块里
    skip_depth = 0
    skip_stack: list[bool] = []

    for line in lines:
        stripped = line.strip()

        # 选择器行：判断这个规则是否要跳过
        if stripped.endswith("{") and not stripped.startswith("@"):
            marker = stripped[:-1].strip()
            skip = any(m in marker for m in SKIP_BLOCK_MARKERS)
            skip_stack.append(skip)
            out.append(line)
            continue

        if stripped == "}" and skip_stack:
            skip_stack.pop()
            out.append(line)
            continue

        in_skip = any(skip_stack)

        match = re.match(r"^(\s*)([a-z-]+):\s*(.+?)(;?)$", line)
        if match and not in_skip:
            indent, prop, value, semi = match.groups()
            if prop in CONVERT_PROPS:
                new_value, changed = convert_value(value, prop)
                if changed:
                    total += changed
                    rules += 1
                    out.append(f"{indent}{prop}: {new_value}{semi}")
                    continue

        out.append(line)

    if not dry and total:
        path.write_text("\n".join(out), encoding="utf-8")

    return total, rules


def main() -> int:
    dry = "--dry" in sys.argv
    grand = 0

    for name in TARGETS:
        path = RENDERER / name
        if not path.is_file():
            print(f"  跳过（不存在）：{name}")
            continue
        count, rules = process(path, dry)
        grand += count
        print(f"  {name:16s} {count:4d} 处 px → rem（涉及 {rules} 条声明）")

    print()
    if dry:
        print(f"预计改动 {grand} 处（--dry，未写入）")
    else:
        print(f"已改动 {grand} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
