"""Curated model catalog, hardware assessment, and local installation service.

The catalog is deliberately small.  A model is listed only when VoxSub has a
runtime adapter for it; downloading a weight that the application cannot use is
treated as a product bug, not as a marketplace feature.
"""
from __future__ import annotations

import json
import shutil
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib import request as urlrequest

from voxsub.logging_setup import get_logger
from voxsub.hardware import HardwareProfile, detect_hardware, discover_llama_runtimes
from voxsub.model_storage import model_lookup_roots, resolve_models_root
from voxsub.models import DownloadCancelled, fetch_file, sha256_of

logger = get_logger("model_catalog")

GIB = 1024 ** 3
CATALOG_UPDATED = "2026-08-20"


def default_models_dir() -> Path:
    """Return the configured root shared by all model runtimes."""
    return resolve_models_root()


@dataclass(frozen=True)
class RemoteFile:
    url: str
    install_rel: str
    size: int
    sha256: str = ""


@dataclass(frozen=True)
class ModelSource:
    id: str                    # global | china
    label: str
    url: str
    probe_url: str
    files: tuple[RemoteFile, ...] = ()


@dataclass(frozen=True)
class ModelSpec:
    id: str
    task: str                  # asr | translate
    name: str
    vendor: str
    release: str
    description: str
    runtime: str
    quality_score: int
    languages: str
    license: str
    download_bytes: int
    installed_bytes: int
    install_rel: str
    required_paths: tuple[str, ...]
    required_patterns: tuple[str, ...] = ()
    legacy_install_rels: tuple[str, ...] = ()
    sources: tuple[ModelSource, ...] = ()
    asset_name: str = ""
    sha256: str = ""
    archive: bool = False
    builtin: bool = False
    min_ram_gb: float = 4.0
    working_ram_gb: float = 1.0
    compute_cost: float = 30.0
    gpu_supported: bool = True
    npu_supported: bool = False
    igpu_supported: bool = False
    tags: tuple[str, ...] = ()

    @property
    def task_label(self) -> str:
        return {"asr": "语音识别", "translate": "字幕翻译"}.get(self.task, self.task)


_GH_ASR = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
_MS_ASR = "https://modelscope.cn/models/csukuangfj/asr-models/resolve/master"
_HF = "https://huggingface.co"
_MS = "https://modelscope.cn"
_HF_MIRROR = "https://hf-mirror.com"


def _opus_remote_files(language_pair: str, base_url: str) -> tuple[RemoteFile, ...]:
    """Return the exact files needed by one Xenova OPUS direction.

    The upstream repository keeps ONNX weights under ``onnx/`` while VoxSub
    stores the four runtime files directly under ``models/nmt/opus_*``.
    """
    prefix = f"opus_{language_pair.replace('-', '_')}"
    repo = f"opus-mt-{language_pair}"
    files = (
        ("onnx/encoder_model_int8.onnx", "encoder_model_int8.onnx"),
        ("onnx/decoder_model_int8.onnx", "decoder_model_int8.onnx"),
        ("config.json", "config.json"),
        ("tokenizer.json", "tokenizer.json"),
    )
    return tuple(
        RemoteFile(
            f"{base_url}/Xenova/{repo}/resolve/main/{remote}?download=true",
            f"{prefix}/{local}",
            0,
        )
        for remote, local in files
    )


CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="asr-funasr-nano-2512-int8",
        task="asr",
        name="Fun-ASR-Nano 2512 · INT8",
        vendor="FunAudioLLM / sherpa-onnx",
        release="2025-12-30",
        description="中英日高质量识别，强化方言、口音、远场、噪声与歌曲场景。",
        runtime="sherpa-funasr-nano",
        quality_score=98,
        languages="中文 / 英语 / 日语 · 7 种中文方言",
        license="Apache-2.0",
        download_bytes=995_000_000,
        installed_bytes=1_018_000_000,
        install_rel="stt/funasr-nano-2512-int8",
        legacy_install_rels=("marketplace/asr-funasr-nano-2512-int8",),
        required_paths=("encoder_adaptor.int8.onnx", "llm.int8.onnx",
                        "embedding.int8.onnx", "Qwen3-0.6B/tokenizer.json"),
        sources=(
            ModelSource("global", "GitHub 全球源",
                        f"{_GH_ASR}/sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2",
                        "https://github.com/favicon.ico"),
            ModelSource("china", "ModelScope 中国源",
                        f"{_MS_ASR}/sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2",
                        "https://modelscope.cn/favicon.ico"),
        ),
        asset_name="sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2",
        archive=True,
        min_ram_gb=8.0,
        working_ram_gb=2.2,
        compute_cost=82,
        gpu_supported=False,
        tags=("中文优先", "抗噪", "方言", "歌曲"),
    ),
    ModelSpec(
        id="asr-qwen3-0.6b-int8",
        task="asr",
        name="Qwen3-ASR 0.6B · INT8",
        vendor="Qwen / sherpa-onnx",
        release="2026-03-25",
        description="新一代多语种识别，覆盖 30 种语言、22 种中文方言和中英混说。",
        runtime="sherpa-qwen3-asr",
        quality_score=96,
        languages="30 种语言 · 22 种中文方言",
        license="Apache-2.0",
        download_bytes=960_000_000,
        installed_bytes=1_005_000_000,
        install_rel="stt/qwen3-asr-0.6b-int8",
        legacy_install_rels=("marketplace/asr-qwen3-0.6b-int8",),
        required_paths=("conv_frontend.onnx", "encoder.int8.onnx",
                        "decoder.int8.onnx", "tokenizer/vocab.json"),
        sources=(
            ModelSource("global", "GitHub 全球源",
                        f"{_GH_ASR}/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2",
                        "https://github.com/favicon.ico"),
            ModelSource("china", "ModelScope 中国源",
                        "https://modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx",
                        "https://modelscope.cn/favicon.ico",
                        files=(
                            RemoteFile(
                                "https://modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx/resolve/master/model_0.6B/conv_frontend.onnx",
                                "conv_frontend.onnx", 44_148_281,
                                "d22dc4423e0940e49884e903d2ea2f7e5567c14fc1aed97e4e26d6b8f208ef9e"),
                            RemoteFile(
                                "https://modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx/resolve/master/model_0.6B/encoder.int8.onnx",
                                "encoder.int8.onnx", 182_491_662,
                                "60748d3e6744a57c9c91e1b17424a6c2990567e8adceb0783940c03ed98fa9d9"),
                            RemoteFile(
                                "https://modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx/resolve/master/model_0.6B/decoder.int8.onnx",
                                "decoder.int8.onnx", 755_914_231,
                                "4f6885be5959ae26af3089d38ee7972c5fafbeeb1cf8d5e76eab6d8b61ca5771"),
                            RemoteFile(
                                "https://modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx/resolve/master/tokenizer/merges.txt",
                                "tokenizer/merges.txt", 1_671_853,
                                "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"),
                            RemoteFile(
                                "https://modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx/resolve/master/tokenizer/tokenizer_config.json",
                                "tokenizer/tokenizer_config.json", 12_487,
                                "4942d005604266809309cabc9f4e9cb89ce855d59b14681fdc0e1cc62ea26c4c"),
                            RemoteFile(
                                "https://modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx/resolve/master/tokenizer/vocab.json",
                                "tokenizer/vocab.json", 2_776_833,
                                "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
                        )),
        ),
        asset_name="sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2",
        archive=True,
        min_ram_gb=8.0,
        working_ram_gb=2.0,
        compute_cost=74,
        gpu_supported=False,
        tags=("多语种", "方言", "混合语言", "2026"),
    ),
    ModelSpec(
        id="asr-sensevoice-small-int8",
        task="asr",
        name="SenseVoice Small · INT8",
        vendor="FunAudioLLM / sherpa-onnx",
        release="2024-07-17",
        description="轻量多语种识别，覆盖中文、粤语、英语、日语和韩语；适合更重视响应速度的轻薄本。",
        runtime="sherpa-sense-voice",
        quality_score=88,
        languages="中文 / 粤语 / 英语 / 日语 / 韩语",
        license="Apache-2.0",
        download_bytes=245_000_000,
        installed_bytes=270_000_000,
        install_rel="stt/sensevoice-small-int8",
        legacy_install_rels=("marketplace/asr-sensevoice-small-int8",),
        required_paths=("model.int8.onnx", "tokens.txt"),
        sources=(
            ModelSource("global", "GitHub 全球源",
                        f"{_GH_ASR}/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
                        "https://github.com/favicon.ico"),
            ModelSource("china", "ModelScope 中国源",
                        f"{_MS_ASR}/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
                        "https://modelscope.cn/favicon.ico"),
        ),
        asset_name="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        archive=True,
        min_ram_gb=4.0,
        working_ram_gb=0.85,
        compute_cost=38,
        tags=("低资源", "粤语", "多语种", "响应快"),
    ),
    ModelSpec(
        id="asr-zipformer-bilingual-fast",
        task="asr",
        name="Zipformer 中英双语 · 极速兼容",
        vendor="k2-fsa",
        release="2023-02-16",
        description="约百兆的实时低资源兜底；精度较低，但在同体积下仍有独特实时优势。",
        runtime="sherpa-streaming-transducer",
        quality_score=72,
        languages="中文 / 英语",
        license="Apache-2.0",
        download_bytes=0,
        installed_bytes=150_000_000,
        install_rel="stt/zipformer",
        required_paths=("tokens.txt",),
        required_patterns=("*encoder*.onnx", "*decoder*.onnx", "*joiner*.onnx"),
        legacy_install_rels=("asr",),
        sources=(
            ModelSource(
                "global", "GitHub 全球源",
                f"{_GH_ASR}/sherpa-onnx-streaming-zipformer-zh-en-2023-02-16.tar.bz2",
                "https://github.com/favicon.ico",
            ),
            ModelSource(
                "china", "ModelScope 中国源",
                f"{_MS_ASR}/sherpa-onnx-streaming-zipformer-zh-en-2023-02-16.tar.bz2",
                "https://modelscope.cn/favicon.ico",
            ),
        ),
        asset_name="sherpa-onnx-streaming-zipformer-zh-en-2023-02-16.tar.bz2",
        archive=True,
        builtin=True,
        min_ram_gb=4.0,
        working_ram_gb=0.45,
        compute_cost=18,
        gpu_supported=False,
        tags=("低延迟", "低内存", "内置"),
    ),
    ModelSpec(
        id="mt-hy-mt2-7b-q4",
        task="translate",
        name="Hy-MT2 7B · Q4_K_M",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="高质量专用翻译模型，复杂语境、术语和指令遵循优先。",
        runtime="llama-hy-mt2",
        quality_score=99,
        languages="33 种语言",
        license="Apache-2.0",
        download_bytes=4_624_648_896,
        installed_bytes=4_624_648_896,
        install_rel="translate/hy-mt2-7b-q4",
        legacy_install_rels=("marketplace/mt-hy-mt2-7b-q4",),
        required_paths=("Hy-MT2-7B-Q4_K_M.gguf",),
        sources=(
            ModelSource("global", "Hugging Face 全球源",
                        f"{_HF}/tencent/Hy-MT2-7B-GGUF/resolve/main/Hy-MT2-7B-Q4_K_M.gguf?download=true",
                        "https://huggingface.co/favicon.ico"),
            ModelSource("china", "ModelScope 中国源",
                        f"{_MS}/models/Tencent-Hunyuan/Hy-MT2-7B-GGUF/resolve/master/Hy-MT2-7B-Q4_K_M.gguf",
                        "https://modelscope.cn/favicon.ico"),
        ),
        asset_name="Hy-MT2-7B-Q4_K_M.gguf",
        sha256="9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b",
        min_ram_gb=12.0,
        working_ram_gb=6.2,
        compute_cost=158,
        npu_supported=True,
        igpu_supported=True,
        tags=("最高质量", "复杂语境", "2026"),
    ),
    ModelSpec(
        id="mt-hy-mt2-7b-q6",
        task="translate",
        name="Hy-MT2 7B · Q6_K",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="保留 7B 的复杂语境与术语优势，同时比 Q8 更适合高性能游戏本和主流独显。",
        runtime="llama-hy-mt2",
        quality_score=100,
        languages="33 种语言",
        license="Apache-2.0",
        download_bytes=6_164_482_720,
        installed_bytes=6_164_482_720,
        install_rel="translate/hy-mt2-7b-q6",
        legacy_install_rels=("marketplace/mt-hy-mt2-7b-q6",),
        required_paths=("HY-MT2-7B-Q6_K.gguf",),
        sources=(
            ModelSource("global", "Hugging Face 全球源",
                        f"{_HF}/tencent/Hy-MT2-7B-GGUF/resolve/main/HY-MT2-7B-Q6_K.gguf?download=true",
                        "https://huggingface.co/favicon.ico"),
            ModelSource("china", "ModelScope 中国源",
                        f"{_MS}/models/Tencent-Hunyuan/Hy-MT2-7B-GGUF/resolve/master/HY-MT2-7B-Q6_K.gguf",
                        "https://modelscope.cn/favicon.ico"),
        ),
        asset_name="HY-MT2-7B-Q6_K.gguf",
        sha256="88ef0aba59952a4cfe4be36cb5baf797dbb370bc60e9dcbd7297036021e52831",
        min_ram_gb=14.0,
        working_ram_gb=7.2,
        compute_cost=174,
        npu_supported=True,
        igpu_supported=True,
        tags=("高质量", "术语", "高性能设备", "Q6"),
    ),
    ModelSpec(
        id="mt-hy-mt2-7b-q8",
        task="translate",
        name="Hy-MT2 7B · Q8_0",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="7B 的高保真量化档，优先保留术语和复杂句质量；只适合内存、显存充裕的电脑。",
        runtime="llama-hy-mt2",
        quality_score=100,
        languages="33 种语言",
        license="Apache-2.0",
        download_bytes=7_981_928_896,
        installed_bytes=7_981_928_896,
        install_rel="translate/hy-mt2-7b-q8",
        legacy_install_rels=("marketplace/mt-hy-mt2-7b-q8",),
        required_paths=("HY-MT2-7B-Q8_0.gguf",),
        sources=(
            ModelSource("global", "Hugging Face 全球源",
                        f"{_HF}/tencent/Hy-MT2-7B-GGUF/resolve/main/HY-MT2-7B-Q8_0.gguf?download=true",
                        "https://huggingface.co/favicon.ico"),
            ModelSource("china", "ModelScope 中国源",
                        f"{_MS}/models/Tencent-Hunyuan/Hy-MT2-7B-GGUF/resolve/master/HY-MT2-7B-Q8_0.gguf",
                        "https://modelscope.cn/favicon.ico"),
        ),
        asset_name="HY-MT2-7B-Q8_0.gguf",
        sha256="58b3ad55dd6f6fa08c695cddc34fb5f8f708a844f78ae10508071914b0ed67c0",
        min_ram_gb=18.0,
        working_ram_gb=9.7,
        compute_cost=190,
        npu_supported=True,
        igpu_supported=True,
        tags=("高保真", "术语", "满载级", "Q8"),
    ),
    ModelSpec(
        id="mt-hy-mt2-1.8b-q4",
        task="translate",
        name="Hy-MT2 1.8B · Q4_K_M",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="面向端侧的专用翻译模型，在速度、资源占用和质量间更均衡。",
        runtime="llama-hy-mt2",
        quality_score=95,
        languages="33 种语言",
        license="Apache-2.0",
        download_bytes=1_133_080_448,
        installed_bytes=1_133_080_448,
        install_rel="translate/hy-mt2-1.8b-q4",
        legacy_install_rels=("marketplace/mt-hy-mt2-1.8b-q4",),
        required_paths=("Hy-MT2-1.8B-Q4_K_M.gguf",),
        sources=(
            ModelSource("global", "Hugging Face 全球源",
                        f"{_HF}/tencent/Hy-MT2-1.8B-GGUF/resolve/main/Hy-MT2-1.8B-Q4_K_M.gguf?download=true",
                        "https://huggingface.co/favicon.ico"),
            ModelSource("china", "ModelScope 中国源",
                        f"{_MS}/models/Tencent-Hunyuan/Hy-MT2-1.8B-GGUF/resolve/master/Hy-MT2-1.8B-Q4_K_M.gguf",
                        "https://modelscope.cn/favicon.ico"),
        ),
        asset_name="Hy-MT2-1.8B-Q4_K_M.gguf",
        sha256="dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699",
        min_ram_gb=6.0,
        working_ram_gb=2.5,
        compute_cost=66,
        npu_supported=True,
        igpu_supported=True,
        tags=("平衡", "端侧", "33 语言", "2026"),
    ),
    ModelSpec(
        id="mt-hy-mt2-1.8b-q6",
        task="translate",
        name="Hy-MT2 1.8B · Q6_K",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="端侧翻译的均衡高保真档，比 Q4 多占一些内存，适合希望进一步减少量化损失的用户。",
        runtime="llama-hy-mt2",
        quality_score=96,
        languages="33 种语言",
        license="Apache-2.0",
        download_bytes=1_474_785_120,
        installed_bytes=1_474_785_120,
        install_rel="translate/hy-mt2-1.8b-q6",
        legacy_install_rels=("marketplace/mt-hy-mt2-1.8b-q6",),
        required_paths=("Hy-MT2-1.8B-Q6_K.gguf",),
        sources=(
            ModelSource("global", "Hugging Face 全球源",
                        f"{_HF}/tencent/Hy-MT2-1.8B-GGUF/resolve/main/Hy-MT2-1.8B-Q6_K.gguf?download=true",
                        "https://huggingface.co/favicon.ico"),
            ModelSource("china", "ModelScope 中国源",
                        f"{_MS}/models/Tencent-Hunyuan/Hy-MT2-1.8B-GGUF/resolve/master/Hy-MT2-1.8B-Q6_K.gguf",
                        "https://modelscope.cn/favicon.ico"),
        ),
        asset_name="Hy-MT2-1.8B-Q6_K.gguf",
        sha256="d98fe604dec1f28f58f80d7d560f7177e584d3b8e5835862687660e5ff97cb40",
        min_ram_gb=6.0,
        working_ram_gb=2.8,
        compute_cost=76,
        npu_supported=True,
        igpu_supported=True,
        tags=("均衡高保真", "端侧", "33 语言", "Q6"),
    ),
    ModelSpec(
        id="mt-hy-mt2-1.8b-q8",
        task="translate",
        name="Hy-MT2 1.8B · Q8_0",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="轻量模型中的高保真档，适合内存充裕但不想运行 7B 的笔记本和台式机。",
        runtime="llama-hy-mt2",
        quality_score=97,
        languages="33 种语言",
        license="Apache-2.0",
        download_bytes=1_908_528_192,
        installed_bytes=1_908_528_192,
        install_rel="translate/hy-mt2-1.8b-q8",
        legacy_install_rels=("marketplace/mt-hy-mt2-1.8b-q8",),
        required_paths=("Hy-MT2-1.8B-Q8_0.gguf",),
        sources=(
            ModelSource("global", "Hugging Face 全球源",
                        f"{_HF}/tencent/Hy-MT2-1.8B-GGUF/resolve/main/Hy-MT2-1.8B-Q8_0.gguf?download=true",
                        "https://huggingface.co/favicon.ico"),
            ModelSource("china", "ModelScope 中国源",
                        f"{_MS}/models/Tencent-Hunyuan/Hy-MT2-1.8B-GGUF/resolve/master/Hy-MT2-1.8B-Q8_0.gguf",
                        "https://modelscope.cn/favicon.ico"),
        ),
        asset_name="Hy-MT2-1.8B-Q8_0.gguf",
        sha256="5c3fe0b1408a5ceb0143184ef247b11b579c525f4b02b060e6c851bb76fef1a4",
        min_ram_gb=8.0,
        working_ram_gb=3.5,
        compute_cost=97,
        npu_supported=True,
        igpu_supported=True,
        tags=("高保真", "端侧", "33 语言", "Q8"),
    ),
    ModelSpec(
        id="mt-opus-fast-builtin",
        task="translate",
        name="OPUS-MT · 极速兼容",
        vendor="Helsinki-NLP",
        release="2020",
        description="近乎即时的低资源兜底，适合老旧电脑；长句和口语质量有限。",
        runtime="opus-onnx",
        quality_score=58,
        languages="中文 / 英语",
        license="Apache-2.0",
        installed_bytes=650_000_000,
        install_rel="translate/opus",
        required_paths=("opus_zh_en/encoder_model_int8.onnx",
                        "opus_zh_en/decoder_model_int8.onnx",
                        "opus_zh_en/config.json",
                        "opus_zh_en/tokenizer.json",
                        "opus_en_zh/encoder_model_int8.onnx",
                        "opus_en_zh/decoder_model_int8.onnx",
                        "opus_en_zh/config.json",
                        "opus_en_zh/tokenizer.json"),
        legacy_install_rels=("nmt",),
        sources=(
            ModelSource(
                "global", "Hugging Face 全球源", f"{_HF}/Xenova/opus-mt-zh-en",
                "https://huggingface.co/favicon.ico",
                files=(
                    _opus_remote_files("zh-en", _HF)
                    + _opus_remote_files("en-zh", _HF)
                ),
            ),
            ModelSource(
                "china", "HF 镜像中国源", f"{_HF_MIRROR}/Xenova/opus-mt-zh-en",
                "https://hf-mirror.com/favicon.ico",
                files=(
                    _opus_remote_files("zh-en", _HF_MIRROR)
                    + _opus_remote_files("en-zh", _HF_MIRROR)
                ),
            ),
        ),
        download_bytes=650_000_000,
        builtin=True,
        min_ram_gb=4.0,
        working_ram_gb=0.5,
        compute_cost=10,
        gpu_supported=True,
        npu_supported=False,
        igpu_supported=True,
        tags=("低延迟", "低内存", "内置"),
    ),
)


def get_model(model_id: str) -> ModelSpec | None:
    return next((model for model in CATALOG if model.id == model_id), None)


def models_for_task(task: str | None = None) -> list[ModelSpec]:
    models = [m for m in CATALOG if task in (None, "all", m.task)]
    return sorted(models, key=lambda m: (-m.quality_score, m.download_bytes, m.name))


@dataclass(frozen=True)
class ModelAssessment:
    level: str
    color: str
    load_percent: int
    reason: str


RECOMMENDATION_COLORS = {
    "不推荐": "#4B5563",      # dark gray
    "较为推荐": "#FBBF24",    # yellow
    "推荐": "#34D399",        # green
    "满载": "#F87171",        # red
}


def _capacity(profile: HardwareProfile, model: ModelSpec) -> tuple[float, str]:
    cpu = max(24.0, profile.physical_cores * 12.0 + profile.logical_cores * 2.0)
    if model.runtime == "llama-hy-mt2":
        required_gb = model.installed_bytes / GIB * 1.18 + 0.5
        if (model.gpu_supported and profile.has_discrete_gpu and
                profile.vram_gb >= required_gb):
            return max(cpu, 115.0 + profile.vram_gb * 7.0), "独立显卡"
        bundled_openvino = any(
            runtime.backend == "openvino" for runtime in discover_llama_runtimes())
        ort_openvino = (profile.has_npu_runtime and
                        "openvino" in profile.npu_provider.casefold())
        if (model.npu_supported and (bundled_openvino or ort_openvino) and
                profile.has_llama_npu and
                profile.ram_gb >= required_gb + 4.0):
            return max(cpu, 132.0), "NPU"
        if (model.igpu_supported and profile.has_integrated_gpu and
                profile.ram_gb >= required_gb + 4.0):
            return max(cpu, 82.0), "核显"
        return cpu, "CPU"
    if model.gpu_supported and profile.has_discrete_gpu and profile.gpu_provider:
        return max(cpu, 115.0 + profile.vram_gb * 7.0), "独立显卡"
    if model.npu_supported and profile.has_npu_runtime:
        return max(cpu, 112.0), "NPU"
    if (model.igpu_supported and profile.has_integrated_gpu and
            profile.integrated_gpu_provider):
        return max(cpu, 72.0), "核显"
    return cpu, "CPU"


def assess_model(model: ModelSpec, profile: HardwareProfile,
                 catalog: Iterable[ModelSpec] = CATALOG) -> ModelAssessment:
    usable_ram = max(1.0, profile.ram_gb * 0.72)
    memory_load = model.working_ram_gb / usable_ram * 100.0
    capacity, accelerator = _capacity(profile, model)
    compute_load = model.compute_cost / capacity * 100.0
    load = int(round(min(199.0, max(memory_load, compute_load))))

    if profile.ram_gb + 0.05 < model.min_ram_gb or load > 110:
        reason = (f"{accelerator} 预计负载 {load}% · 至少需要 {model.min_ram_gb:g} GB 内存，"
                  f"当前约 {profile.ram_gb:.1f} GB")
        return ModelAssessment("不推荐", RECOMMENDATION_COLORS["不推荐"], load, reason)
    if load >= 85:
        return ModelAssessment("满载", RECOMMENDATION_COLORS["满载"], load,
                               f"{accelerator} 预计负载 {load}% · 可运行，但会接近当前配置上限")
    better = []
    for candidate in catalog:
        if candidate.task != model.task or candidate.quality_score <= model.quality_score:
            continue
        candidate_load = max(
            candidate.working_ram_gb / usable_ram * 100.0,
            candidate.compute_cost / _capacity(profile, candidate)[0] * 100.0,
        )
        if profile.ram_gb >= candidate.min_ram_gb and candidate_load < 85:
            better.append(candidate)
    quality_gap = (max(m.quality_score for m in better) - model.quality_score
                   if better else 0)
    if quality_gap >= 25:
        best = max(better, key=lambda m: m.quality_score)
        return ModelAssessment(
            "不推荐", RECOMMENDATION_COLORS["不推荐"], load,
            f"{accelerator} 预计负载 {load}% · 模型能力明显偏低，当前配置可流畅运行 {best.name}",
        )
    if load >= 50:
        return ModelAssessment("较为推荐", RECOMMENDATION_COLORS["较为推荐"], load,
                               f"{accelerator} 预计负载 {load}% · 质量较高，但资源占用超过一半")
    if quality_gap >= 4:
        best = max(better, key=lambda m: m.quality_score)
        return ModelAssessment(
            "较为推荐", RECOMMENDATION_COLORS["较为推荐"], load,
            f"{accelerator} 预计负载 {load}% · 当前配置还能流畅运行质量更高的 {best.name}",
        )
    return ModelAssessment("推荐", RECOMMENDATION_COLORS["推荐"], load,
                           f"{accelerator} 预计负载 {load}% · 性能开销与质量较均衡")


def format_bytes(size: int) -> str:
    if size >= GIB:
        return f"{size / GIB:.1f} GB"
    return f"{size / (1024 ** 2):.0f} MB"


class ModelMarketplace:
    """Install, verify and remove exact catalog model directories."""

    def __init__(self, models_dir: Path | str | None = None) -> None:
        self._uses_default_root = models_dir is None
        self.models_dir = Path(models_dir) if models_dir else default_models_dir()
        self._lookup_roots = (
            model_lookup_roots(self.models_dir)
            if self._uses_default_root else (self.models_dir.resolve(),)
        )
        self._downloads = self.models_dir / ".downloads"
        self._state_path = self.models_dir / "catalog_installs.json"

    def model_dir(self, model: ModelSpec) -> Path:
        """Return the canonical destination for new downloads."""
        return self.models_dir / model.install_rel

    def _model_dir_candidates(self, model: ModelSpec) -> tuple[Path, ...]:
        candidates: list[Path] = []
        for root in self._lookup_roots:
            candidates.append(root / model.install_rel)
            candidates.extend(root / rel for rel in model.legacy_install_rels)
        return tuple(candidates)

    def available_model_dir(self, model: ModelSpec) -> Path:
        """Return a complete canonical or legacy directory for ``model``.

        The normalizer upgrades folders at application startup.  This fallback
        makes an interrupted upgrade safe: a model remains visible and usable
        until its old folder can be organized on a later launch.
        """
        for candidate in self._model_dir_candidates(model):
            if not self._missing_paths_at(candidate, model):
                return candidate
        return self.model_dir(model)

    @staticmethod
    def _missing_paths_at(base: Path, model: ModelSpec) -> tuple[str, ...]:
        missing = [rel for rel in model.required_paths
                   if not (base / rel).is_file()]
        missing.extend(
            pattern for pattern in model.required_patterns
            if not any(path.is_file() for path in base.glob(pattern))
        )
        return tuple(missing)

    def missing_paths(self, model: ModelSpec) -> tuple[str, ...]:
        """Return the exact required files/patterns absent from a model."""
        for candidate in self._model_dir_candidates(model):
            missing = self._missing_paths_at(candidate, model)
            if not missing:
                return ()
        return self._missing_paths_at(self.model_dir(model), model)

    def is_installed(self, model: ModelSpec) -> bool:
        return not self.missing_paths(model)

    def model_file(self, model: ModelSpec) -> Path:
        directory = self.available_model_dir(model)
        if not model.required_paths:
            return directory
        return directory / model.required_paths[0]

    @staticmethod
    def _probe(source: ModelSource) -> float:
        started = time.monotonic()
        try:
            req = urlrequest.Request(source.probe_url, method="HEAD",
                                     headers={"User-Agent": "VoxSub/0.3"})
            with urlrequest.urlopen(req, timeout=2.5):
                return time.monotonic() - started
        except Exception:
            return float("inf")

    def ordered_sources(self, model: ModelSpec, preference: str = "auto") -> list[ModelSource]:
        sources = list(model.sources)
        if preference in {"global", "china"}:
            return sorted(sources, key=lambda s: s.id != preference)
        if len(sources) < 2:
            return sources
        timings: dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=len(sources)) as pool:
            futures = {pool.submit(self._probe, source): source for source in sources}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    timings[source.id] = future.result()
                except Exception:
                    timings[source.id] = float("inf")
        ordered = sorted(sources, key=lambda source: (timings.get(source.id, float("inf")),
                                                       source.id != "global"))
        logger.info("模型下载源自动测速: model=%s timings=%s selected=%s",
                    model.id, timings, ordered[0].id if ordered else "none")
        return ordered

    def install(self, model: ModelSpec, preference: str = "auto",
                progress: Callable[[int, int, str], None] | None = None,
                cancelled: Callable[[], bool] | None = None) -> Path:
        missing = self.missing_paths(model)
        if not missing:
            return self.available_model_dir(model)
        sources = self.ordered_sources(model, preference)
        if not sources:
            if model.builtin:
                raise RuntimeError(
                    "内置模型缺少文件：" + ", ".join(missing) +
                    "；没有可用在线修复源，请通过最新安装包修复。"
                )
            raise RuntimeError("该模型没有可用下载源")

        self._downloads.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        for source in sources:
            try:
                target = self._install_from_source(
                    model, source, progress=progress, cancelled=cancelled)
                remaining = self.missing_paths(model)
                if remaining:
                    raise RuntimeError("下载完成但仍缺少：" + ", ".join(remaining))
                self._record(model, source.id)
                logger.info("模型安装完成: id=%s source=%s path=%s",
                            model.id, source.id, target)
                return target
            except DownloadCancelled:
                raise
            except Exception as exc:
                errors.append(f"{source.label}: {exc}")
                logger.warning("模型源安装失败，尝试下一源: model=%s source=%s error=%s",
                               model.id, source.id, exc)
        prefix = "内置模型在线修复失败，仍缺少：" if model.builtin else "所有下载源均失败："
        detail = ", ".join(self.missing_paths(model))
        fallback = "；请通过最新安装包修复或查看日志" if model.builtin else ""
        raise RuntimeError(prefix + detail + "。" + "；".join(errors) + fallback)

    def _install_from_source(self, model: ModelSpec, source: ModelSource,
                             progress: Callable[[int, int, str], None] | None,
                             cancelled: Callable[[], bool] | None) -> Path:
        if source.files:
            return self._install_remote_files(model, source, progress, cancelled)

        download = self._downloads / model.asset_name

        def _progress(done: int, total: int, _source_url: str) -> None:
            if progress:
                progress(done, total or model.download_bytes, source.label)

        ok = fetch_file(source.url, download, expected_sha=model.sha256 or None,
                        expected_size=model.download_bytes or None,
                        progress=_progress, cancelled=cancelled)
        if not ok:
            raise RuntimeError("下载失败")
        if cancelled and cancelled():
            raise DownloadCancelled("下载已取消")

        target = self.model_dir(model)
        if model.archive:
            self._install_archive(model, download, target)
            download.unlink(missing_ok=True)
        else:
            target.mkdir(parents=True, exist_ok=True)
            final = target / model.asset_name
            if final.exists():
                final.unlink()
            download.replace(final)
        return target

    def _install_remote_files(self, model: ModelSpec, source: ModelSource,
                              progress: Callable[[int, int, str], None] | None,
                              cancelled: Callable[[], bool] | None) -> Path:
        download_root = self._downloads / model.id
        total = sum(item.size for item in source.files) or model.download_bytes
        completed = 0
        for item in source.files:
            destination = download_root / item.install_rel

            target_file = self.model_dir(model) / item.install_rel
            if target_file.is_file() and (
                not item.sha256 or sha256_of(target_file) == item.sha256
            ):
                completed += item.size or target_file.stat().st_size
                if progress:
                    progress(completed, total, source.label)
                continue
            if destination.is_file() and (
                not item.sha256 or sha256_of(destination) == item.sha256
            ):
                completed += item.size or destination.stat().st_size
                if progress:
                    progress(completed, total, source.label)
                continue

            def _progress(done: int, _file_total: int, _url: str,
                          base: int = completed) -> None:
                if progress:
                    progress(base + done, total, source.label)

            ok = fetch_file(item.url, destination, expected_sha=item.sha256 or None,
                            expected_size=item.size or None,
                            progress=_progress, cancelled=cancelled)
            if not ok:
                raise RuntimeError(f"文件下载失败: {item.install_rel}")
            completed += item.size or destination.stat().st_size

        target = self.model_dir(model)
        target.parent.mkdir(parents=True, exist_ok=True)
        for item in source.files:
            staged = download_root / item.install_rel
            if not staged.is_file():
                continue
            destination = target / item.install_rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged, destination)
        if download_root.exists():
            shutil.rmtree(download_root)
        return target

    def _install_archive(self, model: ModelSpec, archive: Path, target: Path) -> None:
        staging_parent = self.models_dir / ".installing"
        staging_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{model.id}-", dir=staging_parent) as raw:
            staging = Path(raw)
            with tarfile.open(archive, "r:bz2") as tf:
                members = tf.getmembers()
                root = staging.resolve()
                for member in members:
                    destination = (staging / member.name).resolve()
                    if root not in destination.parents and destination != root:
                        raise RuntimeError("模型压缩包包含越界路径，已拒绝安装")
                    if member.issym() or member.islnk():
                        raise RuntimeError("模型压缩包包含链接，已拒绝安装")
                tf.extractall(staging, members=members)

            candidates = [staging]
            candidates.extend(path for path in staging.iterdir() if path.is_dir())
            source_root = next(
                (path for path in candidates
                 if not self._missing_paths_at(path, model)), None)
            if source_root is None:
                raise RuntimeError("模型压缩包结构与目录清单不匹配")
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_root, target)

    def uninstall(self, model: ModelSpec, *, in_use: bool = False) -> None:
        if model.builtin:
            raise RuntimeError("内置兼容模型随应用管理，不能在模型广场卸载")
        if in_use:
            raise RuntimeError("该模型正在使用；请先切换到其他模型")
        roots = {root.resolve() for root in self._lookup_roots}
        target = self.available_model_dir(model).resolve()
        if not any(root in target.parents and target != root for root in roots):
            raise RuntimeError("拒绝卸载模型目录之外的路径")
        expected = {path.resolve() for path in self._model_dir_candidates(model)}
        if target not in expected:
            raise RuntimeError("模型卸载目标与目录清单不一致")
        if target.exists():
            shutil.rmtree(target)
        part = self._downloads / f"{model.asset_name}.part"
        part.unlink(missing_ok=True)
        download_root = self._downloads.resolve()
        staged = (self._downloads / model.id).resolve()
        if download_root in staged.parents and staged.exists():
            shutil.rmtree(staged)
        self._remove_record(model.id)
        logger.info("模型已卸载: id=%s path=%s", model.id, target)

    def _read_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"version": 1, "models": {}}
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "models": {}}

    def _write_state(self, state: dict) -> None:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    def _record(self, model: ModelSpec, source_id: str) -> None:
        state = self._read_state()
        state.setdefault("models", {})[model.id] = {
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": source_id,
            "catalog_updated": CATALOG_UPDATED,
        }
        self._write_state(state)

    def _remove_record(self, model_id: str) -> None:
        state = self._read_state()
        state.setdefault("models", {}).pop(model_id, None)
        self._write_state(state)


__all__ = [
    "CATALOG", "CATALOG_UPDATED", "HardwareProfile", "ModelAssessment",
    "ModelMarketplace", "ModelSource", "ModelSpec", "RemoteFile", "RECOMMENDATION_COLORS",
    "assess_model", "default_models_dir", "detect_hardware", "format_bytes",
    "get_model", "models_for_task",
]
