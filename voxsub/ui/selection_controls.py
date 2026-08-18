"""Stable, platform-independent controls for binary and exclusive choices.

Qt's Windows style can replace a radio indicator's rounded geometry in a
checked pseudo-state.  These controls own their indicator painting so the
visual language remains stable across themes and Qt style engines.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QPushButton, QRadioButton

from voxsub.ui.theme import active_theme_name


def _choice_colors() -> tuple[QColor, QColor, QColor, QColor]:
    """Return muted, text, surface and accent colors for the active theme."""
    is_dark = active_theme_name() == "dark"
    return (
        QColor("#9CA3AF" if is_dark else "#6B7280"),
        QColor("#F2F2F2" if is_dark else "#1A1A1A"),
        QColor("#131313" if is_dark else "#FFFFFF"),
        QColor("#14B8A6"),
    )


class RoundRadioButton(QRadioButton):
    """Exclusive-choice radio with a consistent circular indicator."""

    _INDICATOR_DIAMETER = 18.0
    _LABEL_GAP = 10

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(28)

    def sizeHint(self) -> QSize:  # noqa: N802
        base = super().sizeHint()
        text_width = self.fontMetrics().horizontalAdvance(self.text())
        width = int(self._INDICATOR_DIAMETER + self._LABEL_GAP + text_width + 4)
        return QSize(max(base.width(), width), max(base.height(), 28))

    def _repaint_for_interaction(self) -> None:
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._repaint_for_interaction()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._repaint_for_interaction()
        super().leaveEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.StyleChange):
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        # Deliberately skip QRadioButton.paintEvent: native Windows drawing can
        # add its own square checked indicator underneath this custom circle.
        _ = event
        painter = QPainter(self)
        try:
            muted, text_color, _surface, accent = _choice_colors()
            if not self.isEnabled():
                muted.setAlpha(110)
                text_color.setAlpha(110)
                accent.setAlpha(110)

            contents = self.contentsRect()
            diameter = self._INDICATOR_DIAMETER
            center_x = contents.left() + diameter / 2
            center_y = contents.center().y()
            circle = QRectF(
                center_x - diameter / 2,
                center_y - diameter / 2,
                diameter,
                diameter,
            )

            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if self.hasFocus():
                focus = QColor(accent)
                focus.setAlpha(120)
                painter.setPen(QPen(focus, 1.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(circle.adjusted(-2, -2, 2, 2))

            edge = accent if (self.isChecked() or self.underMouse()) else muted
            if self.isDown() and self.isEnabled():
                edge = QColor("#0D9488")
            painter.setPen(QPen(edge, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(circle.adjusted(0.75, 0.75, -0.75, -0.75))

            if self.isChecked():
                dot = diameter * 0.42
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(accent)
                painter.drawEllipse(
                    QRectF(
                        center_x - dot / 2,
                        center_y - dot / 2,
                        dot,
                        dot,
                    )
                )

            painter.setFont(self.font())
            painter.setPen(text_color)
            text_rect = contents.adjusted(int(diameter + self._LABEL_GAP), 0, 0, 0)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self.text(),
            )
        finally:
            painter.end()


class ToggleSwitch(QCheckBox):
    """Binary setting switch with a rounded track and circular thumb."""

    _TRACK_WIDTH = 38.0
    _TRACK_HEIGHT = 22.0
    _LABEL_GAP = 10

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(32)

    def sizeHint(self) -> QSize:  # noqa: N802
        base = super().sizeHint()
        text_width = self.fontMetrics().horizontalAdvance(self.text())
        width = int(self._TRACK_WIDTH + self._LABEL_GAP + text_width + 4)
        return QSize(max(base.width(), width), max(base.height(), 32))

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.StyleChange):
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        # See RoundRadioButton: drawing the track ourselves prevents the
        # platform checkbox glyph from replacing the rounded control state.
        _ = event
        painter = QPainter(self)
        try:
            muted, text_color, surface, accent = _choice_colors()
            if not self.isEnabled():
                muted.setAlpha(110)
                text_color.setAlpha(110)
                accent.setAlpha(110)

            contents = self.contentsRect()
            track = QRectF(
                contents.left(),
                contents.center().y() - self._TRACK_HEIGHT / 2,
                self._TRACK_WIDTH,
                self._TRACK_HEIGHT,
            )
            checked = self.isChecked()
            track_fill = QColor(accent if checked else surface)
            track_edge = QColor(accent if checked else muted)
            if self.isDown() and self.isEnabled():
                track_fill = QColor("#0D9488") if checked else surface
                track_edge = QColor("#0D9488")

            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if self.hasFocus():
                focus = QColor(accent)
                focus.setAlpha(120)
                painter.setPen(QPen(focus, 1.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(track.adjusted(-2, -2, 2, 2), 13, 13)

            painter.setPen(QPen(track_edge, 1.25))
            painter.setBrush(track_fill)
            painter.drawRoundedRect(track, self._TRACK_HEIGHT / 2, self._TRACK_HEIGHT / 2)

            thumb_diameter = self._TRACK_HEIGHT - 6
            thumb_x = (
                track.right() - thumb_diameter - 3
                if checked
                else track.left() + 3
            )
            thumb = QRectF(
                thumb_x,
                track.center().y() - thumb_diameter / 2,
                thumb_diameter,
                thumb_diameter,
            )
            thumb_color = QColor("#FFFFFF" if checked else muted)
            if not self.isEnabled():
                thumb_color.setAlpha(110)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(thumb_color)
            painter.drawEllipse(thumb)

            painter.setFont(self.font())
            painter.setPen(text_color)
            text_rect = contents.adjusted(
                int(self._TRACK_WIDTH + self._LABEL_GAP), 0, 0, 0
            )
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self.text(),
            )
        finally:
            painter.end()


class PillChoiceButton(QPushButton):
    """Checkable capsule button used for compact exclusive filters."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(34)
