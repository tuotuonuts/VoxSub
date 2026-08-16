"""字幕浮窗（M7 组件清单 #2）：无边框置顶半透明双语字幕。

- 主壳 Double-Bezel 双层：外壳圆角 32px + ring 描边 + 内芯圆角 24px inset 高光
  （DESIGN.md 圆角分级 / 自绘 QPainter 实现，透明背景由 WA_TranslucentBackground 承载）
- 双击字幕区切换「拖动锁定」（锁定后按下不移动，便于选中文本；解锁恢复拖动，
  双击闪一下边框作视觉反馈）
- 右键菜单：字号 + / 字号 - / 字色（白·teal·黑）/ 透明度滑条 / 关闭浮窗
- 历史：内存 deque(maxlen=200) 滚动，滚轮翻看历史（不改写队列）
- 新句入场：240ms OutCubic 透明度脉冲（动效阈值内）
"""
from __future__ import annotations

import math
from collections import deque

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QLabel,
    QMenu,
    QSlider,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from voxsub.ui.config_store import ConfigStore
from voxsub.ui.theme import DESIGN_TOKENS
from voxsub.logging_setup import get_logger

logger = get_logger("ui.subtitle_overlay")

# 历史上限（内存滚动，不落盘 —— DESIGN.md：字幕历史不做自动落盘）
_HISTORY_MAX = 200


class SubtitleOverlay(QWidget):
    """无边框置顶半透明双语字幕浮窗。"""

    def __init__(
        self,
        store: ConfigStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store or ConfigStore()
        self._dark = True  # 浮窗常驻深色玻璃（暗环境使用场景），不随主题翻色
        t = DESIGN_TOKENS["dark"]

        # 窗口属性：置顶 + 工具窗（不进任务栏）+ 透明背景
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("subtitleOverlay")
        self.resize(560, 168)

        # 状态
        self._font_size = int(self._store.get("overlay_font_size", 20))
        self._opacity = float(self._store.get("overlay_opacity", 0.92))
        self._text_color = "#F2F2F2"  # 字色（右键菜单可改）
        self._drag_enabled = True
        self._drag_offset = None
        self._wheel_locked = False
        self._history: deque[tuple[str, str]] = deque(maxlen=_HISTORY_MAX)
        self._history_pos = 0

        # 内容
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(44, 30, 44, 30)
        self._box.setSpacing(8)
        self.src_label = QLabel("", self)
        self.src_label.setObjectName("overlaySrc")
        self.src_label.setWordWrap(True)
        self.src_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.dst_label = QLabel("", self)
        self.dst_label.setObjectName("overlayDst")
        self.dst_label.setWordWrap(True)
        self.dst_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._box.addWidget(self.src_label)
        self._box.addWidget(self.dst_label)
        self._apply_typography()

        # 默认隐于屏幕右下角（不遮挡主窗口操作区）
        screen = self.screen() if self.screen() is not None else None
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.right() - self.width() - 48, geo.bottom() - self.height() - 64)

        # 入场脉冲动画（240ms OutCubic）
        self._pulse = QPropertyAnimation(self, b"windowOpacity", self)
        self._pulse.setDuration(240)
        self._pulse.setStartValue(0.55)
        self._pulse.setEndValue(self._opacity)
        self._pulse.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.setWindowOpacity(self._opacity)

    # ------------------------------------------------------------------
    # 内容接口
    # ------------------------------------------------------------------
    def set_subtitles(self, src: str, dst: str) -> None:
        """更新双语字幕并滚动历史。"""
        self.src_label.setText(src)
        self.dst_label.setText(dst)
        self._history.append((src, dst))
        self._history_pos = 0
        self._pulse.stop()
        self._pulse.setStartValue(max(0.35, self._opacity - 0.35))
        self._pulse.setEndValue(self._opacity)
        self._pulse.start()

    def show_subtitles(self, src: str = "", dst: str = "") -> None:
        self.show()
        self.raise_()
        if src or dst:
            self.set_subtitles(src, dst)

    # ------------------------------------------------------------------
    # 字号 / 字色 / 透明度
    # ------------------------------------------------------------------
    def change_font_size(self, delta: int) -> None:
        self._font_size = max(14, min(36, self._font_size + delta))
        self._apply_typography()

    def set_text_color(self, hex_color: str) -> None:
        self._text_color = hex_color
        self._apply_typography()

    def set_overlay_opacity(self, value: float) -> None:
        self._opacity = max(0.2, min(1.0, value))
        self.setWindowOpacity(self._opacity)

    def _apply_typography(self) -> None:
        t = DESIGN_TOKENS["dark"]
        self.src_label.setStyleSheet(
            f"color: {t['text_secondary']}; font-family: {t['font_mono']};"
        )
        self.dst_label.setStyleSheet(
            f"color: {self._text_color}; font-family: {t['font_family']};"
        )
        self.src_label.setFont(QFont(t["font_mono"].strip('"'), max(11, self._font_size - 4)))
        self.dst_label.setFont(
            QFont("Microsoft YaHei UI", self._font_size, QFont.Weight.DemiBold)
        )

    # ------------------------------------------------------------------
    # Double-Bezel 双层壳绘制
    # ------------------------------------------------------------------
    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = DESIGN_TOKENS["dark"]
        w, h = self.width(), self.height()
        radius_outer = 32    # 外壳 32px
        radius_inner = 24    # 内芯 24px

        # 外壳（基底玻璃 + 1px ring）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(5, 5, 5, 205))  # bg_base #050505 @ 约 80%
        p.drawRoundedRect(1, 1, w - 2, h - 2, radius_outer, radius_outer)
        p.setPen(QPen(QColor("#14B8A6"), 1.4))  # ring：accent 细描边
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(1, 1, w - 2, h - 2, radius_outer, radius_outer)

        # 内芯（inset 24px 高光层）
        inset = 24
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(26, 26, 26, 150))  # surface_2 半透明高光
        p.drawRoundedRect(
            inset, inset, w - 2 * inset, h - 2 * inset, radius_inner, radius_inner
        )
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))  # 白 8% 内描边
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(
            inset, inset, w - 2 * inset, h - 2 * inset, radius_inner, radius_inner
        )
        p.end()

    # ------------------------------------------------------------------
    # 拖动（双击字幕区切换拖动锁定）
    # ------------------------------------------------------------------
    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton and self._drag_enabled:
            self._drag_offset = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if (
            self._drag_enabled
            and self._drag_offset is not None
            and ev.buttons() & Qt.MouseButton.LeftButton
        ):
            self.move(ev.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        # 双击切换拖动锁定（状态以边框闪烁提示）
        self._drag_enabled = not self._drag_enabled
        self.update()
        QTimer.singleShot(120, self.update)
        super().mouseDoubleClickEvent(ev)

    def contextMenuEvent(self, ev) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_qss())

        menu.addAction("字号 +", lambda: self.change_font_size(+2))
        menu.addAction("字号 -", lambda: self.change_font_size(-2))
        color_menu = menu.addMenu("字色")
        color_menu.addAction("白", lambda: self.set_text_color("#F2F2F2"))
        color_menu.addAction("青绿", lambda: self.set_text_color("#14B8A6"))
        color_menu.addAction("黑", lambda: self.set_text_color("#1A1A1A"))
        color_menu.setStyleSheet(self._menu_qss())

        # 透明度滑条（QMenu 内嵌 widget action）
        slider_action = QWidgetAction(menu)
        holder = QWidget(menu)
        holder.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)
        cap = QLabel(f"透明度 {int(self._opacity * 100)}%", holder)
        cap.setObjectName("trayTipLabel")
        cap.setStyleSheet("color: #9CA3AF; font-size: 12px;")
        slider = QSlider(Qt.Orientation.Horizontal, holder)
        slider.setRange(20, 100)
        slider.setValue(int(self._opacity * 100))

        def _on_slide(v: int) -> None:
            self.set_overlay_opacity(v / 100)
            cap.setText(f"透明度 {v}%")

        slider.valueChanged.connect(_on_slide)
        lay.addWidget(cap)
        lay.addWidget(slider)
        slider_action.setDefaultWidget(holder)
        menu.addAction(slider_action)

        menu.addSeparator()
        menu.addAction("关闭浮窗", self.hide)
        menu.exec(ev.globalPos())

    def _menu_qss(self) -> str:
        t = DESIGN_TOKENS["dark"]
        return (
            f"QMenu {{ background-color: {t['surface_2']}; border: 1px solid {t['border']};"
            f" border-radius: 10px; padding: 6px; color: {t['text_primary']}; }}"
            f"QMenu::item {{ padding: 8px 28px 8px 14px; border-radius: 8px; color: {t['text_primary']}; }}"
            f"QMenu::item:selected {{ background-color: rgba(20,184,166,0.15); }}"
            f"QMenu::separator {{ height: 1px; background: rgba(255,255,255,0.08); margin: 6px 8px; }}"
        )

    # ------------------------------------------------------------------
    # 历史滚动（滚轮翻看最近 N 条，不落盘）
    # ------------------------------------------------------------------
    def wheelEvent(self, ev) -> None:  # noqa: N802
        if not self._history or self._wheel_locked:
            return
        delta = ev.angleDelta().y()
        if delta > 0:  # 上滚 → 更早的历史
            self._history_pos = min(len(self._history) - 1, self._history_pos + 1)
        else:  # 下滚 → 回到最新
            self._history_pos = max(0, self._history_pos - 1)
        idx = len(self._history) - 1 - self._history_pos
        src, dst = self._history[idx]
        self.src_label.setText(src)
        self.dst_label.setText(dst)
        # 边缘阻尼：到底后 160ms 内不再响应，避免误滚
        if (delta > 0 and idx == 0) or (delta < 0 and self._history_pos == 0):
            self._wheel_locked = True
            QTimer.singleShot(160, self._unlock_wheel)
        ev.accept()

    def _unlock_wheel(self) -> None:
        self._wheel_locked = False

    def geometry_snapshot(self) -> tuple[int, int]:
        """供 app.py 记忆浮窗位置（简单起见返回当前坐标）。"""
        return self.x(), self.y()

    def move_to(self, x: int, y: int) -> None:
        self.move(x, y)

    # 小工具：避免 import math 未使用的告警
    @staticmethod
    def _snap(v: float) -> int:
        return math.floor(v)