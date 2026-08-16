"""语幕 VoxSub —— UI 入口。

启动方式（项目根目录）:
    cd /d/OneDrive/app_dve/VoxSub
    unset PYTHONPATH PYTHONHOME
    .venv/Scripts/python.exe -m voxsub.ui.app

职责：创建 QApplication → 读取配置 → 应用主题 → 主窗 + 字幕浮窗 + 托盘 → 事件循环。
组件间以信号 / 回调松耦合（见各模块 docstring）。
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from voxsub.ui.config_store import ConfigStore
from voxsub.ui.diagnostics_window import DiagnosticsWindow
from voxsub.ui.icons import make_app_icon
from voxsub.ui.main_window import MainWindow
from voxsub.ui.settings_window import SettingsWindow
from voxsub.ui.subtitle_overlay import SubtitleOverlay
from voxsub.ui.theme import AppTheme, load_theme
from voxsub.ui.tray import TrayIcon


def parse_theme(value: str) -> AppTheme:
    try:
        return AppTheme(value)
    except ValueError:
        return AppTheme.SYSTEM


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("VoxSub")
    app.setApplicationDisplayName("语幕 VoxSub")
    app.setOrganizationName("VoxSub")
    app.setWindowIcon(make_app_icon())
    # 退出标志：主窗 closeEvent 依此区分「关闭窗口」与「退出应用」
    app._voxsub_quitting = False  # type: ignore[attr-defined]

    store = ConfigStore()
    load_theme(app, parse_theme(store.get("theme", "system")))

    # -- 组件实例化（主窗 / 浮窗 / 设置 / 诊断 / 托盘）--
    win = MainWindow(store=store)
    overlay = SubtitleOverlay(store=store)
    win.attach_overlay(overlay)

    settings_win = SettingsWindow(store=store)
    diagnostics_win = DiagnosticsWindow()

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
            settings_win.showNormal()
            settings_win.raise_()
            settings_win.activateWindow()

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
        tray.quit_requested.connect(_on_tray_quit)

    # 保持主窗运行态与托盘菜单同步（主窗按钮与托盘按钮互斥一致）
    def _sync_tray_state() -> None:
        if tray is None:
            return
        tray.set_mode_state(win.current_mode())
        tray.set_running_state(win.cta.is_running())

    win.cta.clicked.connect(_sync_tray_state)

    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())