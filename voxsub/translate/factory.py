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

from .base import Translator, TranslationError
from .cloud import CloudTranslator
from .opus import OpusFastTranslator
from .qwen import QwenQualityTranslator

logger = get_logger("translate.factory")

KINDS = ("opus-fast", "qwen-quality", "cloud")


class TranslatorFactory:
    """翻译器工厂。"""

    @staticmethod
    def create(kind: str, config=None) -> Translator:
        kind = (kind or "").strip().lower()
        logger.info("创建翻译器: kind=%s", kind)
        if kind == "opus-fast":
            return OpusFastTranslator()
        if kind == "qwen-quality":
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

        qwen = QwenQualityTranslator()
        found["qwen-quality"] = bool(
            qwen.health() == "ok")   # health() 只校验文件存在, 不 spawn
        logger.info("翻译档位就绪探测: qwen-quality=%s", found["qwen-quality"])
        qwen.close()

        cloud = CloudTranslator(config)
        found["cloud"] = cloud.ready()
        logger.info("翻译档位就绪探测: cloud=%s", found["cloud"])
        cloud.close()
        return found
