"""voxsub.translate.factory —— 翻译档位装配与就绪探测。

契约 (DESIGN.md「翻译层契约/TranslatorFactory」):
    TranslatorFactory.create(kind, config=None) -> Translator
    TranslatorFactory.list_available() -> dict[str, bool]

kind ∈ {"opus-fast", "qwen-quality", "cloud"}。
create 在模型/凭据不满足时抛 FileNotFoundError 或 RuntimeError,
由调用方(pipeline)捕获并降级到可用档位。
"""
from __future__ import annotations

import os
from pathlib import Path

from voxsub.translate.base import Translator  # noqa: F401  (re-export)
from voxsub.translate.cache import TranslationCache  # noqa: F401
from voxsub.translate.prefetch import PrefetchEngine  # noqa: F401

from . import opus as _opus
from . import qwen as _qwen
from . import cloud as _cloud


def _models_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "models"


def _opus_pairs_ready() -> dict[tuple[str, str], bool]:
    """扫描 nmt/ 下语言对子目录, 返回 (zh,en)/(en,zh) 各自是否文件齐全。"""
    nmt = _models_dir() / "nmt"
    pairs = {("zh", "en"): "opus_zh_en", ("en", "zh"): "opus_en_zh"}
    result = {}
    for pair, sub in pairs.items():
        d = nmt / sub
        need = ["encoder_model_int8.onnx", "decoder_model_int8.onnx",
                "config.json", "generation_config.json", "tokenizer.json"]
        result[pair] = all((d / f).exists() for f in need)
    return result


def _qwen_ready() -> bool:
    gguf = _models_dir() / "llm" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    server = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "tools" / "llama" / "llama-server.exe"
    return gguf.exists() and gguf.stat().st_size > 500_000_000 and server.exists()


def _cloud_ready() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


class TranslatorFactory:
    """翻译档位的创建与就绪探测。"""

    KIND_OPUS = "opus-fast"
    KIND_QWEN = "qwen-quality"
    KIND_CLOUD = "cloud"

    @staticmethod
    def list_available() -> dict[str, bool]:
        """各档位 → 是否就绪 (模型文件 / 凭据具备)。"""
        opus_pairs = _opus_pairs_ready()
        return {
            TranslatorFactory.KIND_OPUS: any(opus_pairs.values()),
            TranslatorFactory.KIND_QWEN: _qwen_ready(),
            TranslatorFactory.KIND_CLOUD: _cloud_ready(),
        }

    @staticmethod
    def create(kind: str, config=None) -> Translator:
        """构造指定档位翻译器。

        Raises:
            ValueError: 未知档位。
            FileNotFoundError: 档位模型/凭据缺失。
        """
        if kind == TranslatorFactory.KIND_OPUS:
            t = _opus.OpusFastTranslator()
            if not any(_opus_pairs_ready().values()):
                raise FileNotFoundError("OPUS-MT 快档模型未安装 (models/nmt/)")
            return t
        if kind == TranslatorFactory.KIND_QWEN:
            if not _qwen_ready():
                raise FileNotFoundError("Qwen 质量档模型或 llama-server 未就绪")
            return _qwen.QwenQualityTranslator()
        if kind == TranslatorFactory.KIND_CLOUD:
            return _cloud.CloudTranslator(config)
        raise ValueError(f"未知翻译档位: {kind!r}")

    @staticmethod
    def pick_best() -> Translator:
        """按优先级选择当前可用档位并创建: 质量档 > 快档 > 云(需用户key)。

        返回首个就绪的 Translator; 全无则抛 FileNotFoundError。
        """
        avail = TranslatorFactory.list_available()
        for kind, name in ((TranslatorFactory.KIND_QWEN, _qwen.QwenQualityTranslator),
                           (TranslatorFactory.KIND_OPUS, _opus.OpusFastTranslator),
                           (TranslatorFactory.KIND_CLOUD, _cloud.CloudTranslator)):
            if avail.get(kind):
                return TranslatorFactory.create(kind)
        raise FileNotFoundError("没有可用的翻译档位 (未安装模型且未配云 key)")


__all__ = ["TranslatorFactory", "TranslationCache", "PrefetchEngine"]