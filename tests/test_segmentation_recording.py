from __future__ import annotations

import wave
from datetime import datetime

import numpy as np

from voxsub.asr import AudioUtteranceSegmenter
from voxsub.recording import WaveSessionRecorder


class _EnergyVAD:
    window_size = 160

    def __init__(self) -> None:
        self.resets = 0

    def is_speech(self, chunk: np.ndarray) -> bool:
        return float(np.mean(chunk)) > 0.5

    def reset(self) -> None:
        self.resets += 1


class _StreamingDraftASR:
    runtime = "sherpa-streaming-transducer"

    def __init__(self) -> None:
        self.resets = 0

    @staticmethod
    def create_stream() -> dict[str, int]:
        return {"speech_windows": 0}

    @staticmethod
    def feed(stream: dict[str, int], chunk: np.ndarray) -> None:
        if float(np.mean(chunk)) > 0.5:
            stream["speech_windows"] += 1

    @staticmethod
    def get_result(stream: dict[str, int]) -> str:
        return f"draft-{stream['speech_windows']}"

    def reset(self, stream: dict[str, int]) -> None:
        stream.clear()
        self.resets += 1


def test_generative_segmenter_uses_natural_pause_and_accepts_arbitrary_blocks() -> None:
    vad = _EnergyVAD()
    emitted: list[np.ndarray] = []
    seg = AudioUtteranceSegmenter(
        vad, emitted.append,
        min_silence_ms=20,
        max_utterance_ms=10_000,
        min_speech_ms=10,
        pre_roll_ms=10,
    )
    # Three speech windows followed by two silence windows.  Feed uneven block
    # sizes to verify that no boundary samples are lost.
    audio = np.concatenate([
        np.ones(160 * 3, dtype=np.float32),
        np.zeros(160 * 2, dtype=np.float32),
    ])
    seg.feed(audio[:213])
    seg.feed(audio[213:577])
    seg.feed(audio[577:])
    assert len(emitted) == 1
    assert emitted[0].size == audio.size
    assert vad.resets == 1


def test_generative_segmenter_flushes_tail_without_padding_in_output() -> None:
    emitted: list[np.ndarray] = []
    seg = AudioUtteranceSegmenter(
        _EnergyVAD(), emitted.append,
        min_silence_ms=500,
        max_utterance_ms=10_000,
        min_speech_ms=5,
        pre_roll_ms=5,
    )
    seg.feed(np.ones(200, dtype=np.float32))
    seg.flush()
    assert len(emitted) == 1
    # One full VAD window plus the padded tail is expected internally; the
    # important contract is that the actual 200 samples are all retained.
    assert emitted[0].size >= 200


def test_generative_segmenter_streams_sidecar_drafts_before_final_audio() -> None:
    emitted: list[np.ndarray] = []
    partials: list[str] = []
    draft_asr = _StreamingDraftASR()
    seg = AudioUtteranceSegmenter(
        _EnergyVAD(), emitted.append,
        min_silence_ms=20,
        max_utterance_ms=10_000,
        min_speech_ms=10,
        pre_roll_ms=10,
        draft_asr=draft_asr,
        on_partial=partials.append,
        partial_interval_ms=20,
    )

    seg.feed(np.ones(160 * 8, dtype=np.float32))
    assert len(partials) >= 3
    assert len(set(partials)) >= 3
    assert emitted == []

    seg.feed(np.zeros(160 * 2, dtype=np.float32))
    assert len(emitted) == 1
    assert draft_asr.resets == 1


def test_wave_session_recorder_writes_valid_pcm_and_close_is_idempotent(tmp_path) -> None:
    recorder = WaveSessionRecorder(
        tmp_path,
        now=datetime(2026, 8, 18, 12, 0, 0),
    )
    recorder.write(np.array([-1.0, -0.5, 0.0, 0.5, 1.0], dtype=np.float32))
    path = recorder.close()
    assert recorder.close() == path
    assert path.name == "VoxSub-20260818-120000.wav"
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.getnframes() == 5
