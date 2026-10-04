"""Barrier-controlled regression for Pipeline resource admission races."""
from __future__ import annotations

import threading

import pytest

from voxsub.pipeline import Pipeline, PipelineState


@pytest.mark.parametrize("boundary", ["_prepare_start", "_new_realtime_threads", "_finish_start"])
def test_languages_cannot_change_while_start_owns_configuration(
        tmp_path, monkeypatch, boundary):
    pipe = Pipeline(models=tmp_path / "models")
    recognizer, segmenter, context = object(), object(), object()
    pipe._asr, pipe._seg, pipe._context_processor = recognizer, segmenter, context
    before = pipe._lang_snapshot()
    reached, release = threading.Event(), threading.Event()
    errors = []
    monkeypatch.setattr(pipe, "_new_realtime_threads", lambda: [])
    original = getattr(pipe, boundary)

    def blocked_boundary():
        reached.set()
        if not release.wait(timeout=2):
            raise AssertionError("startup barrier was not released")
        return original()

    monkeypatch.setattr(pipe, boundary, blocked_boundary)

    def start():
        try:
            pipe.start()
        except Exception as exc:
            errors.append(exc)

    starter = threading.Thread(target=start, daemon=True)
    starter.start()
    try:
        assert reached.wait(timeout=2)
        pipe.set_langs("en", "ja")
        assert pipe._lang_snapshot() == before
        assert (pipe._asr, pipe._seg, pipe._context_processor) == (
            recognizer, segmenter, context)
    finally:
        release.set()
        starter.join(timeout=2)
        pipe.close()
    assert not starter.is_alive()
    assert errors == []


def test_closed_pipeline_rejects_language_changes(tmp_path):
    pipe = Pipeline(models=tmp_path / "models")
    assert pipe.close() is True
    before = pipe._lang_snapshot()
    pipe.set_langs("en", "ja")
    assert pipe._lang_snapshot() == before


def test_running_pipeline_accepts_live_language_changes_without_replacing_resources(tmp_path):
    pipe = Pipeline(models=tmp_path / "models")
    recognizer, segmenter, context = object(), object(), object()
    pipe._asr, pipe._seg, pipe._context_processor = recognizer, segmenter, context
    pipe._set_state(PipelineState.RUNNING)
    generation = pipe.config_generation
    try:
        pipe.set_langs("en", "ja")
        assert pipe._lang_snapshot().pair == ("en", "ja")
        assert pipe.config_generation == generation + 1
        assert (pipe._asr, pipe._seg, pipe._context_processor) == (
            recognizer, segmenter, context)
        pipe.set_langs("en", "ja")
        assert pipe.config_generation == generation + 1
    finally:
        pipe.close()


def test_resource_setter_and_start_admission_are_atomic(tmp_path, monkeypatch):
    """start() cannot claim the Pipeline between a setter's gate and mutation."""
    pipe = Pipeline(models=tmp_path / "models")

    class _Translator:
        closed = False

        def close(self) -> None:
            self.closed = True

    old_translator = _Translator()
    pipe._translator = old_translator  # noqa: SLF001
    gate_entered = threading.Event()
    release_gate = threading.Event()
    claim_attempted = threading.Event()
    start_done = threading.Event()
    errors: list[Exception] = []
    state_seen_by_builder: list[tuple[bool, bool]] = []

    original_gate = pipe._may_replace_resources  # noqa: SLF001
    original_claim = pipe._claim_start  # noqa: SLF001

    def blocked_gate() -> bool:
        allowed = original_gate()
        gate_entered.set()
        if not release_gate.wait(timeout=2.0):
            raise AssertionError("test did not release the resource gate")
        return allowed

    def observed_claim() -> bool:
        claim_attempted.set()
        return original_claim()

    def fake_builder() -> list[threading.Thread]:
        state_seen_by_builder.append((
            pipe._translator is None,  # noqa: SLF001
            pipe._start_in_progress,  # noqa: SLF001
        ))
        return []

    monkeypatch.setattr(pipe, "_may_replace_resources", blocked_gate)
    monkeypatch.setattr(pipe, "_claim_start", observed_claim)
    monkeypatch.setattr(pipe, "_new_realtime_threads", fake_builder)

    def run_setter() -> None:
        try:
            pipe.set_translator("qwen-quality", {"testOnly": True})
        except Exception as exc:  # surfaced in the main test thread
            errors.append(exc)

    def run_start() -> None:
        try:
            pipe.start()
        except Exception as exc:  # surfaced in the main test thread
            errors.append(exc)
        finally:
            start_done.set()

    setter_thread = threading.Thread(target=run_setter, daemon=True)
    start_thread = threading.Thread(target=run_start, daemon=True)
    setter_thread.start()
    try:
        assert gate_entered.wait(timeout=2.0)
        start_thread.start()
        assert claim_attempted.wait(timeout=2.0)
        assert not start_done.wait(timeout=0.05), (
            "start() claimed the Pipeline while a resource setter was between "
            "its gate check and mutation"
        )
    finally:
        release_gate.set()
        setter_thread.join(timeout=2.0)
        if start_thread.ident is not None:
            start_thread.join(timeout=2.0)
        pipe.close()

    assert not setter_thread.is_alive()
    assert not start_thread.is_alive()
    assert errors == []
    assert old_translator.closed is True
    assert state_seen_by_builder == [(True, True)]


def _apply_other_resource_update(pipe, tmp_path, setter_name: str) -> None:
    if setter_name == "set_langs":
        pipe.set_langs("en", "zh")
    elif setter_name == "set_models_dir":
        pipe.set_models_dir(tmp_path / "alternate-models")
    elif setter_name == "set_stt":
        pipe.set_stt("cloud", {"test_only": True})
    elif setter_name == "set_asr_model":
        pipe.set_asr_model("asr-funasr-nano-2512-int8")
    elif setter_name == "set_asr_tuning":
        pipe.set_asr_tuning({"asr_vad_threshold": 0.22})
    else:
        raise AssertionError(f"unexpected setter: {setter_name}")


@pytest.mark.parametrize(
    "setter_name",
    ["set_langs", "set_models_dir", "set_stt", "set_asr_model", "set_asr_tuning"],
)
def test_resource_setters_serialize_start_admission(
    tmp_path, monkeypatch, setter_name: str
) -> None:
    pipe = Pipeline(models=tmp_path / "models")
    gate_entered = threading.Event()
    release_gate = threading.Event()
    claim_attempted = threading.Event()
    start_done = threading.Event()
    errors: list[Exception] = []

    original_gate = pipe._may_replace_resources  # noqa: SLF001
    original_claim = pipe._claim_start  # noqa: SLF001

    def blocked_gate() -> bool:
        allowed = original_gate()
        gate_entered.set()
        if not release_gate.wait(timeout=2.0):
            raise AssertionError("test did not release the resource gate")
        return allowed

    def observed_claim() -> bool:
        claim_attempted.set()
        return original_claim()

    monkeypatch.setattr(pipe, "_may_replace_resources", blocked_gate)
    monkeypatch.setattr(pipe, "_claim_start", observed_claim)
    monkeypatch.setattr(pipe, "_new_realtime_threads", lambda: [])

    def run_update() -> None:
        try:
            _apply_other_resource_update(pipe, tmp_path, setter_name)
        except Exception as exc:
            errors.append(exc)

    def run_start() -> None:
        try:
            pipe.start()
        except Exception as exc:
            errors.append(exc)
        finally:
            start_done.set()

    setter_thread = threading.Thread(target=run_update, daemon=True)
    start_thread = threading.Thread(target=run_start, daemon=True)
    setter_thread.start()
    try:
        assert gate_entered.wait(timeout=2.0)
        start_thread.start()
        assert claim_attempted.wait(timeout=2.0)
        assert not start_done.wait(timeout=0.05), (
            f"start() overtook {setter_name} after its gate check"
        )
    finally:
        release_gate.set()
        setter_thread.join(timeout=2.0)
        if start_thread.ident is not None:
            start_thread.join(timeout=2.0)
        pipe.close()

    assert not setter_thread.is_alive()
    assert not start_thread.is_alive()
    assert errors == []
