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
    """Build the fast first stage used immediately after a screen change."""
    prepared = dict(config)
    prepared["ocr_live_mode"] = True
    selected = str(prepared.get(
        "ocr_model_id", "ocr-rapidocr-v6-small-builtin") or
        "ocr-rapidocr-v6-small-builtin")
    staged = selected in _SLOW_LIVE_MODELS
    if staged:
        prepared["ocr_live_refine_model_id"] = selected
        prepared["ocr_model_id"] = "ocr-rapidocr-v6-small-builtin"
        prepared["ocr_live_fast_stage"] = True
        prepared["ocr_minimum_confidence"] = 0.56
        prepared["ocr_maximum_lines"] = 40
        prepared["ocr_maximum_characters"] = 3600
    else:
        prepared["ocr_minimum_confidence"] = 0.54
        prepared["ocr_maximum_lines"] = 48
        prepared["ocr_maximum_characters"] = 4000
    return prepared


def refinement_ocr_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Restore the selected quality model for a stable-screen correction pass."""
    prepared = dict(config)
    selected = str(prepared.get(
        "ocr_model_id", "ocr-rapidocr-v6-small-builtin") or
        "ocr-rapidocr-v6-small-builtin")
    prepared["ocr_model_id"] = selected
    prepared["ocr_live_mode"] = True
    prepared["ocr_refinement_mode"] = True
    prepared["ocr_minimum_confidence"] = 0.48
    prepared["ocr_maximum_lines"] = 72
    prepared["ocr_maximum_characters"] = 6500
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


def _median_line_height(lines: Sequence[OcrLine]) -> int:
    heights = sorted(max(1, line.box.height) for line in lines)
    return heights[len(heights) // 2] if heights else 16


def _same_row(left: OcrLine, right: OcrLine, median_height: int) -> bool:
    overlap = min(left.box.bottom, right.box.bottom) - max(
        left.box.top, right.box.top)
    minimum_height = max(1, min(left.box.height, right.box.height))
    gap = right.box.left - left.box.right
    return (
        overlap / minimum_height >= 0.58
        and -median_height // 2 <= gap <= max(10, round(median_height * 1.35))
    )


def _join_fragments(left: str, right: str) -> str:
    left = left.rstrip()
    right = right.lstrip()
    if not left or not right:
        return left + right
    no_space_before = ",.!?;:，。！？；：、)]}》」』"
    no_space_after = "([{《「『"
    if right[0] in no_space_before or left[-1] in no_space_after:
        return left + right
    if "\u3400" <= left[-1] <= "\u9fff" and "\u3400" <= right[0] <= "\u9fff":
        return left + right
    return f"{left} {right}"


def _merge_row_fragments(
    lines: Sequence[OcrLine], median_height: int
) -> tuple[OcrLine, ...]:
    rows: list[list[OcrLine]] = []
    for line in sorted(lines, key=lambda item: (item.box.top, item.box.left)):
        best: list[OcrLine] | None = None
        best_gap = 1_000_000
        for row in rows:
            last = row[-1]
            if _same_row(last, line, median_height):
                gap = abs(line.box.left - last.box.right)
                if gap < best_gap:
                    best, best_gap = row, gap
        if best is None:
            rows.append([line])
        else:
            best.append(line)

    merged: list[OcrLine] = []
    for fragments in rows:
        fragments.sort(key=lambda item: item.box.left)
        text = fragments[0].text
        for fragment in fragments[1:]:
            text = _join_fragments(text, fragment.text)
        weights = [max(1, len(fragment.text)) for fragment in fragments]
        confidence = sum(
            fragment.confidence * weight
            for fragment, weight in zip(fragments, weights)
        ) / sum(weights)
        merged.append(OcrLine(
            OcrBox(
                min(fragment.box.left for fragment in fragments),
                min(fragment.box.top for fragment in fragments),
                max(fragment.box.right for fragment in fragments),
                max(fragment.box.bottom for fragment in fragments),
            ),
            text,
            confidence,
        ))
    merged.sort(key=lambda item: (item.box.top, item.box.left))
    return tuple(merged)


def _starts_list_item(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return False
    if stripped[0] in "•·▪◦*-":
        return True
    return (
        len(stripped) >= 2
        and stripped[0].isdigit()
        and stripped[1] in ".)、)"
    )


def _paragraph_match_score(
    block: Sequence[OcrLine], candidate: OcrLine, median_height: int
) -> float | None:
    last = block[-1]
    vertical_advance = candidate.box.top - last.box.top
    vertical_gap = candidate.box.top - last.box.bottom
    if vertical_advance < max(3, round(min(last.box.height, candidate.box.height) * 0.42)):
        return None
    if vertical_gap > max(5, round(median_height * 0.62)):
        return None
    if len(block) >= 6 or sum(len(item.text) for item in block) + len(candidate.text) > 700:
        return None
    if _starts_list_item(candidate.text):
        return None
    if not (
        len(last.text) >= 18
        or len(candidate.text) >= 18
        or len(block) >= 2
    ):
        return None
    overlap = max(0, min(last.box.right, candidate.box.right) - max(
        last.box.left, candidate.box.left))
    overlap_ratio = overlap / max(1, min(last.box.width, candidate.box.width))
    left_delta = abs(last.box.left - candidate.box.left)
    if overlap_ratio < 0.42 and left_delta > max(18, round(median_height * 2.1)):
        return None
    return max(0, vertical_gap) * 4.0 + left_delta - overlap_ratio * median_height


def group_ocr_lines(
    lines: Sequence[OcrLine], image_width: int, image_height: int
) -> tuple[OcrLine, ...]:
    """Combine OCR fragments and neighboring prose rows into layout blocks.

    Dense documents are translated paragraph-by-paragraph instead of painting
    one independent box for every detected row. Short controls and isolated UI
    labels remain separate, while columns are kept apart by overlap/alignment
    checks.
    """
    if not lines:
        return ()
    median_height = _median_line_height(lines)
    rows = _merge_row_fragments(lines, median_height)
    blocks: list[list[OcrLine]] = []
    for row in rows:
        best: list[OcrLine] | None = None
        best_score = float("inf")
        for block in blocks:
            score = _paragraph_match_score(block, row, median_height)
            if score is not None and score < best_score:
                best, best_score = block, score
        if best is None:
            blocks.append([row])
        else:
            best.append(row)

    grouped: list[OcrLine] = []
    for block in blocks:
        if len(block) == 1:
            grouped.append(block[0])
            continue
        weights = [max(1, len(item.text)) for item in block]
        grouped.append(OcrLine(
            OcrBox(
                max(0, min(item.box.left for item in block)),
                max(0, min(item.box.top for item in block)),
                min(max(0, image_width), max(item.box.right for item in block)),
                min(max(0, image_height), max(item.box.bottom for item in block)),
            ),
            "\n".join(item.text for item in block),
            sum(
                item.confidence * weight
                for item, weight in zip(block, weights)
            ) / sum(weights),
        ))
    grouped.sort(key=lambda item: (item.box.top, item.box.left))
    return tuple(grouped)


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
        self._refinement_mode = bool(
            selected_config.get("ocr_refinement_mode", False))
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
                "Global.use_cls": not self._live_mode or self._refinement_mode,
            }
            params.update(self._model_params)
            if not self._gpu_disabled:
                params.update(self._acceleration_params)
            self._engine = RapidOCR(params=params)
            self._backend = self._detect_engine_backend(self._engine)
            logger.info(
                "OCR 引擎就绪: model=%s mode=%s backend=%s",
                self._model_id,
                ("refine" if self._refinement_mode else
                 "live" if self._live_mode else "image"),
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


def _select_translation_lines(
    lines: Sequence[OcrLine], maximum_lines: int, maximum_characters: int
) -> tuple[OcrLine, ...]:
    """Retain the most useful text blocks when a very busy screen hits a cap."""
    line_limit = max(1, int(maximum_lines))
    character_limit = max(64, int(maximum_characters))
    if len(lines) <= line_limit and sum(len(line.text) for line in lines) <= character_limit:
        return tuple(lines)

    indexed = list(enumerate(lines))
    anchor = max(
        lines,
        key=lambda line: len(line.text) * max(1, line.box.width),
    )

    def in_dominant_column(line: OcrLine) -> bool:
        overlap = max(0, min(anchor.box.right, line.box.right) - max(
            anchor.box.left, line.box.left))
        return overlap / max(1, min(anchor.box.width, line.box.width)) >= 0.45

    primary = [item for item in indexed if in_dominant_column(item[1])]
    secondary = sorted(
        (item for item in indexed if not in_dominant_column(item[1])),
        key=lambda item: (
            len(item[1].text) * (1.0 + 0.2 * item[1].text.count("\n")),
            item[1].box.width * item[1].box.height,
        ),
        reverse=True,
    )
    ranked = primary + secondary
    selected: list[tuple[int, OcrLine]] = []
    consumed = 0
    for index, line in ranked:
        if len(selected) >= line_limit:
            break
        length = len(line.text)
        if selected and consumed + length > character_limit:
            continue
        selected.append((index, line))
        consumed += length
        if consumed >= character_limit:
            break
    selected.sort(key=lambda item: item[0])
    return tuple(line for _index, line in selected)


def _matches_source_script(text: str, source_lang: str) -> bool:
    """Skip target-language UI chrome that would only add clutter and latency."""
    language = str(source_lang or "").lower().split("-", 1)[0]
    ascii_letters = sum(
        ("a" <= character.lower() <= "z") for character in text)
    cjk = sum("\u3400" <= character <= "\u9fff" for character in text)
    if language == "en":
        return ascii_letters >= max(2, cjk * 2)
    if language == "zh":
        return cjk > 0
    return True


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

    def warmup(self, config: Mapping[str, Any]) -> None:
        """Load the configured translator before the first visible OCR frame."""
        self._ensure_translator(config)

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

        batch_characters = 1400 if source_lang.lower().startswith("en") else 800
        for chunk in _translation_chunks(
            sources, maximum_items=10, maximum_characters=batch_characters
        ):
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
        prepared: list[tuple[OcrLine, str]] = []
        consumed = 0
        source_lines = tuple(
            line for line in frame.lines
            if _matches_source_script(line.text, source_lang)
        )
        grouped = (
            group_ocr_lines(source_lines, frame.width, frame.height)
            if bool(config.get("ocr_group_paragraphs", True))
            else source_lines
        )
        selected = _select_translation_lines(
            grouped, maximum_lines, maximum_characters)
        for line in selected:
            remaining = max(0, maximum_characters - consumed)
            if remaining <= 0:
                break
            source = line.text[:remaining]
            consumed += len(source)
            prepared.append((line, source))

        if not prepared:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return TranslatedOcrFrame(
                frame.width,
                frame.height,
                (),
                frame.elapsed_ms,
                elapsed_ms,
                0,
                frame.backend,
                frame.model_id,
                0,
            )

        translator = self._ensure_translator(config)
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
    "group_ocr_lines",
    "live_ocr_config",
    "materially_changed",
    "polygon_to_box",
    "preferred_ocr_backend",
    "refinement_ocr_config",
]
