"""Small, crash-safe file writers shared by exports and subtitle generation."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

#: ``os.replace`` 的有界重试次数与间隔。
#:
#: 为什么需要重试：Windows 上同步盘（本项目仓库就在 OneDrive 下）和杀毒软件
#: 会短暂持有刚写完的文件句柄，``os.replace`` 于是零星抛
#: ``PermissionError: [WinError 5] 拒绝访问``。全量测试里表现为"跟代码无关的
#: 随机失败"—— 这类失败最消耗排查时间，所以在这里统一兜住：
#: 每次 50ms、最多 4 次，累计约 0.2 秒，远小于重跑一次测试的成本。
_REPLACE_ATTEMPTS = 5
_REPLACE_DELAY_SECONDS = 0.05


def _replace_with_retry(temporary: str, destination: Path) -> None:
    """把临时文件原子地发布到目标位置，容忍 Windows 上的瞬时占用。"""
    last_error: OSError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError as error:  # WinError 5 / 32：被占用
            last_error = error
            if attempt + 1 < _REPLACE_ATTEMPTS:
                time.sleep(_REPLACE_DELAY_SECONDS)
    assert last_error is not None
    raise last_error


def sanitize_text(text: str) -> str:
    """把不成对的 UTF-16 代理字符换成 U+FFFD。

    为什么需要：Windows 窗口标题是 UTF-16，**允许**出现不成对的代理字符
    （`GetWindowTextW` 取到的 Python str 会原样带着它们）。这种字符串无法
    编码成 UTF-8，写 config.json 时会抛 UnicodeEncodeError —— 用户看到的是
    `set_config 失败: UnicodeEncodeError: ... surrogates not allowed`，
    设置没保存，日志里多一条看不懂的回溯。

    这些码元在 UTF-8 里没有任何合法表示，替换是唯一可行的处理。
    """
    if not text:
        return text
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text  # 常见情况：扫一遍就返回，不产生新对象
    return "".join("\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


def sanitize_for_json(value: Any) -> Any:
    """递归清洗 dict/list/tuple 里的字符串，使其一定能被 UTF-8 编码。

    配置里可能有嵌套结构（例如 per-language 的模型选择），只清顶层不够。
    """
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {sanitize_for_json(k): sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_json(item) for item in value]
    return value


def write_text_atomically(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    """Write text beside its destination, then atomically replace the old file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary_name, destination)
        return destination
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def copy_file_atomically(source: Path | str, destination: Path | str) -> Path:
    """Copy a binary file beside its destination, then atomically publish it."""
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with source_path.open("rb") as source_handle, tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination_path.parent,
            prefix=f".{destination_path.name}.",
            suffix=".part",
            delete=False,
        ) as destination_handle:
            temporary_name = destination_handle.name
            shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        _replace_with_retry(temporary_name, destination_path)
        return destination_path
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
