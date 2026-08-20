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
    }
    value = aliases.get(value, value)
    return value if value in {*LANGUAGE_NAMES, "auto"} else "auto"


def language_name(language: str) -> str:
    """Return a prompt-friendly English language name."""
    return LANGUAGE_NAMES.get(normalize_language(language), "the selected language")


def _script_counts(text: str) -> dict[str, int]:
    counts = {"cjk": 0, "latin": 0, "other": 0}
    for char in text:
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if "CJK UNIFIED IDEOGRAPH" in name:
            counts["cjk"] += 1
        elif "LATIN" in name:
            counts["latin"] += 1
        else:
            counts["other"] += 1
    return counts


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
    letters = sum(counts.values())
    if letters == 0:
        return not require_signal

    if language == "zh":
        # Latin words are common in Chinese product names, but a Chinese
        # sentence must still contain CJK and may not contain another script.
        if counts["cjk"] == 0 or counts["other"] > 0:
            return False
        return counts["latin"] <= max(12, counts["cjk"] * 2)
    if language == "en":
        # For English, any CJK or non-Latin alphabetic script is a strong sign
        # that a multilingual decoder selected the wrong language.
        return counts["latin"] > 0 and counts["cjk"] == 0 and counts["other"] == 0
    return True


def guard_text(text: str, language: str, *, kind: str = "text") -> str:
    """Return text when it matches ``language`` or raise a clear error."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned or text_matches_language(cleaned, language):
        return cleaned
    raise ValueError(f"{kind} language mismatch: expected {language_name(language)}")


__all__ = [
    "LANGUAGE_NAMES",
    "guard_text",
    "language_name",
    "normalize_language",
    "text_matches_language",
]
