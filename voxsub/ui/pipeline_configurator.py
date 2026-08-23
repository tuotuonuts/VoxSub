"""Apply a validated ConfigStore snapshot to the Pipeline contract."""
from __future__ import annotations

from typing import Any, Mapping

from voxsub.model_storage import resolve_models_root


def apply_pipeline_config(
    pipeline: Any,
    config: Mapping[str, Any],
    *,
    mode: str,
    store: Any,
) -> None:
    pair = str(config.get("lang_pair", "zh-en"))
    source, target = pair.split("-", 1)
    tier = {
        "fast": "opus-fast",
        "quality": "qwen-quality",
        "cloud": "cloud",
    }.get(str(config.get("translate_tier", "fast")), "opus-fast")

    pipeline.set_models_dir(resolve_models_root(store))
    pipeline.set_langs(source, target)
    pipeline.set_tts(bool(config.get("tts_enabled", False)))
    pipeline.set_audio_devices(
        str(config.get("mic_device_id", "")),
        str(config.get("loopback_device_id", "")),
    )
    pipeline.set_capture_process(
        int(config.get("capture_process_id", 0) or 0),
        str(config.get("capture_window_title", "")),
    )
    pipeline.set_stt(str(config.get("stt_provider", "local")), dict(config))
    pipeline.set_asr_model(str(config.get(
        "asr_model_id", "asr-zipformer-bilingual-fast")))
    pipeline.set_asr_tuning({
        "profile": str(config.get("asr_tuning_profile", "auto")),
        "vad_threshold": float(config.get("asr_vad_threshold", 0.35)),
        "silence_ms": int(config.get("asr_silence_ms", 650)),
        "max_utterance_ms": int(config.get("asr_max_utterance_ms", 12000)),
        "beam_paths": int(config.get("asr_beam_paths", 4)),
        "max_new_tokens": int(config.get("asr_max_new_tokens", 512)),
        "hotwords": str(config.get("asr_hotwords", "")),
    })
    pipeline.set_recording(
        mode == "a" and bool(config.get("record_with_translation", False)))
    pipeline.set_translator(tier, dict(config))
    input_file = str(config.get("last_input_file", ""))
    if input_file:
        pipeline.set_input_file(input_file)


__all__ = ["apply_pipeline_config"]
