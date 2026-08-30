"""voxsub.diagnostics —— 自检中心 (M8)。

契约见 DESIGN.md「设备路由与诊断契约」:

    def run_self_check() -> list[dict]   # 每项 {check, status: ok|warn|fail, detail}
    def repair_self_check(results, ...) -> dict  # 只修复结果声明的目标
    def export_report() -> str           # 纯文本报告(诊断页一键导出)

检查项:
1. 模型完整性  —— manifest.json 登记条目与磁盘比对 (存在 + 大小一致)
2. ORT providers —— onnxruntime 可用执行提供器
3. ASR 冒烟    —— 加载流式识别器, 对短音频做一次完整 decode (参考 spike_m1.py)
4. VAD 冒烟    —— 加载 silero VAD, 合成语音触发检测
5. TTS 冒烟    —— 模型存在则合成 "测试"; 未安装则 warn
6. 磁盘/内存余量 —— shutil.disk_usage + psutil.virtual_memory
"""
from __future__ import annotations

from collections.abc import Callable
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from voxsub.config_store import ConfigStore
from voxsub.model_catalog import CATALOG, ModelMarketplace, get_model
from voxsub.model_storage import resolve_models_root
from voxsub.models import ModelManager
from voxsub.logging_setup import get_logger

logger = get_logger("diagnostics")

#: 磁盘余量警戒线 (GB) / 内存可用率警戒线 (%)
DISK_WARN_GB = 5.0
MEM_WARN_PCT = 15.0


def models_dir() -> Path:
    """Return VoxSub's configured, upgrade-safe model root."""
    return resolve_models_root()


def _zipformer_dir() -> Path:
    """Locate the bundled recognizer before or after the storage upgrade."""
    root = models_dir()
    model = get_model("asr-zipformer-bilingual-fast")
    return ModelMarketplace(root).available_model_dir(model) if model else root


def _model_ids_for_problems(root: Path, problems: list[dict]) -> list[str]:
    """Map integrity paths to catalog models that the repair action can install."""
    problem_paths = [str(item.get("rel", "")).replace("\\", "/")
                     for item in problems]
    marketplace = ModelMarketplace(root)
    ids: list[str] = []
    for model in CATALOG:
        prefixes = (model.install_rel, *model.legacy_install_rels)
        if not any(
            path == prefix or path.startswith(prefix + "/")
            for path in problem_paths for prefix in prefixes
        ):
            continue
        if marketplace.missing_paths(model) and model.id not in ids:
            ids.append(model.id)
    return ids


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------

def _check_model_integrity() -> dict:
    """manifest 登记条目 vs 磁盘: 文件存在 + 大小一致。"""
    mgr = ModelManager(models_dir())
    problems = mgr.verify_all()
    files = mgr.load_manifest().get("files", {})
    total = len(files)
    ready = total - len(problems)
    if problems:
        sample = ", ".join(f"{p['rel']}({p['reason']})" for p in problems[:5])
        more = f" 等 {len(problems)} 项" if len(problems) > 5 else ""
        repair_model_ids = _model_ids_for_problems(models_dir(), problems)
        logger.warning("自检[模型完整性] %d/%d 就绪, %d 项异常", ready, total, len(problems))
        result = {
            "check": "模型完整性",
            "status": "fail",
            "detail": f"{ready}/{total} 就绪, 问题项: {sample}{more}",
            "suggestion": "运行 model_fetch.py scan 重扫, 或用 fetch 重新下载缺失/损坏文件 (见 voxsub.models.ModelManager)",
        }
        if repair_model_ids:
            result["repair"] = {"kind": "models", "model_ids": repair_model_ids}
        return result
    logger.info("自检[模型完整性] %d 条登记全部就绪", total)
    return {
        "check": "模型完整性",
        "status": "ok",
        "detail": f"{total} 条登记全部就绪 (存在且大小一致)",
        "suggestion": "无需处理",
    }


def _check_ort_providers() -> dict:
    """onnxruntime 可用执行提供器。"""
    try:
        import onnxruntime as ort

        providers = list(ort.get_available_providers())
    except Exception as exc:  # noqa: BLE001
        logger.warning("自检[ORT providers] onnxruntime 导入失败: %s", exc)
        return {"check": "ORT providers", "status": "fail",
                "detail": f"onnxruntime 导入失败: {type(exc).__name__}: {exc}",
                "suggestion": "重装 onnxruntime-directml (勿与标准 onnxruntime 同装)"}
    if not providers:
        return {"check": "ORT providers", "status": "fail",
                "detail": "没有任何可用执行提供器", "suggestion": "重装 onnxruntime-directml"}
    if any("Dml" in p for p in providers):
        status, tip = "ok", "DirectML 加速可用"
    else:
        status, tip = "warn", "仅有 CPU, 无 GPU 加速"
    return {"check": "ORT providers", "status": status,
            "detail": f"{', '.join(providers)} ({tip})",
            "suggestion": "无需处理" if status == "ok" else "安装 onnxruntime-directml 以启用 GPU 加速"}


def _load_smoke_wav(max_sec: float = 2.0) -> np.ndarray | None:
    """取一段用于冒烟的真实语音 (Zipformer/test_wavs/*.wav, 截取前 max_sec 秒)。

    真实语音缺失时返回 None, 由调用方决定退化策略。
    """
    sr = 16000
    hits = sorted((_zipformer_dir() / "test_wavs").glob("*.wav"))
    for wav in hits:
        try:
            import wave

            with wave.open(str(wav), "rb") as w:
                if w.getframerate() != sr or w.getnchannels() != 1:
                    continue
                raw = w.readframes(w.getnframes())
                data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if data.size > int(sr * max_sec):
                    data = data[: int(sr * max_sec)]
                return data
        except Exception as exc:  # noqa: BLE001 -- 素材损坏则尝试下一个
            logger.debug("冒烟音频素材读取失败 (%s): %s, 尝试下一文件", wav.name, exc)
            continue
    return None


def _synthetic_tone() -> np.ndarray:
    """合成 静音+220Hz 正弦 (无真实语音素材时的退化输入, 仅验证管线不崩)。"""
    sr = 16000
    t = np.arange(int(1.0 * sr), dtype=np.float32) / sr
    tone = 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
    return np.concatenate([np.zeros(int(0.6 * sr), dtype=np.float32), tone])


def _check_asr_smoke() -> dict:
    """加载流式 ASR, 对短音频做一次完整 decode (参考 spike_m1.py 方法)。"""
    asr_dir = _zipformer_dir()
    if not (asr_dir / "tokens.txt").exists() or not list(asr_dir.glob("*encoder*.onnx")):
        logger.warning(
            "自检[ASR 冒烟] ASR 模型不完整 (缺 tokens.txt 或 encoder onnx): "
            "model=%s path=%s",
            "asr-zipformer-bilingual-fast", asr_dir,
        )
        return {"check": "ASR 冒烟", "status": "fail",
                "detail": f"ASR 模型不完整 (缺 tokens.txt 或 encoder onnx): {asr_dir}",
                "suggestion": "点击修复以补齐 ASR 模型",
                "repair": {"kind": "models", "model_ids": [
                    "asr-zipformer-bilingual-fast"]}}
    try:
        import sherpa_onnx

        tokens = asr_dir / "tokens.txt"
        enc = sorted(asr_dir.glob("*encoder*.onnx"))[0]
        dec = sorted(asr_dir.glob("*decoder*.onnx"))[0]
        joiner = sorted(asr_dir.glob("*joiner*.onnx"))[0]
        recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(tokens), encoder=str(enc), decoder=str(dec), joiner=str(joiner),
            decoding_method="greedy_search", provider="cpu", num_threads=2)

        sr = 16000
        samples = _load_smoke_wav()
        if samples is None:
            samples = _synthetic_tone()

        stream = recognizer.create_stream()
        t0 = time.perf_counter()
        stream.accept_waveform(sr, samples)
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = recognizer.get_result(stream)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {"check": "ASR 冒烟", "status": "ok",
                "detail": f"模型加载+解码通过, 耗时 {elapsed_ms:.0f}ms, 文本={text.strip()!r}",
                "suggestion": "无需处理"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("自检[ASR 冒烟] 加载/解码异常: %s", exc, exc_info=True)
        return {"check": "ASR 冒烟", "status": "fail",
                "detail": f"加载/解码异常: {type(exc).__name__}: {exc}",
                "suggestion": "点击修复以重新检查 ASR 模型",
                "repair": {"kind": "models", "model_ids": [
                    "asr-zipformer-bilingual-fast"]}}


def _check_vad_smoke() -> dict:
    """加载 silero VAD, 合成 220Hz 音应能触发语音检测。"""
    vad_dir = models_dir() / "vad"
    hits = sorted(vad_dir.glob("*.onnx"))
    if not hits:
        logger.warning("自检[VAD 冒烟] 缺少 VAD 模型: %s", vad_dir.name)
        return {"check": "VAD 冒烟", "status": "fail",
                "detail": f"缺少 VAD 模型: {vad_dir}",
                "suggestion": "点击修复以恢复基础 VAD 模型",
                "repair": {"kind": "vad"}}
    try:
        import sherpa_onnx

        cfg = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=str(hits[0]), threshold=0.5,
                min_silence_duration=0.5, min_speech_duration=0.25,
                window_size=512, max_speech_duration=10),
            sample_rate=16000, num_threads=2, provider="cpu")
        vad = sherpa_onnx.VadModel.create(cfg)
        win = vad.window_size()
        samples = _load_smoke_wav(max_sec=3.0)
        if samples is None:
            samples = _synthetic_tone()
        speech_windows = sum(
            1 for i in range(0, len(samples) - win + 1, win)
            if vad.is_speech(samples[i:i + win]))
        vad.reset()
        if speech_windows == 0:
            return {"check": "VAD 冒烟", "status": "warn",
                    "detail": f"模型加载通过, 但输入音频未触发语音检测 ({speech_windows} 语音窗)",
                    "suggestion": "阈值过高或模型异常, 可调低 threshold 再试"}
        return {"check": "VAD 冒烟", "status": "ok",
                "detail": f"模型加载通过, 触发 {speech_windows} 个语音窗口",
                "suggestion": "无需处理"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("自检[VAD 冒烟] 加载/检测异常: %s", exc, exc_info=True)
        return {"check": "VAD 冒烟", "status": "fail",
                "detail": f"加载/检测异常: {type(exc).__name__}: {exc}",
                "suggestion": "点击修复以恢复基础 VAD 模型",
                "repair": {"kind": "vad"}}


def _check_tts_smoke() -> dict:
    """Synthesize with the configured catalog voice; missing TTS stays a warning."""
    root = models_dir()
    store = ConfigStore()
    config = store.load()
    model_ids = {
        "zh": str(config.get("tts_model_id_zh", "tts-icefall-zh-aishell3")),
        "en": str(config.get("tts_model_id_en", "tts-icefall-en-ljspeech-low")),
    }
    marketplace = ModelMarketplace(root)
    pair = str(config.get("lang_pair", "zh-en"))
    preferred = pair.split("-", 1)[-1] if "-" in pair else "en"
    candidates = [preferred] + [lang for lang in ("zh", "en") if lang != preferred]
    installed = []
    for lang in candidates:
        model = get_model(model_ids[lang])
        if (model is not None and model.task == "tts"
                and lang in model.tts_languages
                and marketplace.is_installed(model)):
            installed.append(lang)
    if not installed:
        logger.info("自检[TTS 冒烟] 当前所选朗读模型未安装, 标记 warn")
        return {"check": "TTS 冒烟", "status": "warn",
                "detail": "当前所选 TTS 模型未安装",
                "suggestion": "点击修复以下载当前所选朗读模型 (缺朗读不影响字幕)",
                "repair": {"kind": "models", "model_ids": [
                    model_ids[lang] for lang in candidates
                    if get_model(model_ids[lang]) is not None]}}
    try:
        from voxsub.tts import TTSEngine

        lang = installed[0]
        text = "测试" if lang == "zh" else "Test"
        tts = TTSEngine(root / "tts", provider="cpu", num_threads=2,
                        model_ids=model_ids)
        t0 = time.perf_counter()
        audio = tts.synthesize(text, lang=lang)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if audio is None or not len(audio):
            raise RuntimeError(f"所选 {lang} 朗读模型未产生音频")
        return {"check": "TTS 冒烟", "status": "ok",
                "detail": f"{lang} 所选模型合成通过, 耗时 {elapsed_ms:.0f}ms, 采样数={len(audio)}",
                "suggestion": "无需处理"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("自检[TTS 冒烟] 合成异常: %s", exc, exc_info=True)
        return {"check": "TTS 冒烟", "status": "fail",
                "detail": f"合成异常: {type(exc).__name__}: {exc}",
                "suggestion": "点击修复以重新下载当前朗读模型",
                "repair": {"kind": "models", "model_ids": [model_ids[lang]]}}


def _check_resources() -> dict:
    """磁盘剩余空间 + 内存可用率。"""
    base = models_dir()
    base.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(base)
    free_gb = usage.free / (1024 ** 3)
    mem_detail = ""
    mem_warn = False
    try:
        import psutil

        vm = psutil.virtual_memory()
        avail_pct = vm.available / vm.total * 100.0
        mem_detail = f", 内存可用 {avail_pct:.0f}% ({vm.available / 1e9:.1f}GB / {vm.total / 1e9:.1f}GB)"
        mem_warn = avail_pct < MEM_WARN_PCT
    except Exception as exc:  # noqa: BLE001 -- psutil 缺失时仅报磁盘
        logger.debug("psutil 不可用, 跳过内存检查: %s", exc)
        mem_detail = ", 内存信息不可用 (psutil 未安装)"

    disk_warn = free_gb < DISK_WARN_GB
    if disk_warn or mem_warn:
        parts = []
        if disk_warn:
            parts.append(f"磁盘剩余 {free_gb:.1f}GB < {DISK_WARN_GB:.0f}GB")
        if mem_warn:
            parts.append("内存可用率过低")
        return {"check": "磁盘/内存余量", "status": "warn",
                "detail": f"{parts[0]}{mem_detail}",
                "suggestion": "清理磁盘空间 / 关闭占用内存的应用后重试"}
    return {"check": "磁盘/内存余量", "status": "ok",
            "detail": f"磁盘剩余 {free_gb:.1f}GB{mem_detail}",
            "suggestion": "无需处理"}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_self_check(
    progress: Callable[[int, int, str], None] | None = None,
) -> list[dict]:
    """执行全部自检项，可选地按阶段报告进度。

    ``progress`` 只用于 UI 反馈，回调异常不会被吞掉；调用方应保持回调轻量。
    不传回调时保持原有同步调用契约。
    """
    runners = (
        ("模型完整性", _check_model_integrity),
        ("ORT providers", _check_ort_providers),
        ("ASR 冒烟", _check_asr_smoke),
        ("VAD 冒烟", _check_vad_smoke),
        ("TTS 冒烟", _check_tts_smoke),
        ("磁盘/内存余量", _check_resources),
    )
    total = len(runners)
    checks: list[dict] = []
    for index, (label, runner) in enumerate(runners):
        if progress:
            progress(index, total, f"正在检查：{label}")
        checks.append(runner())
        if progress:
            progress(index + 1, total, f"已完成：{label}")
    logger.info("自检完成 (共 %d 项)", len(checks))
    return checks


def _repair_actions(
    results: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> list[tuple[str, str]]:
    """Extract ordered, de-duplicated repair actions from one result snapshot."""
    model_ids: list[str] = []
    repair_vad = False
    for item in results:
        if str(item.get("status", "")) not in {"warn", "fail"}:
            continue
        descriptor = item.get("repair")
        if not isinstance(descriptor, Mapping):
            continue
        if descriptor.get("kind") == "vad":
            repair_vad = True
        if descriptor.get("kind") != "models":
            continue
        raw_ids = descriptor.get("model_ids", ())
        if isinstance(raw_ids, str):
            raw_ids = (raw_ids,)
        if isinstance(raw_ids, (list, tuple, set)):
            for model_id in raw_ids:
                if str(model_id) not in model_ids:
                    model_ids.append(str(model_id))

    actions = ([("vad", "基础 VAD")] if repair_vad else [])
    actions.extend(("model", model_id) for model_id in model_ids)
    return actions


def _repair_one_action(
    kind: str,
    target: str,
    root: Path,
    marketplace: ModelMarketplace,
    preference: str,
) -> None:
    """Execute one validated repair action and raise a user-facing error."""
    if kind == "vad":
        from voxsub.bootstrap_models import ensure_bundled_vad

        if ensure_bundled_vad(root) is None:
            raise RuntimeError("基础 VAD 文件未生成")
        return
    model = get_model(target)
    if model is None:
        raise RuntimeError(f"未知模型：{target}")
    marketplace.install(model, preference=preference, force=True)


def repair_self_check(
    results: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    store: ConfigStore | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, list[str]]:
    """Repair only targets declared by the latest self-check snapshot.

    Checks that have no explicit repair descriptor are intentionally skipped:
    reinstalling runtimes or changing hardware settings cannot be made safe by
    a generic button.  ``force=True`` is used for model failures so a corrupt
    file with an unchanged size is replaced instead of being treated as ready.
    """
    config = store.load() if store is not None else ConfigStore().load()
    root = resolve_models_root(store)
    marketplace = ModelMarketplace(root)
    actions = _repair_actions(results)
    repaired: list[str] = []
    errors: list[str] = []
    total = len(actions)
    preference = str(config.get("download_source", "auto"))
    logger.info(
        "自检修复开始: root=%s preference=%s targets=%s",
        root, preference, [f"{kind}:{target}" for kind, target in actions],
    )
    if not actions:
        logger.info("自检修复跳过: 最新结果没有声明可执行的修复目标")
    for index, (kind, target) in enumerate(actions):
        logger.info("自检修复目标开始: index=%d/%d kind=%s target=%s",
                    index + 1, total, kind, target)
        if progress:
            progress(index, max(1, total), f"正在修复：{target}")
        try:
            _repair_one_action(kind, target, root, marketplace, preference)
            repaired.append(target)
            logger.info("自检修复目标完成: kind=%s target=%s", kind, target)
        except Exception as exc:  # noqa: BLE001 - report each target separately
            logger.warning("自检修复失败: target=%s error=%s", target, exc,
                           exc_info=True)
            errors.append(f"{target}: {exc}")
        if progress:
            progress(index + 1, max(1, total), f"已处理：{target}")
    return {"repaired": repaired, "errors": errors}


_STATUS_ICON = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[fail]"}


def export_report() -> str:
    """生成纯文本自检报告 (诊断页一键导出): 每行一项, 含时间戳/结论/建议。"""
    items = run_self_check()
    n_fail = sum(1 for i in items if i["status"] == "fail")
    n_warn = sum(1 for i in items if i["status"] == "warn")
    if n_fail:
        conclusion = f"存在 {n_fail} 项失败, 建议修复后再使用"
    elif n_warn:
        conclusion = f"基本可用, 存在 {n_warn} 项警告 (不影响核心字幕流程)"
    else:
        conclusion = "全部通过, 可正常使用"

    lines = [
        "=" * 56,
        "语幕 VoxSub 自检报告",
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 56,
        f"结论: {conclusion}",
        "-" * 56,
    ]
    for item in items:
        icon = _STATUS_ICON.get(item["status"], "[??]  ")
        lines.append(f"{icon} {item['check']}: {item['detail']}")
        if item.get("suggestion") and item["suggestion"] != "无需处理":
            lines.append(f"       建议: {item['suggestion']}")
    lines.append("-" * 56)
    lines.append(f"共 {len(items)} 项: {len(items) - n_fail - n_warn} ok / {n_warn} warn / {n_fail} fail")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # 便于命令行直跑: python -m voxsub.diagnostics
    print(export_report())
