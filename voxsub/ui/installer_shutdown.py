"""Fast, explicit shutdown handshake used by the Windows installer.

Closing VoxSub's main window normally hides it to the tray.  Windows Restart
Manager therefore cannot distinguish a user close from an update request and
waits for its full timeout.  The installer signals a named event instead; the
Qt event loop receives it and runs the same application-level cleanup as the
tray Quit action.  A named mutex remains open until cleanup is complete, so
the installer can use a short bounded grace period before its legacy fallback.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from PySide6.QtCore import QObject, QWinEventNotifier, Signal
from shiboken6 import VoidPtr

from voxsub.logging_setup import get_logger


logger = get_logger("ui.installer_shutdown")

_OBJECT_ID = "7B5F6A3C-2E8D-4B1A-9C7E-VOXSUB0000001"
RUNNING_MUTEX_NAME = rf"Local\VoxSub.Application.{_OBJECT_ID}"
SHUTDOWN_EVENT_NAME = rf"Local\VoxSub.InstallerShutdown.{_OBJECT_ID}"


class InstallerShutdownBridge(QObject):
    """Expose a Windows named-event shutdown request to Qt as a signal."""

    shutdown_requested = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        mutex_name: str = RUNNING_MUTEX_NAME,
        event_name: str = SHUTDOWN_EVENT_NAME,
    ) -> None:
        super().__init__(parent)
        self._kernel32 = None
        self._mutex_handle = 0
        self._event_handle = 0
        self._notifier: QWinEventNotifier | None = None
        if os.name != "nt":
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = (
            wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        mutex_handle = kernel32.CreateMutexW(None, False, mutex_name)
        event_handle = kernel32.CreateEventW(None, True, False, event_name)
        if not mutex_handle or not event_handle:
            error = ctypes.get_last_error()
            if event_handle:
                kernel32.CloseHandle(event_handle)
            if mutex_handle:
                kernel32.CloseHandle(mutex_handle)
            logger.warning("安装器退出握手初始化失败: winerror=%d", error)
            return

        self._kernel32 = kernel32
        self._mutex_handle = int(mutex_handle)
        self._event_handle = int(event_handle)
        # PySide's Windows HANDLE binding requires VoidPtr even though its
        # generated signature describes the argument as ``int``.
        self._notifier = QWinEventNotifier(VoidPtr(self._event_handle), self)
        # Connect signal-to-signal so PySide does not try (and fail) to convert
        # Qt's opaque HANDLE argument before entering Python.
        self._notifier.activated.connect(self.shutdown_requested)
        self.shutdown_requested.connect(self._disable_notifier)
        logger.debug("安装器退出握手已就绪")

    @property
    def is_available(self) -> bool:
        return self._notifier is not None

    def _disable_notifier(self) -> None:
        notifier = self._notifier
        if notifier is None or not notifier.isEnabled():
            return
        # The event is manual-reset so every running instance can observe it.
        # Disable this instance immediately to avoid a tight activation loop.
        notifier.setEnabled(False)
        logger.info("收到安装器退出请求，开始安全收尾")

    def close(self) -> None:
        notifier, self._notifier = self._notifier, None
        if notifier is not None:
            notifier.setEnabled(False)
            notifier.deleteLater()
        kernel32, self._kernel32 = self._kernel32, None
        if kernel32 is not None:
            for handle in (self._event_handle, self._mutex_handle):
                if handle:
                    kernel32.CloseHandle(handle)
        self._event_handle = 0
        self._mutex_handle = 0


__all__ = [
    "InstallerShutdownBridge",
    "RUNNING_MUTEX_NAME",
    "SHUTDOWN_EVENT_NAME",
]
