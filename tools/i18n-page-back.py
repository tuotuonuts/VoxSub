#!/usr/bin/env python
"""补齐二级页面返回栏需要的 i18n 词条。

单独一个小脚本而不是复用 add-i18n-entries.py：那一个维护的是"功能词条总表"，
这里是本次改动的增量；两者都用 --dry 可先看结果。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

I18N = Path(__file__).resolve().parent.parent / "src" / "renderer" / "i18n.ts"

ENTRIES: dict[str, str] = {
    "返回（Esc）": "Back (Esc)",
    "模型": "Models",
    "设置": "Settings",
    "诊断": "Diagnostics",
}


def main() -> int:
    dry = "--dry" in sys.argv
    source = I18N.read_text(encoding="utf-8")
    existing = set(re.findall(r'^\s*"((?:[^"\\]|\\.)*)":', source, re.MULTILINE))

    missing = {zh: en for zh, en in ENTRIES.items() if zh not in existing}
    if not missing:
        print("返回栏词条已完整")
        return 0

    print(f"待补 {len(missing)} 条：")
    for zh in missing:
        print(f"  · {zh}")
    if dry:
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
