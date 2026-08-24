from __future__ import annotations

from pathlib import Path
import shutil
from types import SimpleNamespace

import numpy as np

from voxsub.ocr import (
    OcrBox,
    OcrFrame,
    OcrLine,
    OcrTranslationService,
    RapidOcrEngine,
    fingerprint_distance,
    frame_fingerprint,
    materially_changed,
    polygon_to_box,
)
from voxsub.ui.ocr_worker import OcrJob, OcrWorker


def test_polygon_box_is_clipped_and_rejects_degenerate_input():
    box = polygon_to_box(
        [(-5.2, 3.4), (103.8, 2.0), (101.0, 44.2), (-1.0, 45.0)],
        100,
        40,
    )

    assert box == OcrBox(0, 2, 100, 40)
    assert polygon_to_box([(5, 5), (5, 5)], 100, 40) is None


def test_frame_fingerprint_skips_identical_frames_and_detects_large_change():
    white = np.full((180, 320, 3), 255, dtype=np.uint8)
    changed = white.copy()
    changed[:, 150:] = 0
    first = frame_fingerprint(white)
    same = frame_fingerprint(white.copy())
    second = frame_fingerprint(changed)

    assert first == same
    assert fingerprint_distance(first, same) == 0.0
    assert not materially_changed(first, same)
    assert materially_changed(first, second)


def test_rapidocr_output_is_normalized_sorted_and_confidence_filtered():
    engine = RapidOcrEngine(minimum_confidence=0.6)
    result = SimpleNamespace(
        boxes=np.array([
            [[40, 50], [80, 50], [80, 70], [40, 70]],
            [[10, 5], [70, 5], [70, 25], [10, 25]],
            [[5, 90], [30, 90], [30, 105], [5, 105]],
        ], dtype=np.float32),
        txts=("second", "first", "weak"),
        scores=(0.91, 0.99, 0.4),
    )

    lines = engine._normalize_result(result, 100, 100)  # noqa: SLF001

    assert [line.text for line in lines] == ["first", "second"]
    assert lines[0].box == OcrBox(10, 5, 70, 25)


def test_translation_service_reuses_a_bounded_line_cache(monkeypatch):
    calls: list[str] = []

    class FakeTranslator:
        def warmup(self):
            return None

        def translate(self, text, source, target, *, timeout_ms):
            calls.append(text)
            return f"{target}:{text}"

        def close(self):
            return None

    monkeypatch.setattr(
        "voxsub.ocr.TranslatorFactory.create",
        lambda _kind, _config: FakeTranslator(),
    )
    frame = OcrFrame(300, 120, (
        OcrLine(OcrBox(0, 0, 80, 30), "Hello", 0.99),
        OcrLine(OcrBox(0, 40, 80, 70), "Hello", 0.98),
        OcrLine(OcrBox(0, 80, 100, 110), "World", 0.97),
    ), 15)
    service = OcrTranslationService(cache_size=16)

    first = service.translate_frame(frame, "en", "zh", {"translate_tier": "fast"})
    second = service.translate_frame(frame, "en", "zh", {"translate_tier": "fast"})
    service.close()

    assert calls == ["Hello", "World"]
    assert first.translation_text == "zh:Hello\nzh:Hello\nzh:World"
    assert second.translation_text == first.translation_text


def test_real_rapidocr_smoke_recognizes_generated_english_text():
    import cv2

    image = np.full((150, 620, 3), 255, dtype=np.uint8)
    cv2.putText(
        image,
        "Hello VoxSub OCR",
        (20, 92),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.45,
        (0, 0, 0),
        3,
        cv2.LINE_AA,
    )

    frame = RapidOcrEngine().recognize(image)

    assert "Hello VoxSub OCR" in frame.text
    assert frame.lines[0].confidence > 0.9
    assert frame.lines[0].box.width > 300


def test_downloaded_ocr_preset_uses_model_hub_files(tmp_path):
    import cv2
    import rapidocr

    package_models = Path(rapidocr.__file__).resolve().parent / "models"
    model_dir = tmp_path / "models" / "ocr" / "rapidocr-v6-tiny"
    model_dir.mkdir(parents=True)
    shutil.copyfile(
        package_models / "PP-OCRv6_det_small.onnx", model_dir / "det.onnx")
    shutil.copyfile(
        package_models / "PP-OCRv6_rec_small.onnx", model_dir / "rec.onnx")
    engine = RapidOcrEngine(config={
        "ocr_model_id": "ocr-rapidocr-v6-tiny",
        "models_root": str(tmp_path / "models"),
    })
    image = np.full((120, 420, 3), 255, dtype=np.uint8)
    cv2.putText(image, "OCR MODEL", (15, 78), cv2.FONT_HERSHEY_SIMPLEX,
                1.25, (0, 0, 0), 3, cv2.LINE_AA)

    frame = engine.recognize(image)

    assert "OCR" in frame.text.upper()


def test_worker_keeps_ocr_text_when_translation_backend_fails():
    class BrokenTranslator:
        def translate_frame(self, *_args, **_kwargs):
            raise RuntimeError("translator unavailable")

    frame = OcrFrame(
        200,
        80,
        (OcrLine(OcrBox(5, 5, 150, 35), "Readable source", 0.98),),
        12,
    )
    job = OcrJob(
        1,
        "screenshot",
        np.zeros((80, 200, 3), dtype=np.uint8),
        "en",
        "zh",
        {},
    )

    result, warning = OcrWorker._translate_or_retain(  # noqa: SLF001
        frame, BrokenTranslator(), job
    )

    assert result.source_text == "Readable source"
    assert result.lines[0].translation == ""
    assert result.failed_lines == 1
    assert "翻译失败" in warning
