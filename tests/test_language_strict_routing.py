"""Source-language hints must not discard content; target-script validation remains.

The old tests required foreign/ambiguous source text to disappear. That product
policy was explicitly replaced: preserve partials/finals and attempt translation,
while retaining the existing destination-language failure fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voxsub.language_guard import text_matches_language  # noqa: E402
from voxsub.pipeline import Pipeline  # noqa: E402


class TestLanguageMatchersStrict:
    def test_japanese_strictly_rejects_full_chinese_sentence(self):
        """指定日文时，纯中文或纯汉字短词必须拒绝。"""
        assert text_matches_language("こんにちは", "ja") is True
        assert text_matches_language("会議は三時から始まります", "ja") is True
        assert text_matches_language("今日はいい天気ですね", "ja") is True
        # 纯汉字无法证明是日文，严格拒绝，避免中文误入日文链路
        assert text_matches_language("東京", "ja") is False
        assert text_matches_language("映画", "ja") is False

        # 超过 3 字且全无假名的中文句子必须拒绝
        assert text_matches_language("今天天气很好", "ja") is False
        assert text_matches_language("会议从三点开始", "ja") is False
        assert text_matches_language("这是一个纯中文测试句子没有任何日文假名", "ja") is False

        # 纯英文必须拒绝
        assert text_matches_language("Hello world", "ja") is False
        assert text_matches_language("THAT YOU MIGHT SAY", "ja") is False

    def test_chinese_strictly_rejects_foreign(self):
        """指定中文时，纯英文或包含假名/韩文的句子必须拒绝。"""
        assert text_matches_language("今天天气很好", "zh") is True
        assert text_matches_language("会议从三点开始", "zh") is True
        assert text_matches_language("这是中文句子带 Teams 英文词", "zh") is True

        assert text_matches_language("Hello world", "zh") is False
        assert text_matches_language("こんにちは", "zh") is False
        assert text_matches_language("안녕하세요", "zh") is False

    def test_english_strictly_rejects_cjk(self):
        """指定英文时，任何 CJK/假名/谚文必须拒绝。"""
        assert text_matches_language("Hello world, this is English", "en") is True
        assert text_matches_language("Hello 世界", "en") is False
        assert text_matches_language("こんにちは", "en") is False


class TestPipelineLanguageRouting:
    def test_partial_is_preserved_when_source_is_uncertain(self):
        p = Pipeline()
        p.set_langs("ja", "zh")
        emitted = []
        p.on_partial(emitted.append)
        for text in ["Hello world", "今天天气非常好", "今日は"]:
            p._on_asr_partial(text)
        assert emitted == ["Hello world", "今天天气非常好", "今日は"]

    def test_translate_sentence_preserves_uncertain_source(self):
        p = Pipeline()
        p.set_langs("ja", "zh")
        p._trans_kind = "mock"
        translated_calls = []
        p._translator = type("Stub", (), {
            "translate": lambda self, text, src, dst, **kw: translated_calls.append(text) or "译文",
            "supports": lambda self, s, d: True,
        })()
        emitted = []
        p.on_utterance(lambda s, t: emitted.append((s, t)))
        sources = ["Hello world", "今天天气非常好", "こんにちは"]
        for source in sources:
            p._translate_sentence(source, None)
        assert translated_calls == sources
        assert emitted == [(source, "译文") for source in sources]
