#!/usr/bin/env python
"""检查 VoxSub 窗口是否出现在桌面上 —— 静默模式的验收工具。

用 Win32 的 EnumWindows + IsWindowVisible 直接问系统"这个窗口现在可见吗"，
而不是问 Electron"你显示了吗"。前者才是用户实际看到的真相。

⚠ 单看"没有可见窗口"会假通过：应用根本没启动时也是 0 个可见窗口。
   所以 --expect-hidden 同时要求 electron 进程存在，否则报 FAIL。

用法：
    python tools/check-windows-visible.py                    # 列出窗口及可见性
    python tools/check-windows-visible.py --expect-hidden    # 验收静默模式
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import subprocess
import sys

user32 = ctypes.windll.user32

TITLES = ("语幕", "VoxSub", "字幕浮窗")


def electron_process_count() -> int:
    """当前运行的本项目 electron 进程数。用于排除"应用没起来"的假通过。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq electron.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return sum(1 for line in result.stdout.splitlines() if "electron.exe" in line.lower())
    except (OSError, subprocess.SubprocessError):
        return -1


def enum_windows() -> list[tuple[int, str, bool]]:
    """返回 [(hwnd, 标题, 是否可见)]，只含标题里带 VoxSub 关键字的顶层窗口。"""
    found: list[tuple[int, str, bool]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
            if any(key in title for key in TITLES):
                found.append((hwnd, title, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(callback, 0)
    return found


def main() -> int:
    expect_hidden = "--expect-hidden" in sys.argv
    windows = enum_windows()
    visible = [item for item in windows if item[2]]
    processes = electron_process_count()

    print(f"electron 进程数: {processes}")
    print(f"找到 {len(windows)} 个 VoxSub 相关顶层窗口：")
    for hwnd, title, is_visible in windows:
        mark = "可见 <-" if is_visible else "隐藏"
        print(f"  [{mark}] hwnd={hwnd}  {title}")
    if not windows:
        print("  （无）")

    print()
    if not expect_hidden:
        print(f"其中可见 {len(visible)} 个")
        return 0

    # 静默模式验收：应用必须在跑，且一个可见窗口都不能有
    if processes <= 0:
        print("FAIL 应用没在运行 —— 此时「没有可见窗口」是假通过")
        return 1

    if visible:
        print(f"FAIL 期望全部隐藏，但有 {len(visible)} 个窗口可见：")
        for hwnd, title, _ in visible:
            print(f"       hwnd={hwnd}  {title}")
        return 1

    print(f"PASS 应用在运行（{processes} 个进程），桌面上没有可见窗口")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
