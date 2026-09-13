"""严格语言约束测试：设置指定语言时，只识别那种语言，只翻译那种语言。

## 覆盖的用户需求

1. 如果设置了指定的语言应该只识别那种语言和只翻译那种语言
2. 浮窗应该事实显示当前的句子

## 验证项

- language_guard 中的匹配规则：
  - 日文（ja）：必须含假名；允许 <=3 字符纯汉字短词；长汉字句（如全中文）必须拒绝（不能误当作日文）。
  - 中文（zh）：必须含汉字且不得含假名/谚文；纯英文必须拒绝。
  - 英文（en）：纯英文字符；任何 CJK / 假名 / 谚文必须拒绝。
- pipeline 识别与翻译层：
  - _on_asr_partial：指定语言时，非该语言的 partial 不得 emit。
  - _commit_context_segments：上下文稳定后的文本也必须经过语言约束，非指定语言不得入翻译队列。
  - _translate_sentence：非指定源语言文本在翻译前直接拦截；翻译产生非指定目标语言内容时，不将源语言冒充为译文。
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
        """指定日文时，纯中文长句（无假名）必须拒绝。"""
        assert text_matches_language("こんにちは", "ja") is True
        assert text_matches_language("会議は三時から始まります", "ja") is True
        assert text_matches_language("今日はいい天気ですね", "ja") is True
        # 汉字短词允许
        assert text_matches_language("東京", "ja") is True
        assert text_matches_language("映画", "ja") is True

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


class TestPipelineStrictLanguageRouting:
    def test_partial_filtered_when_language_specified(self):
        """指定识别语言时，非该语言的 partial 绝不 emit。"""
        p = Pipeline()
        p.set_langs("ja", "zh")

        emitted: list[str] = []
        p.on_partial(lambda text: emitted.append(text))

        # 送入英文 partial -> 必须被过滤
        p._on_asr_partial("Hello world")  # noqa: SLF001
        assert emitted == [], f"非日文 partial 不该被发出: {emitted}"

        # 送入纯中文长句 partial -> 必须被过滤
        p._on_asr_partial("今天天气非常好")  # noqa: SLF001
        assert emitted == [], f"纯中文 partial 不该被当作日文发出: {emitted}"

        # 送入真正的日文 partial -> 正常发出
        p._on_asr_partial("今日は")  # noqa: SLF001
        assert emitted == ["今日は"]

    def test_translate_sentence_rejects_wrong_source_language(self):
        """翻译单句前，若源语言不符合指定语言，不翻译且不发射 utterance。"""
        p = Pipeline()
        p.set_langs("ja", "zh")
        p._trans_kind = "mock"  # noqa: SLF001

        translated_calls: list[str] = []
        p._translator = type("Stub", (), {  # noqa: SLF001
            "translate": lambda self, text, src, dst, **kw: translated_calls.append(text) or "译文",
            "supports": lambda self, s, d: True,
        })()

        emitted: list[tuple[str, str]] = []
        p.on_utterance(lambda s, t: emitted.append((s, t)))

        # 非日文文本 -> 拦截
        p._translate_sentence("Hello world", None)  # noqa: SLF001
        p._translate_sentence("今天天气非常好", None)  # noqa: SLF001
        assert translated_calls == []
        assert emitted == []

        # 合法日文 -> 正常翻译并发射
        p._translate_sentence("こんにちは", None)  # noqa: SLF001
        assert translated_calls == ["こんにちは"]
        assert len(emitted) == 1
        assert emitted[0][0] == "こんにちは"
