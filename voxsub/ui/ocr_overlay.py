"""Click-through in-place translation overlay and its small control bar."""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from voxsub.ocr import OcrBox, TranslatedOcrFrame
from voxsub.ui.i18n import tr
from voxsub.ui.screen_capture import exclude_window_from_capture


def _mapped_box(box: OcrBox, frame: TranslatedOcrFrame, target: QRect) -> QRect:
    scale_x = target.width() / max(1, frame.width)
    scale_y = target.height() / max(1, frame.height)
    return QRect(
        round(box.left * scale_x),
        round(box.top * scale_y),
        max(1, round(box.width * scale_x)),
        max(1, round(box.height * scale_y)),
    )


def _sample_background(image: QImage, box: OcrBox) -> QColor:
    if image.isNull():
        return QColor(18, 18, 18, 255)
    points = (
        (box.left, box.top),
        (box.right - 1, box.top),
        (box.left, box.bottom - 1),
        (box.right - 1, box.bottom - 1),
        ((box.left + box.right) // 2, box.top),
        ((box.left + box.right) // 2, box.bottom - 1),
    )
    colors = [
        image.pixelColor(
            max(0, min(image.width() - 1, x)),
            max(0, min(image.height() - 1, y)),
        )
        for x, y in points
    ]
    return QColor(
        sum(color.red() for color in colors) // len(colors),
        sum(color.green() for color in colors) // len(colors),
        sum(color.blue() for color in colors) // len(colors),
        255,
    )


def _contrasting_text(background: QColor) -> QColor:
    luminance = (
        0.2126 * background.red()
        + 0.7152 * background.green()
        + 0.0722 * background.blue()
    )
    return QColor("#111827") if luminance >= 150 else QColor("#F9FAFB")


def _fit_font(rect: QRect, text: str) -> QFont:
    font = QFont("Microsoft YaHei UI")
    size = max(10, min(38, round(rect.height() * 0.62)))
    flags = int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap)
    while size > 9:
        font.setPixelSize(size)
        bounds = QFontMetrics(font).boundingRect(rect, flags, text)
        if bounds.width() <= rect.width() and bounds.height() <= rect.height():
            break
        size -= 1
    font.setPixelSize(size)
    font.setWeight(QFont.Weight.DemiBold)
    return font


def _translation_rect(base: QRect, bounds: QRect, source: str, translation: str) -> QRect:
    """Grow around the source center when the target language needs more room."""
    ratio = min(2.5, max(1.0, len(translation) / max(1, len(source))))
    width = min(bounds.width(), max(base.width(), round(base.width() * min(1.8, ratio))))
    rows = min(2.2, max(1.0, ratio / max(1.0, width / max(1, base.width()))))
    height = min(bounds.height(), max(base.height(), round(base.height() * rows)))
    left = max(bounds.left(), min(base.center().x() - width // 2, bounds.right() - width + 1))
    top = max(bounds.top(), min(base.center().y() - height // 2, bounds.bottom() - height + 1))
    return QRect(left, top, width, height)


class OcrTranslationOverlay(QWidget):
    """Paint translations over their source line boxes without taking input."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame: TranslatedOcrFrame | None = None
        self._capture = QImage()
        self.capture_excluded = False
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

    def set_capture_region(self, global_rect: QRect) -> None:
        self.setGeometry(global_rect)

    def set_frame(self, frame: TranslatedOcrFrame, capture: QImage) -> None:
        self._frame = frame
        self._capture = capture.copy()
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_capture_exclusion)

    def _apply_capture_exclusion(self) -> None:
        self.capture_excluded = exclude_window_from_capture(self)

    def paintEvent(self, _event) -> None:  # noqa: N802
        frame = self._frame
        if frame is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for line in frame.lines:
            self._paint_line(painter, frame, line)
        painter.end()

    def _paint_line(self, painter: QPainter, frame, line) -> None:
        text = line.translation.strip() or line.source
        source_rect = _mapped_box(line.box, frame, self.rect()).adjusted(-4, -3, 4, 3)
        rect = _translation_rect(source_rect, self.rect(), line.source, text)
        rect = rect.intersected(self.rect())
        if rect.width() < 4 or rect.height() < 4:
            return
        background = _sample_background(self._capture, line.box)
        painter.setPen(QPen(_contrasting_text(background), 1))
        painter.setBrush(background)
        painter.drawRoundedRect(rect, min(7, rect.height() / 3), min(7, rect.height() / 3))
        painter.setFont(_fit_font(rect, text))
        painter.drawText(
            rect.adjusted(3, 1, -3, -1),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            text,
        )


class OcrLiveControlBar(QFrame):
    paused_changed = Signal(bool)
    original_changed = Signal(bool)
    reselect_requested = Signal()
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._paused = False
        self._showing_original = False
        self.capture_excluded = False
        self.setObjectName("ocrLiveControl")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(
            "QFrame#ocrLiveControl { background: rgba(15,15,15,238); "
            "border: 1px solid rgba(20,184,166,190); border-radius: 16px; }"
            "QPushButton { min-height: 30px; padding: 0 12px; border-radius: 10px; "
            "background: rgba(255,255,255,22); color: #F2F2F2; border: 0; }"
            "QPushButton:hover { background: rgba(20,184,166,90); }"
            "QLabel { color: #9CA3AF; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        self.status = QLabel(tr("实时 OCR"), self)
        self.pause_button = QPushButton(tr("暂停"), self)
        self.original_button = QPushButton(tr("显示原文"), self)
        self.reselect_button = QPushButton(tr("重选区域"), self)
        self.stop_button = QPushButton(tr("结束"), self)
        self.pause_button.clicked.connect(self._toggle_pause)
        self.original_button.clicked.connect(self._toggle_original)
        self.reselect_button.clicked.connect(self.reselect_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        layout.addWidget(self.status)
        layout.addWidget(self.pause_button)
        layout.addWidget(self.original_button)
        layout.addWidget(self.reselect_button)
        layout.addWidget(self.stop_button)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_capture_exclusion)

    def _apply_capture_exclusion(self) -> None:
        self.capture_excluded = exclude_window_from_capture(self)

    def place_near(self, region: QRect) -> None:
        self.adjustSize()
        screen = _screen_for_point(region.center())
        available = screen.availableGeometry() if screen is not None else region
        x = max(available.left() + 8, min(region.left(), available.right() - self.width() - 8))
        above = region.top() - self.height() - 10
        y = above if above >= available.top() else min(
            available.bottom() - self.height() - 8, region.bottom() + 10
        )
        self.move(x, y)

    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setToolTip(text)

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_button.setText(tr("继续") if self._paused else tr("暂停"))
        self.paused_changed.emit(self._paused)

    def _toggle_original(self) -> None:
        self._showing_original = not self._showing_original
        self.original_button.setText(
            tr("显示译文") if self._showing_original else tr("显示原文")
        )
        self.original_changed.emit(self._showing_original)


def _screen_for_point(point):
    for screen in QApplication.screens():
        if screen.geometry().contains(point):
            return screen
    return QApplication.primaryScreen()


__all__ = ["OcrLiveControlBar", "OcrTranslationOverlay"]
