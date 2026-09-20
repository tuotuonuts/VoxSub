"""Crash-safety tests for shared atomic file operations."""
from __future__ import annotations

from pathlib import Path

import pytest

from voxsub import file_io


def test_atomic_binary_copy_preserves_old_file_when_copy_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "new.bin"
    destination = tmp_path / "model.bin"
    source.write_bytes(b"new model bytes")
    destination.write_bytes(b"known good model")

    def fail_mid_copy(source_handle, destination_handle) -> None:
        destination_handle.write(source_handle.read(3))
        raise OSError("simulated interruption")

    monkeypatch.setattr(file_io.shutil, "copyfileobj", fail_mid_copy)

    with pytest.raises(OSError, match="simulated interruption"):
        file_io.copy_file_atomically(source, destination)

    assert destination.read_bytes() == b"known good model"
    assert not list(tmp_path.glob(".model.bin.*.part"))


# ------------------------------------------------------- Windows 瞬时占用重试
#
# 背景：本项目仓库放在 OneDrive 下，同步盘与杀毒软件会短暂持有刚写完的文件
# 句柄，``os.replace`` 于是零星抛 ``PermissionError: [WinError 5] 拒绝访问``。
# 全量测试里这表现为"跟代码无关的随机失败"—— 最消耗排查时间的一类问题。
# 共享的原子写入负责把它兜住，所以在这里钉死。


class _FlakyOs:
    """只替换 ``os.replace`` 的替身，避免 monkeypatch 到全局 os 模块。

    其余属性（``fsync`` 等）原样转发给真正的 ``os``。
    """

    def __init__(self, fail_times: int) -> None:
        import os as real_os

        self._real_os = real_os
        self._fail_times = fail_times
        self.calls = 0

    def __getattr__(self, name):
        if name == "_real_os":  # 未初始化时避免无限递归
            raise AttributeError(name)
        return getattr(self._real_os, name)

    def replace(self, source, destination):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise PermissionError(5, "拒绝访问")
        return self._real_os.replace(source, destination)


def _install_flaky_os(monkeypatch, fail_times: int) -> _FlakyOs:
    shim = _FlakyOs(fail_times)
    monkeypatch.setattr(file_io, "os", shim)
    monkeypatch.setattr(file_io.time, "sleep", lambda _seconds: None)
    return shim


def test_atomic_write_retries_through_transient_lock(tmp_path, monkeypatch) -> None:
    """被短暂占用时应该重试成功，而不是把偶发失败直接抛给调用方。"""
    destination = tmp_path / "config.json"
    destination.write_text("old", encoding="utf-8")
    shim = _install_flaky_os(monkeypatch, fail_times=2)

    file_io.write_text_atomically(destination, "new")

    assert destination.read_text(encoding="utf-8") == "new"
    assert shim.calls == 3, "前两次失败 + 第三次成功"
    assert not list(tmp_path.glob(".config.json.*.part"))


def test_atomic_write_gives_up_after_bounded_attempts(tmp_path, monkeypatch) -> None:
    """一直被占用就要抛错 —— 不能无限重试把界面卡死。"""
    destination = tmp_path / "config.json"
    destination.write_text("old", encoding="utf-8")
    shim = _install_flaky_os(monkeypatch, fail_times=999)

    with pytest.raises(PermissionError):
        file_io.write_text_atomically(destination, "new")

    assert shim.calls == file_io._REPLACE_ATTEMPTS, "重试次数必须是有界的"
    assert destination.read_text(encoding="utf-8") == "old", "旧文件必须保持完好"


def test_atomic_binary_copy_retries_too(tmp_path, monkeypatch) -> None:
    source = tmp_path / "new.bin"
    source.write_bytes(b"payload")
    destination = tmp_path / "model.bin"
    destination.write_bytes(b"old")
    shim = _install_flaky_os(monkeypatch, fail_times=1)

    file_io.copy_file_atomically(source, destination)

    assert destination.read_bytes() == b"payload"
    assert shim.calls == 2
