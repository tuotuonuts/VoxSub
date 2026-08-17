"""Session recording for microphone simultaneous-translation mode."""
from __future__ import annotations

import os
import threading
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from voxsub.logging_setup import get_logger

logger = get_logger("recording")


def default_recordings_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "VoxSub" / "recordings"


class WaveSessionRecorder:
    """Thread-safe 16 kHz mono WAV writer used by the capture worker.

    The output path is allocated when recording starts, like a phone recorder.
    Pausing is controlled by the pipeline: paused chunks are intentionally not
    passed to ``write``.  Closing is idempotent so device-error cleanup is safe.
    """

    def __init__(self, directory: Path | str | None = None, *, sample_rate: int = 16000,
                 now: datetime | None = None) -> None:
        folder = Path(directory) if directory is not None else default_recordings_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        candidate = folder / f"VoxSub-{stamp}.wav"
        suffix = 2
        while candidate.exists():
            candidate = folder / f"VoxSub-{stamp}-{suffix}.wav"
            suffix += 1
        self.path = candidate
        self.sample_rate = int(sample_rate)
        self.frames_written = 0
        self._lock = threading.Lock()
        self._wave: wave.Wave_write | None = wave.open(str(candidate), "wb")
        self._wave.setnchannels(1)
        self._wave.setsampwidth(2)
        self._wave.setframerate(self.sample_rate)
        logger.info("同传录音开始: %s", candidate)

    def write(self, samples: np.ndarray) -> None:
        pcm = np.asarray(samples, dtype=np.float32).reshape(-1)
        if pcm.size == 0:
            return
        data = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        with self._lock:
            if self._wave is None:
                return
            self._wave.writeframesraw(data)
            self.frames_written += pcm.size

    def close(self) -> Path:
        with self._lock:
            writer, self._wave = self._wave, None
            if writer is not None:
                writer.close()
        logger.info("同传录音结束: path=%s duration=%.2fs", self.path,
                    self.frames_written / max(1, self.sample_rate))
        return self.path

    @property
    def duration_seconds(self) -> float:
        return self.frames_written / max(1, self.sample_rate)

    def __enter__(self) -> "WaveSessionRecorder":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
