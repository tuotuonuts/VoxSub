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
    render_translated_image,
)
from voxsub.ui.ocr_workspace import OcrWorkspace  # noqa: E402
from voxsub.ui.screen_capture import CapturedRegion, qimage_to_bgr  # noqa: E402


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


def test_workspace_uses_shared_language_direction_and_staged_live_models(
    monkeypatch, tmp_path
):
    _app()
    store = ConfigStore(tmp_path / "config.json")
    store.update({
        "lang_pair": "en-zh",
        "ocr_model_id": "ocr-rapidocr-v6-medium",
    })
    workspace = OcrWorkspace(store)
    jobs = []
    monkeypatch.setattr(
        workspace._worker, "submit",  # noqa: SLF001
        lambda job: jobs.append(job) or True,
    )
    image = QImage(160, 90, QImage.Format.Format_RGB888)
    image.fill(QColor(245, 245, 245))
    captured = CapturedRegion(image, QRect(0, 0, 160, 90))
    try:
        assert workspace._submit(captured, "live")  # noqa: SLF001
        assert workspace._submit(captured, "live-refine")  # noqa: SLF001

        assert [(job.source_lang, job.target_lang) for job in jobs] == [
            ("en", "zh"), ("en", "zh")]
        assert jobs[0].config["ocr_model_id"] == "ocr-rapidocr-v6-small-builtin"
        assert jobs[0].config["ocr_maximum_lines"] == 24
        assert jobs[1].config["ocr_model_id"] == "ocr-rapidocr-v6-medium"
        assert jobs[1].config["ocr_refinement_mode"] is True
    finally:
        workspace.shutdown()
        workspace.deleteLater()


def test_peer_mode_prewarms_the_selected_language_direction(monkeypatch, tmp_path):
    _app()
    store = ConfigStore(tmp_path / "config.json")
    store.update({
        "lang_pair": "en-zh",
        "ocr_model_id": "ocr-rapidocr-v6-medium",
    })
    workspace = OcrWorkspace(store)
    prepared = []
    monkeypatch.setattr(
        workspace._worker, "prepare",  # noqa: SLF001
        lambda config, source, target: (
            prepared.append((config, source, target)) or True),
    )
    try:
        workspace.prepare_live()

        assert len(prepared) == 1
        config, source, target = prepared[0]
        assert (source, target) == ("en", "zh")
        assert config["ocr_model_id"] == "ocr-rapidocr-v6-small-builtin"
        assert "预热" in workspace.live_status.text()
    finally:
        workspace.shutdown()
        workspace.deleteLater()


def test_workspace_renders_screenshot_result_without_starting_capture(tmp_path):
    _app()
    workspace = OcrWorkspace(ConfigStore(tmp_path / "config.json"))
    workspace._store.update({  # noqa: SLF001
        "ocr_cache_root": str(tmp_path / "ocr-cache"),
    })
    capture = QImage(200, 100, QImage.Format.Format_RGB888)
    capture.fill(QColor(240, 240, 240))
    try:
        workspace._show_screenshot_result(capture, _frame(), "")  # noqa: SLF001
        assert workspace.source_text.toPlainText() == "Hello"
        assert workspace.translation_text.toPlainText() == "你好"
        assert "1 行" in workspace.screenshot_status.text()
        assert not workspace._translated_image.isNull()  # noqa: SLF001
        assert len(list((tmp_path / "ocr-cache" / "translated").glob("*.png"))) == 1
    finally:
        workspace.shutdown()
        workspace.deleteLater()


def test_workspace_waits_for_desktop_settle_before_picker(monkeypatch, tmp_path):
    _app()
    workspace = OcrWorkspace(ConfigStore(tmp_path / "config.json"))
    calls = []
    monkeypatch.setattr(
        workspace._picker, "begin", lambda: calls.append("picker"))  # noqa: SLF001

    def settle(callback, *, minimum_delay_ms):
        calls.append(minimum_delay_ms)
        callback()

    monkeypatch.setattr("voxsub.ui.ocr_workspace.wait_for_desktop_settle", settle)
    try:
        workspace._begin_pick("screenshot")  # noqa: SLF001
        _app().processEvents()
        assert calls == [300, "picker"]
    finally:
        workspace.shutdown()
        workspace.deleteLater()


def test_live_ocr_stops_retry_timer_after_engine_failure(tmp_path):
    _app()
    workspace = OcrWorkspace(ConfigStore(tmp_path / "config.json"))
    try:
        workspace._revision = 7  # noqa: SLF001
        workspace._live_timer.start()  # noqa: SLF001
        workspace._on_failure(7, "live", "OCR 引擎不可用")  # noqa: SLF001
        assert not workspace._live_timer.isActive()  # noqa: SLF001
        assert workspace._live_paused  # noqa: SLF001
    finally:
        workspace.shutdown()
        workspace.deleteLater()


def test_render_translated_image_replaces_pixels_in_line_region():
    _app()
    capture = QImage(200, 100, QImage.Format.Format_RGB888)
    capture.fill(QColor(240, 240, 240))

    rendered = render_translated_image(capture, _frame())

    assert not rendered.isNull()
    assert rendered != capture
