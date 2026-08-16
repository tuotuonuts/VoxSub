"""翻译层纯逻辑单测 (cache/prefetch/factory)。真实模型翻译由 Integration 测试覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from voxsub.translate.cache import TranslationCache, normalize_text as normalize_key
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
    c = TranslationCache(max_size=2)
    c.put("a", "zh", "en", "A")
    c.put("b", "zh", "en", "B")
    c.get("a", "zh", "en")  # 标记 a 最近使用
    c.put("c", "zh", "en", "C")
    assert c.get("a", "zh", "en") == "A"  # a 仍命中(LRU 提升)
    assert c.get("b", "zh", "en") is None  # b 被淘汰
    assert len(c) == 2


def test_cache_ignore_empty_translation() -> None:
    """空译文不入缓存 (M4 契约: 仅忽略空 result, 不因空原文拒写)。"""
    c = TranslationCache()
    c.put("x", "zh", "en", "")      # 空译文 → 不入缓存
    assert len(c) == 0
    c.put("x", "zh", "en", "译文")   # 正常写入
    sec = TranslationCache(max_size=1)
    sec.put("x", "zh", "en", "A")
    assert len(sec) == 1


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
    from voxsub.translate.base import TranslationError
    with pytest.raises(TranslationError):
        TranslatorFactory.create("nope")


# ---------- cloud (mock 端点) ----------

def _serve_once(handler):
    """起一个本地 HTTP 服务器, 处理一次请求后返回响应体/记录收到的消息。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    captured: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            captured["endpoint"] = self.path
            captured["messages"] = body.get("messages")
            captured["model"] = body.get("model")
            payload = json.dumps(
                {"choices": [{"message": {"role": "assistant",
                                           "content": "Hola 译文"}}]},
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.handle_request, daemon=True)  # 只处理一次
    t.start()
    return srv, port, captured


def test_cloud_translate_via_mock_endpoint() -> None:
    from voxsub.translate.cloud import CloudTranslator
    srv, port, captured = _serve_once(lambda: None)
    tr = CloudTranslator({"api_key": "sk-test",
                          "base_url": f"http://127.0.0.1:{port}"})
    try:
        out = tr.translate("你好", "zh", "en")
    finally:
        srv.server_close()
    assert out == "Hola 译文"
    assert captured["messages"][-1]["content"] == "你好"   # 用户消息透传
    assert captured["model"] == "deepseek-chat"


def test_cloud_whitelist_rejects_unknown_host() -> None:
    from voxsub.translate.cloud import CloudTranslator
    from voxsub.translate.base import TranslationError
    tr = CloudTranslator({"api_key": "k",
                          "base_url": "http://evil.example.com/"})
    assert tr.ready() is False                     # ready() 返回 bool, 不抛
    with pytest.raises(TranslationError):          # 真正发起翻译时才拒绝
        tr.translate("hi", "en", "zh")


# ---------- 真实中英翻译 (opus 快档; 模型缺失则跳过) ----------

MODELS_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "VoxSub" / "models"
_OPUS_DIR = MODELS_DIR / "nmt" / "opus_zh_en"
if not (_OPUS_DIR / "encoder_model_int8.onnx").exists():
    pytest.skip("缺少 opus_zh_en 模型, 跳过真实翻译测试", allow_module_level=True)


@pytest.fixture(scope="module")
def opus():
    from voxsub.translate.opus import OpusFastTranslator
    tr = OpusFastTranslator()
    yield tr
    tr.close()


@pytest.mark.integration
def test_opus_real_zh_to_en(opus) -> None:
    """真实 zh→en: 手写样例, 断言译文非空、长度合理且含关键英文词。"""
    cases = [
        ("你好世界", "world"),
        ("我想学习中文", "Chinese"),
        ("今天天气很好，我们去公园散步吧", "park"),
    ]
    for src, keyword in cases:
        out = opus.translate(src, "zh", "en")
        assert out.strip(), f"译文不应为空: {src!r} -> {out!r}"
        assert 3 <= len(out) <= 300, f"译文长度不合理: {out!r}"
        assert keyword.lower() in out.lower(), \
            f"译文应含关键词 {keyword!r}: {src!r} -> {out!r}"


@pytest.mark.integration
def test_opus_real_en_to_zh(opus) -> None:
    """真实 en→zh: 断言译文非空且出现中文字符。"""
    out = opus.translate("I want to learn Chinese today.", "en", "zh")
    assert out.strip()
    assert any("\u4e00" <= ch <= "\u9fff" for ch in out), f"应输出中文: {out!r}"


def test_opus_roundtrip_not_empty(opus) -> None:
    """往返 sanity: 两次方向均返回非空合理文本 (模型加载一次, 共享会话)。"""
    a = opus.translate("早上好", "zh", "en")
    b = opus.translate("Good morning", "en", "zh")
    assert a.strip() and b.strip()