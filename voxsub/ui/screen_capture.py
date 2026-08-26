"""Screen-region selection and high-DPI capture helpers for OCR."""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Callable

import numpy as np
from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from voxsub.logging_setup import get_logger

logger = get_logger("ui.screen_capture")


@dataclass(frozen=True)
class CapturedRegion:
    image: QImage
    global_rect: QRect


def _screen_for_rect(rect: QRect):
    screens = QGuiApplication.screens()
    for screen in screens:
        if screen.geometry().contains(rect.center()):
            return screen
    ranked = sorted(
        screens,
        key=lambda screen: screen.geometry().intersected(rect).width()
        * screen.geometry().intersected(rect).height(),
        reverse=True,
    )
    return ranked[0] if ranked else None


def capture_screen_region(global_rect: QRect) -> CapturedRegion:
    """Capture one logical region and preserve its high-DPI image pixels."""
    requested = global_rect.normalized()
    screen = _screen_for_rect(requested)
    if screen is None:
        raise RuntimeError("未找到可捕获的屏幕")
    clipped = requested.intersected(screen.geometry())
    if clipped.width() < 2 or clipped.height() < 2:
        raise ValueError("所选屏幕区域过小")
    local = clipped.translated(-screen.geometry().topLeft())
    pixmap = screen.grabWindow(
        0, local.x(), local.y(), local.width(), local.height()
    )
    if pixmap.isNull():
        raise RuntimeError("屏幕捕获失败，请确认应用有屏幕捕获权限")
    return CapturedRegion(pixmap.toImage().copy(), QRect(clipped))


def qimage_to_bgr(image: QImage) -> np.ndarray:
    """Detach a QImage into the BGR ndarray expected by RapidOCR."""
    converted = image.convertToFormat(QImage.Format.Format_RGB888)
    raw = np.frombuffer(converted.constBits(), dtype=np.uint8, count=converted.sizeInBytes())
    rows = raw.reshape(converted.height(), converted.bytesPerLine())
    rgb = rows[:, : converted.width() * 3].reshape(
        converted.height(), converted.width(), 3
    ).copy()
    return rgb[..., ::-1].copy()


def exclude_window_from_capture(widget: QWidget) -> bool:
    """Keep VoxSub's translated overlay out of subsequent desktop captures."""
    if os.name != "nt":
        return False
    try:
        hwnd = int(widget.winId())
        result = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x00000011)
        if not result:
            logger.warning("设置 WDA_EXCLUDEFROMCAPTURE 失败: hwnd=%s", hwnd)
        return bool(result)
    except (AttributeError, OSError, ValueError):
        logger.warning("当前系统不支持覆盖层捕获排除", exc_info=True)
        return False


def wait_for_desktop_settle(
    callback: Callable[[], None], *, minimum_delay_ms: int = 260
) -> None:
    """Run ``callback`` only after hidden VoxSub windows leave the DWM frame.

    Hiding a top-level Qt window is asynchronous on Windows. Capturing on the
    next timer tick can therefore preserve a translucent afterimage of the app.
    Flush posted Qt work, ask DWM to finish its queued composition, then leave a
    short compositor-safe interval before taking the selector background.
    """
    app = QApplication.instance()
    if app is not None:
        app.sendPostedEvents()
        app.processEvents()
    if os.name == "nt":
        try:
            ctypes.windll.dwmapi.DwmFlush()
        except (AttributeError, OSError):
            logger.debug("DwmFlush 不可用，使用定时等待桌面稳定", exc_info=True)
    QTimer.singleShot(max(160, int(minimum_delay_ms)), callback)


class _ScreenSelector(QWidget):
    selected = Signal(QRect)
    cancelled = Signal()

    def __init__(self, screen, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._screen = screen
        self._origin: QPoint | None = None
        self._selection = QRect()
        self._background = screen.grabWindow(0)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(screen.geometry())

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background = self._background.scaled(
            self.size(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap(self.rect(), background)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 118))
        if not self._selection.isNull():
            source = QRect(self._selection)
            painter.drawPixmap(self._selection, background, source)
            painter.setPen(QPen(QColor("#14B8A6"), 2))
            painter.drawRoundedRect(self._selection, 4, 4)
            self._draw_size_label(painter)
        self._draw_instruction(painter)

    def _draw_instruction(self, painter: QPainter) -> None:
        text = "拖动选择 OCR 区域  ·  Esc / 右键取消"
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 34
        rect = QRect(max(16, (self.width() - width) // 2), 24, width, 38)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(12, 12, 12, 220))
        painter.drawRoundedRect(rect, 19, 19)
        painter.setPen(QColor("#F2F2F2"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_size_label(self, painter: QPainter) -> None:
        text = f"{self._selection.width()} × {self._selection.height()}"
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 20
        x = min(max(8, self._selection.left()), max(8, self.width() - width - 8))
        y = min(self.height() - 34, self._selection.bottom() + 8)
        rect = QRect(x, max(8, y), width, 28)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(13, 148, 136, 235))
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.activateWindow()
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._origin = event.position().toPoint()
            self._selection = QRect(self._origin, self._origin)
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._origin is None:
            return
        point = event.position().toPoint()
        self._selection = QRect(self._origin, point).normalized().intersected(self.rect())
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        self._origin = None
        selected = self._selection.normalized()
        if selected.width() < 24 or selected.height() < 24:
            self._selection = QRect()
            self.update()
            return
        self.selected.emit(selected.translated(self.geometry().topLeft()))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ScreenRegionPicker(QObject):
    """PowerToys-style selector shown on every attached monitor."""

    selected = Signal(QRect)
    cancelled = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._selectors: list[_ScreenSelector] = []
        self._active = False

    def begin(self) -> None:
        self.cancel(emit_signal=False)
        screens = QGuiApplication.screens()
        if not screens:
            self.cancelled.emit()
            return
        self._active = True
        for screen in screens:
            selector = _ScreenSelector(screen)
            selector.selected.connect(self._on_selected)
            selector.cancelled.connect(self.cancel)
            self._selectors.append(selector)
            selector.show()
            selector.raise_()
        target = QApplication.activeWindow()
        screen = target.screen() if target is not None else QGuiApplication.primaryScreen()
        focused = next(
            (item for item in self._selectors if item._screen is screen),  # noqa: SLF001
            self._selectors[0],
        )
        focused.activateWindow()
        focused.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _on_selected(self, rect: QRect) -> None:
        if not self._active:
            return
        self._active = False
        self._close_selectors()
        chosen = QRect(rect)
        QTimer.singleShot(70, lambda: self.selected.emit(chosen))

    def _close_selectors(self) -> None:
        selectors, self._selectors = self._selectors, []
        for selector in selectors:
            selector.hide()
            selector.close()
            selector.deleteLater()

    def cancel(self, emit_signal: bool = True) -> None:
        was_active, self._active = self._active, False
        self._close_selectors()
        if was_active and emit_signal:
            self.cancelled.emit()


__all__ = [
    "CapturedRegion",
    "ScreenRegionPicker",
    "capture_screen_region",
    "exclude_window_from_capture",
    "qimage_to_bgr",
    "wait_for_desktop_settle",
]
