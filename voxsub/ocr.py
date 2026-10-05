"""Qt-independent OCR and OCR-translation services.

The UI owns screen selection and painting.  This module owns the replaceable
recognizer adapter, normalized text geometry, cheap frame-change detection,
and translation caching.  Keeping those boundaries separate lets a future
handwriting or stylized-text model replace RapidOCR without changing the
screen overlay.
"""
from __future__ import annotations

from voxsub.language_registry import split_language_pair

import hashlib
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from voxsub.logging_setup import get_logger
from voxsub.diagnostic_trace import error as trace_error
from voxsub.translate.base import TranslationError
from voxsub.translate.factory import TranslatorFactory

logger = get_logger("ocr")

_SLOW_LIVE_MODELS = frozenset({
    "ocr-rapidocr-v6-medium",
    "ocr-rapidocr-v5-document",
})


class OcrUnavailableError(RuntimeError):
    """The configured OCR runtime cannot be loaded."""


class OcrRuntimeClosedError(OcrUnavailableError):
    """OCR 运行时已经被独立回收，使用者却还在用它。

    明确报错是刻意的：静默重建会掩盖\"谁在会话结束后仍持有这个实例\"的调用
    错误，静默继续用则会用到已经释放的原生会话。
    """


#: 凭据类配置：进配置指纹前先哈希。指纹会进缓存键、日志与诊断输出，
#: 原文绝不能落进去（docs/ARCHITECTURE.md §七）。
_SECRET_CONFIG_KEYS = frozenset({
    "api_key", "translate_api_key", "stt_api_key",
})

#: 会影响**译文结果**的配置维度。缓存键必须覆盖这些键 —— 少一个就会出现
#: \"设置保存了、界面也显示改了，但译文还是旧配置的\"。
RESULT_CONFIG_KEYS: tuple[str, ...] = (
    # 翻译方向：语言对既决定档位能否翻译，也决定译文本身
    "lang_pair", "src_lang", "dst_lang",
    # 档位与端点。旧别名 api_key/base_url/model 不是历史包袱 ——
    # CloudTranslator 真的会读它们（voxsub/translate/cloud.py:73-81），
    # 只列新键会导致改旧别名后旧译文继续命中。
    "translate_tier", "translate_model_id", "translate_api_key",
    "translate_base_url", "translate_model", "api_key", "base_url", "model",
    # 本地权重解析位置：换模型目录等于换权重
    "models_root", "models_root_mode",
    # OCR 侧：决定\"送去翻译的文本本身\"与批量切分
    "ocr_model_id", "ocr_minimum_confidence", "ocr_maximum_lines",
    "ocr_maximum_characters", "ocr_group_paragraphs",
    "ocr_live_batch_items", "ocr_live_batch_characters",
)

#: 决定**翻译器实例身份**的维度。刻意比 RESULT_CONFIG_KEYS 窄：live 与
#: refinement 两档的 OCR 调优值（置信度/行数/字符数）在同一会话里逐帧变化，
#: 把它们算进身份会让翻译器每帧重建。第一阶段优先正确性，既不引入资源池，
#: 也不制造无谓的重建。
TRANSLATOR_CONFIG_KEYS: tuple[str, ...] = (
    "translate_tier", "translate_model_id", "translate_api_key",
    "translate_base_url", "translate_model", "api_key", "base_url", "model",
    "models_root", "models_root_mode",
)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _config_value(config: Mapping[str, Any] | Any, key: str) -> str:
    if isinstance(config, Mapping):
        raw = config.get(key)
    else:
        raw = getattr(config, key, None)
    return str(raw if raw is not None else "")


def _config_signature(
    config: Mapping[str, Any] | Any,
    keys: Sequence[str],
    source_lang: str = "",
    target_lang: str = "",
) -> str:
    """把一组配置维度折成一个短指纹（凭据只贡献哈希，不落原文）。"""
    parts = [
        f"{key}={_hash_text(value) if key in _SECRET_CONFIG_KEYS else value}"
        for key, value in ((key, _config_value(config, key)) for key in keys)
    ]
    parts.append(f"pair={source_lang}->{target_lang}")
    return _hash_text("\x1f".join(parts))[:16]


def ocr_result_signature(
    config: Mapping[str, Any] | Any, *,
    source_lang: str = "", target_lang: str = "",
) -> str:
    """译文结果的配置指纹：缓存键带上它，改配置后旧条目自然不命中。"""
    return _config_signature(
        config, RESULT_CONFIG_KEYS, source_lang, target_lang)


def translator_config_signature(
    config: Mapping[str, Any] | Any, *,
    source_lang: str = "", target_lang: str = "",
) -> str:
    """翻译器实例身份的配置指纹。"""
    return _config_signature(
        config, TRANSLATOR_CONFIG_KEYS, source_lang, target_lang)


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
        # The first frame is a latency-sensitive preview.  Keeping this
        # budget bounded prevents a dense page from turning into several
        # serial LLM requests; the stable-screen refinement pass restores the
        # selected model and full document budget afterwards.
        prepared["ocr_maximum_lines"] = 20
        prepared["ocr_maximum_characters"] = 2200
    else:
        prepared["ocr_minimum_confidence"] = 0.54
        prepared["ocr_maximum_lines"] = 24
        prepared["ocr_maximum_characters"] = 2400
    prepared["ocr_live_batch_items"] = 10
    source_lang = split_language_pair(prepared.get("lang_pair", "zh-en"))[0]
    # English source usually compresses into fewer target tokens; Chinese
    # source expands into English, so keep that batch smaller to avoid a
    # truncated JSON response from the local model.
    prepared["ocr_live_batch_characters"] = 1200 if source_lang == "en" else 800
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
        engine_factory: Callable[[dict[str, Any]], Any] | None = None,
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
        self._closed = False
        #: 唯一的构造缝：默认走真正的 RapidOCR；测试里换成替身，这样
        #: “回收 / 幂等 / 关闭后使用”这些纯逻辑不需要加载 ONNX 模型。
        self._engine_factory = engine_factory

    @property
    def closed(self) -> bool:
        """本实例是否已被独立回收。"""
        return self._closed

    def _create_engine(self, params: dict[str, Any]) -> Any:
        if self._engine_factory is not None:
            return self._engine_factory(params)
        # RapidOCR exposes this class through module-level __getattr__.
        # PyInstaller cannot reliably discover that lazy import, so import
        # the concrete module here and list it as an explicit hidden import
        # in the release build.
        from rapidocr.main import RapidOCR

        return RapidOCR(params=params)

    def _ensure_engine(self) -> Any:
        if self._closed:
            raise OcrRuntimeClosedError(
                "OCR 引擎已被独立回收：请新建 RapidOcrEngine，"
                "不要在 close() 之后继续复用同一个实例")
        if self._engine is not None:
            return self._engine
        if self._initialization_error is not None:
            raise self._initialization_error
        try:
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
            self._engine = self._create_engine(params)
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
        if self._closed:
            raise OcrRuntimeClosedError(
                "OCR 引擎已被独立回收：请新建 RapidOcrEngine，"
                "不要在 close() 之后继续复用同一个实例")
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
                    # 失败的引擎真的释放掉再重建：只把引用置空会留下已经加载
                    # 的 ONNX 会话（可能还占着显存），等于漏一处。
                    failed, self._engine = self._engine, None
                    self._release_engine(failed)
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

    @staticmethod
    def _release_engine(engine: Any) -> None:
        """尽力释放一个引擎实例；失败只记日志，绝不把关闭路径炸掉。"""
        if engine is None:
            return
        for name in ("close", "release", "shutdown"):
            method = getattr(engine, name, None)
            if callable(method):
                try:
                    method()
                except Exception:  # noqa: BLE001 - shutdown must stay best effort
                    logger.debug("OCR 引擎 %s() 释放失败", name, exc_info=True)
                return

    def close(self) -> None:
        """独立回收本实例持有的引擎。

        · **不依赖会话**：OCR 独立工作时也是这样回收的（谁创建、谁回收）。
        · **幂等**：重复调用既不报错也不重复释放。
        · 关闭后再调用 ``recognize()`` 会抛 ``OcrRuntimeClosedError``，
          而不是静默重建或使用已释放的会话。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            engine, self._engine = self._engine, None
            self._initialization_error = None
        self._release_engine(engine)

    def __enter__(self) -> "RapidOcrEngine":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _translator_kind(config: Mapping[str, Any]) -> str:
    # 映射表在 factory 里（单一来源）。此处曾有一份副本，且 ipc_server 还
    # 写过另一个不存在的配置键，三处各说各话。
    from voxsub.translate.factory import kind_for_tier  # noqa: PLC0415

    return kind_for_tier(str(config.get("translate_tier", "fast")), dict(config))


def _translator_key(
    config: Mapping[str, Any], *,
    source_lang: str = "", target_lang: str = "",
) -> tuple[str, str]:
    """翻译器实例身份：档位 + 配置指纹。

    维度表只有一份（``TRANSLATOR_CONFIG_KEYS``）。这里曾经手写 5 个新键，
    于是**旧别名**（``api_key``/``base_url``/``model``）改了不算变化 ——
    而 CloudTranslator 读的正是这些键（voxsub/translate/cloud.py:73-81），
    用户改了端点却继续用旧翻译器和旧译文。语言对也必须进来：档位能否翻译
    一个语言对取决于它（voxsub/translate/factory.py:60-100）。
    """
    return (
        _translator_kind(config),
        translator_config_signature(
            config, source_lang=source_lang, target_lang=target_lang),
    )


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
    ascii_letters = sum("a" <= character.lower() <= "z" for character in text)
    cjk = sum("\u3400" <= character <= "\u9fff" for character in text)
    kana = sum(
        ("\u3040" <= character <= "\u30ff") or
        ("\uff66" <= character <= "\uff9f")
        for character in text)
    hangul = sum("\uac00" <= character <= "\ud7af" for character in text)
    if language == "en":
        return ascii_letters >= max(2, (cjk + kana + hangul) * 2)
    if language == "zh":
        return cjk > 0 and kana == 0 and hangul == 0
    if language == "ja":
        return kana > 0 or (cjk > 0 and hangul == 0)
    if language == "ko":
        return hangul > 0
    return True


def _invoke_batch_translate(
    batch_translate: Any,
    chunk: Sequence[str],
    source_lang: str,
    target_lang: str,
    *,
    allow_single_fallback: bool,
) -> tuple[list[str] | None, Exception | None]:
    """Call a batch translator and normalize legacy signature failures."""
    try:
        try:
            outputs = list(batch_translate(
                list(chunk),
                source_lang,
                target_lang,
                timeout_ms=12_000,
                allow_single_fallback=allow_single_fallback,
            ))
        except TypeError as exc:
            # Third-party/test translators may still implement the older batch
            # signature. Retry only for the new keyword mismatch.
            if "allow_single_fallback" not in str(exc):
                raise
            outputs = list(batch_translate(
                list(chunk), source_lang, target_lang, timeout_ms=12_000))
        if len(outputs) != len(chunk):
            raise TranslationError("OCR 批量翻译返回数量与输入不一致")
    except (TranslationError, OSError, RuntimeError, ValueError) as exc:
        return None, exc
    return outputs, None


@dataclass(frozen=True)
class TranslatorLease:
    """借来的翻译器 + 它的世代号。世代变了说明实例已被替换或关闭。"""

    translator: Any
    generation: Any


class OcrTranslationService:
    """One-owner translator with a bounded line cache for changing frames.

    三种所有权必须分清（docs/ARCHITECTURE.md §五.1、§五.7）：

    · **自有**：按配置建出来的实例，本类负责关闭。
    · **借用**：会话正在用的实例。只通过 ``attach_translator_provider`` 拿一个
      *provider*，并且**每帧重新取一次** —— 本类不保存实例本身，也从不关闭
      借来的实例。抓着裸引用就是缺陷：会话换档/停止时会关掉旧实例
      （voxsub/pipeline.py:707-724），旧引用随即变成已关闭的翻译器。
    · **没有**：建不出来时降级为"只返回识别结果"。

    缓存只对**产生它的那套配置**有效：缓存键含 ``ocr_result_signature``
    （翻译方向/档位/端点/本地模型目录/OCR 版面）与借用世代号，所以配置一变
    旧条目自动不命中；同时保留原有的"翻译器身份变化 → 清缓存 + 关闭实例"
    失效路径，两条一起用，不另写缓存。
    """

    def __init__(
        self, *, cache_size: int = 512,
        translator_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._translator: Any | None = None
        self._translator_config_key: tuple[str, str] | None = None
        self._cache: OrderedDict[tuple[Any, ...], str] = OrderedDict()
        self._cache_size = max(16, int(cache_size))
        self._provider: Callable[[], Any] | None = translator_provider
        self._closed = False
        self._lock = threading.RLock()

    @property
    def closed(self) -> bool:
        """本服务是否已独立回收（回收后再用会抛 OcrRuntimeClosedError）。"""
        return self._closed

    def _raise_if_closed(self, action: str) -> None:
        if self._closed:
            raise OcrRuntimeClosedError(
                f"OCR 翻译服务已独立回收，不能再{action}："
                "请新建 OcrTranslationService")

    # ---------------------------------------------------------------- 借用协议

    def attach_translator_provider(self, provider: Callable[[], Any]) -> None:
        """显式借出。

        ``provider`` 每次调用返回 ``(translator, generation)``、``TranslatorLease``、
        裸翻译器或 ``None``。返回值里带世代号时，换代即视为"实例已替换"：
        旧世代产生的译文不会被复用。调用方（会话拥有者）**必须**在停止/换档
        **之前**调 ``detach_translator_provider()`` 归还。
        """
        self._raise_if_closed("借用会话翻译器")
        with self._lock:
            self._provider = provider

    def detach_translator_provider(self) -> None:
        """归还：只解除引用，**绝不关闭**借来的实例。幂等。"""
        with self._lock:
            self._provider = None

    @staticmethod
    def _lease_of(value: Any) -> TranslatorLease | None:
        if value is None:
            return None
        if isinstance(value, TranslatorLease):
            return value
        if isinstance(value, tuple) and len(value) == 2:
            translator, generation = value
            try:
                hash(generation)
            except TypeError:
                generation = repr(generation)
            return TranslatorLease(translator, generation)
        # 裸对象：用 id 当世代号。对象被替换后 id 通常不同。
        return TranslatorLease(value, id(value))

    def _borrowed_lease(self) -> TranslatorLease | None:
        """每帧重新向 provider 要一次 —— 这是"不持有裸引用"的落点。"""
        with self._lock:
            provider = self._provider
        if provider is None:
            return None
        try:
            return self._lease_of(provider())
        except Exception:  # noqa: BLE001 - 拥有者正在拆，退回自有翻译器
            logger.warning("取会话翻译器失败，退回 OCR 自有翻译器", exc_info=True)
            return None

    # ------------------------------------------------------------------ 自有翻译器

    def _release_owned_translator(self) -> None:
        """释放**自有**翻译器：复用原有失效机制（清缓存 + 关实例）。"""
        translator, self._translator = self._translator, None
        self._translator_config_key = None
        self._cache.clear()
        if translator is not None:
            try:
                translator.close()
            except Exception:  # noqa: BLE001 - shutdown must remain best effort
                logger.debug("OCR 翻译器关闭失败", exc_info=True)

    def _ensure_translator(
        self, config: Mapping[str, Any], *,
        source_lang: str = "", target_lang: str = "",
    ) -> Any:
        key = _translator_key(
            config, source_lang=source_lang, target_lang=target_lang)
        with self._lock:
            # 关闭与建翻译器在同一把锁里判断：否则"关闭前通过检查、关闭后才
            # 建实例"会留下一个没人回收的翻译器。
            self._raise_if_closed("按配置新建翻译器")
            if self._translator is not None and key == self._translator_config_key:
                return self._translator
            self._release_owned_translator()
            self._translator = TranslatorFactory.create(
                _translator_kind(config), dict(config))
            self._translator_config_key = key
            warmup = getattr(self._translator, "warmup", None)
            if callable(warmup):
                warmup()
            return self._translator

    def warmup(
        self, config: Mapping[str, Any], *,
        source_lang: str = "", target_lang: str = "",
    ) -> None:
        """Load the configured translator before the first visible OCR frame."""
        self._raise_if_closed("预热翻译器")
        self._ensure_translator(
            config, source_lang=source_lang, target_lang=target_lang)

    # ------------------------------------------------------------------ 缓存

    def _remember(self, key: tuple[Any, ...], translation: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._cache[key] = translation
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def _cached(self, key: tuple[Any, ...]) -> str | None:
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
            return cached

    @staticmethod
    def _scope(
        config: Mapping[str, Any], source_lang: str, target_lang: str,
        generation: Any,
    ) -> tuple[str, Any]:
        """缓存作用域：配置指纹 + 翻译器世代。任一变化 → 旧条目不再命中。"""
        return (
            ocr_result_signature(
                config, source_lang=source_lang, target_lang=target_lang),
            generation,
        )

    def _translate_text(
        self, translator: Any, text: str, source_lang: str, target_lang: str,
        scope: tuple[str, Any],
    ) -> tuple[str, bool]:
        key = (scope, source_lang, target_lang, text)
        cached = self._cached(key)
        if cached is not None:
            return cached, False
        try:
            translated = str(translator.translate(
                text, source_lang, target_lang, timeout_ms=12_000
            )).strip()
        except (TranslationError, OSError, RuntimeError) as error:
            trace_error("ocr_translation", error, source=source_lang, target=target_lang, input_chars=len(text))
            logger.warning("OCR 行翻译失败: chars=%d", len(text))
            return "", True
        if translated:
            self._remember(key, translated)
            return translated, False
        return "", True

    def _partition_cached_sources(
        self, sources: Sequence[str], source_lang: str, target_lang: str,
        scope: tuple[str, Any],
    ) -> tuple[dict[str, str], list[str]]:
        """Return cache hits and unique misses while preserving source order."""
        targets: dict[str, str] = {}
        uncached: list[str] = []
        seen: set[str] = set()
        for source in sources:
            cached = self._cached((scope, source_lang, target_lang, source))
            if cached is not None:
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
        *,
        scope: tuple[str, Any],
        allow_single_fallback: bool = True,
        batch_items: int = 10,
        batch_characters: int | None = None,
    ) -> tuple[dict[str, str], int]:
        """Translate cache misses in bounded batches.

        Live OCR deliberately does not fall back to one request per line when
        a model violates the JSON batch contract.  That fallback is useful for
        a static screenshot, but on a changing screen it can queue dozens of
        requests and block the next page for tens of seconds.
        """
        targets: dict[str, str] = {}
        requests = 0
        batch_translate = getattr(translator, "translate_many", None)
        if not callable(batch_translate):
            for source in sources:
                requests += 1
                target, _failed = self._translate_text(
                    translator, source, source_lang, target_lang, scope)
                if target:
                    targets[source] = target
            return targets, requests

        if batch_characters is None:
            batch_characters = 1400 if source_lang.lower().startswith("en") else 800
        for chunk in _translation_chunks(
            sources,
            maximum_items=max(1, int(batch_items)),
            maximum_characters=max(64, int(batch_characters)),
        ):
            requests += 1
            outputs, error = _invoke_batch_translate(
                batch_translate,
                chunk,
                source_lang,
                target_lang,
                allow_single_fallback=allow_single_fallback,
            )
            if error is not None:
                trace_error("ocr_translation", error, source=source_lang, target=target_lang,
                            input_chars=sum(len(source) for source in chunk))
                logger.warning(
                    "OCR 批量翻译失败%s: lines=%d error_type=%s",
                    "，跳过逐行回退" if not allow_single_fallback else "，回退逐行",
                    len(chunk), type(error).__name__,
                )
                if not allow_single_fallback:
                    # Keep the OCR boxes visible without covering them with a
                    # partial/incorrect translation.  The next changed frame
                    # gets another bounded batch opportunity.
                    continue
                outputs = []
                for source in chunk:
                    requests += 1
                    target, _failed = self._translate_text(
                        translator, source, source_lang, target_lang, scope)
                    outputs.append(target)
            for source, target in zip(chunk, outputs):
                cleaned = str(target or "").strip()
                if cleaned:
                    self._remember(
                        (scope, source_lang, target_lang, source), cleaned)
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
        self._raise_if_closed("翻译 OCR 帧")
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

        translator, generation = self._active_translator(
            config, source_lang, target_lang)
        scope = self._scope(config, source_lang, target_lang, generation)
        sources = [source for _line, source in prepared]
        targets, uncached = self._partition_cached_sources(
            sources, source_lang, target_lang, scope)
        live_fast = bool(config.get("ocr_live_mode", False)) and not bool(
            config.get("ocr_refinement_mode", False))
        fresh, requests = self._translate_uncached_sources(
            translator,
            uncached,
            source_lang,
            target_lang,
            scope=scope,
            allow_single_fallback=not live_fast,
            batch_items=int(config.get("ocr_live_batch_items", 10))
            if live_fast else 10,
            batch_characters=int(config.get("ocr_live_batch_characters", 1200))
            if live_fast else None,
        )
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

    def _active_translator(
        self, config: Mapping[str, Any], source_lang: str, target_lang: str,
    ) -> tuple[Any, Any]:
        """本帧要用的翻译器 + 它的世代号。

        优先借用会话的实例（每帧现取，不持有裸引用）；借不到才按配置自建。
        """
        lease = self._borrowed_lease()
        if lease is not None:
            return lease.translator, lease.generation
        translator = self._ensure_translator(
            config, source_lang=source_lang, target_lang=target_lang)
        # 自有翻译器也有"世代"：它的身份键。配置变了键就变，旧译文不再命中。
        return translator, self._translator_config_key

    def close(self) -> None:
        """独立回收（幂等）：本类只关**自有**翻译器；借来的只归还。

        · **不依赖会话**：会话停了 OCR 仍能自己回收干净；会话没停也能单独回收。
        · 重复调用不报错、不重复释放。
        · 关闭后再调 ``translate_frame``/``warmup`` 抛 ``OcrRuntimeClosedError``，
          不会静默重建一个翻译器继续用。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # 借来的实例属于会话：只解除引用，绝不 close（那是拥有者的职责）。
            self._provider = None
            logger.debug("OCR 翻译服务已独立回收")
            self._release_owned_translator()


__all__ = [
    "OcrBox",
    "OcrFrame",
    "OcrLine",
    "OcrRuntimeClosedError",
    "OcrTranslationService",
    "OcrUnavailableError",
    "RESULT_CONFIG_KEYS",
    "RapidOcrEngine",
    "TRANSLATOR_CONFIG_KEYS",
    "TranslatedOcrFrame",
    "TranslatedOcrLine",
    "TranslatorLease",
    "fingerprint_distance",
    "frame_fingerprint",
    "group_ocr_lines",
    "live_ocr_config",
    "materially_changed",
    "ocr_result_signature",
    "polygon_to_box",
    "preferred_ocr_backend",
    "refinement_ocr_config",
    "translator_config_signature",
]
