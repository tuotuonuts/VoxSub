from __future__ import annotations

from voxsub.translate.cloud import CloudTranslator
from voxsub.translate.opus import OpusFastTranslator
from voxsub.translate.qwen import QwenQualityTranslator


def test_quality_and_cloud_declare_japanese_and_korean_support() -> None:
    for translator in (QwenQualityTranslator(), CloudTranslator({"translate_api_key": "x"})):
        assert translator.supports("ja", "ko")
        assert translator.supports("ko", "ja")
        assert translator.supports("auto", "ja")
        translator.close()


def test_fast_translator_does_not_claim_unbundled_languages() -> None:
    translator = OpusFastTranslator(model_dir=None)
    assert not translator.supports("ja", "zh")
    assert not translator.supports("ko", "en")
    translator.close()
