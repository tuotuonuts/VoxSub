from __future__ import annotations

import pytest

from voxsub.translate.base import TranslationError, parse_translation_batch
from voxsub.translate.cloud import CloudTranslator


def test_parse_translation_batch_accepts_json_fence_but_rejects_wrong_count():
    assert parse_translation_batch(
        '```json\n["你好", "世界"]\n```', 2) == ["你好", "世界"]

    with pytest.raises(TranslationError, match="数量不匹配"):
        parse_translation_batch('["只有一项"]', 2)


def test_cloud_ocr_batch_uses_one_request(monkeypatch):
    calls: list[dict] = []

    def fake_chat(endpoint, **kwargs):
        calls.append({"endpoint": endpoint, **kwargs})
        return '["你好", "世界"]'

    monkeypatch.setattr("voxsub.translate.cloud.chat_completion", fake_chat)
    translator = CloudTranslator({
        "translate_api_key": "test-key",
        "translate_base_url": "https://api.deepseek.com/v1",
        "translate_model": "test-model",
    })

    translated = translator.translate_many(["Hello", "World"], "en", "zh")

    assert translated == ["你好", "世界"]
    assert len(calls) == 1
    assert calls[0]["endpoint"].endswith("/chat/completions")
    assert calls[0]["api_key"] == "test-key"
    assert "exactly 2" in calls[0]["messages"][-1]["content"]
