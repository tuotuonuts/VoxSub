"""Run PyInstaller with a deterministic Windows DLL search path.

The desktop execution environment may prepend helper runtimes (for example
Poppler/ICU) to ``PATH``.  PyInstaller resolves native dependencies through
that path, so an unrelated ``icuuc.dll`` can be copied into the application
and later prevent PySide6 from loading QtCore.  Keep the isolation in this
small launcher instead of relying on the parent shell environment.
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def _deduplicate(paths: list[Path]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path.is_dir():
            continue
        normalized = os.path.normcase(os.path.normpath(os.fspath(path)))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(os.fspath(path))
    return result


def isolated_windows_path() -> str:
    """Return only system/Python directories needed while analysing the app."""
    system_root = Path(os.environ.get("SystemRoot", r"C:\\Windows"))
    executable_dir = Path(sys.executable).resolve().parent
    base_prefix = Path(sys.base_prefix).resolve()
    prefix = Path(sys.prefix).resolve()
    return os.pathsep.join(
        _deduplicate(
            [
                executable_dir,
                prefix,
                base_prefix,
                base_prefix / "DLLs",
                system_root / "System32",
                system_root,
            ]
        )
    )


def main() -> None:
    if sys.platform == "win32":
        # These variables can point to a different interpreter/runtime in
        # the host environment. Python has already started, so clearing them
        # here only affects PyInstaller dependency discovery.
        os.environ.pop("PYTHONPATH", None)
        os.environ.pop("PYTHONHOME", None)
        os.environ["PATH"] = isolated_windows_path()
    runpy.run_module("PyInstaller.__main__", run_name="__main__")


if __name__ == "__main__":
    main()
