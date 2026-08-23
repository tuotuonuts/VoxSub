"""Qt-independent state models shared by the main window and overlay."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Mapping


@dataclass
class ConversationSession:
    entries: list[tuple[str, str, int]] = field(default_factory=list)
    _started_at: float | None = None

    def append(self, source: str, translation: str, *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else float(now)
        if self._started_at is None:
            self._started_at = timestamp
        elapsed_ms = int((timestamp - self._started_at) * 1000)
        self.entries.append((source, translation, elapsed_ms))

    def snapshot(self) -> tuple[tuple[str, str, int], ...]:
        return tuple(self.entries)

    def clear(self) -> None:
        self.entries.clear()
        self._started_at = None


@dataclass
class SubtitleHistory:
    maximum: int = 200
    items: deque[tuple[str, str]] = field(init=False)
    position: int = 0

    def __post_init__(self) -> None:
        self.items = deque(maxlen=max(1, int(self.maximum)))

    def append(self, source: str, translation: str) -> None:
        self.items.append((source, translation))
        self.position = 0

    def clear(self) -> None:
        self.items.clear()
        self.position = 0

    def step(self, delta: int) -> tuple[tuple[str, str] | None, bool]:
        if not self.items:
            return None, False
        if delta > 0:
            self.position = min(len(self.items) - 1, self.position + 1)
        else:
            self.position = max(0, self.position - 1)
        index = len(self.items) - 1 - self.position
        hit_edge = ((delta > 0 and index == 0) or
                    (delta < 0 and self.position == 0))
        return self.items[index], hit_edge


@dataclass
class RecognitionTuningDraft:
    snapshot: dict[str, object] = field(default_factory=dict)
    dirty: bool = False

    def load(self, values: Mapping[str, object]) -> None:
        self.snapshot = dict(values)
        self.dirty = False

    def compare(self, values: Mapping[str, object]) -> bool:
        self.dirty = dict(values) != self.snapshot
        return self.dirty

    def commit(self, values: Mapping[str, object]) -> None:
        self.load(values)


__all__ = ["ConversationSession", "RecognitionTuningDraft", "SubtitleHistory"]
