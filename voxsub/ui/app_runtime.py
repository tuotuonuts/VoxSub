"""Application runtime composition for the VoxSub Qt application.

This module owns application startup, window composition, signal wiring, and
shutdown registration. The module-level entry point remains in ``app.py``;
keeping the composition object here prevents the command-line entry point from
becoming a second god object.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from voxsub import __version__ as _CORE_VERSION
from voxsub import diagnostics as diagnostics_module
from voxsub.config_store import ConfigStore
from voxsub.error_reporting import initialize_error_reporting
from voxsub.logging_setup import get_logger, setup_logging
from voxsub.model_storage import initialize_model_storage
from voxsub.ui import __version__ as _UI_VERSION
from voxsub.ui.diagnostics_window import DiagnosticsWindow
from voxsub.ui.icons import make_app_icon
from voxsub.ui.i18n import language_manager, tr
from voxsub.ui.installer_shutdown import (
    InstallerShutdownBridge,
    RUNNING_MUTEX_NAME,
    SHUTDOWN_EVENT_NAME,
)
from voxsub.ui.main_window import MainWindow
from voxsub.ui.model_hub_window import ModelHubWindow
from voxsub.ui.navigation_controller import ApplicationNavigationController
from voxsub.ui.ocr_workspace import OcrWorkspace
from voxsub.ui.shutdown_coordinator import ApplicationShutdownCoordinator
from voxsub.ui.release_notes import show_release_notes_once
from voxsub.ui.settings_window import SettingsWindow
from voxsub.ui.subtitle_overlay import SubtitleOverlay
from voxsub.ui.theme import AppTheme, load_theme
from voxsub.ui.tray import TrayIcon


logger = get_logger("ui.app")


def parse_theme(value: str) -> AppTheme:
    """Parse a persisted theme value with a safe system-theme fallback."""
    try:
        return AppTheme(value)
    except ValueError:
        logger.debug("未知主题名 %r, 回落 SYSTEM", value)
        return AppTheme.SYSTEM


def _log_runtime_context(context: object) -> None:
    """Write the diagnostic runtime context without coupling the UI to its type."""
    logger.info(
        "运行上下文: version=%s build=%s environment=%s os=%s cpu=%s "
        "memory_gb=%s gpu=%s gpu_provider=%s npu=%s npu_provider=%s "
        "npu_driver=%s inference_backend=%s",
        context.version,
        context.build_id,
        context.environment,
        context.os_name,
        context.cpu,
        context.memory_gb,
        context.gpu or "none",
        context.gpu_provider or "none",
        context.npu or "none",
        context.npu_provider or "none",
        context.npu_driver or "unknown",
        context.inference_backend,
    )


class ApplicationRuntime:
    """Own the lifetime of one QApplication and its top-level components."""

    def __init__(self, argv: list[str] | None = None) -> None:
        self.argv = argv
        self.app: QApplication | None = None
        self._runtime_context = None

    def run(self) -> int:
        # main() 最开头（建 QApplication 前）：幂等重设打包日志配置
        # （正常情况下模块级已完成初始化，此处仅作直接调用 main() 时的保险丝）
        setup_logging(log_to_console=False)
        self._runtime_context = initialize_error_reporting()
        _log_runtime_context(self._runtime_context)
        app = QApplication(self.argv if self.argv is not None else sys.argv)
        self.app = app
        app.setApplicationName("VoxSub")

        # Create the installer shutdown bridge immediately after QApplication.
        # Model migration and UI construction can take several seconds on a cold
        # start; registering the named event only after those steps made the
        # installer believe VoxSub was unresponsive and fall back to taskkill.
        # Keeping the bridge alive under QApplication also lets a shutdown request
        # arrive while the remaining startup work is still being prepared.
        installer_shutdown = InstallerShutdownBridge(
            app,
            mutex_name=os.environ.get(
                "VOXSUB_INSTALLER_MUTEX", RUNNING_MUTEX_NAME),
            event_name=os.environ.get(
                "VOXSUB_INSTALLER_SHUTDOWN_EVENT", SHUTDOWN_EVENT_NAME),
        )

        store = ConfigStore()
        initialize_model_storage(store)
        language_manager.set_language(store.get("language", "system"))
        app.setApplicationDisplayName(tr("语幕 VoxSub", "VoxSub"))
        app.setOrganizationName("VoxSub")
        app.setWindowIcon(make_app_icon())
        app.setQuitOnLastWindowClosed(False)
        # 退出标志：主窗 closeEvent 依此区分「关闭窗口」与「退出应用」
        app._voxsub_quitting = False  # type: ignore[attr-defined]

        # 防止双击快捷方式产生两个实例并同时争用音频设备/轮转日志。
        lock_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub"
        lock_dir.mkdir(parents=True, exist_ok=True)
        # VOXSUB_INSTANCE_LOCK 仅供自动化冒烟/并行开发实例使用；Path.name 防止
        # 环境变量把锁文件写到应用数据目录之外。生产包不设置时仍严格单实例。
        lock_name = Path(os.environ.get("VOXSUB_INSTANCE_LOCK", "voxsub-app.lock")).name
        instance_lock = QLockFile(str(lock_dir / lock_name))
        instance_lock.setStaleLockTime(30_000)
        if not instance_lock.tryLock(100):
            logger.warning("检测到另一个 VoxSub 实例，当前实例退出")
            QMessageBox.information(
                None,
                tr("语幕 VoxSub", "VoxSub"),
                tr("语幕已经在运行，请查看任务栏或系统托盘。",
                   "VoxSub is already running. Check the taskbar or system tray."),
            )
            return 0
        logger.info("应用启动: ui=%s core=%s argv=%r", _UI_VERSION, _CORE_VERSION, self.argv)

        # Legacy ``debug_mode`` is intentionally not restored at startup.  Verbose
        # logging is now an explicit, auto-expiring diagnostics session.
        if store.get("debug_mode", False):
            store.set("debug_mode", False)
        theme = parse_theme(store.get("theme", "system"))
        load_theme(app, theme)
        logger.info("配置已加载, 主题=%s", theme.value)

        # -- 组件实例化（主窗 / 浮窗 / 内置设置与模型广场 / 诊断 / 托盘）--
        win = MainWindow(store=store)
        overlay = SubtitleOverlay(store=store)
        win.attach_overlay(overlay)

        settings_win = SettingsWindow(store=store, overlay=overlay)
        diagnostics_win = DiagnosticsWindow(
            store=store, diagnostics_module=diagnostics_module)
        model_hub_win = ModelHubWindow(store=store)
        ocr_workspace = OcrWorkspace(store=store)
        win.install_in_app_pages(settings_win, model_hub_win, ocr_workspace)
        settings_win.set_storage_change_guard(model_hub_win.has_active_downloads)
        settings_win.model_storage_changed.connect(
            lambda _root: model_hub_win.reload_model_storage())
        settings_win.model_storage_changed.connect(
            lambda _root: settings_win.refresh_tts_model_choices())
        model_hub_win.selection_changed.connect(
            lambda task, _model_id: (
                settings_win.refresh_tts_model_choices() if task == "tts" else None
            )
        )
        logger.info(
            "窗口组件已创建: 主窗 / 浮窗 / 内置模型广场 / 内置设置 / OCR / 诊断"
        )

        tray = TrayIcon.create(make_app_icon(), win)

        navigation = ApplicationNavigationController(
            app=app,
            main_window=win,
            settings_window=settings_win,
            diagnostics_window=diagnostics_win,
            tray=tray,
            installer_shutdown=installer_shutdown,
        )
        navigation.connect()
        # -- 主窗 ↔ 漂浮窗联动 --
        overlay.hide()

        win.show()
        QTimer.singleShot(0, lambda: show_release_notes_once(win, store, _UI_VERSION))
        shutdown = ApplicationShutdownCoordinator(
            app=app,
            model_hub=model_hub_win,
            ocr_workspace=ocr_workspace,
            settings_window=settings_win,
            pipeline=win.pipeline,
        )
        shutdown.connect()
        logger.info("事件循环开始")
        exit_code = app.exec()
        logger.info("事件循环结束: code=%s", exit_code)
        return exit_code
