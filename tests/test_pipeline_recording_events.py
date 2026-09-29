"""Real Pipeline lifecycle notifications with only audio/model boundaries faked."""
from __future__ import annotations

import threading

import pytest

from voxsub.pipeline import PipelineState
from tests.test_pipeline_stale_stop import runtime  # shared external-boundary fixture


def observe(pipe):
    events = []
    ready = threading.Event()

    def callback():
        snapshot = {"state": pipe.state, **pipe.recording_state}
        events.append(snapshot)
        if snapshot["recordingCanChange"]:
            ready.set()

    pipe.on_state(callback)
    return events, ready


def assert_ready(pipe, events):
    assert events[-1]["state"] is PipelineState.IDLE
    assert events[-1]["recordingCanChange"] is True
    assert events[-1]["recordingActive"] is False
    pipe.set_recording(True)
    pipe.set_recording(False)


def test_public_synchronous_stop_publishes_setter_admission(runtime):
    pipe, source, *_ = runtime
    events, _ = observe(pipe)
    pipe.start()
    assert source.reading.wait(2)
    assert pipe.stop() is True
    assert_ready(pipe, events)
    assert pipe.stop() is True
    assert_ready(pipe, events)


@pytest.mark.parametrize("owner", ["capture", "tts"])
@pytest.mark.parametrize("action", ["stop", "close"])
def test_timeout_retains_admission_until_real_watcher_settles(runtime, monkeypatch, owner, action):
    from voxsub import pipeline as module

    pipe, source, *_, translator = runtime
    events, ready = observe(pipe)
    release, entered = threading.Event(), threading.Event()
    monkeypatch.setattr(module, "_STOP_JOIN_SECONDS", 0.02)
    monkeypatch.setattr(module, "_SETTLE_POLL_SECONDS", 0.005)
    if owner == "capture":
        pipe.set_tts(False)
        def delayed_read():
            entered.set()
            assert release.wait(5)
            return None
        monkeypatch.setattr(source, "read_chunk", delayed_read)
    pipe.start()
    if owner == "capture":
        assert entered.wait(2)
    else:
        assert source.reading.wait(2)
        tts = pipe._tts_worker
        release = tts.release
        original_stop = tts.stop
        monkeypatch.setattr(tts, "stop", lambda: False if tts.is_alive else original_stop())
    try:
        assert getattr(pipe, action)() is False
        watcher = pipe._settle_watcher
        assert events[-1]["state"] is PipelineState.STOPPING
        assert not events[-1]["recordingCanChange"]
        assert not ready.is_set()
        with pytest.raises(RuntimeError):
            pipe.set_recording(True)
        with pytest.raises(RuntimeError):
            pipe.start()
        release.set()
        watcher.join(3)
        assert not watcher.is_alive()
        assert events[-1]["state"] is PipelineState.IDLE
        if action == "stop":
            assert ready.is_set()
            assert_ready(pipe, events)
        else:
            assert not ready.is_set()
            assert all(not e["recordingCanChange"] for e in events)
            assert translator.closed
            with pytest.raises(RuntimeError):
                pipe.set_recording(True)
    finally:
        release.set()
        if pipe._settle_watcher is not None:
            pipe._settle_watcher.join(3)


def test_overlapping_stop_owners_publish_only_after_external_close_returns(runtime, monkeypatch):
    pipe, source, *_ = runtime
    events, ready = observe(pipe)
    pipe.start()
    assert source.reading.wait(2)
    tts = pipe._tts_worker
    original_stop = tts.stop
    entered, release = threading.Event(), threading.Event()
    results = []

    def delayed_stop():
        result = original_stop()
        if threading.current_thread().name == "first-stop":
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(tts, "stop", delayed_stop)
    stopper = threading.Thread(target=lambda: results.append(pipe.stop()), name="first-stop", daemon=True)
    stopper.start()
    try:
        assert entered.wait(2)
        assert pipe._state_lock.acquire(timeout=1), "external close must not hold state lock"
        pipe._state_lock.release()
        assert pipe.stop() is True
        assert not ready.is_set()
        assert not events[-1]["recordingCanChange"]
        with pytest.raises(RuntimeError):
            pipe.set_recording(True)
        with pytest.raises(RuntimeError):
            pipe.start()
        release.set()
        stopper.join(2)
        assert not stopper.is_alive()
        assert results == [True]
        assert_ready(pipe, events)
    finally:
        release.set()
        stopper.join(2)


@pytest.mark.parametrize("action", ["stop", "close"])
def test_cancelled_public_start_publishes_admission_after_builder_releases(runtime, monkeypatch, action):
    pipe, source, *_ = runtime
    events, ready = observe(pipe)
    building, release = threading.Event(), threading.Event()
    original_build = pipe._build_real_time
    errors = []

    def delayed_build():
        building.set()
        assert release.wait(5)
        original_build()

    def start():
        try:
            pipe.start()
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(pipe, "_build_real_time", delayed_build)
    starter = threading.Thread(target=start, daemon=True)
    starter.start()
    try:
        assert building.wait(2)
        assert getattr(pipe, action)() is False
        assert not ready.is_set()
        with pytest.raises(RuntimeError):
            pipe.set_recording(True)
        release.set()
        starter.join(2)
        assert not starter.is_alive()
        assert errors == []
        assert not source.reading.is_set()
        if action == "stop":
            assert_ready(pipe, events)
        else:
            assert all(not e["recordingCanChange"] for e in events)
    finally:
        release.set()
        starter.join(2)
