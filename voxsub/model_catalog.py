"""Curated model catalog, hardware assessment, and local installation service.

The catalog is deliberately small.  A model is listed only when VoxSub has a
runtime adapter for it; downloading a weight that the application cannot use is
treated as a product bug, not as a marketplace feature.
"""
from __future__ import annotations

import json
import hashlib
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
from urllib.parse import quote

from voxsub.catalog_assets import (
    KOKORO_FILES, KOKORO_REPO, KOKORO_REVISION,
    PARAKEET_FILES, PARAKEET_REPO, PARAKEET_REVISION,
    SENSEVOICE_FILES, SENSEVOICE_CHINA_REPO, SENSEVOICE_CHINA_REVISION,
)
from voxsub.logging_setup import get_logger
from voxsub.file_io import (
    copy_file_atomically,
    replace_with_retry,
    write_text_atomically,
)
from voxsub.hardware import HardwareProfile, detect_hardware, discover_llama_runtimes
from voxsub.model_storage import model_lookup_roots, resolve_models_root
from voxsub.models import DownloadCancelled, fetch_file, sha256_of

logger = get_logger("model_catalog")

GIB = 1024 ** 3
CATALOG_UPDATED = "2026-10-05"


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
    task: str                  # asr | translate | tts | ocr
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
    tts_languages: tuple[str, ...] = ()
    tts_speaker_ids: tuple[tuple[str, int], ...] = ()
    official_repo: str = ""

    @property
    def task_label(self) -> str:
        return {
            "asr": "语音识别",
            "speech": "语音翻译",
            "translate": "字幕翻译",
            "tts": "语音朗读",
            "ocr": "图片文字识别",
        }.get(self.task, self.task)


_GH_ASR = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
_GH_TTS = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"
_MS_ASR = "https://modelscope.cn/models/csukuangfj/asr-models/resolve/master"
# Keep this filename in one place: the upstream release asset includes the
# ``small-bilingual`` segment.  The older, shorter name returns HTTP 404.
_ZIPFORMER_ASSET = "sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16.tar.bz2"
_HF = "https://huggingface.co"
_MS = "https://modelscope.cn"
_HF_MIRROR = "https://hf-mirror.com"
_MS_OCR = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx"


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


def _pinned_mirror_source(repo: str, revision: str,
                          files: tuple[tuple[str, int, str], ...]) -> ModelSource:
    """China option is explicitly a third-party HF mirror, pinned and verified."""
    prefix = f"{_HF_MIRROR}/{repo}/resolve/{revision}"
    return ModelSource(
        "china", "中国大陆 · HF 第三方镜像（校验固定版本）", prefix,
        f"{prefix}/tokens.txt",
        tuple(RemoteFile(f"{prefix}/{quote(path, safe='/')}", path, size, sha)
              for path, size, sha in files),
    )


CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="asr-moonshine-tiny-en-v2", task="asr",
        name="Moonshine Tiny EN · 轻量离线", vendor="Useful Sensors / k2-fsa",
        release="2026-02-27",
        description="把英语语音转成文字，体积小、占用少，适合日常短句和轻薄本；说完一句后出结果，不支持中文。",
        runtime="sherpa-moonshine-v2", quality_score=77, languages="英语", license="MIT",
        download_bytes=29_858_559, installed_bytes=44_243_206,
        install_rel="asr/moonshine-tiny-en-v2",
        required_paths=("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt"),
        sources=(
            ModelSource("global", "海外 · GitHub 上游发布", f"{_GH_ASR}/sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27.tar.bz2", "https://github.com/favicon.ico"),
            ModelSource("china", "中国大陆 · ModelScope 上游镜像", f"{_MS_ASR}/sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27.tar.bz2", "https://modelscope.cn/favicon.ico"),
        ),
        asset_name="sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27.tar.bz2",
        sha256="9ec31b342d8fa3240c3b81b8f82e1cf7e3ac467c93ca5a999b741d5887164f8d",
        archive=True, min_ram_gb=4, working_ram_gb=0.5, compute_cost=16,
        gpu_supported=False, tags=('轻量省资源', '短句识别', '离线使用'),
        official_repo="https://github.com/moonshine-ai/moonshine",
    ),
    ModelSpec(
        id="asr-parakeet-tdt-0.6b-v3-int8", task="asr",
        name="Parakeet TDT 0.6B v3 INT8 · 欧洲多语", vendor="NVIDIA / k2-fsa",
        release="v3（上游导出）",
        description="适合英语及欧洲多语会议、课程录音，覆盖 25 种语言；说完一句后出结果，不支持中文。",
        runtime="sherpa-parakeet-tdt", quality_score=94,
        languages="英语 / 德语 / 法语 / 西班牙语等 25 种欧洲语言（不含中文）",
        license="CC-BY-4.0", download_bytes=487_170_055,
        installed_bytes=sum(item[1] for item in PARAKEET_FILES),
        install_rel="asr/parakeet-tdt-0.6b-v3-int8",
        required_paths=tuple(item[0] for item in PARAKEET_FILES),
        sources=(
            ModelSource("global", "海外 · GitHub 上游发布", f"{_GH_ASR}/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2", "https://github.com/favicon.ico"),
            _pinned_mirror_source(PARAKEET_REPO, PARAKEET_REVISION, PARAKEET_FILES),
        ),
        asset_name="sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
        sha256="5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf",
        archive=True, min_ram_gb=8, working_ram_gb=2, compute_cost=55,
        gpu_supported=False, tags=('欧洲多语', '不含中文', '离线使用'),
        official_repo="https://github.com/NVIDIA-NeMo/Speech",
    ),
    ModelSpec(
        id="tts-kokoro-v1.1-int8-zh-en", task="tts",
        name="Kokoro v1.1 INT8 · 中英朗读", vendor="hexgrad / k2-fsa",
        release="v1.1（上游导出）",
        description="把中文或英文译文读出来，音色自然，可选择多种声音，适合听译文和日常朗读。",
        runtime="sherpa-kokoro", quality_score=95, languages="中文 / 英语",
        license="Apache-2.0", download_bytes=147_031_220,
        installed_bytes=sum(item[1] for item in KOKORO_FILES),
        install_rel="tts/kokoro-v1.1-int8-zh-en",
        required_paths=tuple(item[0] for item in KOKORO_FILES),
        sources=(
            ModelSource("global", "海外 · GitHub 上游发布", f"{_GH_TTS}/kokoro-int8-multi-lang-v1_1.tar.bz2", "https://github.com/favicon.ico"),
            _pinned_mirror_source(KOKORO_REPO, KOKORO_REVISION, KOKORO_FILES),
        ),
        asset_name="kokoro-int8-multi-lang-v1_1.tar.bz2",
        sha256="a1e94694776049035c4f2c6529f003aaece993c76aae9a78995831c3c4dcafc6",
        archive=True, min_ram_gb=4, working_ram_gb=1, compute_cost=35,
        gpu_supported=False, tags=('自然音色', '多种声音', '离线使用'),
        official_repo="https://github.com/hexgrad/kokoro",
        tts_languages=("zh", "en"), tts_speaker_ids=(("zh", 3), ("en", 0)),
    ),

    ModelSpec(
        id="ocr-rapidocr-v6-small-builtin",
        task="ocr",
        name="PP-OCRv6 Small · 通用内置",
        vendor="PaddleOCR / RapidAI",
        release="2026-08-20",
        description="识别屏幕、网页和图片里的文字，兼顾速度与清晰度，适合日常屏幕翻译。",
        runtime="rapidocr-v6-small",
        quality_score=88,
        languages="中文 / 英语 / 多语言",
        license="Apache-2.0",
        download_bytes=0,
        installed_bytes=31_750_000,
        install_rel="ocr/rapidocr-v6-small-builtin",
        required_paths=(),
        builtin=True,
        min_ram_gb=4.0,
        working_ram_gb=0.45,
        compute_cost=28,
        gpu_supported=True,
        tags=('应用内置', '均衡', '多语言'),
        official_repo="https://github.com/PaddlePaddle/PaddleOCR",
    ),
    ModelSpec(
        id="ocr-rapidocr-v6-tiny",
        task="ocr",
        name="PP-OCRv6 Tiny · 实时低延迟",
        vendor="PaddleOCR / RapidAI",
        release="2026-08-20",
        description="快速读出屏幕和图片里的文字，占用少，适合轻薄本和频繁变化的字幕画面。",
        runtime="rapidocr-v6-tiny",
        quality_score=78,
        languages="中文 / 英语 / 多语言",
        license="Apache-2.0",
        download_bytes=16_000_000,
        installed_bytes=16_000_000,
        install_rel="ocr/rapidocr-v6-tiny",
        required_paths=("det.onnx", "rec.onnx"),
        sources=(ModelSource(
            "china", "ModelScope 官方源", f"{_MS_OCR}/PP-OCRv6",
            "https://modelscope.cn/favicon.ico",
            files=(
                RemoteFile(f"{_MS_OCR}/PP-OCRv6/det/PP-OCRv6_det_tiny.onnx",
                           "det.onnx", 0,
                           "f42c0fbd294d95eac1a550e131b277dac97462c8025fa4b6c3cec1b7894bd3d5"),
                RemoteFile(f"{_MS_OCR}/PP-OCRv6/rec/PP-OCRv6_rec_tiny.onnx",
                           "rec.onnx", 0,
                           "e16e242de5937ad92609223f19bc2aff3727ee40b095f996907c24749bad251b"),
            ),
        ),),
        min_ram_gb=4.0,
        working_ram_gb=0.25,
        compute_cost=15,
        gpu_supported=True,
        tags=('响应快', '省内存', '多语言'),
        official_repo="https://github.com/PaddlePaddle/PaddleOCR",
    ),
    ModelSpec(
        id="ocr-rapidocr-v6-medium",
        task="ocr",
        name="PP-OCRv6 Medium · 多语言高精度",
        vendor="PaddleOCR / RapidAI",
        release="2026-08-20",
        description="更擅长识别小字和复杂排版，适合网页、文档及图片翻译；电脑负担比轻量款更高。",
        runtime="rapidocr-v6-medium",
        quality_score=96,
        languages="中文 / 英语 / 多语言",
        license="Apache-2.0",
        download_bytes=95_000_000,
        installed_bytes=95_000_000,
        install_rel="ocr/rapidocr-v6-medium",
        required_paths=("det.onnx", "rec.onnx"),
        sources=(ModelSource(
            "china", "ModelScope 官方源", f"{_MS_OCR}/PP-OCRv6",
            "https://modelscope.cn/favicon.ico",
            files=(
                RemoteFile(f"{_MS_OCR}/PP-OCRv6/det/PP-OCRv6_det_medium.onnx",
                           "det.onnx", 0,
                           "92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2"),
                RemoteFile(f"{_MS_OCR}/PP-OCRv6/rec/PP-OCRv6_rec_medium.onnx",
                           "rec.onnx", 0,
                           "eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b"),
            ),
        ),),
        min_ram_gb=6.0,
        working_ram_gb=0.9,
        compute_cost=58,
        gpu_supported=True,
        tags=('细小文字', '复杂排版', '多语言'),
        official_repo="https://github.com/PaddlePaddle/PaddleOCR",
    ),
    ModelSpec(
        id="ocr-rapidocr-v5-document",
        task="ocr",
        name="PP-OCRv5 Server · 文档与手写增强",
        vendor="PaddleOCR / RapidAI",
        release="2025-05-20",
        description="适合中英文文档、竖排文字和手写内容；电脑负担较高，花体字的效果取决于图片清晰度。",
        runtime="rapidocr-v5-server",
        quality_score=94,
        languages="简体中文 / 英语",
        license="Apache-2.0",
        download_bytes=180_000_000,
        installed_bytes=180_000_000,
        install_rel="ocr/rapidocr-v5-document",
        required_paths=("det.onnx", "rec.onnx", "cls.onnx"),
        sources=(ModelSource(
            "china", "ModelScope 官方源", f"{_MS_OCR}/PP-OCRv5",
            "https://modelscope.cn/favicon.ico",
            files=(
                RemoteFile(f"{_MS_OCR}/PP-OCRv5/det/ch_PP-OCRv5_det_server.onnx",
                           "det.onnx", 0,
                           "0f8846b1d4bba223a2a2f9d9b44022fbc22cc019051a602b41a7fda9667e4cad"),
                RemoteFile(f"{_MS_OCR}/PP-OCRv5/rec/ch_PP-OCRv5_rec_server.onnx",
                           "rec.onnx", 0,
                           "e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a"),
                RemoteFile(
                    f"{_MS_OCR}/PP-OCRv5/cls/ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx",
                    "cls.onnx", 0,
                    "54379ae5174d026780215fc748a7f31910dee36818e63d49e17dc598ecc82df7"),
            ),
        ),),
        min_ram_gb=8.0,
        working_ram_gb=1.4,
        compute_cost=76,
        gpu_supported=True,
        tags=('手写文字', '竖排文字', '复杂文档'),
        official_repo="https://github.com/PaddlePaddle/PaddleOCR",
    ),
    ModelSpec(
        id="asr-funasr-nano-2512-int8",
        task="asr",
        name="Fun-ASR-Nano 2512 · INT8",
        vendor="FunAudioLLM / sherpa-onnx",
        release="2025-12-30",
        description="适合中英日语音，也能处理方言、口音和较嘈杂的环境，兼顾日常对话与歌曲识别。",
        runtime="sherpa-funasr-nano",
        quality_score=98,
        languages="中文 / 英语 / 日语 · 7 种中文方言",
        license="Apache-2.0",
        download_bytes=841_730_611,
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
        sha256="eb43d7ccc2e86b243f6a03b7df361033dda66db9523d1a92bf6aca2b50c9476b",
        archive=True,
        min_ram_gb=8.0,
        working_ram_gb=2.2,
        compute_cost=82,
        gpu_supported=False,
        tags=('中文优先', '方言口音', '嘈杂环境'),
        official_repo="https://github.com/QwenAudio/Fun-ASR",
    ),
    ModelSpec(
        id="asr-qwen3-0.6b-int8",
        task="asr",
        name="Qwen3-ASR 0.6B · INT8",
        vendor="Qwen / sherpa-onnx",
        release="2026-03-25",
        description="适合跨语言会议与中英混合对话，覆盖 30 种语言和多种中文方言，电脑负担高于轻量款。",
        runtime="sherpa-qwen3-asr",
        quality_score=96,
        languages="30 种语言 · 22 种中文方言",
        license="Apache-2.0",
        download_bytes=878_702_423,
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
        sha256="393f8a14e2f5fb96746aaab342997a40641001fbd5bf9592a080a8329178ee96",
        archive=True,
        min_ram_gb=8.0,
        working_ram_gb=2.0,
        compute_cost=74,
        gpu_supported=False,
        tags=('多种语言', '方言口音', '中英混说'),
        official_repo="https://github.com/QwenLM/Qwen3-ASR",
    ),
    ModelSpec(
        id="asr-sensevoice-small-int8",
        task="asr",
        name="SenseVoice Small · INT8",
        vendor="FunAudioLLM / sherpa-onnx",
        release="2024-07-17",
        description="支持中文、粤语、英语、日语和韩语，占用较少，适合日常对话和轻薄本。",
        runtime="sherpa-sense-voice",
        quality_score=88,
        languages="中文 / 粤语 / 英语 / 日语 / 韩语",
        license="Apache-2.0",
        download_bytes=163_002_883,
        installed_bytes=270_000_000,
        install_rel="stt/sensevoice-small-int8",
        legacy_install_rels=("marketplace/asr-sensevoice-small-int8",),
        required_paths=("model.int8.onnx", "tokens.txt"),
        sources=(
            ModelSource("global", "GitHub 全球源",
                        f"{_GH_ASR}/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
                        "https://github.com/favicon.ico"),
            ModelSource("china", "ModelScope 中国源",
                        f"{_MS}/models/{SENSEVOICE_CHINA_REPO}",
                        f"{_MS}/models/{SENSEVOICE_CHINA_REPO}/resolve/{SENSEVOICE_CHINA_REVISION}/tokens.txt",
                        files=tuple(RemoteFile(
                            f"{_MS}/models/{SENSEVOICE_CHINA_REPO}/resolve/{SENSEVOICE_CHINA_REVISION}/{path}",
                            path, size, digest) for path, size, digest in SENSEVOICE_FILES)),
        ),
        asset_name="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        sha256="7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e",
        archive=True,
        min_ram_gb=4.0,
        working_ram_gb=0.85,
        compute_cost=38,
        tags=('粤语识别', '响应快', '省资源'),
        official_repo="https://github.com/QwenAudio/SenseVoice",
    ),
    ModelSpec(
        id="asr-zipformer-bilingual-fast",
        task="asr",
        name="Zipformer 中英双语 · 极速兼容",
        vendor="k2-fsa",
        release="2023-02-16",
        description="边说边识别中英文，响应快、占用少，适合老电脑；复杂表达的识别能力不如较大的模型。",
        runtime="sherpa-streaming-transducer",
        quality_score=72,
        languages="中文 / 英语",
        license="Apache-2.0",
        download_bytes=458_187_351,
        installed_bytes=150_000_000,
        install_rel="stt/zipformer",
        required_paths=("tokens.txt",),
        required_patterns=("*encoder*.onnx", "*decoder*.onnx", "*joiner*.onnx"),
        legacy_install_rels=("asr",),
        sources=(
            ModelSource(
                "global", "GitHub 全球源",
                f"{_GH_ASR}/{_ZIPFORMER_ASSET}",
                "https://github.com/favicon.ico",
            ),
        ),
        asset_name=_ZIPFORMER_ASSET,
        archive=True,
        builtin=True,
        min_ram_gb=4.0,
        working_ram_gb=0.45,
        compute_cost=18,
        gpu_supported=False,
        tags=('边说边识别', '省资源', '中英双语'),
        official_repo="https://github.com/k2-fsa/icefall",
    ),
    ModelSpec(
        id="mt-hy-mt2-7b-q4",
        task="translate",
        name="Hy-MT2 7B · Q4_K_M",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="更擅长长句、专业术语和复杂上下文翻译，适合重视译文质量、配置较好的电脑。",
        runtime="llama-hy-mt2",
        quality_score=99,
        languages="多语互译 · 含粤语与繁体中文",
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
        tags=('复杂语境', '专业术语', '质量优先'),
        official_repo="https://github.com/Tencent-Hunyuan/HY-MT",
    ),
    ModelSpec(
        id="mt-hy-mt2-7b-q6",
        task="translate",
        name="Hy-MT2 7B · Q6_K",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="更擅长复杂语境和专业术语，保留更多细节，适合配置较好的电脑；占用比同系列轻量档更高。",
        runtime="llama-hy-mt2",
        quality_score=100,
        languages="多语互译 · 含粤语与繁体中文",
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
        tags=('复杂语境', '专业术语', '细节优先'),
        official_repo="https://github.com/Tencent-Hunyuan/HY-MT",
    ),
    ModelSpec(
        id="mt-hy-mt2-7b-q8",
        task="translate",
        name="Hy-MT2 7B · Q8_0",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="着重保留复杂句和术语的表达细节，适合对翻译质量要求较高、内存充裕的电脑。",
        runtime="llama-hy-mt2",
        quality_score=100,
        languages="多语互译 · 含粤语与繁体中文",
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
        tags=('复杂语境', '专业术语', '细节优先'),
        official_repo="https://github.com/Tencent-Hunyuan/HY-MT",
    ),
    ModelSpec(
        id="mt-hy-mt2-1.8b-q4",
        task="translate",
        name="Hy-MT2 1.8B · Q4_K_M",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="适合日常对话和字幕翻译，在译文质量与电脑负担之间取得平衡，支持多种语言互译。",
        runtime="llama-hy-mt2",
        quality_score=95,
        languages="多语互译 · 含粤语与繁体中文",
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
        tags=('日常翻译', '负担适中', '多语言'),
        official_repo="https://github.com/Tencent-Hunyuan/HY-MT",
    ),
    ModelSpec(
        id="mt-hy-mt2-1.8b-q6",
        task="translate",
        name="Hy-MT2 1.8B · Q6_K",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="适合日常翻译，也更注重表达细节；比同系列轻量档多占一些内存，支持多种语言互译。",
        runtime="llama-hy-mt2",
        quality_score=96,
        languages="多语互译 · 含粤语与繁体中文",
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
        tags=('日常翻译', '细节优先', '多语言'),
        official_repo="https://github.com/Tencent-Hunyuan/HY-MT",
    ),
    ModelSpec(
        id="mt-hy-mt2-1.8b-q8",
        task="translate",
        name="Hy-MT2 1.8B · Q8_0",
        vendor="Tencent Hunyuan",
        release="2026-05-21",
        description="在较小体积里尽量保留翻译细节，适合不想运行大型模型、但内存比较充裕的电脑。",
        runtime="llama-hy-mt2",
        quality_score=97,
        languages="多语互译 · 含粤语与繁体中文",
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
        tags=('日常翻译', '细节优先', '多语言'),
        official_repo="https://github.com/Tencent-Hunyuan/HY-MT",
    ),
    ModelSpec(
        id="tts-melo-zh-en",
        task="tts",
        name="MeloTTS 中英双语 · 自然音色",
        vendor="MyShell AI / sherpa-onnx",
        release="2024-11-03",
        description="自然地读出中文、英文或中英混合译文，适合日常朗读；体积和电脑负担比轻量款更高。",
        runtime="sherpa-vits",
        quality_score=94,
        languages="中文 / 英语 / 中英混读",
        license="MIT",
        download_bytes=167_006_755,
        installed_bytes=180_000_000,
        install_rel="tts/vits-melo-zh-en",
        required_paths=("model.onnx", "tokens.txt", "lexicon.txt"),
        sources=(
            ModelSource(
                "global", "GitHub 全球源",
                f"{_GH_TTS}/vits-melo-tts-zh_en.tar.bz2",
                "https://github.com/favicon.ico",
            ),
        ),
        asset_name="vits-melo-tts-zh_en.tar.bz2",
        archive=True,
        min_ram_gb=4.0,
        working_ram_gb=0.8,
        compute_cost=48,
        gpu_supported=False,
        tags=('自然音色', '中英混读', '离线使用'),
        official_repo="https://github.com/myshell-ai/MeloTTS",
        tts_languages=("zh", "en"),
    ),
    ModelSpec(
        id="tts-icefall-zh-aishell3",
        task="tts",
        name="Icefall AISHELL3 · 中文轻量",
        vendor="k2-fsa / AISHELL",
        release="2024-04-08",
        description="快速读出中文译文，占用少，适合实时朗读和配置较低的电脑。",
        runtime="sherpa-vits",
        quality_score=84,
        languages="中文",
        license="Apache-2.0（模型）",
        download_bytes=31_559_701,
        installed_bytes=214_000_000,
        install_rel="tts/vits-icefall-zh-aishell3",
        legacy_install_rels=("tts/zh",),
        required_paths=("model.onnx", "tokens.txt", "lexicon.txt"),
        sources=(
            ModelSource(
                "global", "GitHub 全球源",
                f"{_GH_TTS}/vits-icefall-zh-aishell3.tar.bz2",
                "https://github.com/favicon.ico",
            ),
        ),
        asset_name="vits-icefall-zh-aishell3.tar.bz2",
        sha256="ab468db3a3308cdd861495e0db2f25d79418a0c00639f74944c7cdf5dd8c6ec1",
        archive=True,
        min_ram_gb=4.0,
        working_ram_gb=0.35,
        compute_cost=18,
        gpu_supported=False,
        tags=('响应快', '省资源', '离线使用'),
        official_repo="https://github.com/k2-fsa/icefall",
        tts_languages=("zh",),
    ),
    ModelSpec(
        id="tts-icefall-en-ljspeech-low",
        task="tts",
        name="Icefall LJSpeech · 英文轻量",
        vendor="k2-fsa / LJ Speech",
        release="2026-07-13",
        description="用美式英文女声读出译文，占用少，适合实时朗读和配置较低的电脑。",
        runtime="sherpa-vits",
        quality_score=80,
        languages="英语（美国）",
        license="Public Domain（数据）",
        download_bytes=31_722_013,
        installed_bytes=45_000_000,
        install_rel="tts/vits-icefall-en-ljspeech-low",
        legacy_install_rels=("tts/en",),
        required_paths=("model.onnx", "tokens.txt", "espeak-ng-data/phontab"),
        sources=(
            ModelSource(
                "global", "GitHub 全球源",
                f"{_GH_TTS}/vits-icefall-en_US-ljspeech-low.tar.bz2",
                "https://github.com/favicon.ico",
            ),
        ),
        asset_name="vits-icefall-en_US-ljspeech-low.tar.bz2",
        sha256="c5115bc85775ed15bf1121057055ed35d5d17c9b83be7769c23a2402fc2d4c74",
        archive=True,
        min_ram_gb=4.0,
        working_ram_gb=0.3,
        compute_cost=16,
        gpu_supported=False,
        tags=('美式女声', '省资源', '离线使用'),
        official_repo="https://github.com/k2-fsa/icefall",
        tts_languages=("en",),
    ),
    ModelSpec(
        id="mt-opus-fast-builtin",
        task="translate",
        name="OPUS-MT · 极速兼容",
        vendor="Helsinki-NLP",
        release="2020",
        description="快速翻译中英文，占用少，适合老电脑和简单短句；长句与口语表达的译文质量比较有限。",
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
        tags=('短句翻译', '响应快', '省资源'),
        official_repo="https://github.com/Helsinki-NLP/Opus-MT",
    ),
)


from voxsub.speech_catalog import speech_catalog

CATALOG += speech_catalog()


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


def _llama_capacity(profile: HardwareProfile, model: ModelSpec,
                    cpu: float) -> tuple[float, str]:
    required_gb = model.installed_bytes / GIB * 1.18 + 0.5
    if (model.gpu_supported and profile.has_discrete_gpu and
            profile.vram_gb >= required_gb):
        return max(cpu, 115.0 + profile.vram_gb * 7.0), "独立显卡"
    bundled_openvino = any(
        runtime.backend == "openvino" for runtime in discover_llama_runtimes())
    ort_openvino = (profile.has_npu_runtime and
                    "openvino" in profile.npu_provider.casefold())
    if (model.npu_supported and (bundled_openvino or ort_openvino) and
            profile.has_llama_npu and profile.ram_gb >= required_gb + 4.0):
        return max(cpu, 132.0), "NPU"
    if (model.igpu_supported and profile.has_integrated_gpu and
            profile.ram_gb >= required_gb + 4.0):
        return max(cpu, 82.0), "核显"
    return cpu, "CPU"


def _capacity(profile: HardwareProfile, model: ModelSpec) -> tuple[float, str]:
    cpu = max(24.0, profile.physical_cores * 12.0 + profile.logical_cores * 2.0)
    if model.runtime == "llama-hy-mt2":
        return _llama_capacity(profile, model, cpu)
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
        self._integrity_cache: dict[Path, tuple[int, int, bool]] = {}

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
    def _sha256_matches(path: Path, expected: str) -> bool:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return False
        return digest.hexdigest().lower() == expected

    def _single_file_valid(self, path: Path, model: ModelSpec) -> bool:
        """Validate metadata for a non-archive single-file model."""
        if not path.is_file():
            return False
        expected_size = int(model.download_bytes or 0)
        expected_sha = str(model.sha256 or "").strip().lower()
        if expected_size <= 0 and not expected_sha:
            return True
        try:
            stat = path.stat()
        except OSError:
            return False
        if expected_size > 0 and stat.st_size != expected_size:
            logger.warning("模型文件大小不一致: model=%s expected=%d actual=%d",
                           model.id, expected_size, stat.st_size)
            return False
        key = path.resolve()
        cached = self._integrity_cache.get(key)
        if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns):
            return cached[2]
        valid = self._sha256_matches(path, expected_sha) if expected_sha else True
        if expected_sha and not valid:
            logger.warning("模型文件 SHA256 不一致: model=%s expected=%s",
                           model.id, expected_sha[:12])
        self._integrity_cache[key] = (stat.st_size, stat.st_mtime_ns, valid)
        return valid

    def _missing_paths_at(self, base: Path, model: ModelSpec) -> tuple[str, ...]:
        missing: list[str] = []
        sizes = {item.install_rel: item.size for source in model.sources[:1] for item in source.files}
        for rel in model.required_paths:
            path = base / rel
            valid = path.is_file()
            if valid and model.task == "speech":
                valid = path.stat().st_size == sizes.get(rel, path.stat().st_size)
            if valid and not model.archive and len(model.required_paths) == 1:
                valid = self._single_file_valid(path, model)
            if not valid:
                missing.append(rel)
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

    def _source_has_partial(self, model: ModelSpec, source: ModelSource) -> bool:
        assets = [(self._downloads / model.asset_name, source.url)]
        if source.files:
            assets = [(self._downloads / model.id / item.install_rel, item.url) for item in source.files]
        for destination, url in assets:
            part = destination.with_name(destination.name + ".part")
            metadata = part.with_name(part.name + ".resume.json")
            if self._partial_source_matches(part, metadata, url):
                return True
        return False

    @staticmethod
    def _partial_source_matches(part: Path, metadata: Path, url: str) -> bool:
        try:
            if not part.is_file() or not part.stat().st_size or metadata.stat().st_size > 16 * 1024:
                return False
            data = json.loads(metadata.read_text(encoding="utf-8"))
            return data.get("source") == hashlib.sha256(url.encode()).hexdigest()
        except (OSError, ValueError, AttributeError):
            return False

    def ordered_sources(self, model: ModelSpec, preference: str = "auto") -> list[ModelSource]:
        sources = list(model.sources)
        if preference in {"global", "china"}:
            return sorted(sources, key=lambda s: s.id != preference)
        if len(sources) < 2:
            return sources
        # Auto resumes keep the previous proven origin. A new latency probe must not
        # gratuitously switch mirrors and discard a valid no-SHA partial prefix.
        resume = next((source for source in sources if self._source_has_partial(model, source)), None)
        if resume is not None:
            logger.info("续传优先使用上次下载源: model=%s source=%s", model.id, resume.id)
            return [resume, *(source for source in sources if source is not resume)]
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
                cancelled: Callable[[], bool] | None = None,
                force: bool = False) -> Path:
        """Install a model; ``force`` revalidates every remote asset."""
        if cancelled and cancelled():
            raise DownloadCancelled("下载已暂停")
        missing = self.missing_paths(model)
        if not missing and not force:
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
            if cancelled and cancelled():
                raise DownloadCancelled("下载已暂停")
            try:
                self._notify_source(model, source, progress)
                target = self._install_from_source(
                    model, source, progress=progress, cancelled=cancelled,
                    force=force)
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

    @staticmethod
    def _notify_source(model: ModelSpec, source: ModelSource,
                       progress: Callable[[int, int, str], None] | None) -> None:
        if progress:
            total = sum(item.size for item in source.files) or model.download_bytes
            progress(0, total, source.label)

    def _install_from_source(self, model: ModelSpec, source: ModelSource,
                             progress: Callable[[int, int, str], None] | None,
                             cancelled: Callable[[], bool] | None,
                             force: bool = False) -> Path:
        if source.files:
            return self._install_remote_files(model, source, progress, cancelled,
                                              force=force)

        download = self._downloads / model.asset_name

        def _progress(done: int, total: int, _source_url: str) -> None:
            if progress:
                progress(done, total or model.download_bytes, source.label)

        ok = fetch_file(source.url, download, expected_sha=model.sha256 or None,
                        expected_size=model.download_bytes or None,
                        progress=_progress, cancelled=cancelled, safe_resume=True, raise_on_error=True)
        if not ok:
            raise RuntimeError("下载失败")
        if cancelled and cancelled():
            raise DownloadCancelled("下载已取消")

        target = self.model_dir(model)
        if model.archive:
            self._commit_boundary(cancelled, progress, model.download_bytes, model.download_bytes)
            self._install_archive(model, download, target, cancelled)
            download.unlink(missing_ok=True)
        else:
            target.mkdir(parents=True, exist_ok=True)
            final = target / model.asset_name
            if final.exists():
                final.unlink()
            replace_with_retry(download, final)
        return target

    def _install_remote_files(self, model: ModelSpec, source: ModelSource,
                              progress: Callable[[int, int, str], None] | None,
                              cancelled: Callable[[], bool] | None,
                              force: bool = False) -> Path:
        download_root = self._downloads / model.id
        total = sum(item.size for item in source.files) or model.download_bytes
        completed = 0
        for item in source.files:
            target_file = self.model_dir(model) / item.install_rel
            destination = download_root / item.install_rel
            if not force and self._remote_file_valid(target_file, item):
                completed += item.size or target_file.stat().st_size
                if progress:
                    progress(completed, total, source.label)
                continue
            if not force and self._remote_file_valid(destination, item):
                completed += item.size or destination.stat().st_size
                if progress:
                    progress(completed, total, source.label)
                continue
            self._download_remote_file(
                item, destination, completed, total, source.label,
                progress, cancelled)
            completed += item.size or destination.stat().st_size
        target = self.model_dir(model)
        self._commit_boundary(cancelled, progress, completed, total)
        self._commit_remote_files(source.files, download_root, target)
        return target

    @staticmethod
    def _remote_file_valid(path: Path, item: RemoteFile) -> bool:
        if not path.is_file():
            return False
        if item.size and path.stat().st_size != item.size:
            return False
        return not item.sha256 or sha256_of(path) == item.sha256

    @staticmethod
    def _download_remote_file(
        item: RemoteFile,
        destination: Path,
        completed: int,
        total: int,
        source_label: str,
        progress: Callable[[int, int, str], None] | None,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        def report(done: int, _file_total: int, _url: str) -> None:
            if progress:
                progress(completed + done, total, source_label)

        ok = fetch_file(
            item.url, destination, expected_sha=item.sha256 or None,
            expected_size=item.size or None, progress=report, cancelled=cancelled, safe_resume=True, raise_on_error=True,
        )
        if not ok:
            raise RuntimeError(f"文件下载失败: {item.install_rel}")

    @staticmethod
    def _commit_remote_files(files: tuple[RemoteFile, ...], download_root: Path,
                             target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        for item in files:
            staged = download_root / item.install_rel
            if not staged.is_file():
                continue
            destination = target / item.install_rel
            copy_file_atomically(staged, destination)
        if download_root.exists():
            try:
                shutil.rmtree(download_root)
            except OSError as exc:
                # Runtime files are already atomically committed and will be
                # checked by install(). A locked cache is not a failed model.
                logger.warning("模型已提交，但下载缓存暂无法清理: path=%s error=%s",
                               download_root, exc)

    @staticmethod
    def _commit_boundary(cancelled: Callable[[], bool] | None,
                         progress: Callable[[int, int, str], None] | None,
                         completed: int, total: int) -> None:
        if progress:
            progress(completed, total, "校验并安装")
        if cancelled and cancelled():
            raise DownloadCancelled("下载已暂停")

    @staticmethod
    def _extract_archive(archive: Path, staging: Path,
                         cancelled: Callable[[], bool] | None) -> None:
        with tarfile.open(archive, "r:bz2") as tf:
            root = staging.resolve()
            for member in tf.getmembers():
                if cancelled and cancelled():
                    raise DownloadCancelled("下载已暂停")
                destination = (staging / member.name).resolve()
                if root not in destination.parents and destination != root:
                    raise RuntimeError("模型压缩包包含越界路径，已拒绝安装")
                if member.issym() or member.islnk():
                    raise RuntimeError("模型压缩包包含链接，已拒绝安装")
                if not member.isfile() and not member.isdir():
                    raise RuntimeError("模型压缩包包含不支持的特殊文件")
                options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                tf.extract(member, staging, **options)

    def _install_archive(self, model: ModelSpec, archive: Path, target: Path,
                         cancelled: Callable[[], bool] | None = None) -> None:
        staging_parent = self.models_dir / ".installing" / model.id
        expected = self.models_dir.resolve() / ".installing" / model.id
        if staging_parent.resolve() != expected:
            raise RuntimeError("模型安装临时目录不能重定向到其他位置")
        staging_parent.mkdir(parents=True, exist_ok=True)
        if staging_parent.resolve() != expected:
            raise RuntimeError("模型安装临时目录不能重定向到其他位置")
        with tempfile.TemporaryDirectory(prefix=f"{model.id}-", dir=staging_parent) as raw:
            staging = Path(raw)
            self._extract_archive(archive, staging, cancelled)
            candidates = [staging]
            candidates.extend(path for path in staging.iterdir() if path.is_dir())
            source_root = next(
                (path for path in candidates
                 if not self._missing_paths_at(path, model)), None)
            if source_root is None:
                raise RuntimeError("模型压缩包结构与目录清单不匹配")
            if cancelled and cancelled():
                raise DownloadCancelled("下载已暂停")
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Same-volume rename publishes a complete directory, never half a copied weight.
            replace_with_retry(source_root, target)

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
        write_text_atomically(
            self._state_path,
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
