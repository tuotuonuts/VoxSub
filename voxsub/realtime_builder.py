"""Transactional construction of Pipeline's realtime recognition chain."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from voxsub import __version__
from voxsub.logging_setup import get_logger

logger = get_logger("realtime_builder")


@dataclass(frozen=True)
class RealtimeBuildSpec:
    models_dir: Path
    stt_provider: str
    stt_config: Any
    asr_model_id: str
    asr_provider: str
    source_lang: str
    tuning: dict
    generative: bool


@dataclass(frozen=True)
class RealtimeComponents:
    asr: Any
    cloud_stt: Any
    vad: Any
    segmenter: Any
    generative: bool
    cloud: bool


def _vad_model_path(models_dir: Path,
                    ensure_vad: Callable[[Path], Path | None]) -> Path:
    model = ensure_vad(models_dir)
    if model is None:
        model = next((models_dir / "vad").glob("*.onnx"), None)
    if model is None:
        raise FileNotFoundError(
            f"缺少基础 VAD 模型。请重新安装 VoxSub {__version__} "
            "或在模型目录中修复 VAD。"
        )
    return model


def _asr_runtime_provider(requested: str,
                          select_device: Callable[..., Any]) -> str:
    if requested != "auto":
        return requested
    route = select_device("asr", benchmark=False)
    provider = route.provider if route.provider in {"cuda", "coreml"} else "cpu"
    logger.info("ASR 自动路由: device=%s runtime_provider=%s", route.name, provider)
    return provider


def _build_draft_asr(
    spec: RealtimeBuildSpec,
    *,
    provider: str,
    threads: int,
    asr_factory: Callable,
) -> Any | None:
    """Build the bundled streaming sidecar used by sentence-level ASR.

    The selected generative/cloud recognizer remains authoritative for finals;
    Zipformer only supplies replaceable Smart Context drafts.  Missing or
    damaged optional draft files must not make the main recognizer unusable.
    """
    if not spec.tuning.get("context_enabled", False):
        return None
    tuning = dict(spec.tuning)
    tuning["beam_paths"] = min(2, max(1, int(tuning.get("beam_paths", 2))))
    try:
        draft_asr = asr_factory(
            "asr-zipformer-bilingual-fast",
            spec.models_dir,
            provider=provider,
            num_threads=threads,
            source_lang=spec.source_lang,
            tuning=tuning,
        )
    except Exception:
        logger.warning(
            "内置 Zipformer 草稿旁路不可用；高质量终句识别仍会继续",
            exc_info=True,
        )
        return None
    if getattr(draft_asr, "runtime", "") != "sherpa-streaming-transducer":
        logger.warning("实时草稿旁路不是流式识别器，已禁用: %s",
                       getattr(draft_asr, "runtime", type(draft_asr).__name__))
        return None
    logger.info("实时草稿旁路已启用: runtime=%s provider=%s paths=%d",
                draft_asr.runtime, getattr(draft_asr, "provider", provider),
                tuning["beam_paths"])
    return draft_asr


def build_realtime_components(
    spec: RealtimeBuildSpec,
    *,
    queue_audio: Callable,
    on_sentence: Callable,
    on_partial: Callable,
    ensure_vad: Callable,
    vad_factory: Callable,
    asr_factory: Callable,
    cloud_factory: Callable,
    audio_segmenter_factory: Callable,
    streaming_segmenter_factory: Callable,
    select_device: Callable,
    semantic_boundary: Callable[[str], bool] | None,
) -> RealtimeComponents:
    """Build every dependency locally and return it only when all are ready."""
    cloud = spec.stt_provider == "cloud"
    generative = spec.generative
    vad = vad_factory(
        str(_vad_model_path(spec.models_dir, ensure_vad)),
        threshold=spec.tuning["vad_threshold"],
    )
    if cloud:
        cloud_client = cloud_factory(spec.stt_config)
        if not cloud_client.ready():
            raise RuntimeError(
                "云 STT 尚未就绪，请填写独立的 STT API Key、BaseURL 和模型名")
        draft_provider = _asr_runtime_provider(spec.asr_provider, select_device)
        threads = min(2, max(1, os.cpu_count() or 1))
        draft_asr = _build_draft_asr(
            spec, provider=draft_provider, threads=threads,
            asr_factory=asr_factory,
        )
        segmenter = audio_segmenter_factory(
            vad, queue_audio,
            min_silence_ms=spec.tuning["silence_ms"],
            max_utterance_ms=spec.tuning["max_utterance_ms"],
            draft_asr=draft_asr,
            on_partial=on_partial if draft_asr is not None else None,
            partial_interval_ms=spec.tuning.get("partial_interval_ms", 140),
        )
        return RealtimeComponents(None, cloud_client, vad, segmenter, True, True)

    provider = _asr_runtime_provider(spec.asr_provider, select_device)
    threads = min(4, max(1, os.cpu_count() or 1))
    asr = asr_factory(
        spec.asr_model_id, spec.models_dir, provider=provider,
        num_threads=threads, source_lang=spec.source_lang, tuning=spec.tuning,
    )
    logger.info(
        "本地 STT 执行器: runtime=%s provider=%s threads=%d",
        getattr(asr, "runtime", type(asr).__name__),
        getattr(asr, "provider", provider), threads,
    )
    if generative:
        draft_asr = _build_draft_asr(
            spec, provider=provider, threads=min(2, threads),
            asr_factory=asr_factory,
        )
        segmenter = audio_segmenter_factory(
            vad, queue_audio,
            min_silence_ms=spec.tuning["silence_ms"],
            max_utterance_ms=spec.tuning["max_utterance_ms"],
            draft_asr=draft_asr,
            on_partial=on_partial if draft_asr is not None else None,
            partial_interval_ms=spec.tuning.get("partial_interval_ms", 140),
        )
    else:
        segmenter = streaming_segmenter_factory(
            asr, vad, on_sentence,
            min_silence_ms=spec.tuning["silence_ms"],
            max_utterance_ms=spec.tuning["max_utterance_ms"],
            on_partial=on_partial,
            partial_interval_ms=spec.tuning.get("partial_interval_ms", 360),
            boundary_decider=semantic_boundary,
            semantic_hold_ms=(
                spec.tuning["context_hold_ms"] if semantic_boundary else 0),
        )
    return RealtimeComponents(asr, None, vad, segmenter, generative, False)
