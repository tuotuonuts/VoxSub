"""Bounded, session-local latency accounting and exact draft reuse.

Snapshots contain numbers only. Cached bodies never leave memory. Scheduling
budgets are not a claim of model throughput, device execution or audio timing.
"""
from __future__ import annotations
from collections import Counter, deque
import math
import threading
import time

STAGES = frozenset({"capture_wait", "recognition_feed", "recognition_wait", "recognition_decode", "context_wait", "translation_wait", "draft_inference", "final_inference", "final_ready", "callback"})


class RealtimeLatency:
    def __init__(self, *, window: int = 256):
        self._window = max(1, min(1024, window))
        self._lock = threading.RLock()
        self.reset()

    def reset(self):
        with self._lock:
            self._samples = {stage: deque(maxlen=self._window) for stage in STAGES}
            self._counts = Counter()
            self._totals = Counter()
            self._recent_drafts = deque(maxlen=8)
            self._slow_finals = 0
            self._backlogged = False

    def observe(self, stage: str, milliseconds: float):
        value = float(milliseconds)
        if stage not in STAGES or not math.isfinite(value) or value < 0:
            return
        with self._lock:
            self._samples[stage].append(value)
            self._counts[stage] += 1
            self._totals[stage] += value
            if stage == "draft_inference":
                self._recent_drafts.append(value)
            if stage == "translation_wait":
                self._slow_finals = self._slow_finals + 1 if value >= 1500 else 0
                self._backlogged = self._slow_finals >= 2

    @property
    def backlogged(self):
        with self._lock:
            return self._backlogged

    @property
    def draft_interval_seconds(self):
        with self._lock:
            cost = max(self._recent_drafts, default=0) / 1000
        return min(2.0, max(0.45, cost + 0.15))

    def snapshot(self):
        with self._lock:
            rows = {stage: self._summary(stage, values) for stage, values in self._samples.items()}
            return {"stages": rows, "backlogged": self._backlogged,
                    "draft_interval_ms": round(self.draft_interval_seconds * 1000, 2),
                    "window_capacity": self._window,
                    "boundary": "Recent bounded stage timings; not acoustic endpoint, device execution, renderer paint or translation quality verification"}

    def _summary(self, stage, values):
        ordered = sorted(values)
        return {"count": self._counts[stage], "sample_count": len(ordered),
                "total_ms": round(self._totals[stage], 2),
                "p50_ms": self._percentile(ordered, 0.50),
                "p95_ms": self._percentile(ordered, 0.95),
                "max_ms": round(ordered[-1], 2) if ordered else None}

    @staticmethod
    def _percentile(ordered, quantile):
        if not ordered:
            return None
        return round(ordered[max(0, math.ceil(len(ordered) * quantile) - 1)], 2)


class ExactDraftCache:
    """One exact, validated result; scoped to sentence/config/model/context.

    No fuzzy text keys: a punctuation change, contextual correction, new
    sentence or model replacement requires fresh inference.
    """
    def __init__(self, *, clock=time.monotonic, ttl_seconds: float = 3.0):
        self._clock = clock
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self.clear()

    def clear(self):
        with self._lock:
            self._key = None
            self._translation = ""
            self._expires = 0.0

    def remember(self, key: tuple, translation: str):
        if not translation:
            return
        with self._lock:
            self._key = key
            self._translation = translation
            self._expires = self._clock() + self._ttl

    def take(self, key: tuple) -> str | None:
        with self._lock:
            exact = self._key == key and self._clock() < self._expires
            value = self._translation if exact else None
            self._key = None
            self._translation = ""
            self._expires = 0.0
            return value


class DraftSkipped(RuntimeError):
    """Optional preview unavailable; final inference must remain intact."""


def caused_by_timeout(error: BaseException) -> bool:
    for _ in range(5):
        if isinstance(error, TimeoutError):
            return True
        cause = error.__cause__
        if cause is None or cause is error:
            return False
        error = cause
    return False
