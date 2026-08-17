"""Curated model catalog, hardware assessment, and local installation service.

The catalog is deliberately small.  A model is listed only when VoxSub has a
runtime adapter for it; downloading a weight that the application cannot use is
treated as a product bug, not as a marketplace feature.
"""
from __future__ import annotations

import json
import os
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
from voxsub.hardware import HardwareProfile, detect_hardware
from voxsub.models import DownloadCancelled, fetch_file

logger = get_logger("model_catalog")

GIB = 1024 ** 3
CATALOG_UPDATED = "2026-08-18"


def default_models_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "VoxSub" / "models"


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
        install_rel="marketplace/asr-funasr-nano-2512-int8",
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
        install_rel="marketplace/asr-qwen3-0.6b-int8",
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
        install_rel="asr",
        required_paths=("tokens.txt",),
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
        download_bytes=4_620_000_000,
        installed_bytes=4_620_000_000,
        install_rel="marketplace/mt-hy-mt2-7b-q4",
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
        download_bytes=1_130_000_000,
        installed_bytes=1_130_000_000,
        install_rel="marketplace/mt-hy-mt2-1.8b-q4",
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
        download_bytes=0,
        installed_bytes=170_000_000,
        install_rel="nmt",
        required_paths=("opus_zh_en/encoder_model_int8.onnx",
                        "opus_en_zh/encoder_model_int8.onnx"),
        builtin=True,
        min_ram_gb=4.0,
        working_ram_gb=0.5,
        compute_cost=10,
        gpu_supported=True,
        npu_supported=True,
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
        if (model.npu_supported and profile.has_npu and
                "intel" in profile.npu_name.casefold() and
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
        self.models_dir = Path(models_dir) if models_dir else default_models_dir()
        self._downloads = self.models_dir / ".downloads"
        self._state_path = self.models_dir / "catalog_installs.json"

    def model_dir(self, model: ModelSpec) -> Path:
        return self.models_dir / model.install_rel

    def is_installed(self, model: ModelSpec) -> bool:
        base = self.model_dir(model)
        return all((base / rel).is_file() for rel in model.required_paths)

    def model_file(self, model: ModelSpec) -> Path:
        if not model.required_paths:
            return self.model_dir(model)
        return self.model_dir(model) / model.required_paths[0]

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
        if model.builtin:
            if self.is_installed(model):
                return self.model_dir(model)
            raise RuntimeError("内置模型文件缺失，请通过安装包修复程序")
        if self.is_installed(model):
            return self.model_dir(model)
        sources = self.ordered_sources(model, preference)
        if not sources:
            raise RuntimeError("该模型没有可用下载源")

        self._downloads.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        for source in sources:
            try:
                target = self._install_from_source(
                    model, source, progress=progress, cancelled=cancelled)
                if not self.is_installed(model):
                    raise RuntimeError("模型已下载，但关键文件校验失败")
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
        raise RuntimeError("所有下载源均失败：" + "；".join(errors))

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
        total = sum(item.size for item in source.files)
        completed = 0
        for item in source.files:
            destination = download_root / item.install_rel

            def _progress(done: int, _file_total: int, _url: str,
                          base: int = completed) -> None:
                if progress:
                    progress(base + done, total, source.label)

            ok = fetch_file(item.url, destination, expected_sha=item.sha256 or None,
                            progress=_progress, cancelled=cancelled)
            if not ok:
                raise RuntimeError(f"文件下载失败: {item.install_rel}")
            completed += item.size
        if not all((download_root / rel).is_file() for rel in model.required_paths):
            raise RuntimeError("下载文件结构与模型目录清单不匹配")
        target = self.model_dir(model)
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        download_root.replace(target)
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
                 if all((path / rel).is_file() for rel in model.required_paths)), None)
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
        root = self.models_dir.resolve()
        target = self.model_dir(model).resolve()
        if root not in target.parents or target == root:
            raise RuntimeError("拒绝卸载模型目录之外的路径")
        expected = (self.models_dir / model.install_rel).resolve()
        if target != expected:
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
