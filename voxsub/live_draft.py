"""Thread-safe state for one replaceable live subtitle draft.

Recognition can revise an interim hypothesis while translation is slower and
finishes out of order.  This module keeps that concurrency policy independent
from the audio pipeline and the Qt presentation layer.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class DraftView:
    """The latest source/translation pair safe to present to the UI."""

    source: str
    translation: str = ""


@dataclass(frozen=True)
class DraftTranslationRequest:
    """An immutable revision handed to the translation worker."""

    revision: int
    source: str


class LiveDraftState:
    """Coordinate interim recognition, stale translation and final commits."""

    def __init__(
        self,
        *,
        debounce_seconds: float = 0.18,
        min_interval_seconds: float = 0.45,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._debounce = max(0.0, float(debounce_seconds))
        self._min_interval = max(0.0, float(min_interval_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._revision = 0
        self._source = ""
        self._translation = ""
        self._ready_at = 0.0
        self._last_translation_started = float("-inf")
        self._translation_requested_revision = -1
        self._finals_pending = 0

    def reset(self) -> None:
        with self._lock:
            self._revision += 1
            self._source = ""
            self._translation = ""
            self._ready_at = 0.0
            self._last_translation_started = float("-inf")
            self._translation_requested_revision = -1
            self._finals_pending = 0

    def update_source(self, source: str) -> DraftView | None:
        """Replace and immediately expose the current recognition hypothesis."""
        normalized = str(source or "").strip()
        if not normalized:
            return None
        with self._lock:
            if normalized == self._source:
                return None
            now = self._clock()
            self._revision += 1
            self._source = normalized
            self._translation = ""
            self._ready_at = max(
                now + self._debounce,
                self._last_translation_started + self._min_interval,
            )
            return DraftView(normalized)

    def begin_final(self) -> None:
        """Block a following draft until every earlier final is presented."""
        with self._lock:
            self._finals_pending += 1
            # The visible draft belongs to the sentence now entering the final
            # queue.  A later partial will create a fresh revision while final
            # translation runs.
            self._revision += 1
            self._source = ""
            self._translation = ""
            self._translation_requested_revision = -1

    def finish_final(self) -> DraftView | None:
        """Finish one commit and restore any following draft it temporarily covered."""
        with self._lock:
            self._finals_pending = max(0, self._finals_pending - 1)
            if not self._source:
                return None
            return DraftView(self._source, self._translation)

    def take_translation_request(
        self, *, now: float | None = None
    ) -> DraftTranslationRequest | None:
        """Return the newest due revision at most once."""
        current = self._clock() if now is None else float(now)
        with self._lock:
            if (
                self._finals_pending
                or not self._source
                or current < self._ready_at
                or self._translation_requested_revision == self._revision
            ):
                return None
            self._translation_requested_revision = self._revision
            self._last_translation_started = current
            return DraftTranslationRequest(self._revision, self._source)

    def accept_translation(
        self, request: DraftTranslationRequest, translation: str
    ) -> DraftView | None:
        """Accept only a result that still belongs to the visible revision."""
        normalized = str(translation or "").strip()
        if not normalized:
            return None
        with self._lock:
            if (
                self._finals_pending
                or request.revision != self._revision
                or request.source != self._source
            ):
                return None
            self._translation = normalized
            return DraftView(self._source, normalized)
