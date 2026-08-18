"""字幕浮窗（M7 组件清单 #2）：无边框置顶半透明双语字幕。

- 主壳 Double-Bezel 双层：外壳圆角 32px + ring 描边 + 内芯圆角 24px inset 高光
  （DESIGN.md 圆角分级 / 自绘 QPainter 实现，透明背景由 WA_TranslucentBackground 承载）
- 悬停显示精简工具条：字号 - / + / 锁定穿透 / 关闭；锁定时由
  独立的悬停控制条只保留解锁入口，其余区域仍可鼠标穿透
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
    QRect,
    Signal,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from voxsub.ui.config_store import ConfigStore
from voxsub.ui.i18n import language_manager, retranslate_widget_tree, tr
from voxsub.ui.theme import DESIGN_TOKENS
from voxsub.logging_setup import get_logger

logger = get_logger("ui.subtitle_overlay")

# 历史上限（内存滚动，不落盘 —— DESIGN.md：字幕历史不做自动落盘）
_HISTORY_MAX = 200
_MIN_WIDTH = 360
_MIN_HEIGHT = 132
_RESIZE_MARGIN = 12


class _LockedHoverPanel(QWidget):
    """Small top-level control island that remains clickable over a locked overlay.

    A click-through native window cannot receive hover or mouse events by
    definition.  Keeping this control island in a separate Tool window lets the
    subtitle body stay transparent to input while preserving an in-place unlock
    path, matching desktop lyric overlays.
    """

    def __init__(self, overlay: "SubtitleOverlay") -> None:
        super().__init__(None)
        self._overlay = overlay
        self.setObjectName("overlayLockedPanel")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        row = QHBoxLayout(self)
        row.setContentsMargins(7, 4, 7, 4)
        row.setSpacing(2)

        self.unlock = self._button("解锁", "解锁浮窗，恢复拖动和文字选择", 80)
        self.unlock.clicked.connect(lambda: overlay.set_click_through(False))
        row.addWidget(self.unlock)
        self.setFixedSize(100, 36)
        self.hide()
        language_manager.language_changed.connect(self._on_language_changed)
        self._on_language_changed(language_manager.language)

    def _button(self, text: str, tooltip: str, width: int) -> QToolButton:
        button = QToolButton(self)
        button.setText(tr(text))
        button.setToolTip(tr(tooltip))
        button.setFixedSize(width, 28)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _on_language_changed(self, _language: str) -> None:
        retranslate_widget_tree(self)


class SubtitleOverlay(QWidget):
    """无边框置顶半透明双语字幕浮窗。"""

    lock_changed = Signal(bool)

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
        self.setMinimumSize(_MIN_WIDTH, _MIN_HEIGHT)
        saved_width = max(_MIN_WIDTH, int(self._store.get("overlay_width", 560)))
        saved_height = max(_MIN_HEIGHT, int(self._store.get("overlay_height", 168)))
        self.resize(saved_width, saved_height)
        self.setMouseTracking(True)

        # 状态
        self._font_size = int(self._store.get("overlay_font_size", 20))
        self._opacity = float(self._store.get("overlay_opacity", 0.92))
        self._text_color = "#F2F2F2"  # 字色（右键菜单可改）
        self._click_through = bool(self._store.get("overlay_click_through", False))
        self._drag_offset = None
        self._resize_edges: tuple[bool, bool, bool, bool] | None = None
        self._resize_start_geometry: QRect | None = None
        self._resize_start_pos = None
        self._manual_size = bool(self._store.get("overlay_size_customized", False))
        self._wheel_locked = False
        self._history: deque[tuple[str, str]] = deque(maxlen=_HISTORY_MAX)
        self._history_pos = 0

        # 内容
        self._box = QVBoxLayout(self)
        # Reserve a dedicated top lane for the hover toolbar so it never
        # covers the first subtitle line, even at the minimum overlay height.
        self._box.setContentsMargins(44, 48, 44, 28)
        self._box.setSpacing(8)
        self.src_label = QLabel("", self)
        self.src_label.setObjectName("overlaySrc")
        self.src_label.setWordWrap(True)
        self.src_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.src_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.src_label.setCursor(Qt.CursorShape.IBeamCursor)
        self.src_label.setMouseTracking(True)
        self.dst_label = QLabel("", self)
        self.dst_label.setObjectName("overlayDst")
        self.dst_label.setWordWrap(True)
        self.dst_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.dst_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.dst_label.setCursor(Qt.CursorShape.IBeamCursor)
        self.dst_label.setMouseTracking(True)
        self._box.addWidget(self.src_label)
        self._box.addWidget(self.dst_label)
        self._apply_typography()
        self._build_hover_toolbar()
        self._locked_panel = _LockedHoverPanel(self)
        language_manager.language_changed.connect(self._on_language_changed)
        self._locked_hover_timer = QTimer(self)
        self._locked_hover_timer.setInterval(90)
        self._locked_hover_timer.timeout.connect(self._poll_locked_hover)
        self._locked_hover_timer.start()

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
        self._apply_click_through_flag(initial=True)

    def _build_hover_toolbar(self) -> None:
        """NetEase-style compact controls shown only while the overlay is hovered."""
        self._toolbar = QFrame(self)
        self._toolbar.setObjectName("overlayToolbar")
        row = QHBoxLayout(self._toolbar)
        row.setContentsMargins(7, 4, 7, 4)
        row.setSpacing(2)
        self._font_down_btn = QToolButton(self._toolbar)
        self._font_down_btn.setObjectName("overlayFontDown")
        self._font_value_label = QLabel(str(self._font_size), self._toolbar)
        self._font_value_label.setObjectName("overlayFontValue")
        self._font_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._font_value_label.setFixedWidth(28)
        self._font_up_btn = QToolButton(self._toolbar)
        self._font_up_btn.setObjectName("overlayFontUp")
        self._lock_btn = QToolButton(self._toolbar)
        self._close_btn = QToolButton(self._toolbar)
        controls = (
            (self._font_down_btn, "A−", "减小字号", lambda: self.change_font_size(-2), 76),
            (self._font_up_btn, "A+", "增大字号", lambda: self.change_font_size(+2), 76),
            (self._lock_btn, "锁定", "锁定并让鼠标点击穿过浮窗",
             lambda: self.set_click_through(True), 80),
            (self._close_btn, "关闭", "暂时关闭字幕浮窗", self.hide, 80),
        )
        row.addWidget(self._font_down_btn)
        row.addWidget(self._font_value_label)
        for button, text, tip, action, width in controls:
            if button is not self._font_down_btn:
                row.addWidget(button)
            button.setText(tr(text))
            button.setFixedSize(width, 28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tr(tip))
            button.clicked.connect(action)
        self._toolbar.adjustSize()
        self._toolbar.hide()

    def _on_language_changed(self, _language: str) -> None:
        retranslate_widget_tree(self)
        self._locked_panel._on_language_changed(_language)

    def font_size(self) -> int:
        return self._font_size

    def is_click_through(self) -> bool:
        return self._click_through

    def set_click_through(self, locked: bool) -> None:
        locked = bool(locked)
        if locked == self._click_through:
            return
        self._click_through = locked
        self._store.set("overlay_click_through", locked)
        self._apply_click_through_flag()
        self.lock_changed.emit(locked)

    def _apply_click_through_flag(self, initial: bool = False) -> None:
        """Toggle native input transparency while preserving geometry/visibility."""
        was_visible = self.isVisible()
        pos = self.pos()
        transparent = Qt.WindowType.WindowTransparentForInput
        # Do not rebuild the complete flag mask with ``setWindowFlags`` here.
        # On Windows/Qt 6 that path can retain WindowTransparentForInput (and
        # therefore WS_EX_TRANSPARENT) when the bit is removed, even though our
        # Python state already says "unlocked".  Toggling the one flag through
        # setWindowFlag reliably updates both Qt and the native HWND style.
        mouse_transparent = Qt.WidgetAttribute.WA_TransparentForMouseEvents
        if self._click_through:
            self.setWindowFlag(transparent, True)
            self.setAttribute(mouse_transparent, True)
        else:
            # Clear the QWidget attribute first.  Qt refuses to remove the
            # native input-transparent window flag while the widget itself is
            # still marked transparent for mouse events.
            self.setAttribute(mouse_transparent, False)
            self.setWindowFlag(transparent, False)
        self._toolbar.hide()
        if not self._click_through:
            self._locked_panel.hide()
        self.move(pos)
        if was_visible and not initial:
            self.show()
            self.raise_()
            if not self._click_through:
                QTimer.singleShot(0, self._show_unlocked_toolbar_if_hovered)

    def _show_unlocked_toolbar_if_hovered(self) -> None:
        if (not self._click_through and self.isVisible()
                and self.frameGeometry().contains(QCursor.pos())):
            self._toolbar.show()
            self._toolbar.raise_()

    def _position_locked_panel(self) -> None:
        geo = self.frameGeometry()
        panel = self._locked_panel
        panel.move(
            geo.left() + max(8, (geo.width() - panel.width()) // 2),
            geo.top() + 8,
        )

    def _poll_locked_hover(self, cursor_pos=None) -> None:
        """Expose the companion controls while the cursor crosses a locked overlay."""
        if not self._click_through or not self.isVisible():
            self._locked_panel.hide()
            return
        cursor = cursor_pos if cursor_pos is not None else QCursor.pos()
        over_overlay = self.frameGeometry().contains(cursor)
        over_panel = (self._locked_panel.isVisible()
                      and self._locked_panel.frameGeometry().contains(cursor))
        if over_overlay or over_panel:
            self._position_locked_panel()
            if not self._locked_panel.isVisible():
                self._locked_panel.show()
            self._locked_panel.raise_()
        else:
            self._locked_panel.hide()

    def clear_history(self) -> None:
        self._history.clear()
        self._history_pos = 0
        self.src_label.clear()
        self.dst_label.clear()
        self.hide()

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
        QTimer.singleShot(0, self._fit_height_to_text)

    def set_partial(self, src: str, dst: str | None = None) -> None:
        """Update temporary text without polluting the scroll-back history."""
        self.src_label.setText(src)
        self.dst_label.setText(dst if dst is not None else tr("识别中…"))
        QTimer.singleShot(0, self._fit_height_to_text)

    def show_subtitles(self, src: str = "", dst: str = "") -> None:
        self.show()
        self.raise_()
        if src or dst:
            self.set_subtitles(src, dst)

    # ------------------------------------------------------------------
    # 字号 / 字色 / 透明度
    # ------------------------------------------------------------------
    def change_font_size(self, delta: int) -> None:
        self._font_size = max(10, min(72, self._font_size + delta))
        self._store.set("overlay_font_size", self._font_size)
        self._font_value_label.setText(str(self._font_size))
        self._apply_typography()
        QTimer.singleShot(0, self._fit_height_to_text)

    def set_text_color(self, hex_color: str) -> None:
        self._text_color = hex_color
        self._apply_typography()

    def set_overlay_opacity(self, value: float) -> None:
        self._opacity = max(0.2, min(1.0, value))
        self._store.set("overlay_opacity", self._opacity)
        self.setWindowOpacity(self._opacity)

    def _apply_typography(self) -> None:
        t = DESIGN_TOKENS["dark"]
        src_size = max(8, self._font_size - 4)
        # The application stylesheet defines QWidget { font-size: 14px; }.
        # A bare setFont() is overridden by that QSS rule, which previously made
        # the counter change while the rendered subtitle stayed at 14px.  Give
        # the two labels explicit local QSS sizes so they win the cascade.
        self.src_label.setStyleSheet(
            f"color: {t['text_secondary']};"
            f" font-family: 'Segoe UI Variable'; font-size: {src_size}pt;"
            " font-weight: 400;"
        )
        self.dst_label.setStyleSheet(
            f"color: {self._text_color};"
            f" font-family: 'Microsoft YaHei UI'; font-size: {self._font_size}pt;"
            " font-weight: 600;"
        )

    def _fit_height_to_text(self) -> None:
        self._box.activate()
        desired = max(_MIN_HEIGHT, min(600, self.sizeHint().height() + 8))
        if self._manual_size:
            # Keep a user-selected height stable, but grow when a larger font
            # would otherwise be clipped.
            if desired > self.height():
                self.resize(self.width(), desired)
            return
        if abs(self.height() - desired) >= 2:
            self.resize(self.width(), desired)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        if hasattr(self, "_toolbar"):
            self._toolbar.adjustSize()
            self._toolbar.move(max(12, (self.width() - self._toolbar.width()) // 2), 8)
        if hasattr(self, "_locked_panel") and self._locked_panel.isVisible():
            self._position_locked_panel()

    def moveEvent(self, ev) -> None:  # noqa: N802
        super().moveEvent(ev)
        if hasattr(self, "_locked_panel") and self._locked_panel.isVisible():
            self._position_locked_panel()

    def enterEvent(self, ev) -> None:  # noqa: N802
        if not self._click_through:
            self._toolbar.show()
            self._toolbar.raise_()
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._toolbar.hide()
        super().leaveEvent(ev)

    def hideEvent(self, ev) -> None:  # noqa: N802
        if hasattr(self, "_locked_panel"):
            self._locked_panel.hide()
        super().hideEvent(ev)

    def closeEvent(self, ev) -> None:  # noqa: N802
        if hasattr(self, "_locked_panel"):
            self._locked_panel.close()
        super().closeEvent(ev)

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
    # 拖动与自由调整大小（锁定时由原生窗口标志实现鼠标穿透）
    # ------------------------------------------------------------------
    def _resize_edges_for_pos(self, pos) -> tuple[bool, bool, bool, bool] | None:
        x, y = pos.x(), pos.y()
        margin = _RESIZE_MARGIN
        left = 0 <= x <= margin
        right = self.width() - margin <= x < self.width()
        top = 0 <= y <= margin
        bottom = self.height() - margin <= y < self.height()
        return (left, right, top, bottom) if left or right or top or bottom else None

    @staticmethod
    def _resize_cursor(edges: tuple[bool, bool, bool, bool] | None):
        if edges is None:
            return Qt.CursorShape.ArrowCursor
        left, right, top, bottom = edges
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeVerCursor

    def _persist_size(self) -> None:
        self._store.update({
            "overlay_width": self.width(),
            "overlay_height": self.height(),
            "overlay_size_customized": True,
        })

    def _resize_from_global(self, global_pos) -> None:
        if self._resize_start_geometry is None or self._resize_edges is None:
            return
        start = self._resize_start_geometry
        delta = global_pos - self._resize_start_pos
        left, right, top, bottom = self._resize_edges
        x, y, width, height = start.left(), start.top(), start.width(), start.height()
        if left:
            x += delta.x()
            width -= delta.x()
        elif right:
            width += delta.x()
        if top:
            y += delta.y()
            height -= delta.y()
        elif bottom:
            height += delta.y()
        if width < _MIN_WIDTH:
            if left:
                x = start.right() - _MIN_WIDTH + 1
            width = _MIN_WIDTH
        if height < _MIN_HEIGHT:
            if top:
                y = start.bottom() - _MIN_HEIGHT + 1
            height = _MIN_HEIGHT
        self.setGeometry(QRect(x, y, width, height))

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton and not self._click_through:
            edges = self._resize_edges_for_pos(ev.position().toPoint())
            if edges is not None:
                self._manual_size = True
                self._resize_edges = edges
                self._resize_start_geometry = self.frameGeometry()
                self._resize_start_pos = ev.globalPosition().toPoint()
                ev.accept()
                return
            self._drag_offset = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if self._resize_edges is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            self._resize_from_global(ev.globalPosition().toPoint())
            ev.accept()
            return
        if (not self._click_through and self._drag_offset is not None
                and ev.buttons() & Qt.MouseButton.LeftButton):
            self.move(ev.globalPosition().toPoint() - self._drag_offset)
        elif not self._click_through:
            self.setCursor(self._resize_cursor(
                self._resize_edges_for_pos(ev.position().toPoint())))
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if self._resize_edges is not None:
            self._persist_size()
            self._resize_edges = None
            self._resize_start_geometry = None
            self._resize_start_pos = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            ev.accept()
            return
        self._drag_offset = None
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        # Double-click no longer changes an invisible state; lock/unlock is an
        # explicit toolbar/main-window action so text selection stays predictable.
        super().mouseDoubleClickEvent(ev)

    def contextMenuEvent(self, ev) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_qss())

        menu.addAction(tr("字号 +"), lambda: self.change_font_size(+2))
        menu.addAction(tr("字号 -"), lambda: self.change_font_size(-2))
        menu.addAction(tr("锁定并允许点击穿透"), lambda: self.set_click_through(True))
        color_menu = menu.addMenu(tr("字色"))
        color_menu.addAction(tr("白"), lambda: self.set_text_color("#F2F2F2"))
        color_menu.addAction(tr("青绿"), lambda: self.set_text_color("#14B8A6"))
        color_menu.addAction(tr("黑"), lambda: self.set_text_color("#1A1A1A"))
        color_menu.setStyleSheet(self._menu_qss())

        # 透明度滑条（QMenu 内嵌 widget action）
        slider_action = QWidgetAction(menu)
        holder = QWidget(menu)
        holder.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)
        cap = QLabel(f"{tr('透明度')} {int(self._opacity * 100)}%", holder)
        cap.setObjectName("trayTipLabel")
        cap.setStyleSheet("color: #9CA3AF; font-size: 12px;")
        slider = QSlider(Qt.Orientation.Horizontal, holder)
        slider.setRange(20, 100)
        slider.setValue(int(self._opacity * 100))

        def _on_slide(v: int) -> None:
            self.set_overlay_opacity(v / 100)
            cap.setText(f"{tr('透明度')} {v}%")

        slider.valueChanged.connect(_on_slide)
        lay.addWidget(cap)
        lay.addWidget(slider)
        slider_action.setDefaultWidget(holder)
        menu.addAction(slider_action)

        menu.addSeparator()
        menu.addAction(tr("关闭浮窗"), self.hide)
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
