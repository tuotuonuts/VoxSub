"""Click-through in-place translation overlay and its small control bar."""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter
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


def _is_paragraph(source: str) -> bool:
    return "\n" in source or len(source) >= 80


def _text_flags(paragraph: bool) -> Qt.AlignmentFlag | Qt.TextFlag:
    horizontal = (
        Qt.AlignmentFlag.AlignLeft if paragraph
        else Qt.AlignmentFlag.AlignCenter
    )
    # TextWordWrap preserves natural word breaks, while TextWrapAnywhere is
    # required for URLs, identifiers, and CJK strings without whitespace.
    return (
        horizontal
        | Qt.AlignmentFlag.AlignVCenter
        | Qt.TextFlag.TextWordWrap
        | Qt.TextFlag.TextWrapAnywhere
    )


def _inner_text_rect(rect: QRect) -> QRect:
    horizontal = min(6, max(1, (rect.width() - 2) // 4))
    vertical = min(4, max(1, (rect.height() - 2) // 4))
    return rect.adjusted(horizontal, vertical, -horizontal, -vertical)


def _fit_font(rect: QRect, text: str, source: str) -> QFont:
    paragraph = _is_paragraph(source)
    source_rows = max(1, source.count("\n") + 1)
    font = QFont("Microsoft YaHei UI")
    # OCR boxes can be only a few pixels high after a DPI conversion.  Start
    # from the source line height, then keep shrinking until the complete
    # translated block fits the actual painted rectangle.
    size = max(4, min(32, round(rect.height() / source_rows * 0.68)))
    flags = int(_text_flags(paragraph))
    inner = _inner_text_rect(rect)
    while size > 4:
        font.setPixelSize(size)
        measured = QFontMetrics(font).boundingRect(inner, flags, text)
        if measured.width() <= inner.width() and measured.height() <= inner.height():
            break
        size -= 1
    font.setPixelSize(size)
    font.setWeight(QFont.Weight.Normal if paragraph else QFont.Weight.DemiBold)
    return font


def _translation_rect(base: QRect, bounds: QRect, source: str, translation: str) -> QRect:
    """Allocate enough measured space while keeping prose as one large block."""
    paragraph = _is_paragraph(source)
    source_rows = max(1, source.count("\n") + 1)
    probe = QFont("Microsoft YaHei UI")
    probe.setPixelSize(max(10, min(32, round(base.height() / source_rows * 0.68))))
    metrics = QFontMetrics(probe)
    padding = 12
    if paragraph:
        width_cap = min(bounds.width(), max(base.width(), round(base.width() * 1.18)))
        width = max(base.width(), min(width_cap, metrics.horizontalAdvance(translation) + padding))
    else:
        width_cap = min(bounds.width(), max(base.width(), round(base.width() * 2.2)))
        width = max(base.width(), min(width_cap, metrics.horizontalAdvance(translation) + padding))
    flags = int(_text_flags(paragraph))
    measured = metrics.boundingRect(
        QRect(0, 0, max(4, width - padding), bounds.height()), flags, translation)
    height = min(bounds.height(), max(base.height(), measured.height() + 8))
    if paragraph:
        left = base.left()
        top = base.top()
    else:
        left = base.center().x() - width // 2
        top = base.center().y() - height // 2
    left = max(bounds.left(), min(left, bounds.right() - width + 1))
    top = max(bounds.top(), min(top, bounds.bottom() - height + 1))
    return QRect(left, top, width, height)


def _paint_translations(
    painter: QPainter,
    frame: TranslatedOcrFrame,
    capture: QImage,
    bounds: QRect,
) -> None:
    for line in frame.lines:
        text = line.translation.strip()
        if not text:
            # A failed translation should leave the readable source untouched,
            # not cover it with a duplicate OCR result.
            continue
        source_rect = _mapped_box(line.box, frame, bounds).adjusted(-4, -3, 4, 3)
        rect = _translation_rect(source_rect, bounds, line.source, text).intersected(bounds)
        if rect.width() < 4 or rect.height() < 4:
            continue
        background = _sample_background(capture, line.box)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, min(7, rect.height() / 3), min(7, rect.height() / 3))
        painter.setPen(_contrasting_text(background))
        painter.setFont(_fit_font(rect, text, line.source))
        painter.drawText(
            _inner_text_rect(rect),
            _text_flags(_is_paragraph(line.source)),
            text,
        )


def render_translated_image(
    capture: QImage, frame: TranslatedOcrFrame
) -> QImage:
    """Return an exportable image with translated text painted in place."""
    if capture.isNull():
        return QImage()
    rendered = capture.convertToFormat(QImage.Format.Format_ARGB32).copy()
    painter = QPainter(rendered)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    _paint_translations(painter, frame, capture, rendered.rect())
    painter.end()
    return rendered


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

    def clear_frame(self) -> None:
        self._frame = None
        self._capture = QImage()
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
        _paint_translations(painter, frame, self._capture, self.rect())
        painter.end()


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


__all__ = ["OcrLiveControlBar", "OcrTranslationOverlay", "render_translated_image"]
