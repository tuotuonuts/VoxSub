"""翻译档位 × 语言对 的测试。

## 覆盖的用户报告

日志里每一句都失败：

    ERROR 翻译失败: error=快档不支持语言对 ('ja', 'zh') (支持: [('zh','en'),('en','zh')])

## 三个真实缺陷

1. **保存的档位从不生效**。设置只在用户点击控件时才推给后端；启动时前端只把
   配置读进界面，从不回推。实测：配置里 ``translate_tier=quality`` 且选了
   ``mt-hy-mt2-1.8b-q8``，会话实际却跑 opus-fast 快档 —— 于是日→中每句失败。

2. **快档 + 日/韩文没人拦**。快档（OPUS-MT）只有 zh↔en 的模型，界面却允许把
   源/目标语言选成日文/韩文。``Translator.supports()`` 早就写好了、也有测试，
   但**生产代码里没有任何地方调用它**。

3. **档位→kind 的映射有三份**：factory 一份、ocr.py 一份副本、ipc_server 写了
   一个**根本不存在的配置键** ``translator_kind``（配置里是 ``translate_tier``），
   于是 OCR 翻译永远用快档，无视用户选择。

这里守住的是"规则与实际行为一致"：能力表由静态声明算出，再用**真实创建出来的
翻译器**逐一核对 —— 只看档位名会得出错误结论（质量档在配置成
mt-opus-fast-builtin 时创建的其实是 OPUS 翻译器）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voxsub.translate import factory  # noqa: E402
from voxsub.translate.base import pair_supported  # noqa: E402


# ------------------------------------------------------------------ 语言对判断


class TestPairSupported:
    def test_membership_rule(self):
        assert pair_supported(("zh", "en"), "zh", "en")
        assert pair_supported(("zh", "en"), "en", "zh")
        assert not pair_supported(("zh", "en"), "ja", "zh")
        assert not pair_supported(("zh", "en", "ja", "ko"), "ja", "zh") is False

    def test_auto_is_wildcard_source(self):
        """auto 是源语言通配符（N→1 的自动识别）。"""
        assert pair_supported(("zh", "en"), "auto", "zh")
        assert pair_supported(("zh", "en", "ja", "ko"), "auto", "ko")

    def test_auto_never_a_destination(self):
        """目标语言必须是具体语言，``en→auto`` 不能算受支持。"""
        assert not pair_supported(("zh", "en", "ja", "ko"), "en", "auto")

    def test_same_language_pair_counts_as_handled(self):
        """同语言对算"受支持"—— 翻译器对它做直通，不是错误。

        实测契约：质量档 ``src_lang == dst_lang`` 时直接返回原文（passthrough），
        不报错。界面允许用户选 zh→zh，所以这里必须不触发"换档位"。
        """
        assert pair_supported(("zh", "en"), "zh", "zh") is True
        from voxsub.translate.qwen import QwenQualityTranslator

        assert QwenQualityTranslator().translate("你好", "zh", "zh") == "你好"

    def test_supports_method_delegates(self):
        """``Translator.supports`` 必须走同一个实现，否则两边会漂移。"""
        from voxsub.translate.opus import OpusFastTranslator

        opus = OpusFastTranslator()
        assert opus.supports("zh", "en") is True
        assert opus.supports("ja", "zh") is False
        assert opus.supports("ja", "zh") == pair_supported(opus.langs, "ja", "zh")


# ------------------------------------------------------------------ 档位能力


class TestTierCapabilities:
    """能力表必须与**真实翻译器**的行为一致。"""

    @pytest.mark.parametrize("tier,kind", [("fast", "opus-fast"),
                                           ("quality", "qwen-quality"),
                                           ("cloud", "cloud")])
    def test_kind_mapping(self, tier, kind):
        assert factory.kind_for_tier(tier, {}) == kind

    def test_static_langs_match_real_translator(self):
        """逐个档位实例化真实翻译器，核对 supports() 与能力表一致。

        这是防"规则与实际脱节"的核心断言：静态表算错了，界面就会给出
        错误提示（例如说质量档支持日语，而实际创建的却是 OPUS）。
        """
        for tier in factory.TIER_KINDS:
            kind = factory.kind_for_tier(tier, {})
            translator = factory.TranslatorFactory.create(kind, {})
            try:
                for src, dst in [("zh", "en"), ("en", "zh"), ("ja", "zh"),
                                 ("zh", "ja"), ("ko", "en"), ("auto", "zh")]:
                    assert factory.tier_supports(tier, src, dst, {}) == translator.supports(src, dst), (
                        f"{tier}/{kind} 对 {src}→{dst} 的判断与真实翻译器不一致")
            finally:
                translator.close()

    def test_quality_tier_configured_as_opus_is_reported_as_opus(self):
        """质量档配成 OPUS 兼容时，它其实只支持 zh/en。

        只看档位名会得出"质量档支持日语"的错结论 —— 这个用例钉住这个坑。
        """
        config = {"translate_model_id": "mt-opus-fast-builtin"}
        assert factory.kind_for_tier("quality", config) == "opus-fast"
        assert factory.tier_supports("quality", "ja", "zh", config) is False
        assert factory.tier_supports("quality", "zh", "en", config) is True

    def test_capabilities_payload_shape(self):
        caps = factory.tier_capabilities("ja", "zh", {"translate_tier": "quality"})
        assert {t["id"] for t in caps["tiers"]} == {"fast", "quality", "cloud"}
        assert caps["source"] == "ja" and caps["target"] == "zh"
        assert caps["selected"] == "quality"
        by_id = {t["id"]: t for t in caps["tiers"]}
        assert by_id["fast"]["supportsPair"] is False
        assert by_id["quality"]["supportsPair"] is True
        # 界面要显示"支持哪些语言"，所以 langs 必须在载荷里
        assert by_id["fast"]["langs"] == ["zh", "en"]


class TestResolveTier:
    def test_supported_pair_keeps_user_choice(self):
        """支持的语言对绝不能被动过 —— 用户选什么就是什么。"""
        assert factory.resolve_tier("fast", "zh", "en", {"translate_tier": "fast"}) == ("fast", None)
        assert factory.resolve_tier("quality", "ja", "zh",
                                    {"translate_tier": "quality"}) == ("quality", None)

    def test_unsupported_pair_switches_to_one_that_works(self):
        """快档 + ja→zh 必须换成支持的档位，而不是每句失败。"""
        effective, replaced = factory.resolve_tier("fast", "ja", "zh", {"translate_tier": "fast"})
        assert replaced == "fast"
        assert factory.tier_supports(effective, "ja", "zh", {"translate_tier": "fast"})

    def test_user_real_config_is_not_substituted(self):
        """用户真实配置（quality + hy-mt2 模型）本就支持 ja→zh，不该被替换。"""
        config = {"translate_tier": "quality", "translate_model_id": "mt-hy-mt2-1.8b-q8"}
        assert factory.resolve_tier("quality", "ja", "zh", config) == ("quality", None)

    def test_all_ko_combinations_are_covered(self):
        """任何界面允许的语言组合，最终都要落到一个能干的档位上。

        界面提供 auto/zh/en/ja/ko → zh/en/ja/ko，穷举一遍，确认不存在
        "选了就一定失败"的组合（排除同语言对，那是合理的直通）。
        """
        langs = ("auto", "zh", "en", "ja", "ko")
        targets = ("zh", "en", "ja", "ko")
        config = {"translate_tier": "fast"}
        uncovered = []
        for src in langs:
            for dst in targets:
                if src == dst:
                    continue
                effective, _ = factory.resolve_tier("fast", src, dst, config)
                if not factory.tier_supports(effective, src, dst, config):
                    uncovered.append((src, dst, effective))
        assert not uncovered, f"这些组合没有任何档位能做: {uncovered}"


# ------------------------------------------------------- 配置键名（曾经的错键）


class TestConfigKeys:
    def test_translator_kind_is_not_a_config_key(self):
        """``translator_kind`` 不是配置键；用它会静默退回默认档位。

        ipc_server 的 OCR 翻译路径曾读这个键，于是 OCR 永远用快档。
        """
        from voxsub.config_store import ConfigStore

        keys = set(ConfigStore().load()) | set(
            __import__("voxsub.config_store", fromlist=["_DEFAULTS"])._DEFAULTS)
        assert "translate_tier" in keys
        assert "translator_kind" not in keys

    def test_ocr_kind_uses_shared_mapping(self):
        """ocr.py 必须走 factory 的映射，不再自带副本。"""
        from voxsub import ocr

        assert ocr._translator_kind({"translate_tier": "quality"}) == "qwen-quality"
        assert ocr._translator_kind({"translate_tier": "cloud"}) == "cloud"
        assert ocr._translator_kind({"translate_tier": "fast"}) == "opus-fast"
        assert ocr._translator_kind({}) == "opus-fast"
