"""Thread-safe state for one replaceable live subtitle draft.

Recognition can revise an interim hypothesis while translation is slower and
finishes out of order.  This module keeps that concurrency policy independent
from the audio pipeline and the Qt presentation layer.
"""
from __future__ import annotations

import re
from os.path import commonprefix
import threading
import time
from dataclasses import dataclass
from typing import Callable


def _source_progresses(older: str, newer: str) -> bool:
    """Whether ``newer`` is a normal append or last-token partial revision."""
    old = " ".join(str(older or "").casefold().split())
    new = " ".join(str(newer or "").casefold().split())
    if not old or not new:
        return False
    if new.startswith(old):
        return True
    old_words = re.findall(r"[\w']+", old, flags=re.UNICODE)
    new_words = re.findall(r"[\w']+", new, flags=re.UNICODE)
    if not old_words or len(new_words) < len(old_words):
        return False
    common = 0
    for old_word, new_word in zip(old_words, new_words):
        if old_word != new_word:
            break
        common += 1
    return common >= max(1, len(old_words) - 1)


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
    sentence_id: int | None = None


class LiveDraftState:
    """Coordinate interim recognition, stale translation and final commits."""

    def __init__(
        self,
        *,
        debounce_seconds: float = 0.18,
        min_interval_seconds: float = 0.45,
        clock: Callable[[], float] = time.monotonic,
        stable_updates: bool = False,
    ) -> None:
        self._debounce = max(0.0, float(debounce_seconds))
        self._min_interval = max(0.0, float(min_interval_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._revision = 0
        self._sentence_id = 0
        self._stable_updates = stable_updates
        self._updated_at = 0.0
        self._requested_source = ""
        self._stable_prefix = ""
        self._source = ""
        self._translation = ""
        self._translation_source = ""
        self._translation_revision = -1
        self._ready_at = 0.0
        self._last_translation_started = float("-inf")
        self._translation_requested_revision = -1
        self._finals_pending = 0

    def reset(self) -> None:
        with self._lock:
            self._revision += 1
            self._sentence_id += 1
            self._requested_source = ""
            self._stable_prefix = ""
            self._source = ""
            self._translation = ""
            self._translation_source = ""
            self._translation_revision = -1
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
            self._updated_at = now
            previous_revision = self._revision
            request_caught_up = (
                self._translation_requested_revision == previous_revision)
            self._stable_prefix = commonprefix([self._source, normalized])
            self._revision += 1
            self._source = normalized
            if (self._translation and not _source_progresses(
                    self._translation_source, normalized)):
                self._translation = ""
                self._translation_source = ""
                self._translation_revision = -1
            # Start a trailing request after the first change, but do not push
            # its due time forward for every 140 ms recognizer update. This is
            # throttle/coalescing rather than an indefinitely resetting debounce.
            if self._ready_at <= 0.0 or request_caught_up:
                self._ready_at = max(
                    now + self._debounce,
                    self._last_translation_started + self._min_interval,
                )
            return DraftView(normalized, self._translation)

    def begin_final(self) -> None:
        """Block a following draft until every earlier final is presented."""
        with self._lock:
            self._finals_pending += 1
            # The visible draft belongs to the sentence now entering the final
            # queue.  A later partial will create a fresh revision while final
            # translation runs.
            self._revision += 1
            self._sentence_id += 1
            self._requested_source = ""
            self._stable_prefix = ""
            self._source = ""
            self._translation = ""
            self._translation_source = ""
            self._translation_revision = -1
            self._ready_at = 0.0
            self._translation_requested_revision = -1

    def finish_final(self) -> DraftView | None:
        """Finish one commit and restore any following draft it temporarily covered."""
        with self._lock:
            self._finals_pending = max(0, self._finals_pending - 1)
            if not self._source:
                return None
            return DraftView(self._source, self._translation)

    def take_translation_request(
        self, *, now: float | None = None, min_interval_seconds: float = 0.0
    ) -> DraftTranslationRequest | None:
        """Return the newest due revision at most once."""
        current = self._clock() if now is None else float(now)
        with self._lock:
            if (
                self._finals_pending
                or not self._source
                or current < max(self._ready_at, self._last_translation_started + min_interval_seconds)
                or self._translation_requested_revision == self._revision
            ):
                return None
            if self._stable_updates and not self._meaningful_update(current):
                return None
            self._requested_source = self._source
            self._translation_requested_revision = self._revision
            self._last_translation_started = current
            self._ready_at = 0.0
            return DraftTranslationRequest(self._revision, self._source, self._sentence_id)

    @property
    def sentence_id(self) -> int:
        with self._lock:
            return self._sentence_id

    def _meaningful_update(self, current: float) -> bool:
        # A brief quiet period always releases the latest hypothesis. Continued
        # speech can also release substantial growth without waiting forever.
        if current - self._updated_at >= 0.30:
            return True
        if not self._requested_source:
            return len(self._stable_prefix.strip()) >= 4
        if not _source_progresses(self._requested_source, self._source):
            return True  # genuine corrections must not be frozen
        growth = self._source[len(self._requested_source):].strip()
        return len(growth) >= 4 or len(growth.split()) >= 2

    def accept_translation(
        self, request: DraftTranslationRequest, translation: str
    ) -> DraftView | None:
        """Accept an exact result or a safe, visibly lagging prefix result."""
        normalized = str(translation or "").strip()
        if not normalized:
            return None
        with self._lock:
            if (
                self._finals_pending
                or (request.sentence_id is not None and request.sentence_id != self._sentence_id)
                or request.revision < self._translation_revision
            ):
                return None
            exact = (
                request.revision == self._revision
                and request.source == self._source
            )
            if not exact and not _source_progresses(request.source, self._source):
                return None
            self._translation = normalized
            self._translation_source = request.source
            self._translation_revision = request.revision
            return DraftView(self._source, normalized)
