"""Background TTS integration tests without requiring a physical speaker."""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from voxsub.pipeline import Pipeline
from voxsub.tts import SAMPLE_RATE, TTSEngine
from voxsub.tts_worker import TTSWorker


class _FakeEngine:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @staticmethod
    def synthesize(text: str, lang: str = "zh") -> np.ndarray:
        return np.full(len(text), 0.25, dtype=np.float32)


class _GeneratedAudio:
    samples = np.ones(80, dtype=np.float32)
    sample_rate = SAMPLE_RATE


class _LateInstalledVoice:
    @staticmethod
    def generate(_text: str, sid: int = 0, speed: float = 1.0) -> _GeneratedAudio:
        return _GeneratedAudio()


def test_engine_discovers_model_installed_after_worker_start(
    tmp_path: Path, monkeypatch,
) -> None:
    engine = TTSEngine(tmp_path)
    assert engine.synthesize("hello", "en") is None

    voice_dir = tmp_path / "en"
    voice_dir.mkdir(parents=True)
    (voice_dir / "model.onnx").write_bytes(b"model")
    (voice_dir / "tokens.txt").write_bytes(b"tokens")
    monkeypatch.setattr(engine, "_build_tts", lambda _lang: _LateInstalledVoice())

    pcm = engine.synthesize("hello", "en")

    assert pcm is not None
    assert pcm.dtype == np.float32
    assert pcm.size == 80


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


def test_tts_worker_stop_timeout_preserves_live_thread_and_rejects_new_speech(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    worker = TTSWorker(
        tmp_path,
        engine_factory=_FakeEngine,
        player=lambda _pcm, _rate: (entered.set(), release.wait()),
    )
    worker.start()
    assert worker.submit("blocking", "en")
    assert entered.wait(2.0)

    try:
        assert worker.stop(timeout=0.01) is False
        assert worker.is_alive is True
        assert worker.submit("must not queue while stopping", "en") is False
    finally:
        release.set()
    assert worker.stop(timeout=1.0) is True
    assert worker.is_alive is False


def test_pipeline_does_not_replace_tts_worker_until_old_playback_exits(
    tmp_path: Path, monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    old = TTSWorker(
        tmp_path,
        engine_factory=_FakeEngine,
        player=lambda _pcm, _rate: (entered.set(), release.wait()),
    )
    old.start()
    assert old.submit("blocking", "en")
    assert entered.wait(2.0)

    created: list[dict[str, object]] = []

    class _FreshWorker:
        is_alive = False

        def __init__(self, _models_root, *, model_ids, **_kwargs):
            created.append(dict(model_ids))

        def start(self):
            self.is_alive = True

    monkeypatch.setattr("voxsub.pipeline.TTSWorker", _FreshWorker)
    pipeline = Pipeline()
    pipeline._running = True  # noqa: SLF001
    pipeline._mode = "a"  # noqa: SLF001
    pipeline._tts_enabled = True  # noqa: SLF001
    pipeline._tts_worker = old  # noqa: SLF001
    try:
        pipeline.set_tts_models({"en": "new-english-model"})
        assert created == []
        assert pipeline._tts_worker is old  # noqa: SLF001
        assert old.is_alive is True
    finally:
        release.set()
    assert old.stop(timeout=1.0) is True

    pipeline._start_tts_worker()  # noqa: SLF001
    assert created == [{"zh": pipeline._tts_model_ids["zh"], "en": "new-english-model"}]
    assert pipeline._tts_worker is not old  # noqa: SLF001


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
