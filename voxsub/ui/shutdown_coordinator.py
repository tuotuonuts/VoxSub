"""Application shutdown coordination for the VoxSub Qt runtime.

The coordinator keeps the application's teardown order in one small, testable
object.  It deliberately depends on duck-typed collaborators instead of Qt
classes so the lifecycle contract can be tested without starting a GUI.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from voxsub.error_reporting import shutdown_error_reporting
from voxsub.logging_setup import get_logger


logger = get_logger("ui.shutdown")


class ApplicationShutdownCoordinator:
    """Register and own the ordered shutdown callbacks for the application.

    The order is intentional and matches the pre-refactor runtime:

    1. stop model-hub downloads/workers;
    2. stop OCR workspace workers;
    3. let Settings persist/leave its page safely;
    4. close the main pipeline;
    5. stop error reporting;
    6. record that application teardown started.

    ``aboutToQuit`` is used instead of calling these methods directly from the
    navigation controller so every exit path (tray, installer, OS/session)
    shares the same lifecycle contract.
    """

    def __init__(
        self,
        *,
        app: Any,
        model_hub: Any,
        ocr_workspace: Any,
        settings_window: Any,
        pipeline: Any,
        error_reporting_shutdown: Callable[[], None] = shutdown_error_reporting,
    ) -> None:
        self._app = app
        self._model_hub = model_hub
        self._ocr_workspace = ocr_workspace
        self._settings_window = settings_window
        self._pipeline = pipeline
        self._error_reporting_shutdown = error_reporting_shutdown
        self._connected = False

    def connect(self) -> None:
        """Connect teardown callbacks once, preserving their required order."""
        if self._connected:
            return

        about_to_quit = self._app.aboutToQuit
        about_to_quit.connect(self._model_hub.shutdown)
        about_to_quit.connect(self._ocr_workspace.shutdown)
        about_to_quit.connect(self._settings_window.prepare_for_page_leave)
        about_to_quit.connect(self._close_pipeline)
        about_to_quit.connect(self._shutdown_error_reporting)
        about_to_quit.connect(self._log_application_exit)
        self._connected = True

    def _close_pipeline(self) -> None:
        """Release pipeline-owned native workers during application teardown."""
        close = getattr(self._pipeline, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            logger.exception("应用退出时关闭 Pipeline 失败")

    def _shutdown_error_reporting(self) -> None:
        try:
            self._error_reporting_shutdown()
        except Exception:
            # Error reporting must not prevent the remaining Qt shutdown path
            # from completing.  The callback is wrapped for the same reason
            # the pipeline close is wrapped above.
            logger.exception("应用退出时关闭错误报告失败")

    @staticmethod
    def _log_application_exit() -> None:
        logger.info("应用退出")
