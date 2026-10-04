"""Production language matrix and pre-audio/IPC safety gates (no real devices)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from voxsub.language_capabilities import build_options, language_capabilities, validate_pair
from voxsub.pipeline import Pipeline

BACKEND = Path(__file__).resolve().parents[1] / "frontend" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from handlers.session import SessionHandlers


@pytest.mark.parametrize("model,sources", [
    ("asr-zipformer-bilingual-fast", {"auto", "zh", "en"}),
    ("asr-funasr-nano-2512-int8", {"auto", "zh", "en", "ja"}),
    ("asr-qwen3-0.6b-int8", {"zh", "en", "ja", "ko"}),
    ("asr-sensevoice-small-int8", {"zh", "en", "ja", "ko"}),
    ("asr-moonshine-tiny-en-v2", {"en"}),
    ("asr-parakeet-tdt-0.6b-v3-int8", {"en"}),
])
def test_asr_effective_contract(model, sources):
    meta = language_capabilities({"asr_model_id": model, "translate_tier": "cloud"})
    assert set(meta["sources"]) == sources
    assert "zh" in meta["targets"]["en"]  # targets are NOT intersected with ASR
    for source in meta["sources"]:
        for target in meta["targets"][source]:
            validate_pair(meta, source, target)


@pytest.mark.parametrize("tier", ["fast", "quality"])
def test_opus_effective_tier(tier):
    meta = language_capabilities({"asr_model_id": "asr-qwen3-0.6b-int8",
                                  "translate_tier": tier,
                                  "translate_model_id": "mt-opus-fast-builtin"})
    assert meta["sources"] == ["zh", "en"]
    assert meta["targets"]["en"] == ["zh", "en"]  # retain passthrough compatibility


def test_unselected_quality_model_matches_factory_default():
    meta = language_capabilities({"asr_model_id": "asr-qwen3-0.6b-int8",
                                  "translate_tier": "quality"})
    assert meta["sources"] == ["zh", "en", "ja", "ko"]
    assert meta["targets"]["en"] == ["zh", "en", "ja", "ko"]


def test_directional_pairs_and_empty_intersection():
    assert build_options(["en"], [("en", "zh")])["targets"] == {"en": ["zh"]}
    assert not build_options(["en"], [("ja", "zh")])["compatible"]


def test_auto_requires_every_possible_source_to_work():
    assert "auto" not in build_options(["en", "ja"], [("en", "zh")], ["en", "ja"])["sources"]
    assert "auto" not in build_options(["en"], [("en", "zh")], ["en", "ja"])["sources"]
    assert build_options(["en", "zh"], [("en", "zh")], ["en", "zh"])["targets"]["auto"] == ["zh"]


@pytest.mark.parametrize("key", ["asr_model_id", "translate_model_id"])
def test_unknown_selection_fails_closed(key):
    assert not language_capabilities({key: "missing"})["compatible"]


def test_ocr_bypasses_asr_and_cloud_auto_is_not_promised():
    config = {"asr_model_id": "asr-moonshine-tiny-en-v2", "translate_tier": "cloud"}
    assert language_capabilities(config, mode="d")["sources"] == ["zh", "en", "ja", "ko"]
    assert language_capabilities({"stt_provider": "cloud", "translate_tier": "cloud"})["sources"] == ["zh", "en", "ja", "ko"]


def test_pipeline_rejects_before_audio_or_native_init(monkeypatch):
    pipeline = Pipeline()
    pipeline.set_asr_model("asr-moonshine-tiny-en-v2")
    pipeline.set_langs("zh", "en")
    monkeypatch.setattr(pipeline, "_make_source", lambda: pytest.fail("audio must not open"))
    monkeypatch.setattr(pipeline, "_build_real_time", lambda: pytest.fail("weights must not load"))
    with pytest.raises(ValueError, match="语言组合"):
        pipeline.start()
    assert not pipeline.is_running()
    assert pipeline.state.name == "FAILED"


def test_ipc_langs_gate_does_not_mutate_rejected_pair():
    pipeline = Pipeline()
    pipeline.set_asr_model("asr-moonshine-tiny-en-v2")
    handlers = SessionHandlers()
    with pytest.raises(ValueError, match="语言组合"):
        handlers._cmd_set_langs(pipeline, {"source": "zh", "target": "en"})
    handlers._cmd_set_langs(pipeline, {"source": "en", "target": "zh"})
    assert pipeline._src_lang == "en"
    assert pipeline._dst_lang == "zh"


def test_model_settings_apply_before_config_ack_and_busy_refuses():
    pipeline = Pipeline()
    updates = {"asr_model_id": "asr-moonshine-tiny-en-v2", "translate_tier": "cloud", "stt_provider": "local"}
    pipeline.apply_language_model_config(updates, updates)
    assert pipeline.language_capabilities["sources"] == ["en"]
    pipeline._running = True
    with pytest.raises(RuntimeError, match="会话"):
        pipeline.apply_language_model_config({"asr_model_id": "asr-qwen3-0.6b-int8"}, {"asr_model_id": "asr-qwen3-0.6b-int8"})
    assert pipeline.set_asr_model("asr-qwen3-0.6b-int8") is False
    assert pipeline.set_translator("opus-fast", {}) is False
    assert pipeline.set_stt("cloud", {}) is False
    assert pipeline.language_capabilities["sources"] == ["en"]


def test_metadata_uses_existing_owner_without_provisioning(monkeypatch):
    from voxsub.config_store import ConfigStore
    pipeline = Pipeline()
    pipeline.set_asr_model("asr-moonshine-tiny-en-v2")
    handlers = SessionHandlers()
    handlers._pipeline = pipeline
    monkeypatch.setattr(ConfigStore, "load", lambda self: {})
    assert handlers._cmd_language_capabilities({})["sources"] == ["en"]
    handlers._pipeline = None
    assert "auto" in handlers._cmd_language_capabilities({})["sources"]


def test_invalid_and_alias_pairs():
    meta = language_capabilities({})
    validate_pair(meta, "en-US", "zh-CN")
    for source, target in [("fr", "zh"), ("en", "auto"), ("ja", "en")]:
        with pytest.raises(ValueError):
            validate_pair(meta, source, target)
