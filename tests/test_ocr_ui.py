from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QFontMetrics, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from voxsub.config_store import ConfigStore  # noqa: E402
from voxsub.ocr import OcrBox, TranslatedOcrFrame, TranslatedOcrLine  # noqa: E402
from voxsub.ui.ocr_overlay import (  # noqa: E402
    OcrLiveControlBar,
    OcrTranslationOverlay,
    _fit_font,
    _inner_text_rect,
    _is_paragraph,
    _translation_layouts,
    _text_flags,
    _translation_rect,
    render_translated_image,
)
from voxsub.ui.ocr_workspace import OcrWorkspace  # noqa: E402
from voxsub.ui.screen_capture import CapturedRegion, qimage_to_bgr  # noqa: E402
from voxsub.ui import screen_capture  # noqa: E402
from voxsub.ui.i18n import language_manager  # noqa: E402


def _app():
    app = QApplication.instance() or QApplication([])
    language_manager.set_language("zh")
    return app


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
        assert jobs[0].config["ocr_maximum_lines"] == 20
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


def test_workspace_renders_screenshot_result_without_starting_capture(
    tmp_path, monkeypatch
):
    # This test checks the screenshot-result layout and write path, while the
    # dedicated ocr_cache tests cover the product rule that rejects C:.  pytest
    # uses C: for tmp_path on this machine, so keep those concerns independent.
    monkeypatch.setattr("voxsub.ocr_cache.is_system_drive", lambda _path: False)
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


def test_overlay_fits_unbroken_translation_inside_adaptive_box():
    """URLs and CJK text must not overflow or clip after wrapping."""
    base = QRect(100, 100, 160, 24)
    bounds = QRect(0, 0, 800, 600)
    source = "A source line"
    translation = "A" * 80

    rect = _translation_rect(base, bounds, source, translation)
    font = _fit_font(rect, translation, source)
    measured = QFontMetrics(font).boundingRect(
        _inner_text_rect(rect),
        int(_text_flags(_is_paragraph(source))),
        translation,
    )

    assert font.pixelSize() < 32
    assert measured.width() <= _inner_text_rect(rect).width()
    assert measured.height() <= _inner_text_rect(rect).height()


def test_overlay_shrinks_long_paragraph_to_capture_bounds():
    base = QRect(100, 100, 160, 24)
    bounds = QRect(0, 0, 800, 600)
    source = "A" * 100
    translation = "中" * 1200

    rect = _translation_rect(base, bounds, source, translation)
    font = _fit_font(rect, translation, source)
    measured = QFontMetrics(font).boundingRect(
        _inner_text_rect(rect),
        int(_text_flags(_is_paragraph(source))),
        translation,
    )

    assert rect.bottom() <= bounds.bottom()
    assert font.pixelSize() >= 4
    assert measured.width() <= _inner_text_rect(rect).width()
    assert measured.height() <= _inner_text_rect(rect).height()


def test_overlay_translation_boxes_never_overlap():
    frame = TranslatedOcrFrame(
        400,
        120,
        (
            TranslatedOcrLine(OcrBox(10, 40, 170, 62), "first", "A" * 80, 0.99),
            TranslatedOcrLine(OcrBox(190, 40, 350, 62), "second", "B" * 80, 0.99),
        ),
        20,
        30,
    )

    layouts = _translation_layouts(frame, QRect(0, 0, 400, 120))

    assert len(layouts) == 2
    assert not layouts[0][1].intersects(layouts[1][1])


def test_failed_translation_leaves_source_image_uncovered():
    _app()
    capture = QImage(200, 100, QImage.Format.Format_RGB888)
    capture.fill(QColor(240, 240, 240))
    frame = TranslatedOcrFrame(
        200,
        100,
        (TranslatedOcrLine(OcrBox(10, 12, 160, 45), "Hello", "", 0.99),),
        20,
        30,
    )

    rendered = render_translated_image(capture, frame)

    assert rendered == capture.convertToFormat(QImage.Format.Format_ARGB32)


def test_windows_capture_affinity_is_applied_to_overlay_window(monkeypatch):
    calls = []
    fake_user32 = SimpleNamespace(
        SetWindowDisplayAffinity=lambda hwnd, affinity: (
            calls.append((hwnd, affinity)) or 1
        )
    )
    monkeypatch.setattr(screen_capture, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        screen_capture,
        "ctypes",
        SimpleNamespace(windll=SimpleNamespace(user32=fake_user32)),
    )
    widget = SimpleNamespace(winId=lambda: 4321)

    assert screen_capture.exclude_window_from_capture(widget)
    assert calls == [(4321, 0x00000011)]
