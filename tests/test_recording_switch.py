"""Recording is opt-in WAV saving, not capture or recognition control.

All frames are synthesized; device/model boundaries are replaced. Never opens
microphones, loopback, playback, real media, or real user configuration.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend" / "backend"))
from handlers.session import SessionHandlers


def test_handler_returns_confirmed_state_not_void():
    class Pipeline:
        recording_state = {"recordingEnabled": False, "recordingActive": False,
                           "recordingSupported": True, "recordingCanChange": True}

        def set_recording(self, enabled, directory):
            # Deliberately rejects without throwing: never echo requested value.
            pass

    result = SessionHandlers()._cmd_set_recording(Pipeline(), {"enabled": True})
    assert result == Pipeline.recording_state


import numpy as np
import pytest
import wave
from voxsub.pipeline import Pipeline, PipelineState


@pytest.mark.parametrize("phase", [PipelineState.STARTING, PipelineState.RUNNING, PipelineState.STOPPING])
def test_live_setter_rejects_and_preserves_intent(tmp_path, phase):
    pipeline = Pipeline(models=tmp_path / "models")
    pipeline.set_recording(True, tmp_path)
    pipeline._set_state(phase)
    with pytest.raises(RuntimeError):
        pipeline.set_recording(False)
    assert pipeline._recording_enabled is True
    assert pipeline._recordings_dir == tmp_path
    assert pipeline.state == phase
    pipeline._set_state(PipelineState.IDLE)


@pytest.mark.parametrize("guard", ["_closed", "_start_in_progress", "_stop_finalizers"])
def test_recording_respects_existing_lifecycle_admission(tmp_path, guard):
    pipeline = Pipeline(models=tmp_path / "models")
    setattr(pipeline, guard, True)
    with pytest.raises(RuntimeError):
        pipeline.set_recording(True, tmp_path)
    assert pipeline._recording_enabled is False


def test_idle_toggle_returns_effective_snapshot(tmp_path):
    pipeline = Pipeline(models=tmp_path / "models")
    service = SessionHandlers()
    for enabled in (True, False, True):
        result = service._cmd_set_recording(pipeline, {"enabled": enabled, "directory": str(tmp_path)})
        assert result == {"recordingEnabled": enabled, "recordingActive": False,
                          "recordingSupported": True, "recordingCanChange": True}
    assert list(tmp_path.glob("*.wav")) == []


@pytest.mark.parametrize("mode", ["a", "b", "c"])
def test_mode_capability_and_no_capture_side_effect(tmp_path, mode):
    pipeline = Pipeline(models=tmp_path / "models")
    pipeline.set_recording(True, tmp_path)
    pipeline.set_mode(mode)
    snapshot = pipeline.recording_state
    assert snapshot["recordingSupported"] is (mode == "a")
    assert snapshot["recordingActive"] is False
    assert pipeline._source is None
    assert pipeline.is_running() is False


def test_synthetic_frames_append_pause_finalize_and_restart(tmp_path, monkeypatch):
    pipeline = Pipeline(models=tmp_path / "models")
    monkeypatch.setattr(pipeline, "_build_real_time", lambda: None)
    monkeypatch.setattr(pipeline, "_start_tts_worker", lambda: None)
    pipeline.set_recording(True, tmp_path / "wav")
    # Build the real WAV owner; no worker is started, no device or model opened.
    pipeline._new_realtime_threads()
    first = pipeline._recorder
    frame = np.array([-1, -0.25, 0, 0.25, 1], dtype=np.float32)
    pipeline._accept_capture_chunk(frame)
    pipeline._accept_capture_chunk(frame)
    pipeline._pause_evt.set()
    pipeline._accept_capture_chunk(frame)
    assert first.frames_written == 10
    pipeline._pause_evt.clear()
    pipeline._accept_capture_chunk(frame)
    # Real capture-loop finally closes its WAV even on immediate stop.
    class Source:
        device_name = "synthetic-not-hardware"
        def start(self): pass
        def stop(self): pass
        def close(self): pass
    monkeypatch.setattr(pipeline, "_make_source", lambda: Source())
    pipeline._stop_evt.set()
    pipeline._capture_loop()
    assert pipeline._recorder is None
    with wave.open(str(first.path), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (1, 2, 16000, 15)
        expected = (np.clip(frame, -1, 1) * 32767).astype("<i2").tobytes() * 3
        assert wav.readframes(15) == expected
    before = first.path.read_bytes()
    pipeline.set_recording(False, tmp_path / "wav")
    pipeline._new_realtime_threads()
    pipeline._accept_capture_chunk(frame)
    assert first.path.read_bytes() == before
    assert pipeline._recorder is None
    assert not pipeline._queue.empty(), "WAV off must not stop independent recognition input"
    pipeline.set_recording(True, tmp_path / "wav")
    pipeline._new_realtime_threads()
    second = pipeline._recorder
    assert second.path != first.path
    pipeline._accept_capture_chunk(frame)
    pipeline._capture_loop()
    with wave.open(str(second.path), "rb") as wav:
        assert wav.getnframes() == 5
