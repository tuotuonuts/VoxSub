"""语幕 VoxSub —— Qt application entry point.

Startup composition lives in :mod:`voxsub.ui.app_runtime`.  This module keeps
logging initialization ahead of all other VoxSub imports, then delegates the
application lifetime to ``ApplicationRuntime``.
"""
from __future__ import annotations

import sys

from voxsub.logging_setup import setup_logging

# The packaged application must initialize logging before importing modules
# that create module-level loggers or native runtime objects.
setup_logging(log_to_console=False)

from voxsub.ui.app_runtime import ApplicationRuntime  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Run one Qt application instance."""
    return ApplicationRuntime(argv).run()


if __name__ == "__main__":
    sys.exit(main())
