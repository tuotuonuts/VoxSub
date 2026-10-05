"""Deterministic scheduling tests; not model-performance measurements."""
from __future__ import annotations
import json
import math
from types import SimpleNamespace
import time
import numpy as np
import pytest
from voxsub.realtime_latency import ExactDraftCache, RealtimeLatency, DraftSkipped
from voxsub.live_draft import LiveDraftState, DraftTranslationRequest
from voxsub.pipeline import Pipeline
from voxsub.translate.context import TranslationContext, translate_contextual
from voxsub.translate.qwen import QwenQualityTranslator
from voxsub.translate.opus import _OpusModel
from voxsub.diagnostic_runtime import pipeline_snapshot


def test_numeric_bounded_stage_metrics_and_unknown_or_invalid_samples():
    meter = RealtimeLatency(window=4)
    for value in range(1, 11):
        meter.observe("final_inference", value)
    for value in (math.nan, math.inf, -1):
        meter.observe("final_inference", value)
    meter.observe("transcript", 12)
    row = meter.snapshot()["stages"]["final_inference"]
    assert row == {"count": 10, "sample_count": 4, "total_ms": 55, "p50_ms": 8, "p95_ms": 10, "max_ms": 10}
    assert "transcript" not in json.dumps(meter.snapshot())
    assert meter.snapshot()["stages"]["recognition_decode"]["p50_ms"] is None
    meter.reset()
    assert meter.snapshot()["stages"]["final_inference"]["count"] == 0


def test_preview_pacing_learns_slow_inference_and_recovers_in_bounded_window():
    meter = RealtimeLatency()
    assert meter.draft_interval_seconds == 0.45
    meter.observe("draft_inference", 900)
    assert meter.draft_interval_seconds == pytest.approx(1.05)
    meter.observe("draft_inference", 8000)
    assert meter.draft_interval_seconds == 2
    for _ in range(8):
        meter.observe("draft_inference", 20)
    assert meter.draft_interval_seconds == 0.45
    meter.observe("translation_wait", 1600)
    assert not meter.backlogged
    meter.observe("translation_wait", 1600)
    assert meter.backlogged
    meter.observe("translation_wait", 0)
    assert not meter.backlogged


def test_draft_cache_is_exact_one_entry_one_use_expiring_and_not_exported():
    now = [0.0]
    cache = ExactDraftCache(clock=lambda: now[0], ttl_seconds=1)
    cache.remember((1, "source", "context"), "private translation")
    assert cache.take((1, "changed", "context")) is None
    assert cache.take((1, "source", "context")) is None
    cache.remember((1,), "translation")
    now[0] = 1
    assert cache.take((1,)) is None
    cache.remember((1,), "translation")
    assert cache.take((1,)) == "translation"
    assert cache.take((1,)) is None
    cache.remember((1,), "")
    assert cache.take((1,)) is None


def test_stability_gate_coalesces_tiny_growth_but_never_starves_quiet_tail():
    now = [0.0]
    state = LiveDraftState(clock=lambda: now[0], stable_updates=True)
    state.update_source("Welcome")
    now[0] = 0.18
    assert state.take_translation_request() is None  # only one unstable observation
    state.update_source("Welcome to")
    now[0] = 0.20
    request = state.take_translation_request()
    assert request is not None
    state.accept_translation(request, "欢迎")
    now[0] = 0.55
    assert state.update_source("Welcome to a").translation == "欢迎"
    now[0] = 0.74
    assert state.take_translation_request() is None
    now[0] = 0.86
    assert state.take_translation_request() is not None


def test_adaptive_interval_and_sentence_reset_invalidate_earlier_request():
    now = [0.0]
    state = LiveDraftState(debounce_seconds=0, min_interval_seconds=0, clock=lambda: now[0])
    state.update_source("Hello world")
    request = state.take_translation_request()
    old_sentence = state.sentence_id
    now[0] = 0.1
    state.update_source("Hello world again")
    assert state.take_translation_request(min_interval_seconds=1) is None
    now[0] = 1
    assert state.take_translation_request(min_interval_seconds=1)
    state.begin_final()
    assert state.sentence_id != old_sentence
    assert state.update_source("Next sentence")
    assert state.accept_translation(request, "旧译文") is None
    assert state.take_translation_request() is None
    state.finish_final()
    assert state.take_translation_request()
    state.reset()
    assert state.accept_translation(request, "旧译文") is None


class RecordingTranslator:
    def __init__(self):
        self.calls = []
        self.output = "有效译文"
    def supports(self, source, target):
        return True
    def translate(self, text, source, target, *, timeout_ms=15000):
        self.calls.append((text, source, target, timeout_ms))
        return self.output
    def translate_with_context(self, text, source, target, *, context, timeout_ms=15000):
        self.calls.append((text, source, target, timeout_ms, context))
        return self.output


def pipe_fixture(profile="balanced"):
    pipe = Pipeline()
    pipe.set_langs("en", "zh")
    pipe.set_asr_tuning({"profile": profile})
    pipe._translator = RecordingTranslator()
    pipe._trans_kind = "opus-fast"
    pipe._trans_pair = ("en", "zh")
    return pipe


def infer_preview(pipe, text):
    pipe._live_draft.update_source(text)
    request = pipe._live_draft.take_translation_request(now=time.monotonic() + 3)
    assert request
    pipe._translate_draft(request)


@pytest.mark.parametrize("profile", ["responsive", "balanced", "accuracy", "context", "custom", "auto"])
def test_existing_partial_models_can_translate_drafts_in_each_live_profile(profile):
    pipe = pipe_fixture(profile)
    pipe.on_draft(lambda *_: None)
    assert pipe._live_draft_translation_enabled()
    infer_preview(pipe, "Hello world")
    assert pipe._translator.calls[0][3] == 1200
    pipe._mode = "c"
    assert not pipe._live_draft_translation_enabled()
    pipe._mode = "b"
    pipe.set_asr_tuning({"profile": profile, "live_draft_enabled": False})
    assert not pipe._live_draft_translation_enabled()


@pytest.mark.parametrize("profile", ["context", "balanced"])
def test_exact_preview_reused_for_final_only_in_same_sentence_and_context(profile):
    pipe = pipe_fixture(profile)
    finals = []
    pipe.on_utterance(lambda *pair: finals.append(pair))
    infer_preview(pipe, "Hello world")
    pipe._queue_translation("Hello world", time.monotonic())
    pipe._translate_queued_item(pipe._translation_queue.get_nowait())
    assert len(pipe._translator.calls) == 1
    assert finals == [("Hello world", "有效译文")]
    pipe._queue_translation("Hello world", time.monotonic())
    pipe._translate_queued_item(pipe._translation_queue.get_nowait())
    assert len(pipe._translator.calls) == 2  # identical words in a NEW sentence are not cached


@pytest.mark.parametrize("change", ["punctuation", "model", "configuration", "context"])
def test_preview_reuse_invalidates_on_every_semantic_dependency(change):
    pipe = pipe_fixture("context")
    infer_preview(pipe, "Hello world")
    text = "Hello world"
    if change == "punctuation":
        text += "."
    elif change == "model":
        pipe._translator = RecordingTranslator()
    elif change == "configuration":
        pipe._bump_config_generation("test change")
    else:
        scope = (pipe.config_generation, ("en", "zh"), id(pipe._translator))
        pipe._translation_context.remember(scope, "Prior sentence", "上文")
    before = len(pipe._translator.calls)
    pipe._queue_translation(text, time.monotonic())
    pipe._translate_queued_item(pipe._translation_queue.get_nowait())
    assert len(pipe._translator.calls) == before + 1


def test_long_final_queue_records_real_wait_without_dropping_finals():
    pipe = pipe_fixture()
    finals = []
    pipe.on_utterance(lambda *pair: finals.append(pair))
    for i in range(3):
        pipe._queue_translation(f"This is sentence {i}", time.monotonic() - 4)
    while not pipe._translation_queue.empty():
        pipe._translate_queued_item(pipe._translation_queue.get_nowait())
    metrics = pipe._latency.snapshot()["stages"]
    assert len(finals) == 3
    assert metrics["context_wait"]["p50_ms"] >= 4000
    assert metrics["translation_wait"]["p95_ms"] < 500
    assert metrics["final_ready"]["count"] == 3
    assert pipe._active_translation_sentence_id is None


def test_upstream_pressure_defers_optional_work_without_erasing_source():
    pipe = pipe_fixture()
    pipe._live_draft.update_source("Current source")
    for _ in range(11):
        pipe._queue.put(np.zeros(480, dtype=np.float32))
    assert not pipe._draft_work_allowed()
    while not pipe._queue.empty():
        pipe._queue.get_nowait()
    assert pipe._draft_work_allowed()
    assert pipe._live_draft.take_translation_request(now=time.monotonic() + 3)


@pytest.mark.parametrize("exception", [TimeoutError("budget"), DraftSkipped("optional"), RuntimeError("wrapped")])
def test_preview_timeout_or_skip_keeps_selected_final_model(exception):
    pipe = pipe_fixture()
    pipe._trans_kind = "qwen-quality"
    translator = pipe._translator
    if type(exception) is RuntimeError:
        exception.__cause__ = TimeoutError("budget")
    def failed(*args, **kwargs):
        raise exception
    translator.translate = failed
    pipe._translate_draft(DraftTranslationRequest(1, "Hello world"))
    assert pipe._translator is translator
    assert pipe._latency.snapshot()["stages"]["draft_inference"]["count"] == 1


def test_unvalidated_preview_cannot_seed_final_reuse():
    pipe = pipe_fixture()
    pipe._translator.output = "This is not Chinese"
    infer_preview(pipe, "Hello world")
    pipe._translator.output = "正确译文"
    pipe._queue_translation("Hello world", time.monotonic())
    pipe._translate_queued_item(pipe._translation_queue.get_nowait())
    assert len(pipe._translator.calls) == 2


def test_pipeline_snapshot_contains_timings_not_cached_bodies():
    pipe = pipe_fixture()
    infer_preview(pipe, "Private source sentence")
    serialized = json.dumps(pipeline_snapshot(pipe), ensure_ascii=False)
    assert "latency" in serialized
    assert "Private source" not in serialized and "有效译文" not in serialized


def test_single_pass_qwen_preview_never_retries_reloads_or_falls_back(monkeypatch):
    model = QwenQualityTranslator()
    model._endpoint = "http://127.0.0.1/mock"
    model._proc = SimpleNamespace(poll=lambda: None)
    calls = []
    monkeypatch.setattr(model, "_request_translation", lambda *a, **kw: calls.append((a, kw)) or "正确译文")
    monkeypatch.setattr(model, "_ensure_instance", lambda: pytest.fail("must not load/rebuild for a draft"))
    assert model.translate_draft("Hello world", "en", "zh", context=(("Earlier", "上文"),)) == "正确译文"
    assert len(calls) == 1 and 0 < calls[0][0][3] <= 1200
    assert calls[0][1]["context"] == (("Earlier", "上文"),)
    monkeypatch.setattr(model, "_request_translation", lambda *a, **kw: "This is not Chinese")
    with pytest.raises(DraftSkipped):
        model.translate_draft("Hello world", "en", "zh")
    assert model._endpoint == "http://127.0.0.1/mock"
    model._proc = None
    with pytest.raises(DraftSkipped):
        model.translate_draft("Hello world", "en", "zh")


def test_qwen_preview_observes_lock_budget_and_releases_lifecycle_lock():
    model = QwenQualityTranslator()
    model._lock.acquire()
    try:
        with pytest.raises(TimeoutError):
            model.translate_draft("Hello world", "en", "zh", timeout_ms=1)
    finally:
        model._lock.release()
    assert model._lifecycle_lock.acquire(blocking=False)
    model._lifecycle_lock.release()


def test_opus_preview_deadline_checked_between_native_decoder_calls(monkeypatch):
    import voxsub.translate.opus as module
    model = object.__new__(_OpusModel)
    model._tok = SimpleNamespace(encode=lambda text: [1], decode=lambda tokens: "translation")
    model._enc = SimpleNamespace(run=lambda *a: [np.zeros((1, 1, 1))])
    model._dec = SimpleNamespace(run=lambda *a: pytest.fail("expired budget must not start decode"))
    model._max_position = 100
    model._decoder_start = 0
    model._max_length = 10
    now = iter([0.0, 2.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(now))
    with pytest.raises(TimeoutError):
        model.translate_str("Hello", timeout_ms=1200)


def test_plugin_adapter_without_timeout_remains_compatible():
    calls = []
    translator = SimpleNamespace(translate=lambda *args: calls.append(args) or "译文")
    assert translate_contextual(translator, "source", "en", "zh", memory=TranslationContext(), scope=(1,), enabled=False, draft=True) == "译文"
    assert calls == [("source", "en", "zh")]


def test_inflight_preview_from_previous_sentence_cannot_cross_similar_prefix():
    state = LiveDraftState(debounce_seconds=0, min_interval_seconds=0)
    state.update_source("Hello world")
    request = state.take_translation_request()
    state.begin_final()
    state.update_source("Hello world again")
    state.finish_final()
    assert state.accept_translation(request, "旧句译文") is None
    state.reset()
    state.update_source("Hello world again")
    assert state.accept_translation(request, "旧会话译文") is None


def test_pipeline_rejects_dequeued_preview_if_final_already_advanced_sentence():
    pipe = pipe_fixture()
    pipe._live_draft.update_source("Hello world")
    request = pipe._live_draft.take_translation_request(now=time.monotonic() + 3)
    pipe._queue_translation("Hello world", time.monotonic())
    pipe._translate_draft(request)
    assert pipe._translator.calls == []


@pytest.mark.parametrize("text,language", [("我到家了", "zh"), ("我们到了", "zh"), ("We reached the station", "en"), ("The door opened", "en")])
def test_clear_short_completions_do_not_wait_for_word_count(text, language):
    from voxsub.contextual_text import looks_incomplete
    assert not looks_incomplete(text, language)


@pytest.mark.parametrize("text,language", [("因为我们到了", "zh"), ("我们需要", "zh"), ("We went to", "en"), ("I would like to", "en")])
def test_connector_fragments_still_wait_for_context(text, language):
    from voxsub.contextual_text import looks_incomplete
    assert looks_incomplete(text, language)


def test_rejected_final_releases_pending_slot_without_translating_or_leaking_text(caplog):
    pipe = pipe_fixture()
    finals = []
    drafts = []
    pipe.on_utterance(lambda *pair: finals.append(pair))
    pipe.on_draft(lambda *pair: drafts.append(pair))
    pipe._queue_translation("不符合指定英语的正文", time.monotonic())
    pipe._live_draft.update_source("Hello world")
    assert pipe._live_draft.take_translation_request(now=time.monotonic() + 3) is None
    pipe._translate_queued_item(pipe._translation_queue.get_nowait())
    assert not finals
    assert not pipe._translator.calls
    assert "不符合指定英语的正文" not in caplog.text
    assert drafts[-1][0] == "Hello world"
    assert pipe._live_draft.take_translation_request(now=time.monotonic() + 3) is not None
