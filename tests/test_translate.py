"""翻译层纯逻辑单测 (cache/prefetch/factory)。真实模型翻译由 Integration 测试覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from voxsub.translate.cache import TranslationCache, normalize_key
from voxsub.translate.prefetch import PrefetchEngine


# ---------- cache ----------

def test_cache_get_miss_put_hit() -> None:
    c = TranslationCache()
    assert c.get("你好", "zh", "en") is None
    c.put("你好", "zh", "en", "Hello")
    assert c.get("你好", "zh", "en") == "Hello"


def test_cache_normalize_whitespace_and_case() -> None:
    c = TranslationCache()
    c.put("  你好  世界 ", "zh", "en", "Hello world")
    # 不同空白/大小写应命中同一键
    assert c.get("你好 世界", "zh", "en") == "Hello world"
    assert normalize_key("  Hello   World  ") == normalize_key("hello world")


def test_cache_evict_lru() -> None:
    c = TranslationCache(max_items=2)
    c.put("a", "zh", "en", "A")
    c.put("b", "zh", "en", "B")
    c.get("a", "zh", "en")  # 标记 a 最近使用
    c.put("c", "zh", "en", "C")
    assert c.get("a", "zh", "en") == "A"  # a 仍命中(LRU 提升)
    assert c.get("b", "zh", "en") is None  # b 被淘汰
    assert c.size() == 2


def test_cache_ignore_empty() -> None:
    c = TranslationCache()
    c.put("", "zh", "en", "x")
    c.put("x", "zh", "en", "")
    assert c.size() == 0


# ---------- prefetch ----------

def test_prefetch_translate_now_returns() -> None:
    """translate_now 绕过防抖直接返回译文, 不触发 on_final 回调。"""
    calls: list[str] = []
    pf = PrefetchEngine(lambda t, s, d: f"T({t})",
                        debounce_ms=0, on_final=lambda s, d: calls.append(d))
    got = pf.translate_now("你好", "zh", "en")
    assert got == "T(你好)"
    assert calls == []  # translate_now 直接返回, 不回调

    # 但通过 on_final 走回调时, on_final 会收到译文
    pf2 = PrefetchEngine(lambda t, s, d: f"T({t})",
                         debounce_ms=0, on_final=lambda s, d: calls.append(d))
    pf2.on_final("你好", "zh", "en")
    assert calls == ["T(你好)"]


def test_prefetch_on_final_bypass_debounce_when_idle() -> None:
    """隔离场景: 无进行中输入时 on_final 立即翻译。"""
    calls: list[tuple[str, str]] = []
    pf = PrefetchEngine(lambda t, s, d: f"R:{t}",
                        debounce_ms=1000, on_final=lambda s, d: calls.append((s, d)))
    pf.on_final("hello", "zh", "en")
    assert calls == [("hello", "R:hello")]


def test_prefetch_on_partial_then_immediate_final_suppressed() -> None:
    """部分文本刚到达后立刻 on_final: 距上次输入 < debounce → 不发终稿。"""
    calls: list[tuple[str, str]] = []
    import time
    pf = PrefetchEngine(lambda t, s, d: f"R:{t}",
                        debounce_ms=10_000, on_final=lambda s, d: calls.append((s, d)))
    pf.on_partial("你")
    pf.on_final("你好")
    assert calls == []


# ---------- factory ----------

def test_factory_list_available_shape() -> None:
    from voxsub.translate.factory import TranslatorFactory
    avail = TranslatorFactory.list_available()
    assert set(avail) == {"opus-fast", "qwen-quality", "cloud"}
    assert isinstance(avail["opus-fast"], bool)


def test_factory_unknown_kind_raises() -> None:
    from voxsub.translate.factory import TranslatorFactory
    with pytest.raises(ValueError):
        TranslatorFactory.create("nope")