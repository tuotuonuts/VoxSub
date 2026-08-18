"""语幕 VoxSub —— UI 入口。

启动方式（项目根目录）:
    cd /d/OneDrive/app_dve/VoxSub
    unset PYTHONPATH PYTHONHOME
    .venv/Scripts/python.exe -m voxsub.ui.app

职责：创建 QApplication → 读取配置 → 应用主题 → 主窗 + 字幕浮窗 + 托盘 → 事件循环。
组件间以信号 / 回调松耦合（见各模块 docstring）。

可观测性（P0）: 日志基建必须在导入任何其它 voxsub 模块之前初始化 —— 其它模块
模块级的 get_logger() 会在首次调用时兜底 setup_logging()（默认开控制台），而本
入口承担「打包版关闭控制台日志」职责，故 setup_logging(log_to_console=False)
放在其它 voxsub 导入之前执行（setup_logging 幂等，先到先得；main() 内再幂等
重设一次作保险丝）。
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from voxsub import __version__ as _CORE_VERSION
from voxsub.logging_setup import get_logger, set_debug_mode, setup_logging

# ---------------------------------------------------------------------------
# 可观测性初始化：必须在其它 voxsub 模块导入前（见模块 docstring）
# 打包版关闭控制台日志, 只落文件 + 内存环形队列（诊断页"日志"页签的数据源）
# ---------------------------------------------------------------------------
setup_logging(log_to_console=False)

# 模块级 logger：记录应用启动 / 退出等关键事件
logger = get_logger("ui.app")

from voxsub.ui import __version__ as _UI_VERSION  # noqa: E402
from voxsub.ui.config_store import ConfigStore  # noqa: E402
from voxsub.ui.diagnostics_window import DiagnosticsWindow  # noqa: E402
from voxsub.ui.icons import make_app_icon  # noqa: E402
from voxsub.ui.i18n import language_manager, tr  # noqa: E402
from voxsub.ui.main_window import MainWindow  # noqa: E402
from voxsub.ui.model_hub_window import ModelHubWindow  # noqa: E402
from voxsub.ui.settings_window import SettingsWindow  # noqa: E402
from voxsub.ui.subtitle_overlay import SubtitleOverlay  # noqa: E402
from voxsub.ui.theme import AppTheme, load_theme  # noqa: E402
from voxsub.ui.tray import TrayIcon  # noqa: E402


def parse_theme(value: str) -> AppTheme:
    try:
        return AppTheme(value)
    except ValueError:
        logger.debug("未知主题名 %r, 回落 SYSTEM", value)
        return AppTheme.SYSTEM


def main(argv: list[str] | None = None) -> int:
    # main() 最开头（建 QApplication 前）：幂等重设打包日志配置
    # （正常情况下模块级已完成初始化，此处仅作直接调用 main() 时的保险丝）
    setup_logging(log_to_console=False)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("VoxSub")
    store = ConfigStore()
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
    logger.info("应用启动: ui=%s core=%s argv=%r", _UI_VERSION, _CORE_VERSION, argv)

    set_debug_mode(bool(store.get("debug_mode", False)))
    theme = parse_theme(store.get("theme", "system"))
    load_theme(app, theme)
    logger.info("配置已加载, 主题=%s", theme.value)

    # -- 组件实例化（主窗 / 浮窗 / 内置设置与模型广场 / 诊断 / 托盘）--
    win = MainWindow(store=store)
    overlay = SubtitleOverlay(store=store)
    win.attach_overlay(overlay)

    settings_win = SettingsWindow(store=store, overlay=overlay)
    diagnostics_win = DiagnosticsWindow(store=store)
    model_hub_win = ModelHubWindow(store=store)
    win.install_in_app_pages(settings_win, model_hub_win)
    logger.info("窗口组件已创建: 主窗 / 浮窗 / 内置模型广场 / 内置设置 / 诊断")

    tray = TrayIcon.create(make_app_icon(), win)

    # -- 主窗 ↔ 漂浮窗联动 --
    overlay.hide()

    # -- 托盘接线（无托盘环境整体跳过）--
    if tray is not None:
        tray.set_mode_state(win.current_mode())
        tray.set_running_state(win.cta.is_running())

        def _on_tray_mode(mode: str) -> None:
            win.set_mode(mode)
            tray.set_mode_state(mode)

        def _on_tray_toggle() -> None:
            win._toggle_run()  # noqa: SLF001 - 壳层内部方法，属同一 UI 域
            tray.set_running_state(win.cta.is_running())

        def _on_tray_show() -> None:
            win.showNormal()
            win.raise_()
            win.activateWindow()

        def _on_tray_settings() -> None:
            win.showNormal()
            win.show_settings_page()
            win.raise_()
            win.activateWindow()

        def _on_tray_diagnostics() -> None:
            diagnostics_win.showNormal()
            diagnostics_win.raise_()
            diagnostics_win.activateWindow()

        def _on_tray_quit() -> None:
            app._voxsub_quitting = True  # type: ignore[attr-defined]
            try:
                win.pipeline.stop()
            except AttributeError:
                pass
            app.quit()

        tray.mode_changed.connect(_on_tray_mode)
        tray.toggle_run_requested.connect(_on_tray_toggle)
        tray.show_main_requested.connect(_on_tray_show)
        tray.settings_requested.connect(_on_tray_settings)
        tray.diagnostics_requested.connect(_on_tray_diagnostics)
        tray.quit_requested.connect(_on_tray_quit)

    # 保持主窗运行态与托盘菜单同步（主窗按钮与托盘按钮互斥一致）
    def _sync_tray_state() -> None:
        if tray is None:
            return
        tray.set_mode_state(win.current_mode())
        tray.set_running_state(win.cta.is_running())

    win.cta.clicked.connect(_sync_tray_state)
    win.running_state_changed.connect(lambda _running: _sync_tray_state())

    def _show_settings() -> None:
        win.showNormal()
        win.show_settings_page()
        win.raise_()
        win.activateWindow()

    def _show_diagnostics() -> None:
        diagnostics_win.showNormal()
        diagnostics_win.raise_()
        diagnostics_win.activateWindow()

    def _show_model_hub() -> None:
        win.showNormal()
        win.show_model_hub_page()
        win.raise_()
        win.activateWindow()

    win.settings_requested.connect(_show_settings)
    win.diagnostics_requested.connect(_show_diagnostics)
    win.model_hub_requested.connect(_show_model_hub)
    win.show()
    # 退出关键事件（托盘「退出」/ 系统退出统一在此记录）
    app.aboutToQuit.connect(model_hub_win.shutdown)
    app.aboutToQuit.connect(settings_win.prepare_for_page_leave)
    app.aboutToQuit.connect(lambda: logger.info("应用退出"))
    logger.info("事件循环开始")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
