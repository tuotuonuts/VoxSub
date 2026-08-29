"""Early runtime setup required by the frozen Windows application.

PySide6 extension modules are loaded before the UI module can do any setup.
In a PyInstaller onedir build their dependent DLLs live below ``_internal``
and are not guaranteed to be visible to the Windows loader when the process
starts from a shortcut or an installer-created working directory.  This
module keeps that platform-specific bootstrap in one small, testable place.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# The handles returned by ``os.add_dll_directory`` must stay alive for the
# lifetime of the process; otherwise Windows removes the directory from the
# DLL search list when the handle is garbage-collected.
_DLL_DIRECTORY_HANDLES: list[object] = []
_CONFIGURED_ROOT: Path | None = None


def configure_frozen_dll_search_path() -> None:
    """Expose bundled Qt and Python DLL directories before importing PySide6.

    The function is intentionally a no-op for normal source execution.  It is
    safe to call more than once, which helps direct entry-point tests and
    avoids making callers care whether another bootstrap already ran.
    """

    global _CONFIGURED_ROOT
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return

    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    bundle_root = Path(meipass).resolve()
    # PyInstaller 6 onedir builds normally point ``_MEIPASS`` at the bundle's
    # ``_internal`` directory, while older bootloaders and a few launch modes
    # point at the application directory itself.  Resolve both layouts so the
    # bootstrap does not silently become a no-op after a bootloader upgrade.
    roots = [bundle_root]
    internal_root = bundle_root / "_internal"
    if internal_root.is_dir():
        roots.insert(0, internal_root)
    root = next(
        (candidate for candidate in roots if (candidate / "PySide6").is_dir()),
        roots[0],
    )
    if _CONFIGURED_ROOT == root:
        return

    # Do not register the whole ``_internal`` directory with
    # ``os.add_dll_directory``. PyInstaller may place a second copy of the
    # Microsoft C runtime there; registering it as a DLL directory can make
    # Windows choose that copy ahead of the Qt wheel's runtime and make
    # ``QtCore.pyd`` fail with ERROR_MOD_NOT_FOUND. The application root still
    # belongs on PATH for non-Qt bundled tools, but only the two binding
    # directories are safe AddDllDirectory entries.
    dll_candidates = (root / "PySide6", root / "shiboken6")
    path_candidates = (*dll_candidates, root)
    paths: list[str] = []
    seen: set[str] = set()
    for directory in path_candidates:
        if not directory.is_dir():
            continue
        path = os.fspath(directory)
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
        if directory in dll_candidates:
            try:
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(path))
            except (AttributeError, OSError):
                # ``PATH`` below still helps older Python/Windows combinations;
                # a missing optional directory must not prevent source startup.
                continue

    if paths:
        current_path = os.environ.get("PATH", "")
        existing = {
            os.path.normcase(os.path.normpath(part))
            for part in current_path.split(os.pathsep)
            if part
        }
        prefix = [path for path in paths
                  if os.path.normcase(os.path.normpath(path)) not in existing]
        if prefix:
            os.environ["PATH"] = os.pathsep.join(
                prefix + ([current_path] if current_path else [])
            )

    _CONFIGURED_ROOT = root
