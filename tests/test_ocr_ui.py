from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from voxsub.config_store import ConfigStore  # noqa: E402
from voxsub.ocr import OcrBox, TranslatedOcrFrame, TranslatedOcrLine  # noqa: E402
from voxsub.ui.ocr_overlay import (  # noqa: E402
    OcrLiveControlBar,
    OcrTranslationOverlay,
)
from voxsub.ui.ocr_workspace import OcrWorkspace  # noqa: E402
from voxsub.ui.screen_capture import qimage_to_bgr  # noqa: E402


def _app():
    return QApplication.instance() or QApplication([])


def _frame():
    return TranslatedOcrFrame(
        200,
        100,
        (TranslatedOcrLine(OcrBox(10, 12, 160, 45), "Hello", "你好", 0.99),),
        20,
        30,
    )


def test_qimage_conversion_detaches_bgr_pixels():
    image = QImage(4, 3, QImage.Format.Format_RGB888)
    image.fill(QColor(10, 20, 30))

    converted = qimage_to_bgr(image)

    assert converted.shape == (3, 4, 3)
    assert converted[0, 0].tolist() == [30, 20, 10]
    image.fill(QColor(255, 255, 255))
    assert converted[0, 0].tolist() == [30, 20, 10]


def test_translation_overlay_is_click_through_and_accepts_a_frame(monkeypatch):
    _app()
    monkeypatch.setattr(
        "voxsub.ui.ocr_overlay.exclude_window_from_capture", lambda _widget: True
    )
    overlay = OcrTranslationOverlay()
    capture = QImage(200, 100, QImage.Format.Format_RGB888)
    capture.fill(QColor(20, 20, 20))
    try:
        overlay.set_capture_region(QRect(0, 0, 200, 100))
        overlay.set_frame(_frame(), capture)
        overlay.show()
        _app().processEvents()

        assert overlay.windowFlags() & overlay.windowFlags().WindowTransparentForInput
        assert overlay.capture_excluded
        assert not overlay.grab().isNull()
    finally:
        overlay.close()
        overlay.deleteLater()


def test_live_control_bar_is_excluded_from_later_screen_captures(monkeypatch):
    _app()
    monkeypatch.setattr(
        "voxsub.ui.ocr_overlay.exclude_window_from_capture", lambda _widget: True
    )
    control = OcrLiveControlBar()
    try:
        control.show()
        _app().processEvents()
        assert control.capture_excluded
    finally:
        control.close()
        control.deleteLater()


def test_workspace_exposes_two_modes_and_can_be_embedded(tmp_path):
    _app()
    workspace = OcrWorkspace(ConfigStore(tmp_path / "config.json"))
    host = QWidget()
    try:
        workspace.setParent(host)
        workspace.set_mode("live")
        assert workspace.pages.currentWidget() is workspace.live_page
        assert workspace.live_mode_button.isChecked()
        workspace.set_mode("screenshot")
        assert workspace.pages.currentWidget() is workspace.screenshot_page
        assert workspace.screenshot_mode_button.isChecked()
    finally:
        workspace.shutdown()
        workspace.deleteLater()
        host.deleteLater()


def test_workspace_renders_screenshot_result_without_starting_capture(tmp_path):
    _app()
    workspace = OcrWorkspace(ConfigStore(tmp_path / "config.json"))
    capture = QImage(200, 100, QImage.Format.Format_RGB888)
    capture.fill(QColor(240, 240, 240))
    try:
        workspace._show_screenshot_result(capture, _frame(), "")  # noqa: SLF001
        assert workspace.source_text.toPlainText() == "Hello"
        assert workspace.translation_text.toPlainText() == "你好"
        assert "1 行" in workspace.screenshot_status.text()
    finally:
        workspace.shutdown()
        workspace.deleteLater()
