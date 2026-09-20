"""IPC 适配层共用的小工具：目录体积、可用空间、可读大小、模型根解析。

被入口文件与多个 handler 共用，所以单独一层，避免 handler 反向 import 入口。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any


def _dir_size(directory: Path) -> int:
    """递归求目录占用字节数；不可读的条目跳过（不因单文件失败丢掉整体统计）。"""
    total = 0
    try:
        for entry in directory.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def _free_bytes(path: str) -> int:
    """目标路径所在卷的可用空间。用于迁移前判断空间是否够。

    路径可能还不存在（新建目标目录），所以逐级向上找第一个存在的父目录。
    """
    import shutil  # noqa: PLC0415

    probe = Path(path)
    while True:
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            parent = probe.parent
            if parent == probe:
                return 0
            probe = parent


def _human_size(num: int) -> str:
    if num <= 0:
        return "内置"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _resolve_models_root() -> Path:
    """复用应用自己的模型根目录解析。

    刻意不硬编码 %LOCALAPPDATA%：用户改过存储位置、或将来做便携版时，
    硬编码会让界面静默显示"全部未安装"——这类故障最难排查。
    """
    try:
        from voxsub.paths import resolve_models_root  # noqa: PLC0415

        return Path(resolve_models_root())
    except (ImportError, AttributeError):
        pass
    for module_name in ("voxsub.diagnostics", "voxsub.router", "voxsub.asr"):
        try:
            module = __import__(module_name, fromlist=["models_dir"])
            return Path(module.models_dir())
        except (ImportError, AttributeError):
            continue
    # 最后兜底：与应用默认布局一致
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(local) / "VoxSub" / "models"

