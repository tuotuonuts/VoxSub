from __future__ import annotations

import pytest

from voxsub.language_guard import (
    detect_text_language,
    guard_text,
    normalize_language,
    text_matches_language,
)


def test_language_aliases_are_normalized() -> None:
    assert normalize_language("zh-CN") == "zh"
    assert normalize_language("en_US") == "en"
    assert normalize_language("Hindi") == "auto"


def test_chinese_gate_allows_chinese_with_latin_name() -> None:
    assert text_matches_language("请打开 Teams meeting", "zh")
    assert guard_text("  请打开 Teams meeting  ", "zh") == "请打开 Teams meeting"


def test_chinese_gate_rejects_english_and_devanagari() -> None:
    assert not text_matches_language("This is an English sentence", "zh")
    assert not text_matches_language("यह एक वाक्य है", "zh")
    with pytest.raises(ValueError, match="language mismatch"):
        guard_text("यह एक वाक्य है", "zh", kind="STT")


def test_english_gate_rejects_cjk_and_devanagari() -> None:
    assert text_matches_language("This is an English sentence", "en")
    assert not text_matches_language("这是一句话", "en")
    assert not text_matches_language("यह एक वाक्य है", "en")


def test_punctuation_only_text_is_not_a_language_signal() -> None:
    assert not text_matches_language("...", "en")
    assert text_matches_language("...", "en", require_signal=False)


def test_auto_language_detection_is_conservative() -> None:
    assert detect_text_language("这是中文") == "zh"
    assert detect_text_language("This is English") == "en"
    assert detect_text_language("...") == "auto"
