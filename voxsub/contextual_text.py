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
    r"(?:嗯+|呃+|额+|啊+|(?:um+|uh+|erm+)\b)",
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
_LEADING_CJK_FILLER_RE = re.compile(r"^\s*嗯+(?=[\u3400-\u9fff])")
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
    "can", "could", "should", "would", "will", "must", "may", "might",
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
    # ASR punctuation is evidence, not proof: "because." or "我们需要。"
    # still requires a complement. Closing quotation marks do not hide a stop.
    tail = value.rstrip('。！？!?；;.… \t\r\n”’"）)]')
    if not tail or value.rstrip().endswith(("...", "…", ",", "，", ":", "：")):
        return True
    if _ends_with_connector(tail):
        return True
    if _TERMINAL_RE.search(value.rstrip('”’"）)]')):
        return False
    if source_lang.lower().startswith("zh") or _CJK_RE.search(value):
        return _looks_incomplete_zh(value)
    return _looks_incomplete_en(value)


def _ends_with_connector(text: str) -> bool:
    if _CJK_RE.search(text):
        # Single grammatical particles may also end complete statements;
        # only strong multi-character complements override a decoder period.
        return any(text.endswith(word) for word in _ZH_INCOMPLETE_SUFFIXES
                   if len(word) >= 2)
    words = re.findall(r"[A-Za-z']+", text.lower())
    return bool(words and words[-1] in _EN_INCOMPLETE_SUFFIXES)


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
    # A short subject/predicate is not intrinsically incomplete. Keep bare
    # auxiliaries and trailing prepositions waiting, without delaying every
    # four-word statement until the hard deadline.
    if any(word in {"is", "are", "was", "were", "has", "have"} for word in words[:-1]):
        return False
    return len(words) < 7


def _substitution_distance(left: str, right: str, limit: int) -> int:
    """Same-length correction only: no insertions/deletions or word reordering.

    Acoustic typos are substitutions here. Stop at the edit budget rather than
    constructing a quadratic edit-distance matrix for every sliding window.
    """
    distance = 0
    for char_left, char_right in zip(left, right):
        distance += char_left != char_right
        if distance > limit:
            break
    return distance


def _split_hotwords(value: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = re.split(r"[,，;；\n]", value)
    else:
        parts = list(value)
    return tuple(dict.fromkeys(
        normalized for part in parts
        if (normalized := _normalize_text(str(part))) and 3 <= len(normalized) <= 64
    ))[:64]


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
    for run in _CJK_RUN_RE.findall(text[-512:]):
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
            if self._defer_incomplete and len(self._pending_text) < 600 and looks_incomplete(
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
        # Corrections must not become their own evidence on later sentences.
        self._remember(segment.raw_text)
        self._pending_text = ""
        self._pending_raw = ""
        self._pending_corrections = []
        self._pending_fillers = 0
        self._deadline = None
        return segment

    def _remember(self, text: str) -> None:
        if len(self._history) == self._history.maxlen:
            self._rebuild_term_counts(tuple(self._history)[1:])
        self._history.append(text[-512:])
        for term in _extract_context_ngrams(text):
            self._term_counts[term] += 1

    def _rebuild_term_counts(self, history: Iterable[str]) -> None:
        self._term_counts.clear()
        for item in history:
            for term in _extract_context_ngrams(item):
                self._term_counts[term] += 1

    def _correct(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        if not self._correction_enabled or len(text) > 1024:
            return text, []
        terms = [(term, 2) for term in self._hotwords]
        terms.extend(
            (term, 1) for term, count in sorted(
                self._term_counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))[:64]
            if count >= 2
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
    candidates = tuple(terms)
    protected = [(match.start(), match.end()) for canonical, _ in candidates
                 for match in re.finditer(re.escape(canonical), text)]
    options: list[tuple[tuple[float, int], int, int, str]] = []
    for canonical, max_distance in candidates:
        match = _term_match(text, canonical, max_distance)
        if match is None:
            continue
        start, end, distance = match
        if any(start < right and end > left for left, right in protected):
            continue
        if _meaning_signature(text[start:end]) != _meaning_signature(canonical):
            continue
        score = (distance / max(1, len(canonical)), -len(canonical))
        options.append((score, start, end, canonical))
    if not options:
        return None
    best = min(options)
    if any(option[0] == best[0] and option[3] != best[3]
           and option[1] < best[2] and option[2] > best[1] for option in options):
        return None  # equally plausible terms: retain the acoustic evidence
    return best[1], best[2], best[3]


def _meaning_signature(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Reject numeric/version edits and changes to polarity/action markers."""
    numbers = tuple(re.findall(r"\d+(?:[._-]\d+)*|[零〇一二三四五六七八九十百千万亿两]+", text))
    polarity = tuple(re.findall(
        r"不|没|无|未|非|禁|否|拒|勿|唔|停|减|增|开|关|启|闭|"
        r"\b(?:not|no|none|never|without|cannot|off|on|disable[ds]?|enable[ds]?|stop(?:ped)?|start(?:ed)?)\b", text.lower()))
    return numbers, polarity


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
        distance = _substitution_distance(candidate, canonical, max_distance)
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
        distance = _substitution_distance(candidate.lower(), canonical.lower(), max_distance)
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
