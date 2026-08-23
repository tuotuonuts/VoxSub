"""Background TTS integration tests without requiring a physical speaker."""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from voxsub.pipeline import Pipeline
from voxsub.tts import SAMPLE_RATE
from voxsub.tts_worker import TTSWorker


class _FakeEngine:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @staticmethod
    def synthesize(text: str, lang: str = "zh") -> np.ndarray:
        return np.full(len(text), 0.25, dtype=np.float32)


def test_worker_synthesizes_and_plays_in_background(tmp_path: Path) -> None:
    played: list[tuple[int, int]] = []
    ready = threading.Event()

    def player(pcm: np.ndarray, sample_rate: int) -> None:
        played.append((pcm.size, sample_rate))
        ready.set()

    worker = TTSWorker(tmp_path, engine_factory=_FakeEngine, player=player)
    worker.start()
    try:
        assert worker.submit("hello", "en")
        assert ready.wait(2.0)
    finally:
        worker.stop()

    assert played == [(5, SAMPLE_RATE)]


def test_worker_replaces_oldest_pending_speech_when_full(tmp_path: Path) -> None:
    worker = TTSWorker(tmp_path, max_pending=2, engine_factory=_FakeEngine,
                       player=lambda _pcm, _rate: None)

    assert worker.submit("first", "en")
    assert worker.submit("second", "en")
    assert worker.submit("latest", "en")

    pending = [worker._queue.get_nowait().text for _ in range(2)]  # noqa: SLF001
    assert pending == ["second", "latest"]


def test_pipeline_submits_successful_translation_to_tts_worker() -> None:
    submitted: list[tuple[str, str]] = []
    pipeline = Pipeline()
    pipeline._translator = type("Translator", (), {  # noqa: SLF001
        "translate": lambda self, *_args, **_kwargs: "Hello",
    })()
    pipeline._trans_kind = None  # noqa: SLF001
    pipeline._tts_worker = type("Worker", (), {  # noqa: SLF001
        "submit": lambda self, text, lang: submitted.append((text, lang)),
    })()

    pipeline._translate_sentence("你好")  # noqa: SLF001

    assert submitted == [("Hello", "en")]
