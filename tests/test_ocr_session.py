"""OCR session contracts. Synthetic pixels only; never access screen/audio/UI."""
from __future__ import annotations
from types import SimpleNamespace
import threading
import numpy as np
import pytest
from PIL import Image
from voxsub import ocr
from voxsub.ocr_session import OcrSession
from voxsub.ocr_layout import fitted_text, paint_translation


@pytest.fixture
def engines(monkeypatch):
    made = []
    class Engine:
        def __init__(self, **options):
            self.options, self.calls, self.closed = options, 0, 0
            made.append(self)
        def recognize(self, pixels):
            self.calls += 1
            return ocr.OcrFrame(pixels.shape[1], pixels.shape[0],
                (ocr.OcrLine(ocr.OcrBox(1, 1, 19, 10), "Hello world", .98),), 1, "CPU", self.options["config"].get("ocr_model_id", "builtin"))
        def close(self):
            self.closed += 1
    monkeypatch.setattr(ocr, "RapidOcrEngine", Engine)
    return made


class Translator:
    def __init__(self):
        self.calls, self.closed, self.fail = [], 0, False
    def translate(self, text, source, target, **kw):
        self.calls.append((text, source, target))
        if self.fail:
            raise RuntimeError("private input must not leak")
        return "你好世界" if target == "zh" else "bonjour"
    def close(self):
        self.closed += 1


def test_static_uses_selected_model_confidence_and_releases_each_request(engines):
    session = OcrSession()
    pixels = np.zeros((20, 30, 3), dtype=np.uint8)
    for _ in range(2):
        result = session.recognize(pixels, {"ocr_model_id": "selected", "ocr_minimum_confidence": .72}, translate=False)
        assert result["ocrModelId"] == "selected"
        assert result["failedLines"] == 0
    assert len(engines) == 2
    assert all(engine.options["minimum_confidence"] == .72 and engine.closed == 1 for engine in engines)
    session.close()


def test_identical_live_frame_skips_but_one_pixel_change_is_never_lost(engines):
    now = [0.0]
    session = OcrSession(clock=lambda: now[0])
    pixels = np.zeros((20, 30, 3), dtype=np.uint8)
    assert not session.recognize(pixels, {}, live=True, translate=False)["unchanged"]
    assert session.recognize(pixels, {}, live=True, translate=False)["unchanged"]
    assert engines[0].calls == 1
    pixels[19, 29, 2] = 1
    assert not session.recognize(pixels, {}, live=True, translate=False)["unchanged"]
    now[0] = 3
    assert not session.recognize(pixels, {}, live=True, translate=False)["unchanged"]
    assert engines[0].calls == 3
    session.release_live()
    assert engines[0].closed == 1
    session.close()
    session.close()
    with pytest.raises(ocr.OcrRuntimeClosedError):
        session.recognize(pixels, {})


def test_live_model_replacement_closes_previous_without_swapping_user_selection(engines):
    session = OcrSession()
    pixels = np.zeros((20, 30, 3), dtype=np.uint8)
    session.recognize(pixels, {"ocr_model_id": "quality"}, live=True, translate=False)
    session.recognize(pixels, {"ocr_model_id": "tiny"}, live=True, translate=False)
    assert engines[0].closed == 1
    assert engines[1].options["config"]["ocr_model_id"] == "tiny"
    session.close()
    assert engines[1].closed == 1


def test_translation_line_cache_direction_and_configuration_invalidation(engines, monkeypatch):
    translator = Translator()
    made = []
    monkeypatch.setattr(ocr.TranslatorFactory, "create", lambda *args: made.append(translator) or translator)
    session = OcrSession()
    pixels = np.zeros((20, 30, 3), dtype=np.uint8)
    result = session.recognize(pixels, {}, source="en", target="zh", live=True)
    assert result["lines"][0]["translation"] == "你好世界"
    assert result["lines"][0]["box"] == [1, 1, 19, 10]
    pixels[0, 0] = 255
    session.recognize(pixels, {}, source="en", target="zh", live=True)
    assert len(translator.calls) == 1
    session.recognize(pixels, {}, source="en", target="fr", live=True)
    assert len(translator.calls) == 2
    session.recognize(pixels, {"translate_api_key": "changed-secret"}, source="en", target="fr", live=True)
    assert len(translator.calls) == 3
    # Translator settings do not unnecessarily rebuild the OCR engine.
    assert len(engines) == 1
    session.close()


def test_partial_failure_retries_same_pixels_and_does_not_log_text(engines, monkeypatch, caplog):
    translator = Translator()
    translator.fail = True
    monkeypatch.setattr(ocr.TranslatorFactory, "create", lambda *args: translator)
    session = OcrSession()
    pixels = np.zeros((20, 30, 3), dtype=np.uint8)
    for _ in range(2):
        result = session.recognize(pixels, {}, source="en", target="zh", live=True)
        assert result["failedLines"] == 1
        assert not result["unchanged"]
    assert len(translator.calls) == 2
    assert "private input must not leak" not in caplog.text
    assert "Hello world" not in caplog.text
    translator.fail = False
    assert session.recognize(pixels, {}, source="en", target="zh", live=True)["failedLines"] == 0
    session.close()


def test_new_session_provider_is_borrowed_never_closed(engines):
    translator = Translator()
    session = OcrSession(translator_provider=lambda: (translator, 3))
    session.recognize(np.zeros((20, 30, 3), dtype=np.uint8), {}, source="en", target="zh")
    session.close()
    assert translator.closed == 0


def test_translation_overlay_export_never_changes_pixels_outside_rect():
    from PIL import ImageFont
    original = Image.new("RGB", (180, 120), (252, 252, 252))
    output = original.copy()
    load = lambda size: ImageFont.load_default(size=size)
    rect = (15, 15, 95, 50)
    assert paint_translation(output, rect, "A very long translation " * 40, (10, 10, 10), (255, 255, 255), load)
    before, after = np.asarray(original), np.asarray(output)
    mask = np.ones(before.shape[:2], dtype=bool)
    mask[15:50, 15:95] = False
    assert np.array_equal(before[mask], after[mask])
    assert not np.array_equal(before[~mask], after[~mask])


def test_layout_wraps_short_multiline_text_without_unnecessary_truncation():
    from PIL import ImageFont
    load = lambda size: ImageFont.load_default(size=size)
    text, font, box, truncated = fitted_text("Hello world\nNext line", 160, 80, load)
    assert not truncated
    assert "\n" in text
    assert box[2] - box[0] <= 160 and box[3] - box[1] <= 80


def test_recognition_only_multiple_lines_has_truly_empty_translation(engines):
    session = OcrSession()
    result = session.recognize(np.zeros((20, 30, 3), dtype=np.uint8), {}, translate=False)
    assert result["translation"] == ""
    assert result["translationRequests"] == 0
    session.close()


def test_handler_export_respects_configured_root_limit_and_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend/backend"))
    import ipc_server
    from contract_validation import ContractRegistry
    from voxsub.config_store import ConfigStore
    monkeypatch.setattr("voxsub.ocr_cache.is_system_drive", lambda _: False)
    root = tmp_path / "chosen-cache"
    ConfigStore().update({"ocr_cache_root": str(root), "ocr_cache_limit": 2})
    source = tmp_path / "original.png"
    Image.new("RGB", (100, 70), "white").save(source)
    service = ipc_server.BackendService()
    registry = ContractRegistry(Path(__file__).resolve().parents[1] / "contracts")
    values = {"source": str(source), "lines": [{"translation": "Hello", "box": [5, 5, 80, 40]}]}
    registry.validate_args("render_ocr_image", values)
    try:
        for _ in range(3):
            result = service.handle("render_ocr_image", values)
            registry.validate_result("render_ocr_image", result)
            assert Path(result["path"]).parent == root / "translated"
        assert len(list((root / "translated").glob("*.png"))) == 2
        assert service.handle("ocr_cache_dir", {})["path"] == str(root)
        registry.validate_args("ocr_recognize", {"path": str(source), "live": True})
        registry.validate_result("ocr_release", service.handle("ocr_release", {}))
    finally:
        service.close()


def test_handler_applies_exif_orientation_before_returning_pixel_boxes(engines, tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend/backend"))
    import ipc_server
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    image = Image.new("RGB", (30, 20), "white")
    exif = image.getexif()
    exif[274] = 6
    source = tmp_path / "rotated.jpg"
    image.save(source, exif=exif)
    service = ipc_server.BackendService()
    try:
        result = service.handle("ocr_recognize", {"path": str(source), "translate": False})
        assert (result["width"], result["height"]) == (20, 30)
        assert engines[0].closed == 1
    finally:
        service.close()
