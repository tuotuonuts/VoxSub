#!/usr/bin/env python
"""判定 Qt 版卸载器是否会删除给定的路径 —— 只读分析，不执行卸载。

方法：Inno Setup 的 unins000.dat 是二进制记录表。用 Inno 官方的记录类型
语义解读它太重，这里改用"证据组合"的方式做保守判定：

  1. 解析 .dat 中的 UTF-16 路径字符串（Inno 记录里路径是宽字符）
  2. 判断路径落在安装目录内的哪个子树
  3. 结合 installer.iss 的 [InstallDelete] 声明做交叉验证
  4. 输出风险判定：会删 / 不删 / 需人工确认

为什么不能只看 .dat 字符串出现次数：Inno 会记录 [Dirs] 创建的空目录，
它们出现在 .dat 里但不等于卸载时会被删除。所以必须结合 iss 声明。

用法：python tools/check-uninstaller-risk.py "D:/VoxSub" "D:/VoxSub/Models"
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def extract_paths(dat_path: Path) -> list[str]:
    """从 unins000.dat 里抽出看起来像路径的宽字符串。"""
    raw = dat_path.read_bytes()
    # Inno 的路径是 UTF-16LE；解码整体再按分隔符切分
    text = raw.decode("utf-16-le", errors="ignore")
    # 路径片段：盘符或反斜杠开头，含反斜杠，长度合理
    candidates = re.findall(r"[A-Za-z]:\\[^\x00\r\n\t]{2,200}", text)
    return candidates


def normalize(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\").lower()


def installed_delete_scope(iss_path: Path) -> list[str]:
    """解析 installer.iss 里 [InstallDelete] 声明的相对路径。"""
    if not iss_path.is_file():
        return []
    text = iss_path.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"\[InstallDelete\](.*?)(?=\n\[|\Z)", text, re.S)
    if not block:
        return []
    names = re.findall(r'Name:\s*"([^"]+)"', block.group(1))
    return [n for n in names]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    install_root = Path(sys.argv[1])
    target = Path(sys.argv[2])
    dat = install_root / "unins000.dat"
    iss = Path(__file__).resolve().parent.parent.parent / "VoxSub" / "scripts" / "installer.iss"

    print(f"安装目录   : {install_root}")
    print(f"待判定路径 : {target}")
    print()

    if not dat.is_file():
        print("找不到 unins000.dat —— 可能不是 Inno Setup 安装，无法判定")
        return 1

    root_norm = normalize(str(install_root))
    target_norm = normalize(str(target))

    inside = target_norm.startswith(root_norm)
    print(f"是否在安装目录内 : {'是' if inside else '否'}")
    if not inside:
        print()
        print("结论：不在安装目录内。Inno 卸载只清理自己的安装树，")
        print("      除非 [UninstallDelete] 显式列出，否则不会触碰此处。")
        print("      风险判定：不删")
        return 0

    relative = target_norm[len(root_norm):].strip("\\")
    top = relative.split("\\")[0] if relative else ""
    print(f"相对安装根的路径 : {relative or '(即为安装根)'}")
    print(f"顶层子项         : {top or '(无)'}")
    print()

    declared = installed_delete_scope(iss)
    print("installer.iss 的 [InstallDelete] 声明：")
    for name in declared:
        print(f"  · {name}")
    print()

    declared_tops = {
        re.sub(r"^\{app\}\\?", "", n).split("\\")[0].lower() for n in declared
    }
    print(f"其中会被删除的顶层目录：{sorted(declared_tops)}")
    print()

    # 判定
    if not top:
        verdict = "会删"
        reason = "目标是安装根目录本身，卸载会整体移除"
    elif top.lower() in declared_tops:
        verdict = "会删"
        reason = f"顶层目录 {top} 出现在 [InstallDelete]（安装时删、卸载时同样移除）"
    else:
        # 出现在 .dat 里的非删除项 = [Dirs] 创建记录，卸载时只删空目录
        strings = extract_paths(dat)
        hits = [s for s in strings if normalize(s).startswith(target_norm)]
        verdict = "不删（有条件的）"
        reason = (
            f"顶层目录 {top} 不在 [InstallDelete] 中；"
            f".dat 里有 {len(hits)} 条相关记录，属 [Dirs] 创建项。"
            "Inno 卸载默认不删非空目录 → 数据可存活。"
        )

    print(f"风险判定 : {verdict}")
    print(f"依据     : {reason}")
    print()

    if verdict.startswith("不删"):
        print("警告：这是 Inno 的默认行为，不是显式保证。以下情况会改变结论：")
        print("  · 用户手动删除了整个安装目录")
        print("  · 某些清理工具/杀软把 {app} 当作残留目录整体清理")
        print("  · 将来重新打包时在 [UninstallDelete] 里加入了该目录")
        print()
        print("因此仍需在 Electron 版里做主动防护（首启动检测 + 复制到安全位置）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
