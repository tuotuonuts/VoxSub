"""Complete per-model language routing, native hints and bounded smooth previews.

No recording, playback, window or network. Native factories are explicit doubles.
"""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest

from voxsub import asr
from voxsub.config_store import APP_CONFIG_SCHEMA
from voxsub.language_registry import (
    ASR_LANGUAGES, HY_MT_LANGUAGES, QWEN_ASR_LANGUAGES, PARAKEET_LANGUAGES,
    LANGUAGE_NAMES, LANGUAGE_PAIRS, split_language_pair,
)
from voxsub.language_capabilities import language_capabilities, validate_pair
from voxsub.language_guard import normalize_language, text_matches_language
from voxsub.translate.factory import tier_langs, TranslatorFactory
from voxsub.translate.qwen import QwenQualityTranslator, _can_passthrough_batch
from voxsub.live_draft import LiveDraftState
from voxsub.pipeline import Pipeline, resolve_tuning_values
from voxsub.realtime_builder import RealtimeBuildSpec, _build_draft_asr
from voxsub.model_catalog import CATALOG

HY = "mt-hy-mt2-1.8b-q4"
CONFIG = {"translate_tier": "quality", "translate_model_id": HY}


@pytest.mark.parametrize("model", [m for m in CATALOG if m.task == "asr"], ids=lambda m: m.id)
def test_catalog_asr_full_intersection_and_directional_targets(model):
    meta = language_capabilities({**CONFIG, "asr_model_id": model.id})
    expected = set(ASR_LANGUAGES[model.runtime]) & set(HY_MT_LANGUAGES)
    assert set(meta["sources"]) - {"auto"} == expected
    for source in expected:
        assert set(meta["targets"][source]) == set(HY_MT_LANGUAGES)
        for target in HY_MT_LANGUAGES:
            validate_pair(meta, source, target)
    assert all(code in meta["labels"] for code in expected | set(HY_MT_LANGUAGES))


@pytest.mark.parametrize("model", [m for m in CATALOG if m.runtime == "llama-hy-mt2"], ids=lambda m: m.id)
def test_every_hy_quantization_matches_factory_and_prompts(model):
    config = {**CONFIG, "translate_model_id": model.id}
    assert tier_langs("quality", config) == HY_MT_LANGUAGES
    translator = TranslatorFactory.create("qwen-quality", config)
    assert translator.langs == HY_MT_LANGUAGES
    assert translator.supports("fr", "zh-hant")
    translator.close()


def test_registry_counts_and_safe_auto():
    assert len(QWEN_ASR_LANGUAGES) == 30
    assert len(PARAKEET_LANGUAGES) == 25
    assert len(HY_MT_LANGUAGES) == 38  # explicit official table, not its stale headline
    sense = language_capabilities({**CONFIG, "asr_model_id": "asr-sensevoice-small-int8"})
    assert "yue" in sense["sources"] and "auto" in sense["sources"]
    qwen = language_capabilities({**CONFIG, "asr_model_id": "asr-qwen3-0.6b-int8"})
    assert "fr" in qwen["sources"] and "de" in qwen["sources"]
    assert "sv" not in qwen["sources"] and "auto" not in qwen["sources"]
    opus = language_capabilities({"asr_model_id": "asr-qwen3-0.6b-int8"})
    assert opus["sources"] == ["zh", "en"]


def test_config_roundtrips_every_registry_pair_without_silent_default():
    for pair, expected in LANGUAGE_PAIRS.items():
        assert APP_CONFIG_SCHEMA.normalize("lang_pair", pair) == pair
        assert split_language_pair(pair) == expected
    assert normalize_language("tl") == "fil"
    assert normalize_language("zh-TW") == "zh-hant"
    with pytest.raises(ValueError): split_language_pair("unknown-zh")


@pytest.mark.parametrize("lang,text", [("fr", "Bonjour"), ("ru", "Привет"), ("ar", "مرحبا"),
    ("hi", "नमस्ते"), ("th", "สวัสดี"), ("yue", "你好"), ("zh-hant", "學習"),
    ("he", "שלום"), ("ta", "வணக்கம்"), ("gu", "નમસ્તે"), ("bo", "བོད")])
def test_target_scripts_and_names_are_not_normalized_to_auto(lang, text):
    assert normalize_language(lang, strict=True) == lang
    assert text_matches_language(text, lang)
    assert not text_matches_language("这是中文", lang) if lang not in {"yue", "zh-hant"} else True


@pytest.fixture
def native_factory(monkeypatch, tmp_path):
    observed = []
    class Native:
        def __init__(self): self.options = {}; self.result = SimpleNamespace(text="Bonjour")
        def set_option(self, key, value): self.options[key] = value
        def accept_waveform(self, *_): pass
    def factory(**kw):
        config = SimpleNamespace(model_config=SimpleNamespace(sense_voice=SimpleNamespace(language=kw.get("language", ""))))
        r = SimpleNamespace(config=config, kwargs=kw, create_stream=Native)
        r.recognizer = SimpleNamespace(set_config=lambda c: observed.append(("config", c.model_config.sense_voice.language)))
        r.decode_stream = lambda n: observed.append(("decode", dict(n.options)))
        return r
    for name in ("from_qwen3_asr", "from_sense_voice", "from_funasr_nano", "from_moonshine_v2", "from_transducer"):
        monkeypatch.setattr(asr.sherpa_onnx.OfflineRecognizer, name, factory)
    monkeypatch.setattr(asr.OfflineGenerativeASR, "_require", lambda *_: None)
    return lambda runtime, lang: asr.OfflineGenerativeASR(tmp_path, runtime, source_lang=lang), observed


@pytest.mark.parametrize("lang", QWEN_ASR_LANGUAGES)
def test_all_qwen_language_hints_reach_decode_once(native_factory, lang):
    create, observed = native_factory
    model = create("sherpa-qwen3-asr", "zh")
    stream = asr.create_asr_stream(model, lang)
    model.feed(stream, np.full(512, .01, dtype=np.float32))
    assert model.decode(stream) == "Bonjour"
    assert observed == [("decode", {"language": LANGUAGE_NAMES[lang]})]
    model.decode(stream)
    assert len(observed) == 1


def test_sensevoice_language_snapshot_applied_at_decode_without_reload(native_factory):
    create, observed = native_factory
    model = create("sherpa-sense-voice", "zh")
    recognizer = model._recognizer
    for lang in ("en", "yue", "ja", "auto"):
        stream = asr.create_asr_stream(model, lang)
        model.feed(stream, np.full(512, .01, dtype=np.float32))
        model.decode(stream)
        assert observed[-2] == ("config", "" if lang == "auto" else lang)
        assert observed[-1] == ("decode", {})
        assert model._recognizer is recognizer


@pytest.mark.parametrize("lang,expected", [("zh", "中文"), ("en", "英语"), ("ja", "日语"), ("auto", "")])
def test_funasr_uses_native_prompt_language_names(native_factory, lang, expected):
    create, _ = native_factory
    model = create("sherpa-funasr-nano", lang)
    assert model._recognizer.kwargs["language"] == expected
    stream = asr.create_asr_stream(model, "en" if lang != "en" else "zh")
    model.feed(stream, np.full(512, .01, dtype=np.float32))
    with pytest.raises(RuntimeError, match="停止"): model.decode(stream)


def test_funasr_live_language_change_refuses_before_mutation():
    pipe = Pipeline(); pipe.set_langs("en", "zh")
    pipe._asr = SimpleNamespace(runtime="sherpa-funasr-nano"); pipe._running = True
    with pytest.raises(RuntimeError, match="停止"): pipe.set_langs("ja", "zh")
    assert pipe._lang_snapshot().pair == ("en", "zh")
    assert pipe.set_langs("en", "ja")
    pipe._running = False
    assert pipe.set_langs("ja", "zh")


def test_hy_translation_uses_full_language_names_not_auto(monkeypatch):
    translator = QwenQualityTranslator(prompt_style="hy-mt2")
    monkeypatch.setattr(translator, "_ensure_instance", lambda: ("http://fixture", 1))
    requests = []
    monkeypatch.setattr(translator, "_request_translation", lambda endpoint, text, names, *a, **kw: requests.append(names) or "你好")
    assert translator.translate("Bonjour", "fr", "zh") == "你好"
    assert requests == [("French", "Chinese")]
    assert not _can_passthrough_batch(["Bonjour"], "auto", "en")
    requests.clear()
    monkeypatch.setattr(translator, "_request_translation", lambda endpoint, text, names, *a, **kw: requests.append(names) or "Hello")
    assert translator.translate("Bonjour", "auto", "en") == "Hello"
    assert len(requests) == 1
    translator.close()


@pytest.mark.parametrize("profile", ["auto", "responsive", "balanced", "accuracy", "context", "custom"])
def test_preview_enabled_independently_of_context_profile(profile):
    calls = []
    tuning = resolve_tuning_values({"profile": profile, "live_draft_enabled": True, "auxiliary_preview_enabled": True}, True)
    spec = RealtimeBuildSpec(Path("unused"), "local", {}, "qwen", "cpu", "en", tuning, True)
    factory = lambda *a, **kw: calls.append((a, kw)) or SimpleNamespace(runtime="sherpa-streaming-transducer")
    assert _build_draft_asr(spec, provider="cpu", threads=2, asr_factory=factory) is not None
    assert len(calls) == 1 and tuning["partial_interval_ms"] == 140
    for language in ("fr", "ja", "auto"):
        assert _build_draft_asr(replace(spec, source_lang=language), provider="cpu", threads=2, asr_factory=factory) is None
    assert len(calls) == 1


def test_stable_prefix_then_quiet_tail_without_repeating_translation():
    now = [0.0]; state = LiveDraftState(stable_updates=True, clock=lambda: now[0])
    state.update_source("We like translat")
    now[0] = .1; state.update_source("We like translation")
    now[0] = .2; first = state.take_translation_request()
    assert first.source == "We like"
    assert state.accept_translation(first, "我们喜欢").translation == "我们喜欢"
    now[0] = .7; tail = state.take_translation_request()
    assert tail.source == "We like translation"
    now[0] = 2; assert state.take_translation_request() is None


def test_foreign_language_never_uses_bilingual_preview_after_hot_switch():
    pipe = Pipeline(); pipe._is_generative = True; pipe.set_langs("fr", "zh")
    seen = []; pipe.on_partial(seen.append)
    pipe._on_asr_partial("Fake bilingual guess")
    assert seen == []
    pipe._on_sentence("Bonjour")
    assert pipe._translation_queue.get_nowait().text == "Bonjour"

def test_no_second_asr_is_loaded_by_default():
    tuning = resolve_tuning_values({"profile": "context"}, True)
    spec = RealtimeBuildSpec(Path("unused"), "local", {}, "qwen", "cpu", "en", tuning, True)
    assert _build_draft_asr(spec, provider="cpu", threads=2,
        asr_factory=lambda *a, **kw: pytest.fail("no extra model without opt-in")) is None
    assert APP_CONFIG_SCHEMA.defaults["asr_auxiliary_preview_enabled"] is False


def test_final_source_precedes_translation_without_newer_draft_overwrite():
    pipe = Pipeline(); pipe.set_langs("en", "zh")
    events = []; pipe.on_draft(lambda *pair: events.append(("draft", pair)))
    pipe._translator = SimpleNamespace(translate=lambda *a, **kw: events.append(("inference", ())) or "你好")
    pipe._trans_kind = "fixture"; pipe._trans_pair = ("en", "zh")
    pipe._on_sentence("Hello")
    item = pipe._translation_queue.get_nowait()
    pipe._translate_queued_item(item)
    assert events[0] == ("draft", ("Hello", ""))
    assert sum(kind == "inference" for kind, _ in events) == 1
    state = LiveDraftState()
    sentence = state.sentence_id; state.begin_final(); state.update_source("Next sentence")
    assert state.pending_final_view("Old sentence", sentence) is None
