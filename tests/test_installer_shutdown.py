from __future__ import annotations

import ctypes
import os
import uuid
from ctypes import wintypes

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from voxsub.ui.installer_shutdown import InstallerShutdownBridge


@pytest.mark.skipif(os.name != "nt", reason="Windows named events only")
def test_named_event_requests_shutdown_without_closing_a_window() -> None:
    app = QApplication.instance() or QApplication([])
    unique = uuid.uuid4().hex
    mutex_name = rf"Local\VoxSub.Test.Application.{unique}"
    event_name = rf"Local\VoxSub.Test.Shutdown.{unique}"
    bridge = InstallerShutdownBridge(
        mutex_name=mutex_name,
        event_name=event_name,
    )
    requests: list[bool] = []
    bridge.shutdown_requested.connect(lambda: requests.append(True))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = (
        wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenEventW(0x0002, False, event_name)
    try:
        assert bridge.is_available
        assert handle
        assert kernel32.SetEvent(handle)
        for _ in range(100):
            QTest.qWait(10)
            if requests:
                break
        assert requests == [True]
    finally:
        if handle:
            kernel32.CloseHandle(handle)
        bridge.close()
