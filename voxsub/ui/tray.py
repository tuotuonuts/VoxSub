"""系统托盘（M7 组件清单 #3）。

- 图标 + 菜单：模式快捷切换（A/B/C/D 单选）/ 开始·停止 / 显示主窗 / 设置 / 退出
- 双击托盘图标 → 显示主窗
- 环境无托盘时（isSystemTrayAvailable() False）create() 返回 None，不阻塞主程序。
- 与主窗松耦合：托盘只发信号（mode_changed / toggle_run_requested /
  show_main_requested / settings_requested / quit_requested），由 app.py 接线。
- 开机自启（QStandardPaths 启动项）按 M7 范围先实现占位接口，M9 发布前补齐。

信号（类体声明）:
    mode_changed(str)            模式切换（"a"/"b"/"c"/"d"）
    toggle_run_requested()       开始/停止
    show_main_requested()        显示主窗
    settings_requested()         打开设置
    quit_requested()             退出应用
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from voxsub.ui.main_window import MODE_INFO, MODE_ORDER
from voxsub.ui.i18n import language_manager, retranslate_widget_tree, tr


class TrayIcon(QSystemTrayIcon):
    """托盘图标：发射信号与主窗 / 应用联动（信号即契约，不直接碰窗口）。"""

    mode_changed = Signal(str)
    toggle_run_requested = Signal()
    show_main_requested = Signal()
    settings_requested = Signal()
    diagnostics_requested = Signal()
    quit_requested = Signal()

    def __init__(self, icon: QIcon, parent=None) -> None:
        super().__init__(icon, parent)
        self.setToolTip("语幕 VoxSub")
        self._running = False

        menu = QMenu()
        self._menu = menu
        # -- 模式子菜单（单选）--
        self.mode_group = QActionGroup(menu)
        self.mode_actions: dict[str, QAction] = {}
        mode_menu = menu.addMenu(tr("模式"))
        self._mode_menu = mode_menu
        for m in MODE_ORDER:
            info = MODE_INFO[m]
            act = QAction(f"{info['badge']}  {info['title']}", mode_menu, checkable=True)
            act.setData(m)
            self.mode_group.addAction(act)
            self.mode_actions[m] = act
            mode_menu.addAction(act)
        self.mode_group.setExclusive(True)
        self.mode_group.triggered.connect(self._on_mode_picked)

        menu.addSeparator()
        self.toggle_action = QAction("开始", menu)
        self.toggle_action.triggered.connect(self.toggle_run_requested.emit)
        menu.addAction(self.toggle_action)

        show_action = QAction(tr("显示主窗"), menu)
        self._show_action = show_action
        show_action.triggered.connect(self.show_main_requested.emit)
        menu.addAction(show_action)

        settings_action = QAction(tr("设置"), menu)
        self._settings_action = settings_action
        settings_action.triggered.connect(self.settings_requested.emit)
        menu.addAction(settings_action)

        diagnostics_action = QAction(tr("诊断与实时日志"), menu)
        self._diagnostics_action = diagnostics_action
        diagnostics_action.triggered.connect(self.diagnostics_requested.emit)
        menu.addAction(diagnostics_action)

        menu.addSeparator()
        quit_action = QAction(tr("退出"), menu)
        self._quit_action = quit_action
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_action)
        self.setContextMenu(menu)

        self.activated.connect(self._on_activated)
        language_manager.language_changed.connect(self._on_language_changed)
        self._on_language_changed(language_manager.language)

    # -- 状态同步（主窗 / app.py 回调入口）---------------------------------
    def set_mode_state(self, mode: str) -> None:
        self._mode = mode if mode in MODE_ORDER else "a"
        act = self.mode_actions.get(self._mode)
        if act is not None:
            act.setChecked(True)
        self.toggle_action.setEnabled(self._mode != "d")

    def set_running_state(self, running: bool) -> None:
        self._running = bool(running)
        self.toggle_action.setText(tr("停止") if running else tr("开始"))

    def _on_language_changed(self, _language: str) -> None:
        retranslate_widget_tree(self)
        self.setToolTip(tr("语幕 VoxSub"))
        self._mode_menu.setTitle(tr("模式"))
        for mode, action in self.mode_actions.items():
            info = MODE_INFO[mode]
            action.setText(f"{info['badge']}  {tr(info['title'])}")
        self._show_action.setText(tr("显示主窗"))
        self._settings_action.setText(tr("设置"))
        self._diagnostics_action.setText(tr("诊断与实时日志"))
        self._quit_action.setText(tr("退出"))
        self.toggle_action.setText(tr("停止") if self._running else tr("开始"))

    # -- 内部 ---------------------------------------------------------------
    def _on_mode_picked(self, action: QAction) -> None:
        mode = action.data()
        if mode:
            self.set_mode_state(mode)
            self.mode_changed.emit(mode)

    def _on_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self.show_main_requested.emit()

    # -- 预留（M9 按 DESIGN 落地，暂不越界实现）----------------------------
    def set_auto_start(self, enabled: bool) -> bool:
        """开机自启（QStandardPaths 启动项）占位。返回是否已应用。"""
        _ = enabled
        return False

    @staticmethod
    def create(icon: QIcon, parent=None) -> "TrayIcon | None":
        """无托盘环境返回 None（不拖垮主程序）。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = TrayIcon(icon, parent)
        tray.show()
        return tray
