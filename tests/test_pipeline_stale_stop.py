"""Deterministic public start/stop regression; no audio, models or GUI."""
from __future__ import annotations

import threading

import pytest

from voxsub import pipeline as pipeline_module
from voxsub.pipeline import Pipeline, PipelineState


class _Source:
    def __init__(self, stop_event):
        self.stop_event = stop_event
        self.reading = threading.Event()
        self.stopped = False
        self.closed = False

    def start(self):
        pass

    def read_chunk(self):
        self.reading.set()
        if not self.stop_event.wait(timeout=5):
            raise AssertionError("test failed to stop capture")
        return None

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _Segmenter:
    def feed(self, _chunk):
        pass

    def flush(self):
        pass


class _Translator:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _TTS:
    """Replace only speech synthesis/playback, preserving a real worker."""
    def __init__(self, *_args, **_kwargs):
        self.release = threading.Event()
        self.thread = threading.Thread(target=self.release.wait, daemon=True)
        self.stop_calls = 0

    def start(self):
        self.thread.start()

    @property
    def is_alive(self):
        return self.thread.is_alive()

    def stop(self):
        self.stop_calls += 1
        self.release.set()
        self.thread.join(timeout=2)
        return not self.thread.is_alive()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    pipe = Pipeline(models=tmp_path / "models")
    source = _Source(pipe._stop_evt)
    segmenter, recognizer, translator = _Segmenter(), object(), _Translator()

    def build_models():
        pipe._seg = segmenter
        pipe._asr = recognizer
        pipe._translator = translator

    monkeypatch.setattr(pipe, "_build_real_time", build_models)
    monkeypatch.setattr(pipe, "_make_source", lambda: source)
    monkeypatch.setattr(pipeline_module, "TTSWorker", _TTS)
    pipe.set_tts(True)
    try:
        yield pipe, source, segmenter, recognizer, translator
    finally:
        pipe.close()
        for worker in pipe._threads:
            worker.join(timeout=2)


def _record_states(pipe):
    events = []
    pipe.on_state(lambda: events.append((pipe.state, pipe.recording_state)))
    return events


def test_stale_idle_stop_cannot_finalize_a_new_start(runtime, monkeypatch):
    pipe, source, segmenter, recognizer, translator = runtime
    events = _record_states(pipe)
    decided, resume = threading.Event(), threading.Event()
    original_begin = pipe._begin_stop
    results, errors = [], []

    def paused_begin():
        decision = original_begin()
        decided.set()
        if not resume.wait(timeout=5):
            raise AssertionError("idle stop was not released")
        return decision

    def stop():
        try:
            results.append(pipe.stop())
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(pipe, "_begin_stop", paused_begin)
    stopper = threading.Thread(target=stop, daemon=True)
    stopper.start()
    try:
        assert decided.wait(timeout=2)
        pipe.start()  # Real admission, construction, publication and Python worker loops.
        assert source.reading.wait(timeout=2)
        workers = tuple(pipe._threads)
        tts = pipe._tts_worker
        assert workers and all(worker.is_alive() for worker in workers)
        resume.set()
        stopper.join(timeout=2)
        assert not stopper.is_alive()
        assert errors == []
        assert pipe.state is PipelineState.RUNNING
        assert pipe.is_running() is True
        assert results == [False], "live workers cannot be reported fully stopped"
        assert events[-1][0] is PipelineState.RUNNING
        assert all(not snapshot["recordingCanChange"] for _, snapshot in events)
        with pytest.raises(RuntimeError):
            pipe.set_recording(True)
        assert tuple(pipe._threads) == workers
        assert all(worker.is_alive() for worker in workers)
        assert not pipe._stop_evt.is_set()
        assert pipe._tts_worker is tts and tts.is_alive
        assert tts.stop_calls == 0
        assert pipe._source is source and not source.stopped and not source.closed
        assert pipe._seg is segmenter and pipe._asr is recognizer
        assert pipe._translator is translator and not translator.closed
    finally:
        resume.set()
        stopper.join(timeout=2)
        monkeypatch.setattr(pipe, "_begin_stop", original_begin)


def test_idle_stop_and_serial_real_start_stop(runtime):
    pipe, source, _segmenter, _recognizer, translator = runtime
    assert pipe.stop() is True
    assert pipe.state is PipelineState.IDLE
    pipe.start()
    assert source.reading.wait(timeout=2)
    workers = tuple(pipe._threads)
    assert pipe.stop() is True
    assert pipe.state is PipelineState.IDLE
    assert not pipe.is_running()
    assert all(not worker.is_alive() for worker in workers)
    assert pipe._stop_evt.is_set()
    assert source.stopped and source.closed
    assert pipe._tts_worker is None
    assert not translator.closed  # stop retains reusable models; close owns release.
    assert pipe.close() is True
    assert translator.closed


def test_stale_idle_stop_preserves_a_later_failed_start(runtime, monkeypatch):
    """An intervening start may leave no live worker: state alone is not ownership."""
    pipe, _source, _seg, _asr, _translator = runtime
    decided, resume = threading.Event(), threading.Event()
    original_begin = pipe._begin_stop
    results = []

    def paused_begin():
        decision = original_begin()
        decided.set()
        assert resume.wait(timeout=5)
        return decision

    def failed_model_build():
        raise RuntimeError("fixture model failure")

    monkeypatch.setattr(pipe, "_begin_stop", paused_begin)
    monkeypatch.setattr(pipe, "_build_real_time", failed_model_build)
    stopper = threading.Thread(target=lambda: results.append(pipe.stop()), daemon=True)
    stopper.start()
    try:
        assert decided.wait(timeout=2)
        with pytest.raises(RuntimeError, match="fixture model failure"):
            pipe.start()
        assert pipe.state is PipelineState.FAILED
        resume.set()
        stopper.join(timeout=2)
        assert not stopper.is_alive()
        assert results == [False]
        assert pipe.state is PipelineState.FAILED
    finally:
        resume.set()
        stopper.join(timeout=2)
        monkeypatch.setattr(pipe, "_begin_stop", original_begin)


def test_idle_tts_finalization_excludes_start_without_holding_state_lock(runtime, monkeypatch):
    pipe, _source, _seg, _asr, _translator = runtime
    pipe._start_tts_worker()
    tts = pipe._tts_worker
    stopped, resume = threading.Event(), threading.Event()
    original_stop = tts.stop
    results = []

    def paused_tts_stop():
        result = original_stop()
        stopped.set()
        assert resume.wait(timeout=5)
        return result

    monkeypatch.setattr(tts, "stop", paused_tts_stop)
    stopper = threading.Thread(target=lambda: results.append(pipe.stop()), daemon=True)
    stopper.start()
    try:
        assert stopped.wait(timeout=2)
        assert not tts.is_alive
        # TTS has exited but its external close has not returned. Admission and
        # resource gates must remain shut, without holding the state lock over IO.
        assert pipe._state_lock.acquire(timeout=1)
        pipe._state_lock.release()
        with pytest.raises(RuntimeError, match="上一任务仍在安全收尾"):
            pipe.start()
        assert not pipe._may_replace_resources()
        resume.set()
        stopper.join(timeout=2)
        assert not stopper.is_alive()
        assert results == [True]
        assert pipe._may_replace_resources()
        assert pipe._tts_worker is None
        pipe.start()
        assert pipe.state is PipelineState.RUNNING
    finally:
        resume.set()
        stopper.join(timeout=2)


def _timed_out_tts(pipe, monkeypatch):
    pipe._start_tts_worker()
    old_tts = pipe._tts_worker
    original_stop = old_tts.stop
    monkeypatch.setattr(old_tts, "stop", lambda: False if old_tts.is_alive else original_stop())
    return old_tts


def test_stale_watcher_cannot_stop_or_finalize_a_new_start(runtime, monkeypatch):
    pipe, source, _seg, _asr, translator = runtime
    events = _record_states(pipe)
    old_tts = _timed_out_tts(pipe, monkeypatch)
    paused, resume = threading.Event(), threading.Event()
    original_alive = pipe._tts_worker_is_alive

    def paused_observation():
        alive = original_alive()
        if threading.current_thread().name == "pipeline-settle-watch" and not alive:
            paused.set()
            assert resume.wait(timeout=5)
        return alive

    monkeypatch.setattr(pipe, "_tts_worker_is_alive", paused_observation)
    assert pipe.stop() is False
    watcher = pipe._settle_watcher
    try:
        old_tts.release.set()
        old_tts.thread.join(timeout=2)
        assert paused.wait(timeout=2)
        assert pipe.stop() is True
        pipe.start()
        assert source.reading.wait(timeout=2)
        new_tts = pipe._tts_worker
        workers = tuple(pipe._threads)
        start_events = len(events)
        resume.set()
        watcher.join(timeout=2)
        assert events[-1][0] is PipelineState.RUNNING
        assert not events[-1][1]["recordingCanChange"]
        assert len(events) == start_events, "stale watcher must not publish for a new owner"
        with pytest.raises(RuntimeError):
            pipe.set_recording(True)
        assert not watcher.is_alive()
        assert pipe.state is PipelineState.RUNNING
        assert pipe.is_running() and not pipe._stop_evt.is_set()
        assert tuple(pipe._threads) == workers and all(t.is_alive() for t in workers)
        assert pipe._tts_worker is new_tts and new_tts.is_alive
        assert new_tts.stop_calls == 0
        assert not translator.closed
    finally:
        resume.set()
        watcher.join(timeout=2)


def test_watcher_tts_shutdown_holds_admission_not_state_lock(runtime, monkeypatch):
    pipe, source, *_ = runtime
    old_tts = _timed_out_tts(pipe, monkeypatch)
    paused, resume = threading.Event(), threading.Event()
    original_shutdown = pipe._stop_tts_worker

    def paused_shutdown():
        result = original_shutdown()
        if threading.current_thread().name == "pipeline-settle-watch" and result:
            paused.set()
            assert resume.wait(timeout=5)
        return result

    monkeypatch.setattr(pipe, "_stop_tts_worker", paused_shutdown)
    assert pipe.stop() is False
    watcher = pipe._settle_watcher
    try:
        old_tts.release.set()
        old_tts.thread.join(timeout=2)
        assert paused.wait(timeout=2)
        assert pipe._state_lock.acquire(timeout=1)
        pipe._state_lock.release()
        assert pipe.stop() is True
        with pytest.raises(RuntimeError, match="上一任务仍在安全收尾"):
            pipe.start()
        assert not pipe._may_replace_resources()
        resume.set()
        watcher.join(timeout=2)
        assert not watcher.is_alive()
        pipe.start()
        assert source.reading.wait(timeout=2)
        assert pipe.state is PipelineState.RUNNING
        assert pipe._tts_worker.is_alive
    finally:
        resume.set()
        watcher.join(timeout=2)


@pytest.mark.parametrize("action", ["stop", "close"])
def test_shutdown_during_real_start_construction(runtime, monkeypatch, action):
    pipe, source, _seg, _asr, translator = runtime
    building, resume = threading.Event(), threading.Event()
    original_build = pipe._build_real_time
    errors = []

    def paused_build():
        building.set()
        assert resume.wait(timeout=5)
        original_build()

    def start():
        try:
            pipe.start()
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(pipe, "_build_real_time", paused_build)
    starter = threading.Thread(target=start, daemon=True)
    starter.start()
    try:
        assert building.wait(timeout=2)
        assert getattr(pipe, action)() is False
        assert pipe._stop_evt.is_set()
        resume.set()
        starter.join(timeout=2)
        assert not starter.is_alive()
        assert errors == []
        assert pipe.state is PipelineState.IDLE
        assert pipe._threads == []
        assert pipe._tts_worker is None
        assert not source.reading.is_set()
        assert translator.closed is (action == "close")
    finally:
        resume.set()
        starter.join(timeout=2)
