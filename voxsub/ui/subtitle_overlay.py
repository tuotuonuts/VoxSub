"""字幕浮窗（M7 组件清单 #2）：无边框置顶半透明双语字幕。

- 主壳 Double-Bezel 双层：外壳圆角 32px + ring 描边 + 内芯圆角 24px inset 高光
  （DESIGN.md 圆角分级 / 自绘 QPainter 实现，透明背景由 WA_TranslucentBackground 承载）
- 悬停显示精简工具条：字号、显示模式、内容边距、原译间距、锁定和关闭；锁定时由
  独立的悬停控制条只保留解锁入口，其余区域仍可鼠标穿透
- 右键菜单：字号 + / 字号 - / 字色（白·teal·黑）/ 透明度滑条 / 关闭浮窗
- 内容区保持用户指定尺寸，长句在窗口内换行并滚动，不再把浮窗扩到屏幕外
- 历史：内存 deque(maxlen=200)，Ctrl + 滚轮翻看历史（不改写队列）
- 新句入场：240ms OutCubic 透明度脉冲（动效阈值内）
"""
from __future__ import annotations

import math
from collections import deque

from PySide6.QtCore import (
    QEvent,
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
    QLayout,
    QMenu,
    QScrollArea,
    QSlider,
    QSizePolicy,
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
_MIN_WIDTH = 400
_MIN_HEIGHT = 88
_RESIZE_MARGIN = 12
_PADDING_MIN = 8
_PADDING_MAX = 64
_LINE_GAP_MIN = 0
_LINE_GAP_MAX = 40


class _SubtitleScrollArea(QScrollArea):
    """Fixed subtitle viewport; normal wheel scrolls, Ctrl-wheel opens history."""

    history_requested = Signal(int)

    def wheelEvent(self, ev) -> None:  # noqa: N802
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.history_requested.emit(ev.angleDelta().y())
            ev.accept()
            return
        super().wheelEvent(ev)


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
        saved_height = max(_MIN_HEIGHT, int(self._store.get("overlay_height", 132)))
        self.resize(saved_width, saved_height)
        self.setMouseTracking(True)

        # 状态
        self._font_size = int(self._store.get("overlay_font_size", 20))
        self._opacity = float(self._store.get("overlay_opacity", 0.92))
        self._text_color = "#F2F2F2"  # 字色（右键菜单可改）
        mode = str(self._store.get("overlay_display_mode", "bilingual"))
        self._display_mode = (
            mode if mode in {"bilingual", "source", "translation"} else "bilingual"
        )
        self._content_padding = max(
            _PADDING_MIN,
            min(_PADDING_MAX, int(self._store.get("overlay_content_padding", 18))),
        )
        self._line_gap = max(
            _LINE_GAP_MIN,
            min(_LINE_GAP_MAX, int(self._store.get("overlay_line_gap", 6))),
        )
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
        self._box.setContentsMargins(
            self._content_padding,
            self._content_padding,
            self._content_padding,
            self._content_padding,
        )
        self._box.setSpacing(0)
        self._content_scroll = _SubtitleScrollArea(self)
        self._content_scroll.setObjectName("overlayContentScroll")
        self._content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content_scroll.setWidgetResizable(True)
        self._content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._content_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._content_scroll.setStyleSheet(
            "QScrollArea#overlayContentScroll { background: transparent; border: none; }"
            "QScrollArea#overlayContentScroll > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 5px; margin: 1px 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,0.24);"
            " border-radius: 2px; min-height: 18px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
            " background: transparent; }"
        )
        self._content_widget = QWidget(self._content_scroll)
        self._content_widget.setObjectName("overlayContent")
        self._content_widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._content_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._content_box = QVBoxLayout(self._content_widget)
        self._content_box.setContentsMargins(0, 0, 0, 0)
        self._content_box.setSpacing(self._line_gap)
        self._content_box.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self.src_label = QLabel("", self._content_widget)
        self.src_label.setObjectName("overlaySrc")
        self.src_label.setWordWrap(True)
        self.src_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.src_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.src_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.src_label.setCursor(Qt.CursorShape.IBeamCursor)
        self.src_label.setMouseTracking(True)
        self.dst_label = QLabel("", self._content_widget)
        self.dst_label.setObjectName("overlayDst")
        self.dst_label.setWordWrap(True)
        self.dst_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.dst_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.dst_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.dst_label.setCursor(Qt.CursorShape.IBeamCursor)
        self.dst_label.setMouseTracking(True)
        self._content_box.addWidget(self.src_label)
        self._content_box.addWidget(self.dst_label)
        self._content_scroll.setWidget(self._content_widget)
        self._content_scroll.history_requested.connect(self._step_history)
        self._box.addWidget(self._content_scroll)
        self._apply_typography()
        self._build_hover_toolbar()
        self._apply_display_mode()
        self._locked_panel = _LockedHoverPanel(self)
        self._toolbar_hide_timer = QTimer(self)
        self._toolbar_hide_timer.setSingleShot(True)
        self._toolbar_hide_timer.setInterval(260)
        self._toolbar_hide_timer.timeout.connect(self._hide_toolbar_if_cursor_left)
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
        # Keep controls outside the subtitle surface.  This prevents the
        # toolbar from consuming subtitle height and matches desktop lyric
        # overlays: hovering the content reveals a small control island above.
        self._toolbar = QFrame(None)
        self._toolbar.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._toolbar.setMouseTracking(True)
        self._toolbar.installEventFilter(self)
        self._toolbar.setObjectName("overlayToolbar")
        toolbar_box = QVBoxLayout(self._toolbar)
        toolbar_box.setContentsMargins(6, 4, 6, 4)
        toolbar_box.setSpacing(2)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self._font_down_btn = QToolButton(self._toolbar)
        self._font_down_btn.setObjectName("overlayFontDown")
        self._font_value_label = QLabel(str(self._font_size), self._toolbar)
        self._font_value_label.setObjectName("overlayFontValue")
        self._font_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._font_value_label.setFixedWidth(26)
        self._font_up_btn = QToolButton(self._toolbar)
        self._font_up_btn.setObjectName("overlayFontUp")
        self._display_btn = QToolButton(self._toolbar)
        self._display_btn.setObjectName("overlayDisplayMode")
        self._display_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._display_menu = QMenu(self._display_btn)
        self._display_menu.setStyleSheet(self._menu_qss())
        self._display_actions = {}
        for display_mode in ("source", "translation", "bilingual"):
            action = self._display_menu.addAction("")
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, selected=display_mode: self.set_display_mode(selected)
            )
            self._display_actions[display_mode] = action
        self._display_btn.setMenu(self._display_menu)
        self._spacing_btn = QToolButton(self._toolbar)
        self._spacing_btn.setObjectName("overlaySpacingToggle")
        self._spacing_btn.setCheckable(True)
        self._lock_btn = QToolButton(self._toolbar)
        self._close_btn = QToolButton(self._toolbar)
        controls = (
            (self._font_down_btn, "A−", "减小字号", lambda: self.change_font_size(-2), 60),
            (self._font_up_btn, "A+", "增大字号", lambda: self.change_font_size(+2), 60),
            (self._spacing_btn, "间距", "调整内容边距和原译间距",
             self._toggle_spacing_controls, 56),
            (self._lock_btn, "锁定", "锁定并让鼠标点击穿过浮窗",
             lambda: self.set_click_through(True), 56),
            (self._close_btn, "×", "暂时关闭字幕浮窗", self.hide, 42),
        )
        row.addWidget(self._font_down_btn)
        row.addWidget(self._font_value_label)
        row.addWidget(self._font_up_btn)
        row.addWidget(self._display_btn)
        row.addWidget(self._spacing_btn)
        row.addWidget(self._lock_btn)
        row.addWidget(self._close_btn)
        for button, text, tip, action, width in controls:
            button.setText(tr(text))
            button.setStyleSheet("QToolButton { padding: 0; }")
            button.setFixedSize(width, 28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tr(tip))
            button.clicked.connect(action)
        self._display_btn.setFixedSize(66, 28)
        self._display_btn.setStyleSheet("QToolButton { padding: 0 5px; }")
        self._display_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._spacing_controls = QFrame(self._toolbar)
        self._spacing_controls.setObjectName("overlaySpacingControls")
        spacing_row = QHBoxLayout(self._spacing_controls)
        spacing_row.setContentsMargins(3, 2, 3, 0)
        spacing_row.setSpacing(2)
        self._padding_label = QLabel(self._spacing_controls)
        self._gap_label = QLabel(self._spacing_controls)
        self._padding_value_label = QLabel(str(self._content_padding), self._spacing_controls)
        self._padding_value_label.setObjectName("overlayControlValue")
        self._padding_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._padding_value_label.setFixedWidth(24)
        self._gap_value_label = QLabel(str(self._line_gap), self._spacing_controls)
        self._gap_value_label.setObjectName("overlayControlValue")
        self._gap_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gap_value_label.setFixedWidth(24)
        spacing_row.addWidget(self._padding_label)
        self._padding_down_btn = self._spacing_button(
            "−", "减小边框与文字之间的间距", lambda: self.change_content_padding(-2))
        self._padding_up_btn = self._spacing_button(
            "+", "增大边框与文字之间的间距", lambda: self.change_content_padding(+2))
        spacing_row.addWidget(self._padding_down_btn)
        spacing_row.addWidget(self._padding_value_label)
        spacing_row.addWidget(self._padding_up_btn)
        spacing_row.addSpacing(6)
        spacing_row.addWidget(self._gap_label)
        self._gap_down_btn = self._spacing_button(
            "−", "减小原文与译文之间的间距", lambda: self.change_line_gap(-2))
        self._gap_up_btn = self._spacing_button(
            "+", "增大原文与译文之间的间距", lambda: self.change_line_gap(+2))
        spacing_row.addWidget(self._gap_down_btn)
        spacing_row.addWidget(self._gap_value_label)
        spacing_row.addWidget(self._gap_up_btn)
        self._spacing_controls.hide()
        toolbar_box.addLayout(row)
        toolbar_box.addWidget(self._spacing_controls)
        self._update_toolbar_text()
        self._toolbar.adjustSize()
        self._toolbar.hide()

    def _spacing_button(self, text: str, tooltip: str, action) -> QToolButton:
        button = QToolButton(self._spacing_controls)
        button.setText(text)
        button.setToolTip(tr(tooltip))
        button.setStyleSheet("QToolButton { padding: 0; }")
        button.setFixedSize(32, 26)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(action)
        return button

    def _toggle_spacing_controls(self, checked: bool) -> None:
        self._spacing_controls.setVisible(bool(checked))
        self._position_toolbar()
        self._refresh_content_layout()

    def _update_toolbar_text(self) -> None:
        mode_labels = {
            "source": tr("原文", "Source"),
            "translation": tr("译文", "Trans."),
            "bilingual": tr("对照", "Both"),
        }
        self._display_btn.setText(mode_labels[self._display_mode])
        self._display_btn.setToolTip(tr(
            "选择仅原文、仅译文或对照翻译", "Show source, translation, or both"))
        action_labels = {
            "source": tr("仅原文", "Source only"),
            "translation": tr("仅译文", "Translation only"),
            "bilingual": tr("对照翻译", "Source and translation"),
        }
        for mode, action in self._display_actions.items():
            action.setText(action_labels[mode])
            action.setChecked(mode == self._display_mode)
        self._spacing_btn.setText(tr("间距", "Gap"))
        self._spacing_btn.setToolTip(tr(
            "调整内容边距和原译间距", "Adjust content padding and line gap"))
        self._lock_btn.setText(tr("锁定", "Lock"))
        self._lock_btn.setToolTip(tr(
            "锁定并让鼠标点击穿过浮窗", "Lock and let clicks pass through"))
        self._close_btn.setToolTip(tr("暂时关闭字幕浮窗", "Hide subtitle overlay"))
        self._padding_label.setText(tr("边距", "Padding"))
        self._gap_label.setText(tr("原译", "Lines"))

    def _on_language_changed(self, _language: str) -> None:
        retranslate_widget_tree(self)
        if hasattr(self, "_toolbar"):
            retranslate_widget_tree(self._toolbar)
        self._update_toolbar_text()
        self._position_toolbar()
        self._locked_panel._on_language_changed(_language)

    def font_size(self) -> int:
        return self._font_size

    def display_mode(self) -> str:
        return self._display_mode

    def content_padding(self) -> int:
        return self._content_padding

    def line_gap(self) -> int:
        return self._line_gap

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
        self._set_toolbar_visible(False)
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
            self._set_toolbar_visible(True)

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
        self._refresh_content_layout("top")
        self.hide()

    def set_display_mode(self, mode: str) -> None:
        """Choose which subtitle lines are visible and persist the choice."""
        if mode not in {"bilingual", "source", "translation"}:
            logger.warning("忽略未知浮窗显示模式: %s", mode)
            return
        if mode == self._display_mode:
            self._update_toolbar_text()
            return
        self._display_mode = mode
        self._store.set("overlay_display_mode", mode)
        self._apply_display_mode()
        self._update_toolbar_text()
        self._refresh_content_layout("top")

    def change_content_padding(self, delta: int) -> None:
        old_padding = self._content_padding
        old_gap = self._line_gap
        value = max(
            _PADDING_MIN,
            min(_PADDING_MAX, self._content_padding + int(delta)),
        )
        if value == old_padding:
            return
        self._content_padding = value
        # Treat this control as an overall spacing scale.  Keep the current
        # padding-to-line-gap ratio, so top/bottom insets and the source/
        # translation gap grow and shrink together instead of only resizing
        # the text viewport.
        ratio = old_gap / max(1, old_padding)
        self._line_gap = max(
            _LINE_GAP_MIN,
            min(_LINE_GAP_MAX, int(round(value * ratio))),
        )
        self._store.set("overlay_content_padding", value)
        self._store.set("overlay_line_gap", self._line_gap)
        self._padding_value_label.setText(str(value))
        self._gap_value_label.setText(str(self._line_gap))
        self._refresh_content_layout()
        self.update()

    def change_line_gap(self, delta: int) -> None:
        value = max(_LINE_GAP_MIN, min(_LINE_GAP_MAX, self._line_gap + int(delta)))
        if value == self._line_gap:
            return
        self._line_gap = value
        self._store.set("overlay_line_gap", value)
        self._gap_value_label.setText(str(value))
        self._refresh_content_layout()

    def _apply_display_mode(self) -> None:
        self.src_label.setVisible(self._display_mode != "translation")
        self.dst_label.setVisible(self._display_mode != "source")

    def _visible_subtitle_labels(self) -> list[QLabel]:
        if self._display_mode == "source":
            return [self.src_label]
        if self._display_mode == "translation":
            return [self.dst_label]
        return [self.src_label, self.dst_label]

    def _refresh_content_layout(self, scroll_target: str | None = None) -> None:
        """Reflow wrapped labels inside the current user-selected window size."""
        if not hasattr(self, "_content_scroll"):
            return
        self._box.setContentsMargins(
            self._content_padding,
            self._content_padding,
            self._content_padding,
            self._content_padding,
        )
        self._content_box.setSpacing(self._line_gap)

        # Reserve the slim scrollbar width even before it appears.  This avoids
        # a second wrap point after a long line first makes the bar visible.
        available_width = max(1, self._content_scroll.viewport().width() - 8)
        visible_labels = self._visible_subtitle_labels()
        content_height = 0
        for label in visible_labels:
            wrapped_height = label.heightForWidth(available_width)
            if wrapped_height < 0:
                wrapped_height = label.sizeHint().height()
            label_height = max(label.fontMetrics().height(), wrapped_height)
            label.setFixedHeight(label_height)
            content_height += label_height
        if len(visible_labels) > 1:
            content_height += self._line_gap

        self._content_widget.setMinimumHeight(max(1, content_height))
        self._content_box.invalidate()
        self._content_box.activate()
        if scroll_target is not None:
            QTimer.singleShot(0, lambda target=scroll_target: self._set_scroll_target(target))

    def _set_scroll_target(self, target: str) -> None:
        bar = self._content_scroll.verticalScrollBar()
        if target == "bottom":
            bar.setValue(bar.maximum())
        elif target == "top":
            bar.setValue(bar.minimum())

    def _position_toolbar(self) -> None:
        if not hasattr(self, "_toolbar"):
            return
        self._toolbar.adjustSize()
        geo = self.frameGeometry()
        screen = self.screen()
        available = screen.availableGeometry() if screen is not None else None
        if available is None:
            x = geo.left() + max(6, (geo.width() - self._toolbar.width()) // 2)
            y = geo.top() - self._toolbar.height() - 8
        else:
            x = geo.left() + max(6, (geo.width() - self._toolbar.width()) // 2)
            x = min(x, available.right() - self._toolbar.width() - 6)
            x = max(available.left() + 6, x)
            y = geo.top() - self._toolbar.height() - 8
            # At the very top of a monitor, keep the controls visible below
            # the overlay because there is no space above it.
            if y < available.top() + 6:
                y = min(available.bottom() - self._toolbar.height() - 6,
                        geo.bottom() + 8)
        self._toolbar.move(x, y)
        self._toolbar.raise_()

    def _set_toolbar_visible(self, visible: bool) -> None:
        if not hasattr(self, "_toolbar"):
            return
        visible = bool(visible) and not self._click_through and self.isVisible()
        self._toolbar.setVisible(visible)
        if visible:
            self._position_toolbar()

    def _cursor_over_toolbar_or_overlay(self) -> bool:
        cursor = QCursor.pos()
        return (self.frameGeometry().contains(cursor)
                or (self._toolbar.isVisible()
                    and self._toolbar.frameGeometry().contains(cursor)))

    def _schedule_toolbar_hide(self) -> None:
        if hasattr(self, "_toolbar_hide_timer"):
            self._toolbar_hide_timer.start()

    def _hide_toolbar_if_cursor_left(self) -> None:
        if not self._cursor_over_toolbar_or_overlay():
            self._set_toolbar_visible(False)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if hasattr(self, "_toolbar") and watched is self._toolbar:
            if event.type() == QEvent.Type.Enter:
                self._toolbar_hide_timer.stop()
            elif event.type() == QEvent.Type.Leave:
                self._schedule_toolbar_hide()
        return super().eventFilter(watched, event)

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
        QTimer.singleShot(0, lambda: self._refresh_content_layout("top"))

    def set_partial(self, src: str, dst: str | None = None) -> None:
        """Update temporary text without polluting the scroll-back history."""
        self.src_label.setText(src)
        self.dst_label.setText(dst if dst is not None else tr("识别中…"))
        QTimer.singleShot(0, lambda: self._refresh_content_layout("bottom"))

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
        QTimer.singleShot(0, self._refresh_content_layout)

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
        """Compatibility entry point: text now reflows without resizing the window."""
        self._refresh_content_layout()

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        if hasattr(self, "_toolbar") and self._toolbar.isVisible():
            self._position_toolbar()
        if hasattr(self, "_content_scroll"):
            QTimer.singleShot(0, self._refresh_content_layout)
        if hasattr(self, "_locked_panel") and self._locked_panel.isVisible():
            self._position_locked_panel()

    def moveEvent(self, ev) -> None:  # noqa: N802
        super().moveEvent(ev)
        if hasattr(self, "_toolbar") and self._toolbar.isVisible():
            self._position_toolbar()
        if hasattr(self, "_locked_panel") and self._locked_panel.isVisible():
            self._position_locked_panel()

    def enterEvent(self, ev) -> None:  # noqa: N802
        if not self._click_through:
            self._set_toolbar_visible(True)
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._schedule_toolbar_hide()
        super().leaveEvent(ev)

    def hideEvent(self, ev) -> None:  # noqa: N802
        self._set_toolbar_visible(False)
        if hasattr(self, "_locked_panel"):
            self._locked_panel.hide()
        super().hideEvent(ev)

    def closeEvent(self, ev) -> None:  # noqa: N802
        if hasattr(self, "_toolbar"):
            self._toolbar.close()
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

        # 内芯跟随用户内容边距，紧凑模式下不再浪费大块空白。
        inset = max(5, min(18, self._content_padding // 2))
        radius_inner = min(radius_inner, max(8, (min(w, h) - 2 * inset) // 2))
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
        display_menu = menu.addMenu(tr("显示内容", "Display"))
        display_menu.setStyleSheet(self._menu_qss())
        for mode, label in (
            ("source", tr("仅原文", "Source only")),
            ("translation", tr("仅译文", "Translation only")),
            ("bilingual", tr("对照翻译", "Source and translation")),
        ):
            action = display_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(mode == self._display_mode)
            action.triggered.connect(
                lambda _checked=False, selected=mode: self.set_display_mode(selected)
            )
        spacing_menu = menu.addMenu(tr("间距", "Spacing"))
        spacing_menu.setStyleSheet(self._menu_qss())
        spacing_menu.addAction(
            tr("减小内容边距", "Decrease content padding"),
            lambda: self.change_content_padding(-2),
        )
        spacing_menu.addAction(
            tr("增大内容边距", "Increase content padding"),
            lambda: self.change_content_padding(+2),
        )
        spacing_menu.addAction(
            tr("减小原译间距", "Decrease line gap"),
            lambda: self.change_line_gap(-2),
        )
        spacing_menu.addAction(
            tr("增大原译间距", "Increase line gap"),
            lambda: self.change_line_gap(+2),
        )
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
    def _step_history(self, delta: int) -> None:
        if not self._history or self._wheel_locked:
            return
        if delta > 0:  # 上滚 → 更早的历史
            self._history_pos = min(len(self._history) - 1, self._history_pos + 1)
        else:  # 下滚 → 回到最新
            self._history_pos = max(0, self._history_pos - 1)
        idx = len(self._history) - 1 - self._history_pos
        src, dst = self._history[idx]
        self.src_label.setText(src)
        self.dst_label.setText(dst)
        self._refresh_content_layout("top")
        # 边缘阻尼：到底后 160ms 内不再响应，避免误滚
        if (delta > 0 and idx == 0) or (delta < 0 and self._history_pos == 0):
            self._wheel_locked = True
            QTimer.singleShot(160, self._unlock_wheel)

    def wheelEvent(self, ev) -> None:  # noqa: N802
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._step_history(ev.angleDelta().y())
            ev.accept()
            return
        super().wheelEvent(ev)

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
