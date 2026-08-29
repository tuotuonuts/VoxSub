from __future__ import annotations

import os
import sys

from voxsub import runtime_bootstrap


def test_source_execution_is_a_noop(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    runtime_bootstrap._CONFIGURED_ROOT = None  # noqa: SLF001
    calls: list[str] = []
    monkeypatch.setattr(os, "add_dll_directory", lambda path: calls.append(path))

    runtime_bootstrap.configure_frozen_dll_search_path()

    assert calls == []


def test_frozen_execution_registers_qt_directories(monkeypatch, tmp_path):
    root = tmp_path / "_internal"
    (root / "PySide6").mkdir(parents=True)
    (root / "shiboken6").mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")
    runtime_bootstrap._CONFIGURED_ROOT = None  # noqa: SLF001
    calls: list[str] = []

    class _Handle:
        pass

    monkeypatch.setattr(
        os,
        "add_dll_directory",
        lambda path: calls.append(path) or _Handle(),
    )

    runtime_bootstrap.configure_frozen_dll_search_path()

    expected = [str(root / "PySide6"), str(root / "shiboken6"), str(root)]
    assert calls == expected[:2]
    assert os.environ["PATH"].split(os.pathsep)[:3] == expected


def test_frozen_execution_resolves_internal_root_when_meipass_is_app_dir(
    monkeypatch, tmp_path
):
    app_root = tmp_path / "VoxSub"
    root = app_root / "_internal"
    (root / "PySide6").mkdir(parents=True)
    (root / "shiboken6").mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(app_root), raising=False)
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")
    runtime_bootstrap._CONFIGURED_ROOT = None  # noqa: SLF001
    calls: list[str] = []

    class _Handle:
        pass

    monkeypatch.setattr(
        os,
        "add_dll_directory",
        lambda path: calls.append(path) or _Handle(),
    )

    runtime_bootstrap.configure_frozen_dll_search_path()

    expected = [str(root / "PySide6"), str(root / "shiboken6"), str(root)]
    assert calls == expected[:2]
