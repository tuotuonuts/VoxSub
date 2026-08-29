"""Exercise the packaged Windows installer-shutdown handshake end to end."""
from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import time
import uuid
from ctypes import wintypes
from pathlib import Path


EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000


def _kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.OpenEventW.argtypes = (
        wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    library.OpenEventW.restype = wintypes.HANDLE
    library.SetEvent.argtypes = (wintypes.HANDLE,)
    library.SetEvent.restype = wintypes.BOOL
    library.OpenMutexW.argtypes = (
        wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    library.OpenMutexW.restype = wintypes.HANDLE
    library.CloseHandle.argtypes = (wintypes.HANDLE,)
    library.CloseHandle.restype = wintypes.BOOL
    return library


def run_smoke(executable: Path) -> float:
    if os.name != "nt":
        raise RuntimeError("installer shutdown smoke requires Windows")
    if not executable.is_file():
        raise FileNotFoundError(executable)

    unique = uuid.uuid4().hex
    event_name = rf"Local\VoxSub.Test.Shutdown.{unique}"
    mutex_name = rf"Local\VoxSub.Test.Application.{unique}"
    # Never reuse the developer's installed configuration/model cache.  A
    # real local model can start a several-second OCR/translation warm-up and
    # make this lifecycle check report a false timeout.  Keeping the isolated
    # directory beside the tested bundle also avoids C: temp permissions.
    smoke_root = executable.parent / f".smoke-appdata-{unique}"
    environment = os.environ.copy()
    environment.update({
        "QT_QPA_PLATFORM": "offscreen",
        "LOCALAPPDATA": str(smoke_root),
        "APPDATA": str(smoke_root),
        "VOXSUB_INSTANCE_LOCK": f"installer-smoke-{unique}.lock",
        "VOXSUB_INSTALLER_MUTEX": mutex_name,
        "VOXSUB_INSTALLER_SHUTDOWN_EVENT": event_name,
    })
    process = subprocess.Popen([str(executable)], env=environment)
    kernel32 = _kernel32()
    event_handle = 0
    try:
        ready_deadline = time.monotonic() + 30.0
        while time.monotonic() < ready_deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"packaged app exited before handshake (exit {process.returncode})"
                )
            event_handle = kernel32.OpenEventW(
                EVENT_MODIFY_STATE, False, event_name)
            if event_handle:
                break
            time.sleep(0.1)
        if not event_handle:
            raise TimeoutError("packaged app did not expose shutdown event in 30s")

        mutex_handle = kernel32.OpenMutexW(SYNCHRONIZE, False, mutex_name)
        if not mutex_handle:
            raise RuntimeError("running mutex was missing before shutdown")
        kernel32.CloseHandle(mutex_handle)

        started_at = time.monotonic()
        if not kernel32.SetEvent(event_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # A cold packaged start may begin OCR/translation warm-up before
            # the Qt event loop can dispatch the shutdown notifier.  Keep this
            # smoke check bounded, but allow that one-time initialization to
            # unwind instead of reporting a false failure on slower machines.
            exit_code = process.wait(timeout=30.0)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("packaged app did not exit within 30s") from exc
        elapsed = time.monotonic() - started_at
        if exit_code != 0:
            raise RuntimeError(f"packaged app shutdown exit code was {exit_code}")

        stale_mutex = kernel32.OpenMutexW(SYNCHRONIZE, False, mutex_name)
        if stale_mutex:
            kernel32.CloseHandle(stale_mutex)
            raise RuntimeError("running mutex survived packaged app termination")
        return elapsed
    finally:
        if event_handle:
            kernel32.CloseHandle(event_handle)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5.0)
        shutil.rmtree(smoke_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    arguments = parser.parse_args()
    elapsed = run_smoke(arguments.exe.resolve())
    print(f"packaged installer shutdown smoke passed in {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
