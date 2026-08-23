"""Local, conservative context processing for real-time transcripts.

The processor deliberately does not invent missing words.  It can delay an
incomplete acoustic fragment, merge it with the next fragment, remove isolated
fillers, and correct a small edit only when a canonical term is supplied as a
hotword or has been established repeatedly in recent committed context.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import re
import threading
import time
from typing import Callable, Iterable


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{4,}")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,}")
_TERMINAL_RE = re.compile(r"[。！？!?；;.]\s*$")
_LIGHT_FILLER_RE = re.compile(
    r"(?:嗯+|呃+|额+|唔+|啊+|(?:um+|uh+|erm+)\b)",
    re.IGNORECASE,
)
_ISOLATED_FILLER_RE = re.compile(
    rf"^\s*{_LIGHT_FILLER_RE.pattern}\s*[，,、。.!！?？…]*\s*$",
    re.IGNORECASE,
)
_LEADING_FILLER_RE = re.compile(
    rf"^\s*{_LIGHT_FILLER_RE.pattern}(?:\s+|[，,、。.!！?？…]+\s*)",
    re.IGNORECASE,
)
_LEADING_CJK_FILLER_RE = re.compile(r"^\s*(?:嗯+|呃+|额+|唔+)(?=[\u3400-\u9fff])")
_MID_FILLER_RE = re.compile(
    rf"([，,、；;]\s*){_LIGHT_FILLER_RE.pattern}(?=\s|[，,、。.!！?？；;])",
    re.IGNORECASE,
)

_ZH_INCOMPLETE_SUFFIXES = (
    "因为", "所以", "但是", "不过", "而且", "以及", "或者", "如果",
    "虽然", "然后", "就是", "例如", "比如", "关于", "对于", "通过",
    "需要", "可以把", "我们要", "我们会", "我认为", "我觉得", "我想",
    "的", "地", "得", "把", "被", "在", "从", "向", "和", "与", "或",
)
_ZH_SUBORDINATE_PREFIXES = (
    "因为", "如果", "虽然", "只要", "既然", "当", "除非", "为了",
)
_ZH_ACKNOWLEDGEMENTS = frozenset({
    "好", "好的", "可以", "行", "没问题", "明白", "知道了", "谢谢",
    "对", "是的", "不是", "同意", "收到",
})
_EN_INCOMPLETE_SUFFIXES = frozenset({
    "and", "or", "but", "because", "if", "although", "when", "while",
    "to", "from", "with", "for", "of", "the", "a", "an", "that",
})
_EN_ACKNOWLEDGEMENTS = frozenset({
    "ok", "okay", "yes", "no", "thanks", "agreed", "understood",
})


@dataclass(frozen=True)
class ContextualSegment:
    """A committed source segment plus audit information for diagnostics."""

    text: str
    raw_text: str
    corrections: tuple[tuple[str, str], ...] = ()
    fillers_removed: int = 0


def _normalize_text(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return re.sub(r"(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])", "", value)


def format_partial_for_display(text: str, source_lang: str) -> str:
    """Make all-caps English decoder drafts readable without changing evidence.

    Some streaming transducer token tables expose interim English hypotheses in
    uppercase even though the authoritative final recognizer restores casing.
    This is a presentation-only transform: mixed/proper casing is preserved,
    and final transcripts continue through the original recognition path.
    """
    value = _normalize_text(text)
    if not str(source_lang or "").lower().startswith("en"):
        return value
    letters = [char for char in value if char.isascii() and char.isalpha()]
    if not letters or any(char.islower() for char in letters):
        return value
    lowered = value.lower()
    lowered = re.sub(r"\bi\b", "I", lowered)
    return re.sub(
        r"(^|[.!?]\s+)([\"'“‘(\[]*)([a-z])",
        lambda match: (
            match.group(1) + match.group(2) + match.group(3).upper()),
        lowered,
    )


def _join_fragments(left: str, right: str) -> str:
    left, right = left.rstrip(), right.lstrip()
    if not left:
        return right
    if not right:
        return left
    if left[-1].isascii() and right[0].isascii():
        return f"{left} {right}"
    return left + right


def _balanced(text: str) -> bool:
    return all(text.count(opening) == text.count(closing) for opening, closing in (
        ("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"),
        ("“", "”"), ("‘", "’"),
    ))


def looks_incomplete(text: str, source_lang: str = "zh") -> bool:
    """Return whether a transcript fragment should wait for more context."""
    value = _normalize_text(text)
    if not value or not _balanced(value):
        return True
    if _TERMINAL_RE.search(value):
        return False
    if source_lang.lower().startswith("zh") or _CJK_RE.search(value):
        return _looks_incomplete_zh(value)
    return _looks_incomplete_en(value)


def _looks_incomplete_zh(text: str) -> bool:
    compact = re.sub(r"[^\u3400-\u9fffA-Za-z0-9]", "", text)
    if compact in _ZH_ACKNOWLEDGEMENTS:
        return False
    if any(compact.endswith(suffix) for suffix in _ZH_INCOMPLETE_SUFFIXES):
        return True
    if any(compact.startswith(prefix) for prefix in _ZH_SUBORDINATE_PREFIXES):
        return not any(marker in compact[2:] for marker in ("所以", "就", "那么"))
    if len(compact) <= 5:
        return True
    if len(compact) >= 18:
        return False
    return not compact.endswith(("了", "吗", "吧", "呢", "好", "行", "完成", "结束"))


def _looks_incomplete_en(text: str) -> bool:
    words = re.findall(r"[A-Za-z']+", text.lower())
    if not words:
        return True
    if " ".join(words) in _EN_ACKNOWLEDGEMENTS:
        return False
    if words[-1] in _EN_INCOMPLETE_SUFFIXES:
        return True
    return len(words) < 7


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, char_left in enumerate(left, 1):
        current = [row]
        for column, char_right in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (char_left != char_right),
            ))
        previous = current
    return previous[-1]


def _split_hotwords(value: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = re.split(r"[,，;；\n]", value)
    else:
        parts = list(value)
    return tuple(dict.fromkeys(
        normalized for part in parts
        if (normalized := _normalize_text(str(part))) and len(normalized) >= 3
    ))


def _clean_fillers(text: str, mode: str) -> tuple[str, int]:
    if mode != "light":
        return text, 0
    if _ISOLATED_FILLER_RE.fullmatch(text):
        return "", 1
    cleaned, count = _LEADING_FILLER_RE.subn("", text, count=1)
    if not count:
        cleaned, count = _LEADING_CJK_FILLER_RE.subn("", text, count=1)
    cleaned, middle = _MID_FILLER_RE.subn(r"\1", cleaned)
    return cleaned.strip(), count + middle


def _extract_context_ngrams(text: str) -> set[str]:
    result: set[str] = set()
    for run in _CJK_RUN_RE.findall(text):
        for size in range(4, min(8, len(run)) + 1):
            result.update(run[index:index + size] for index in range(len(run) - size + 1))
    return result


class ContextualTextProcessor:
    """Thread-safe rolling semantic boundary and conservative cleanup stage."""

    def __init__(
        self,
        *,
        source_lang: str = "zh",
        hotwords: str | Iterable[str] = (),
        filler_mode: str = "light",
        correction_enabled: bool = True,
        hold_ms: int = 1800,
        defer_incomplete: bool = True,
        history_size: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source_lang = source_lang
        self._hotwords = _split_hotwords(hotwords)
        self._filler_mode = filler_mode if filler_mode in {"off", "light"} else "light"
        self._correction_enabled = bool(correction_enabled)
        self._hold_seconds = max(0.2, min(4.0, int(hold_ms) / 1000.0))
        self._defer_incomplete = bool(defer_incomplete)
        self._history: deque[str] = deque(maxlen=max(1, min(8, history_size)))
        self._term_counts: Counter[str] = Counter()
        self._pending_text = ""
        self._pending_raw = ""
        self._pending_corrections: list[tuple[str, str]] = []
        self._pending_fillers = 0
        self._deadline: float | None = None
        self._clock = clock
        self._lock = threading.RLock()

    @property
    def pending_text(self) -> str:
        with self._lock:
            return self._pending_text

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._term_counts.clear()
            self._pending_text = ""
            self._pending_raw = ""
            self._pending_corrections = []
            self._pending_fillers = 0
            self._deadline = None

    def should_defer_endpoint(self, text: str) -> bool:
        return looks_incomplete(text, self._source_lang)

    def preview(self, partial: str) -> str:
        with self._lock:
            normalized = format_partial_for_display(partial, self._source_lang)
            corrected, _changes = self._correct(normalized)
            cleaned, _fillers = _clean_fillers(corrected, self._filler_mode)
            return _join_fragments(self._pending_text, cleaned)

    def submit(self, text: str, *, now: float | None = None) -> list[ContextualSegment]:
        received_at = self._clock() if now is None else float(now)
        raw = _normalize_text(text)
        if not raw:
            return []
        with self._lock:
            committed = self._finalize_expired_locked(received_at)
            corrected, corrections = self._correct(raw)
            cleaned, fillers = _clean_fillers(corrected, self._filler_mode)
            if not cleaned:
                return committed
            self._pending_raw = _join_fragments(self._pending_raw, raw)
            self._pending_text = _join_fragments(self._pending_text, cleaned)
            self._pending_corrections.extend(corrections)
            self._pending_fillers += fillers
            if self._deadline is None:
                self._deadline = received_at + self._hold_seconds
            if self._defer_incomplete and looks_incomplete(
                    self._pending_text, self._source_lang):
                return committed
            committed.append(self._finalize_locked())
            return committed

    def poll(self, *, now: float | None = None) -> list[ContextualSegment]:
        current = self._clock() if now is None else float(now)
        with self._lock:
            if not self._pending_text or self._deadline is None or current < self._deadline:
                return []
            return [self._finalize_locked()]

    def flush(self) -> list[ContextualSegment]:
        with self._lock:
            return [self._finalize_locked()] if self._pending_text else []

    def _finalize_expired_locked(self, now: float) -> list[ContextualSegment]:
        if (not self._pending_text or self._deadline is None or
                now < self._deadline):
            return []
        return [self._finalize_locked()]

    def _finalize_locked(self) -> ContextualSegment:
        segment = ContextualSegment(
            self._pending_text,
            self._pending_raw,
            tuple(self._pending_corrections),
            self._pending_fillers,
        )
        self._remember(segment.text)
        self._pending_text = ""
        self._pending_raw = ""
        self._pending_corrections = []
        self._pending_fillers = 0
        self._deadline = None
        return segment

    def _remember(self, text: str) -> None:
        if len(self._history) == self._history.maxlen:
            self._rebuild_term_counts(tuple(self._history)[1:])
        self._history.append(text)
        for term in _extract_context_ngrams(text):
            self._term_counts[term] += 1

    def _rebuild_term_counts(self, history: Iterable[str]) -> None:
        self._term_counts.clear()
        for item in history:
            for term in _extract_context_ngrams(item):
                self._term_counts[term] += 1

    def _correct(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        if not self._correction_enabled:
            return text, []
        terms = [(term, 2) for term in self._hotwords]
        terms.extend(
            (term, 1) for term, count in self._term_counts.items() if count >= 2
        )
        corrected = text
        changes: list[tuple[str, str]] = []
        for _ in range(2):
            match = _best_correction(corrected, terms)
            if match is None:
                break
            start, end, canonical = match
            original = corrected[start:end]
            corrected = corrected[:start] + canonical + corrected[end:]
            changes.append((original, canonical))
        return corrected, changes


def _best_correction(
    text: str,
    terms: Iterable[tuple[str, int]],
) -> tuple[int, int, str] | None:
    best: tuple[tuple[float, int], int, int, str] | None = None
    for canonical, max_distance in terms:
        match = _term_match(text, canonical, max_distance)
        if match is None:
            continue
        start, end, distance = match
        score = (distance / max(1, len(canonical)), -len(canonical))
        if best is None or score < best[0]:
            best = (score, start, end, canonical)
    return None if best is None else (best[1], best[2], best[3])


def _term_match(text: str, canonical: str, max_distance: int) -> tuple[int, int, int] | None:
    if canonical in text:
        return None
    if _CJK_RUN_RE.fullmatch(canonical):
        return _cjk_term_match(text, canonical, max_distance)
    if _LATIN_TOKEN_RE.fullmatch(canonical):
        return _latin_term_match(text, canonical, min(1, max_distance))
    return None


def _cjk_term_match(text: str, canonical: str, max_distance: int) -> tuple[int, int, int] | None:
    length = len(canonical)
    best: tuple[int, int, int] | None = None
    for start in range(0, len(text) - length + 1):
        candidate = text[start:start + length]
        if not _CJK_RUN_RE.fullmatch(candidate):
            continue
        distance = _levenshtein(candidate, canonical)
        if 0 < distance <= max_distance and (
                best is None or distance < best[2]):
            best = (start, start + length, distance)
    return best


def _latin_term_match(text: str, canonical: str, max_distance: int) -> tuple[int, int, int] | None:
    best: tuple[int, int, int] | None = None
    for match in _LATIN_TOKEN_RE.finditer(text):
        candidate = match.group(0)
        if len(candidate) != len(canonical):
            continue
        distance = _levenshtein(candidate.lower(), canonical.lower())
        if 0 < distance <= max_distance and (
                best is None or distance < best[2]):
            best = (match.start(), match.end(), distance)
    return best


__all__ = [
    "ContextualSegment",
    "ContextualTextProcessor",
    "format_partial_for_display",
    "looks_incomplete",
]
