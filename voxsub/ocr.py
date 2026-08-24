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
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from voxsub.logging_setup import get_logger
from voxsub.translate.base import TranslationError
from voxsub.translate.factory import TranslatorFactory

logger = get_logger("ocr")

_SLOW_LIVE_MODELS = frozenset({
    "ocr-rapidocr-v6-medium",
    "ocr-rapidocr-v5-document",
})


class OcrUnavailableError(RuntimeError):
    """The configured OCR runtime cannot be loaded."""


def preferred_ocr_backend() -> tuple[str, dict[str, Any]]:
    """Return the packaged ONNX provider and RapidOCR engine parameters."""
    try:
        import onnxruntime as ort

        providers = set(ort.get_available_providers())
    except Exception:  # noqa: BLE001 - optional runtime probe
        providers = set()
    if "CUDAExecutionProvider" in providers:
        return "GPU · CUDA", {"EngineConfig.onnxruntime.use_cuda": True}
    if "DmlExecutionProvider" in providers:
        return "GPU · DirectML", {"EngineConfig.onnxruntime.use_dml": True}
    return "CPU", {}


def live_ocr_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Select a responsive live OCR path without changing static-image quality."""
    prepared = dict(config)
    prepared["ocr_live_mode"] = True
    backend, _params = preferred_ocr_backend()
    selected = str(prepared.get(
        "ocr_model_id", "ocr-rapidocr-v6-small-builtin") or
        "ocr-rapidocr-v6-small-builtin")
    if backend == "CPU" and selected in _SLOW_LIVE_MODELS:
        prepared["ocr_live_fallback_from"] = selected
        prepared["ocr_model_id"] = "ocr-rapidocr-v6-small-builtin"
    return prepared


def _rapidocr_model_params(config: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve the selected, already-installed Model Hub OCR preset."""
    model_id = str(
        config.get("ocr_model_id", "ocr-rapidocr-v6-small-builtin") or
        "ocr-rapidocr-v6-small-builtin")
    if model_id == "ocr-rapidocr-v6-small-builtin":
        return {}

    from voxsub.model_catalog import ModelMarketplace, get_model
    from voxsub.model_storage import resolve_models_root

    model = get_model(model_id)
    if model is None or model.task != "ocr":
        raise OcrUnavailableError(f"未知 OCR 模型: {model_id}")
    configured_root = str(config.get("models_root", "") or "").strip()
    root = Path(configured_root) if configured_root else resolve_models_root()
    marketplace = ModelMarketplace(root)
    if not marketplace.is_installed(model):
        raise OcrUnavailableError(f"OCR 模型尚未安装或文件不完整: {model.name}")
    model_dir = marketplace.available_model_dir(model)
    from rapidocr.utils.typings import ModelType, OCRVersion

    params: dict[str, Any] = {
        "Det.model_path": str(model_dir / "det.onnx"),
        "Rec.model_path": str(model_dir / "rec.onnx"),
    }
    if model_id == "ocr-rapidocr-v6-tiny":
        params.update({
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Det.model_type": ModelType.TINY,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_type": ModelType.TINY,
        })
    elif model_id == "ocr-rapidocr-v6-medium":
        params.update({
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Det.model_type": ModelType.MEDIUM,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_type": ModelType.MEDIUM,
        })
    elif model_id == "ocr-rapidocr-v5-document":
        params.update({
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_type": ModelType.SERVER,
            "Det.lang_type": "ch",
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.SERVER,
            "Rec.lang_type": "ch",
            "Cls.model_path": str(model_dir / "cls.onnx"),
            "Cls.ocr_version": OCRVersion.PPOCRV5,
            "Cls.model_type": ModelType.MOBILE,
        })
    return params


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
    backend: str = "CPU"
    model_id: str = ""

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
    ocr_backend: str = "CPU"
    ocr_model_id: str = ""
    translation_requests: int = 0

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

    def __init__(
        self, *, minimum_confidence: float = 0.52,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        selected_config = dict(config or {})
        self.minimum_confidence = max(0.0, min(1.0, float(minimum_confidence)))
        self._engine: Any | None = None
        self._initialization_error: OcrUnavailableError | None = None
        self._model_params = _rapidocr_model_params(selected_config)
        self._model_id = str(selected_config.get(
            "ocr_model_id", "ocr-rapidocr-v6-small-builtin") or
            "ocr-rapidocr-v6-small-builtin")
        self._live_mode = bool(selected_config.get("ocr_live_mode", False))
        self._requested_backend, self._acceleration_params = preferred_ocr_backend()
        self._backend = "CPU"
        self._gpu_disabled = False
        self._lock = threading.Lock()

    def _ensure_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if self._initialization_error is not None:
            raise self._initialization_error
        try:
            # RapidOCR exposes this class through module-level __getattr__.
            # PyInstaller cannot reliably discover that lazy import, so import
            # the concrete module here and list it as an explicit hidden import
            # in the release build.
            from rapidocr.main import RapidOCR

            params = {
                "Global.text_score": self.minimum_confidence,
                # Desktop UI text is normally upright. Skipping orientation
                # classification cuts one model pass from every live frame;
                # static image translation retains the more tolerant path.
                "Global.use_cls": not self._live_mode,
            }
            params.update(self._model_params)
            if not self._gpu_disabled:
                params.update(self._acceleration_params)
            self._engine = RapidOCR(params=params)
            self._backend = self._detect_engine_backend(self._engine)
            logger.info(
                "OCR 引擎就绪: model=%s mode=%s backend=%s",
                self._model_id,
                "live" if self._live_mode else "image",
                self._backend,
            )
        except Exception as exc:  # noqa: BLE001 - optional runtime boundary
            if self._acceleration_params and not self._gpu_disabled:
                logger.warning(
                    "OCR GPU 初始化失败，回退 CPU: requested=%s error=%s",
                    self._requested_backend, exc,
                    exc_info=True,
                )
                self._gpu_disabled = True
                return self._ensure_engine()
            logger.exception("RapidOCR 初始化失败")
            self._initialization_error = OcrUnavailableError(
                f"OCR 引擎不可用: {exc}")
            raise self._initialization_error from exc
        return self._engine

    @staticmethod
    def _detect_engine_backend(engine: Any) -> str:
        for component_name in ("text_det", "text_rec", "text_cls"):
            component = getattr(engine, component_name, None)
            infer = getattr(component, "session", None)
            session = getattr(infer, "session", None)
            get_providers = getattr(session, "get_providers", None)
            if not callable(get_providers):
                continue
            providers = list(get_providers())
            if not providers:
                continue
            if providers[0] == "DmlExecutionProvider":
                return "GPU · DirectML"
            if providers[0] == "CUDAExecutionProvider":
                return "GPU · CUDA"
            return "CPU"
        return "CPU"

    def recognize(self, image: np.ndarray) -> OcrFrame:
        if image.size == 0 or image.ndim not in (2, 3):
            raise ValueError("OCR 图像为空或格式无效")
        started = time.perf_counter()
        with self._lock:
            engine = self._ensure_engine()
            try:
                result = engine(image)
            except Exception as exc:  # noqa: BLE001 - provider boundary
                if self._backend.startswith("GPU") and not self._gpu_disabled:
                    logger.warning(
                        "OCR GPU 推理失败，本次立即回退 CPU: backend=%s error=%s",
                        self._backend, exc,
                        exc_info=True,
                    )
                    self._gpu_disabled = True
                    self._engine = None
                    self._initialization_error = None
                    engine = self._ensure_engine()
                    result = engine(image)
                else:
                    raise
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        height, width = image.shape[:2]
        lines = self._normalize_result(result, width, height)
        return OcrFrame(
            width, height, lines, elapsed_ms, self._backend, self._model_id)

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


def _translation_chunks(
    texts: Sequence[str], *, maximum_items: int = 12,
    maximum_characters: int = 900,
) -> tuple[tuple[str, ...], ...]:
    """Bound OCR batches so prompts and outputs stay within a 2K context."""
    chunks: list[tuple[str, ...]] = []
    current: list[str] = []
    characters = 0
    for text in texts:
        if current and (
            len(current) >= max(1, maximum_items)
            or characters + len(text) > max(64, maximum_characters)
        ):
            chunks.append(tuple(current))
            current = []
            characters = 0
        current.append(text)
        characters += len(text)
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


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

    def _partition_cached_sources(
        self, sources: Sequence[str], source_lang: str, target_lang: str,
    ) -> tuple[dict[str, str], list[str]]:
        """Return cache hits and unique misses while preserving source order."""
        targets: dict[str, str] = {}
        uncached: list[str] = []
        seen: set[str] = set()
        for source in sources:
            key = (source_lang, target_lang, source)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                targets[source] = cached
            elif source not in seen:
                seen.add(source)
                uncached.append(source)
        return targets, uncached

    def _translate_uncached_sources(
        self,
        translator: Any,
        sources: Sequence[str],
        source_lang: str,
        target_lang: str,
    ) -> tuple[dict[str, str], int]:
        """Translate unique cache misses in bounded batches with safe fallback."""
        targets: dict[str, str] = {}
        requests = 0
        batch_translate = getattr(translator, "translate_many", None)
        if not callable(batch_translate):
            for source in sources:
                requests += 1
                target, _failed = self._translate_text(
                    translator, source, source_lang, target_lang)
                if target:
                    targets[source] = target
            return targets, requests

        for chunk in _translation_chunks(sources):
            requests += 1
            try:
                outputs = list(batch_translate(
                    list(chunk), source_lang, target_lang,
                    timeout_ms=12_000,
                ))
                if len(outputs) != len(chunk):
                    raise TranslationError(
                        "OCR 批量翻译返回数量与输入不一致")
            except (TranslationError, OSError, RuntimeError, ValueError):
                logger.warning(
                    "OCR 批量翻译失败，回退逐行: lines=%d", len(chunk),
                    exc_info=True,
                )
                outputs = []
                for source in chunk:
                    requests += 1
                    target, _failed = self._translate_text(
                        translator, source, source_lang, target_lang)
                    outputs.append(target)
            for source, target in zip(chunk, outputs):
                cleaned = str(target or "").strip()
                if cleaned:
                    self._remember(
                        (source_lang, target_lang, source), cleaned)
                    targets[source] = cleaned
        return targets, requests

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
        prepared: list[tuple[OcrLine, str]] = []
        consumed = 0
        for line in frame.lines[:max(1, maximum_lines)]:
            remaining = max(0, maximum_characters - consumed)
            if remaining <= 0:
                break
            source = line.text[:remaining]
            consumed += len(source)
            prepared.append((line, source))

        sources = [source for _line, source in prepared]
        targets, uncached = self._partition_cached_sources(
            sources, source_lang, target_lang)
        fresh, requests = self._translate_uncached_sources(
            translator, uncached, source_lang, target_lang)
        targets.update(fresh)

        translated: list[TranslatedOcrLine] = []
        failures = 0
        for line, source in prepared:
            target = targets.get(source, "")
            failures += int(not target)
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
            frame.backend,
            frame.model_id,
            requests,
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
