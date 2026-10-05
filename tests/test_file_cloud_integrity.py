"""Synthetic cloud file integrity tests: no service, microphone, speaker or weights."""
import numpy as np
import pytest
from voxsub.file_transcriber import FileRecognizer


class Vad:
    window_size = 4
    def __init__(self, speech=True):
        self.speech = speech
    def is_speech(self, chunk):
        assert len(chunk) == self.window_size, "VAD requires complete frames"
        return self.speech
    def reset(self):
        pass


def recognize(stt, monkeypatch, *, speech=True, pcm=None):
    monkeypatch.setattr(FileRecognizer, "_translate", lambda *args, **kwargs: None)
    audio = np.ones(128, dtype=np.float32) if pcm is None else pcm
    return FileRecognizer.cloud(audio, vad=Vad(speech), cloud_stt=stt,
        translator=None, source_lang="en", target_lang="zh",
        tuning={"silence_ms": 2, "max_utterance_ms": 1}, validate_translation=False)


def test_continuous_speech_obeys_cloud_chunk_budget(monkeypatch):
    sizes = []
    class Stt:
        def transcribe_samples(self, samples, **kwargs):
            sizes.append(len(samples))
            return "hello"
    lines = recognize(Stt(), monkeypatch)
    assert len(lines) == 8
    assert sizes == [16] * 8
    assert sum(sizes) == 128


def test_failed_cloud_chunk_cannot_be_silently_exported_as_complete(monkeypatch):
    class Stt:
        def transcribe_samples(self, samples, **kwargs):
            raise TimeoutError("synthetic service timeout")
    with pytest.raises(RuntimeError, match="第 1 个片段") as error:
        recognize(Stt(), monkeypatch)
    assert isinstance(error.value.__cause__, TimeoutError)


def test_silence_is_not_misreported_as_a_service_failure(monkeypatch):
    class Stt:
        def transcribe_samples(self, samples, **kwargs):
            pytest.fail("silence must not invoke cloud recognition")
    assert recognize(Stt(), monkeypatch, speech=False) == []


@pytest.mark.parametrize("sample_count", [1, 3, 17, 129])
def test_cloud_last_partial_frame_keeps_original_samples(monkeypatch, sample_count):
    received = []
    class Stt:
        def transcribe_samples(self, samples, **kwargs):
            received.append(samples.copy())
            return "hello"
    pcm = np.arange(sample_count, dtype=np.float32) + 1
    recognize(Stt(), monkeypatch, pcm=pcm)
    assert received, "short speech must not disappear"
    np.testing.assert_array_equal(np.concatenate(received), pcm)


@pytest.mark.parametrize("sample_count", [1, 3, 17, 129])
def test_local_last_partial_frame_keeps_original_samples(monkeypatch, sample_count):
    from voxsub import file_transcriber
    received = []
    class Asr:
        def feed(self, stream, chunk):
            received.append(chunk.copy())
        def reset(self, stream):
            pass
    monkeypatch.setattr(file_transcriber, "create_asr_stream", lambda *args: object())
    monkeypatch.setattr(FileRecognizer, "_flush_local_segment", lambda *args: None)
    pcm = np.arange(sample_count, dtype=np.float32) + 1
    FileRecognizer._recognize_local_segments(
        pcm, Vad(), Asr(), "en", 32, 16, [], None, [-1])
    assert received, "short speech must not disappear"
    np.testing.assert_array_equal(np.concatenate(received), pcm)
