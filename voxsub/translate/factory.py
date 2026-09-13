"""voxsub.translate.factory —— 按档位创建翻译器 / 探测可用性 (M4 契约)。

契约 (DESIGN.md)::

    class TranslatorFactory:
        @staticmethod
        def create(kind, config) -> Translator
        @staticmethod
        def list_available() -> dict[str, bool]   # 档位 → 模型/凭据就绪?
"""
from __future__ import annotations

from voxsub.logging_setup import get_logger

from .base import Translator, TranslationError, pair_supported
from .cloud import CloudTranslator
from .opus import OpusFastTranslator
from .qwen import QwenQualityTranslator

logger = get_logger("translate.factory")

KINDS = ("opus-fast", "qwen-quality", "cloud")

#: 界面/配置里的档位名 → 翻译器 kind。
#: 这是唯一的映射表：ocr.py 与前端都从这里取，避免各写一份后漂移
#: （此前 ocr.py 有一份副本，ipc_server 又写了个不存在的配置键）。
TIER_KINDS: dict[str, str] = {
    "fast": "opus-fast",
    "quality": "qwen-quality",
    "cloud": "cloud",
}

#: 按语言对挑档位时的候选顺序。用户所选永远排第一，其后按"更可能支持该语言对"
#: 排列：质量档支持 zh/en/ja/ko，云端也是；快档只有 zh↔en 两个方向。
_FALLBACK_KINDS = ("qwen-quality", "cloud", "opus-fast")


def kind_for_tier(tier: str, config=None) -> str:
    """档位名 → 翻译器 kind，并按配置修正。

    注意**质量档不一定是 Qwen**：配置里 translate_model_id 为
    ``mt-opus-fast-builtin`` 时，create() 实际返回的是 OPUS 翻译器
    （见下方 TranslatorFactory.create）。支持的语言随之变成 zh/en ——
    只看档位名会得出错误结论，界面就会给出"质量档支持日语"的错提示。
    """
    kind = TIER_KINDS.get(str(tier or "").strip().lower())
    if kind is None:
        return str(tier or "opus-fast")
    if kind == "qwen-quality" and isinstance(config, dict):
        if str(config.get("translate_model_id", "") or "") == "mt-opus-fast-builtin":
            return "opus-fast"
    return kind


def _class_for_kind(kind: str):
    """kind → 翻译器类（不实例化，只读 langs 声明）。"""
    if kind == "opus-fast":
        return OpusFastTranslator
    if kind == "qwen-quality":
        return QwenQualityTranslator
    if kind == "cloud":
        return CloudTranslator
    return None


def tier_langs(tier: str, config=None) -> tuple[str, ...]:
    """档位实际支持的源/目标语言代码。"""
    cls = _class_for_kind(kind_for_tier(tier, config))
    return tuple(getattr(cls, "langs", ()) or ())


def tier_supports(tier: str, src_lang: str, dst_lang: str, config=None) -> bool:
    """该档位能否翻译这个语言对。"""
    return pair_supported(tier_langs(tier, config), src_lang, dst_lang)


def resolve_tier(tier: str, src_lang: str, dst_lang: str, config=None) -> tuple[str, str | None]:
    """挑一个**真能翻译这个语言对**的档位。

    返回 ``(生效档位, 被替换掉的档位或 None)``。

    背景：快档（OPUS-MT）只有 zh↔en 的模型，而界面允许把源/目标语言选成
    日文/韩文。此时每一句都会抛 "快档不支持语言对"，用户只看到原文加满屏
    ERROR。这里在**会话开始前**就换成一个支持的档位，而不是让每句话都失败。
    """
    if tier_supports(tier, src_lang, dst_lang, config):
        return tier, None
    # 用户所选不支持 → 按候选顺序找第一个支持的档位。
    # tier_supports 内部会按配置修正（例如质量档被配成 OPUS 兼容时，
    # 它其实只支持 zh/en），所以这里不需要额外特判。
    candidates = [tid for kind in _FALLBACK_KINDS
                  for tid, mapped in TIER_KINDS.items() if mapped == kind]
    for candidate in candidates:
        if candidate == tier:
            continue
        if tier_supports(candidate, src_lang, dst_lang, config):
            logger.warning(
                "档位 %s 不支持语言对 %s→%s，改用 %s",
                tier, src_lang, dst_lang, candidate,
            )
            return candidate, tier
    logger.error("没有任何翻译档位支持语言对 %s→%s", src_lang, dst_lang)
    return tier, None


def tier_capabilities(src_lang: str, dst_lang: str, config=None) -> dict:
    """给界面用的档位能力表（只读配置，不加载模型）。

    前端不硬编码"哪些档位支持哪些语言"，否则后端换了模型它会继续显示旧结论。
    """
    tiers = []
    for tier_id in TIER_KINDS:
        langs = tier_langs(tier_id, config)
        tiers.append({
            "id": tier_id,
            "kind": kind_for_tier(tier_id, config),
            "langs": list(langs),
            "supportsPair": tier_supports(tier_id, src_lang, dst_lang, config),
        })
    effective, substituted_from = resolve_tier(
        str((config or {}).get("translate_tier", "fast")), src_lang, dst_lang, config)
    return {
        "tiers": tiers,
        "source": src_lang,
        "target": dst_lang,
        "selected": str((config or {}).get("translate_tier", "fast")),
        "effective": effective,
        "substitutedFrom": substituted_from,
    }


class TranslatorFactory:
    """翻译器工厂。"""

    @staticmethod
    def create(kind: str, config=None) -> Translator:
        kind = (kind or "").strip().lower()
        logger.info("创建翻译器: kind=%s", kind)
        if kind == "opus-fast":
            from voxsub.router import preferred_onnx_providers

            return OpusFastTranslator(providers=preferred_onnx_providers("translate"))
        if kind == "qwen-quality":
            if isinstance(config, dict):
                model_id = str(config.get("translate_model_id", "") or "")
                if model_id == "mt-opus-fast-builtin":
                    logger.info("质量档未选择新模型，按模型广场选择使用 OPUS 极速兼容")
                    from voxsub.router import preferred_onnx_providers

                    return OpusFastTranslator(providers=preferred_onnx_providers("translate"))
                if model_id:
                    from voxsub.model_catalog import ModelMarketplace, get_model

                    model = get_model(model_id)
                    if model is not None and model.runtime == "llama-hy-mt2":
                        marketplace = ModelMarketplace()
                        return QwenQualityTranslator(
                            model_path=marketplace.model_file(model),
                            prompt_style="hy-mt2",
                            model_name=model.name,
                            expected_size=model.download_bytes,
                            expected_sha256=model.sha256,
                            n_threads=min(8, max(2, __import__("os").cpu_count() or 2)),
                        )
            return QwenQualityTranslator()
        if kind == "cloud":
            return CloudTranslator(config)
        logger.error("未知翻译档位, 拒绝创建: %r", kind)
        raise TranslationError(f"未知翻译档位: {kind!r} (可选: {KINDS})")

    @staticmethod
    def list_available(config=None) -> dict[str, bool]:
        """探测各档位是否就绪 (只查模型文件/凭据, 不实际加载/发起请求)。"""
        found: dict[str, bool] = {}

        opus = OpusFastTranslator()
        found["opus-fast"] = bool(opus.list_available_pairs())
        logger.info("翻译档位就绪探测: opus-fast=%s", found["opus-fast"])
        opus.close()

        # Reuse the selected catalog model metadata when available so a
        # truncated GGUF is reported as unavailable before any server spawn.
        qwen = TranslatorFactory.create("qwen-quality", config)
        found["qwen-quality"] = bool(
            qwen.health() == "ok")   # health() 只校验文件存在, 不 spawn
        logger.info("翻译档位就绪探测: qwen-quality=%s", found["qwen-quality"])
        qwen.close()

        cloud = CloudTranslator(config)
        found["cloud"] = cloud.ready()
        logger.info("翻译档位就绪探测: cloud=%s", found["cloud"])
        cloud.close()
        return found
