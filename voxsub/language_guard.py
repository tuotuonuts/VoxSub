"""Script plausibility for diagnostics and target validation, not source filtering.

Source language is a decoder instruction. A character/word heuristic cannot
prove a spoken language and must never decide whether recognized information
is retained. Latin script cannot distinguish English from Swedish or Spanish.
"""
from __future__ import annotations

import unicodedata


from voxsub.language_registry import LANGUAGE_NAMES


def normalize_language(value: object, *, strict: bool = False) -> str:
    """Return a supported short language code.

    UI/config parsing may keep the historical permissive ``auto`` fallback, but
    pipeline entry points use ``strict=True`` so a typo cannot silently change
    the requested decoding language.
    """
    original = str(value or "auto").strip().lower().replace("_", "-")
    value = original
    aliases = {
        "cn": "zh",
        "zh-cn": "zh",
        "zh-hans": "zh",
        "zh-tw": "zh-hant", "zh-hk": "zh-hant", "tl": "fil",
        "eng": "en",
        "en-us": "en",
        "en-gb": "en",
        "jp": "ja",
        "jpn": "ja",
        "ja-jp": "ja",
        "kr": "ko",
        "kor": "ko",
        "ko-kr": "ko",
    }
    value = aliases.get(value, value)
    if value not in {*LANGUAGE_NAMES, "auto"}:
        if strict:
            raise ValueError(f"unsupported language: {original}")
        return "auto"
    return value


def language_name(language: str) -> str:
    """Return a prompt-friendly English language name."""
    return LANGUAGE_NAMES.get(normalize_language(language), "the selected language")


def _script_counts(text: str) -> dict[str, int]:
    """Count writing-system signals used by the language safety gate.

    Japanese commonly mixes kanji with hiragana/katakana, so kana must be
    tracked separately from the shared CJK ideographs.  Korean likewise needs
    a Hangul signal because Korean text may contain Latin product names.
    """
    counts = {"cjk": 0, "kana": 0, "hangul": 0, "latin": 0, "other": 0}
    for char in text:
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if "CJK UNIFIED IDEOGRAPH" in name:
            counts["cjk"] += 1
        elif "HIRAGANA" in name or "KATAKANA" in name:
            counts["kana"] += 1
        elif "HANGUL" in name:
            counts["hangul"] += 1
        elif "LATIN" in name:
            counts["latin"] += 1
        else:
            counts["other"] += 1
    return counts


def detect_text_language(text: str) -> str:
    """Best-effort detection for scripts supported by the fast translator.

    This is a script detector, not a full statistical language detector.
    Kana and Hangul are strong signals for Japanese and Korean respectively;
    otherwise CJK and Latin text are classified as Chinese and English.
    """
    counts = _script_counts(str(text or ""))
    if counts["hangul"]:
        return "ko"
    if counts["kana"]:
        return "ja"
    if counts["cjk"] and counts["cjk"] >= counts["latin"]:
        return "zh"
    if counts["latin"] and counts["other"] == 0:
        return "en"
    return "auto"


def _matches_zh(counts: dict[str, int]) -> bool:
    """中文：必须有 CJK，且不得混入日文假名/韩文/其他字母表。

    Latin words are common in Chinese product names, but a Chinese sentence
    must still contain CJK and may not contain Japanese/Korean script.
    """
    if counts["cjk"] == 0 or counts["kana"] or counts["hangul"] or counts["other"]:
        return False
    return counts["latin"] <= max(12, counts["cjk"] * 2)


def _matches_ja(counts: dict[str, int]) -> bool:
    """Kana is a Japanese-script signal; kanji alone leaves the language uncertain."""
    if counts["hangul"] or counts["other"]:
        return False
    if counts["kana"] > 0:
        return counts["latin"] <= max(12, (counts["cjk"] + counts["kana"]) * 2)
    # No kana: insufficient target-script evidence, never grounds to discard source text.
    return False


def _matches_ko(counts: dict[str, int]) -> bool:
    """韩文：必须含谚文，且不得混入其他字母表。"""
    return counts["hangul"] > 0 and not counts["other"]


def _matches_en(counts: dict[str, int]) -> bool:
    """Latin text is plausible, not proven English; short words/names are valid."""
    return counts["latin"] > 0 and not any(
        counts[script] for script in ("cjk", "kana", "hangul", "other"))


def retain_source_text(text: object) -> str:
    """Normalize whitespace without deleting uncertain/mixed-language content."""
    return " ".join(str(text or "").split())


# 语种 → 判定函数（查表取代 if 阶梯，新增语种只加一行）
_LANGUAGE_MATCHERS = {
    "zh": _matches_zh,
    "ja": _matches_ja,
    "ko": _matches_ko,
    "en": _matches_en,
}


# Script plausibility only; never a statistical language identity claim.
_SCRIPT_NAMES = {
    "ru": "CYRILLIC", "uk": "CYRILLIC", "bg": "CYRILLIC", "mk": "CYRILLIC",
    "kk": "CYRILLIC", "mn": "CYRILLIC", "el": "GREEK",
    "ar": "ARABIC", "fa": "ARABIC", "ur": "ARABIC", "ug": "ARABIC",
    "hi": "DEVANAGARI", "mr": "DEVANAGARI", "th": "THAI",
    "km": "KHMER", "my": "MYANMAR", "gu": "GUJARATI", "te": "TELUGU",
    "he": "HEBREW", "bn": "BENGALI", "ta": "TAMIL", "bo": "TIBETAN",
}


def _matches_additional(text: str, language: str, counts: dict[str, int]) -> bool:
    if language in {"zh-hant", "yue"}:
        return _matches_zh(counts)
    script = _SCRIPT_NAMES.get(language)
    if script is None:
        return _matches_en(counts)
    return any(script in unicodedata.name(char, "") for char in text if char.isalpha())


def text_matches_language(text: str, language: str, *, require_signal: bool = True) -> bool:
    """Return whether text is plausibly written in ``language``.

    ``require_signal=False`` is useful for punctuation-only intermediate
    results. Unknown/auto language is always accepted.
    """
    language = normalize_language(language)
    text = str(text or "").strip()
    if language == "auto" or not text:
        return True
    counts = _script_counts(text)
    if sum(counts.values()) == 0:
        return not require_signal
    matcher = _LANGUAGE_MATCHERS.get(language)
    return matcher(counts) if matcher is not None else _matches_additional(text, language, counts)


def guard_text(text: str, language: str, *, kind: str = "text") -> str:
    """Return text when it matches ``language`` or raise a clear error."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned or text_matches_language(cleaned, language):
        return cleaned
    raise ValueError(f"{kind} language mismatch: expected {language_name(language)}")


__all__ = [
    "LANGUAGE_NAMES",
    "guard_text",
    "retain_source_text",
    "detect_text_language",
    "language_name",
    "normalize_language",
    "text_matches_language",
]
