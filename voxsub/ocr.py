"""Qt-independent OCR and OCR-translation services.

The UI owns screen selection and painting.  This module owns the replaceable
recognizer adapter, normalized text geometry, cheap frame-change detection,
and translation caching.  Keeping those boundaries separate lets a future
handwriting or stylized-text model replace RapidOCR without changing the
screen overlay.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from voxsub.logging_setup import get_logger
from voxsub.translate.base import TranslationError
from voxsub.translate.factory import TranslatorFactory

logger = get_logger("ocr")


class OcrUnavailableError(RuntimeError):
    """The configured OCR runtime cannot be loaded."""


@dataclass(frozen=True)
class OcrBox:
    """Axis-aligned OCR geometry in source-image pixels."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def expanded(self, padding: int, image_width: int, image_height: int) -> "OcrBox":
        pad = max(0, int(padding))
        return OcrBox(
            max(0, self.left - pad),
            max(0, self.top - pad),
            min(max(0, image_width), self.right + pad),
            min(max(0, image_height), self.bottom + pad),
        )


@dataclass(frozen=True)
class OcrLine:
    box: OcrBox
    text: str
    confidence: float


@dataclass(frozen=True)
class OcrFrame:
    width: int
    height: int
    lines: tuple[OcrLine, ...]
    elapsed_ms: int = 0

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True)
class TranslatedOcrLine:
    box: OcrBox
    source: str
    translation: str
    confidence: float


@dataclass(frozen=True)
class TranslatedOcrFrame:
    width: int
    height: int
    lines: tuple[TranslatedOcrLine, ...]
    ocr_elapsed_ms: int
    translate_elapsed_ms: int
    failed_lines: int = 0

    @property
    def source_text(self) -> str:
        return "\n".join(line.source for line in self.lines)

    @property
    def translation_text(self) -> str:
        return "\n".join(
            line.translation or f"[翻译失败] {line.source}" for line in self.lines
        )


def polygon_to_box(
    polygon: Iterable[Iterable[float]], image_width: int, image_height: int
) -> OcrBox | None:
    """Convert an arbitrary OCR quadrilateral into a clipped rectangle."""
    points: list[tuple[float, float]] = []
    for point in polygon:
        values = tuple(point)
        if len(values) < 2:
            continue
        x, y = float(values[0]), float(values[1])
        if math.isfinite(x) and math.isfinite(y):
            points.append((x, y))
    if not points:
        return None
    left = max(0, min(image_width, math.floor(min(x for x, _ in points))))
    top = max(0, min(image_height, math.floor(min(y for _, y in points))))
    right = max(0, min(image_width, math.ceil(max(x for x, _ in points))))
    bottom = max(0, min(image_height, math.ceil(max(y for _, y in points))))
    if right <= left or bottom <= top:
        return None
    return OcrBox(left, top, right, bottom)


def frame_fingerprint(image: np.ndarray, *, rows: int = 18, columns: int = 32) -> bytes:
    """Return a small perceptual signature without running the OCR engine."""
    if image.size == 0 or image.ndim < 2:
        return b""
    gray = image if image.ndim == 2 else image[..., :3].mean(axis=2)
    height, width = gray.shape[:2]
    y_index = np.linspace(0, max(0, height - 1), max(1, rows), dtype=np.intp)
    x_index = np.linspace(0, max(0, width - 1), max(1, columns), dtype=np.intp)
    sampled = gray[np.ix_(y_index, x_index)]
    return np.clip(sampled, 0, 255).astype(np.uint8).tobytes()


def fingerprint_distance(first: bytes, second: bytes) -> float:
    if not first or not second or len(first) != len(second):
        return 1.0
    difference = sum(abs(left - right) for left, right in zip(first, second))
    return difference / float(len(first) * 255)


def materially_changed(previous: bytes | None, current: bytes, threshold: float = 0.035) -> bool:
    if previous is None:
        return True
    return fingerprint_distance(previous, current) >= max(0.0, float(threshold))


class RapidOcrEngine:
    """Lazy, serialized RapidOCR adapter returning stable VoxSub data types."""

    def __init__(self, *, minimum_confidence: float = 0.52) -> None:
        self.minimum_confidence = max(0.0, min(1.0, float(minimum_confidence)))
        self._engine: Any | None = None
        self._lock = threading.Lock()

    def _ensure_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        try:
            from rapidocr import RapidOCR

            self._engine = RapidOCR(params={
                "Global.text_score": self.minimum_confidence,
                "Global.use_cls": True,
            })
        except Exception as exc:  # noqa: BLE001 - optional runtime boundary
            logger.exception("RapidOCR 初始化失败")
            raise OcrUnavailableError(f"OCR 引擎不可用: {exc}") from exc
        return self._engine

    def recognize(self, image: np.ndarray) -> OcrFrame:
        if image.size == 0 or image.ndim not in (2, 3):
            raise ValueError("OCR 图像为空或格式无效")
        started = time.perf_counter()
        with self._lock:
            result = self._ensure_engine()(image)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        height, width = image.shape[:2]
        lines = self._normalize_result(result, width, height)
        return OcrFrame(width, height, lines, elapsed_ms)

    def _normalize_result(
        self, result: Any, width: int, height: int
    ) -> tuple[OcrLine, ...]:
        boxes = getattr(result, "boxes", None)
        texts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        if boxes is None or texts is None or scores is None:
            return ()
        lines: list[OcrLine] = []
        for polygon, raw_text, raw_score in zip(boxes, texts, scores):
            text = str(raw_text or "").strip()
            score = float(raw_score)
            box = polygon_to_box(polygon, width, height)
            if text and box is not None and score >= self.minimum_confidence:
                lines.append(OcrLine(box, text, score))
        lines.sort(key=lambda line: (line.box.top, line.box.left))
        return tuple(lines)


def _translator_kind(config: Mapping[str, Any]) -> str:
    return {
        "fast": "opus-fast",
        "quality": "qwen-quality",
        "cloud": "cloud",
    }.get(str(config.get("translate_tier", "fast")), "opus-fast")


def _translator_key(config: Mapping[str, Any]) -> tuple[str, ...]:
    keys = (
        "translate_tier",
        "translate_model_id",
        "translate_api_key",
        "translate_base_url",
        "translate_model",
    )
    return tuple(str(config.get(key, "")) for key in keys)


class OcrTranslationService:
    """One-owner translator with a bounded line cache for changing frames."""

    def __init__(self, *, cache_size: int = 512) -> None:
        self._translator: Any | None = None
        self._translator_config_key: tuple[str, ...] | None = None
        self._cache: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._cache_size = max(16, int(cache_size))

    def _ensure_translator(self, config: Mapping[str, Any]) -> Any:
        key = _translator_key(config)
        if self._translator is not None and key == self._translator_config_key:
            return self._translator
        self.close()
        self._translator = TranslatorFactory.create(_translator_kind(config), dict(config))
        self._translator_config_key = key
        warmup = getattr(self._translator, "warmup", None)
        if callable(warmup):
            warmup()
        return self._translator

    def _remember(self, key: tuple[str, str, str], translation: str) -> None:
        self._cache[key] = translation
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _translate_text(
        self, translator: Any, text: str, source_lang: str, target_lang: str
    ) -> tuple[str, bool]:
        key = (source_lang, target_lang, text)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached, False
        try:
            translated = str(translator.translate(
                text, source_lang, target_lang, timeout_ms=12_000
            )).strip()
        except (TranslationError, OSError, RuntimeError):
            logger.warning("OCR 行翻译失败: %r", text, exc_info=True)
            return "", True
        if translated:
            self._remember(key, translated)
            return translated, False
        return "", True

    def translate_frame(
        self,
        frame: OcrFrame,
        source_lang: str,
        target_lang: str,
        config: Mapping[str, Any],
        *,
        maximum_lines: int = 48,
        maximum_characters: int = 3000,
    ) -> TranslatedOcrFrame:
        started = time.perf_counter()
        translator = self._ensure_translator(config)
        translated: list[TranslatedOcrLine] = []
        failures = 0
        consumed = 0
        for line in frame.lines[:max(1, maximum_lines)]:
            remaining = max(0, maximum_characters - consumed)
            if remaining <= 0:
                break
            source = line.text[:remaining]
            consumed += len(source)
            target, failed = self._translate_text(
                translator, source, source_lang, target_lang
            )
            failures += int(failed)
            translated.append(TranslatedOcrLine(
                line.box, source, target, line.confidence
            ))
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return TranslatedOcrFrame(
            frame.width,
            frame.height,
            tuple(translated),
            frame.elapsed_ms,
            elapsed_ms,
            failures,
        )

    def close(self) -> None:
        translator, self._translator = self._translator, None
        self._translator_config_key = None
        if translator is not None:
            try:
                translator.close()
            except Exception:  # noqa: BLE001 - shutdown must remain best effort
                logger.debug("OCR 翻译器关闭失败", exc_info=True)


__all__ = [
    "OcrBox",
    "OcrFrame",
    "OcrLine",
    "OcrTranslationService",
    "OcrUnavailableError",
    "RapidOcrEngine",
    "TranslatedOcrFrame",
    "TranslatedOcrLine",
    "fingerprint_distance",
    "frame_fingerprint",
    "materially_changed",
    "polygon_to_box",
]
