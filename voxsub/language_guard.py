"""Language constraints shared by the speech and translation pipelines.

The ASR runtimes used by VoxSub are not all able to force a language at decode
time. This module therefore provides a small dependency-free safety gate for
the final text. It is intentionally conservative for different writing
systems (for example Devanagari or Japanese kana), while allowing punctuation,
numbers, and a modest amount of Latin text in Chinese proper names.

This is not a full language detector: script checks cannot distinguish English
from Spanish or German. Models still receive an explicit language hint where
their runtime supports one, and this gate prevents the most damaging
cross-script hallucinations from reaching subtitles or translation.
"""
from __future__ import annotations

import unicodedata


LANGUAGE_NAMES: dict[str, str] = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    # ``auto`` is used by N→1 translation: the recognizer/OCR may emit more
    # than one source language while the target remains fixed.
    "auto": "the detected source language",
}


def normalize_language(value: object) -> str:
    """Return a supported short language code, or ``auto``."""
    value = str(value or "auto").strip().lower().replace("_", "-")
    aliases = {
        "cn": "zh",
        "zh-cn": "zh",
        "zh-hans": "zh",
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
    return value if value in {*LANGUAGE_NAMES, "auto"} else "auto"


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
    """日文：假名是可靠信号；纯汉字短标签与中文在字面上无法区分，也接受。"""
    if counts["hangul"] or counts["other"]:
        return False
    return bool(counts["kana"] or counts["cjk"])


def _matches_ko(counts: dict[str, int]) -> bool:
    """韩文：必须含谚文，且不得混入其他字母表。"""
    return counts["hangul"] > 0 and not counts["other"]


def _matches_en(counts: dict[str, int]) -> bool:
    """英文：出现任何 CJK/假名/谚文/其他字母表都视为解码选错了语言。"""
    if counts["latin"] == 0:
        return False
    return not (counts["cjk"] or counts["kana"]
                or counts["hangul"] or counts["other"])


# 语种 → 判定函数（查表取代 if 阶梯，新增语种只加一行）
_LANGUAGE_MATCHERS = {
    "zh": _matches_zh,
    "ja": _matches_ja,
    "ko": _matches_ko,
    "en": _matches_en,
}


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
    return matcher(counts) if matcher else True


def guard_text(text: str, language: str, *, kind: str = "text") -> str:
    """Return text when it matches ``language`` or raise a clear error."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned or text_matches_language(cleaned, language):
        return cleaned
    raise ValueError(f"{kind} language mismatch: expected {language_name(language)}")


__all__ = [
    "LANGUAGE_NAMES",
    "guard_text",
    "detect_text_language",
    "language_name",
    "normalize_language",
    "text_matches_language",
]
