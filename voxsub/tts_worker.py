"""Bounded background synthesis and playback for translated subtitles."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import numpy as np

from voxsub.logging_setup import get_logger
from voxsub.tts import SAMPLE_RATE, TTSEngine

logger = get_logger("tts_worker")


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    lang: str


class TTSWorker:
    """Synthesize and play translations without blocking the pipeline.

    Speech is ephemeral UI output, so when the small queue is full the oldest
    pending sentence is replaced by the newest one.  This explicit policy
    prevents stale speech from lagging minutes behind live subtitles.
    """

    def __init__(
        self,
        models_root: Path | str,
        *,
        external_stop: threading.Event | None = None,
        max_pending: int = 4,
        model_ids: Mapping[str, str] | None = None,
        engine_factory: Callable[..., TTSEngine] = TTSEngine,
        player: Callable[[np.ndarray, int], None] | None = None,
    ) -> None:
        self._tts_dir = Path(models_root) / "tts"
        self._model_ids = dict(model_ids or {})
        self._external_stop = external_stop
        self._engine_factory = engine_factory
        self._player = player or self._play_default
        self._queue: queue.Queue[SpeechRequest] = queue.Queue(
            maxsize=max(1, int(max_pending)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="pipeline-tts",
            daemon=True,
        )
        self._thread.start()

    def submit(self, text: str, lang: str) -> bool:
        request = SpeechRequest(str(text or "").strip(), str(lang or "").strip())
        if not request.text:
            return False
        try:
            self._queue.put_nowait(request)
            return True
        except queue.Full:
            try:
                dropped = self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - another consumer won the race
                dropped = None
            logger.warning("TTS 播放积压，替换最旧句子: dropped_chars=%s new_chars=%d",
                           len(dropped.text) if dropped else 0, len(request.text))
            try:
                self._queue.put_nowait(request)
                return True
            except queue.Full:  # pragma: no cover - consumer/producer race
                return False

    def stop(self, *, timeout: float = 3.0) -> None:
        self._discard_pending()
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        self._thread = None if thread is None or not thread.is_alive() else thread

    def _should_stop(self) -> bool:
        return self._stop.is_set() or bool(
            self._external_stop is not None and self._external_stop.is_set())

    def _discard_pending(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _run(self) -> None:
        try:
            engine_kwargs = (
                {"model_ids": self._model_ids} if self._model_ids else {})
            engine = self._engine_factory(self._tts_dir, **engine_kwargs)
        except Exception:
            logger.exception("TTS 引擎初始化失败，已降级为仅字幕")
            return
        while not self._should_stop():
            try:
                request = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                pcm = engine.synthesize(request.text, lang=request.lang)
                if pcm is not None and np.asarray(pcm).size:
                    self._player(np.asarray(pcm, dtype=np.float32), SAMPLE_RATE)
            except Exception:
                logger.warning("TTS 播放失败，已跳过当前句", exc_info=True)

    @staticmethod
    def _play_default(pcm: np.ndarray, sample_rate: int) -> None:
        import soundcard as sc

        speaker = sc.default_speaker()
        if speaker is None:
            raise RuntimeError("没有可用的默认播放设备")
        speaker.play(pcm, samplerate=sample_rate)


__all__ = ["SpeechRequest", "TTSWorker"]
