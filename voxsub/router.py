"""voxsub.router —— 设备路由 (M8): 枚举 + 实测计分 + 降级链。

契约见 DESIGN.md「设备路由与诊断契约」:

    class DeviceInfo(NamedTuple):
        provider: str            # "cpu" | "dml" | "cuda" | "npu"
        name: str
        score_ms: float | None   # 实测延迟; None=未测

    def enumerate_devices() -> list[DeviceInfo]   # onnxruntime providers 枚举
    def select_device(task) -> DeviceInfo         # task ∈ {asr, tts, translate}

降级链: 有 DmlExecutionProvider 且任务模型就绪 -> dml, 否则 cpu。
score_ms 用实际推理冒烟计时填充:
- asr       : sherpa-onnx 流式识别器对真实短音频 (test_wavs/1.wav, 缺则合成音)
              做一次 feed+decode, 计时;
- translate : ORT 加载 OPUS-MT encoder 并做一次 dummy forward (token id 输入),
              计时 (模型缺失或推理失败 -> None);
- tts       : sherpa OfflineTts 合成 "测试", 计时 (模型缺失 -> None)。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import NamedTuple

import numpy as np

#: provider 内部名 -> 展示名 (DeviceInfo.name 直接取展示名)
_PROVIDER_NAMES = {
    "DmlExecutionProvider": "DirectML",
    "CPUExecutionProvider": "CPU",
    "CUDAExecutionProvider": "CUDA",
    "TensorrtExecutionProvider": "TensorRT",
    "CoreMLExecutionProvider": "CoreML",
    "NPUExecutionProvider": "NPU",
}

TASKS = ("asr", "tts", "translate")


class DeviceInfo(NamedTuple):
    provider: str          # "cpu" | "dml" | "cuda" | "npu"
    name: str
    score_ms: float | None  # 实测延迟; None=未测


def models_dir() -> Path:
    """本地模型根目录 %LOCALAPPDATA%/VoxSub/models (与 voxsub.asr 约定一致)。"""
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "models"


def _norm_provider(raw: str) -> str:
    """onnxruntime provider 内部名 -> 契约 provider 短名。"""
    table = {
        "DmlExecutionProvider": "dml",
        "CPUExecutionProvider": "cpu",
        "CUDAExecutionProvider": "cuda",
        "TensorrtExecutionProvider": "cuda",
        "CoreMLExecutionProvider": "coreml",
        "NPUExecutionProvider": "npu",
    }
    return table.get(raw, raw.lower().replace("executionprovider", ""))


def _display_name(raw: str) -> str:
    return _PROVIDER_NAMES.get(raw, raw)


def enumerate_devices() -> list[DeviceInfo]:
    """枚举可用的 onnxruntime 执行提供器为 DeviceInfo 列表。

    score_ms 初始为 None (选择时才会实测填充); 顺序与
    ort.get_available_providers() 一致 (DirectML 在前即 GPU 优先信号)。
    """
    import onnxruntime as ort

    return [DeviceInfo(provider=_norm_provider(p), name=_display_name(p), score_ms=None)
            for p in ort.get_available_providers()]


# ---------------------------------------------------------------------------
# 任务模型就绪判定
# ---------------------------------------------------------------------------

def _find_first(directory: Path, pattern: str) -> Path | None:
    hits = sorted(directory.glob(pattern))
    return hits[0] if hits else None


def _task_model_ready(task: str) -> bool:
    """任务所需模型是否就位 (决定能否走 GPU/DML 冒烟)。"""
    root = models_dir()
    if task == "asr":
        asr_dir = root / "asr"
        return (asr_dir / "tokens.txt").exists() and _find_first(asr_dir, "*encoder*.onnx") is not None
    if task == "translate":
        return _find_first(root / "nmt", "**/*encoder*.onnx") is not None
    if task == "tts":
        tts_dir = root / "tts"
        return tts_dir.exists() and _find_first(tts_dir, "**/model.onnx") is not None
    return False


# ---------------------------------------------------------------------------
# 实测冒烟计分 (返回毫秒, 失败/缺模型返回 None)
# ---------------------------------------------------------------------------

def _smoke_score_asr(provider: str) -> float | None:
    """sherpa 流式 ASR 对短音频一次 feed+decode 的耗时 (毫秒)。

    素材优先真实语音 test_wavs/1.wav (~2.5s), 缺失时退化为合成静音+正弦音
    (识别文本可为空, 只取计时)。

    注意: sherpa-onnx 1.13.5 的 provider 参数仅支持 cpu/cuda/coreml
    (实测 from_transducer docstring), 传 "DmlExecutionProvider" 会被底层
    StringToProvider 拒绝并静默回退 CPU 且刷警告 —— 故显式映射到 cpu,
    保证分数与真实执行器一致。
    """
    try:
        from voxsub.asr import StreamingASR

        sherpa_provider = "cpu" if provider == "DmlExecutionProvider" else provider
        asr_dir = models_dir() / "asr"
        recognizer = StreamingASR(asr_dir, provider=sherpa_provider, num_threads=2)

        wav = _load_smoke_wav()
        stream = recognizer.create_stream()
        t0 = time.perf_counter()
        recognizer.feed(stream, wav)
        recognizer.decode(stream)
        elapsed = (time.perf_counter() - t0) * 1000.0
        return round(elapsed, 1)
    except Exception as exc:  # noqa: BLE001 -- 冒烟失败即视为不可测
        print(f"  [router] asr 冒烟失败 ({provider}): {type(exc).__name__}: {exc}")
        return None


def _load_smoke_wav() -> np.ndarray:
    """取一段短音频用于 ASR 冒烟: 优先真实语音, 否则合成 静音+正弦。"""
    sr = 16000
    wav = _find_first(models_dir() / "asr" / "test_wavs", "*.wav")
    if wav is not None:
        try:
            import wave

            with wave.open(str(wav), "rb") as w:
                if w.getframerate() == sr and w.getnchannels() == 1:
                    raw = w.readframes(w.getnframes())
                    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    if data.size > sr:  # 冒烟只取前 2 秒, 控制耗时
                        data = data[: sr * 2]
                    return data
        except Exception:  # noqa: BLE001 -- 素材损坏则走合成
            pass
    silence = np.zeros(int(0.8 * sr), dtype=np.float32)
    t = np.arange(int(1.0 * sr), dtype=np.float32) / sr
    tone = 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
    return np.concatenate([silence, tone, np.zeros(int(0.4 * sr), dtype=np.float32)])


def _smoke_score_translate(provider: str) -> float | None:
    """ORT 加载 OPUS-MT encoder + dummy forward 计时 (毫秒)。

    只验证"模型可加载、可推理"这条链路 (完整 seq2seq 解码在 M4 translate 模块,
    此处不做); 模型缺失或加载失败 -> None。
    """
    enc = _find_first(models_dir() / "nmt", "**/*encoder*.onnx")
    if enc is None:
        return None
    try:
        import onnxruntime as ort

        session = ort.InferenceSession(str(enc), providers=[provider])
        ids = np.random.randint(0, 60000, size=(1, 8), dtype=np.int64)
        mask = np.ones((1, 8), dtype=np.int64)
        feed = {}
        for i in session.get_inputs():
            feed[i.name] = ids if "input" in i.name.lower() else mask
        t0 = time.perf_counter()
        session.run(None, feed)
        return round((time.perf_counter() - t0) * 1000.0, 1)
    except Exception as exc:  # noqa: BLE001
        print(f"  [router] translate 冒烟失败 ({provider}): {type(exc).__name__}: {exc}")
        return None


def _smoke_score_tts(provider: str) -> float | None:
    """sherpa OfflineTts 合成 "测试" 计时 (毫秒); tts 模型缺失 -> None。"""
    tts_dir = models_dir() / "tts"
    if not tts_dir.exists():
        return None
    try:
        import sherpa_onnx

        model = _find_first(tts_dir, "**/model.onnx")
        if model is None:
            return None
        tokens = model.parent / "tokens.txt"
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model), tokens=str(tokens), data_dir=str(model.parent)),
                num_threads=2, provider=provider,
            ),
            rule_fsts="",
        )
        tts = sherpa_onnx.OfflineTts(cfg)
        t0 = time.perf_counter()
        tts.generate("测试", sid=0, speed=1.0)
        return round((time.perf_counter() - t0) * 1000.0, 1)
    except Exception as exc:  # noqa: BLE001
        print(f"  [router] tts 冒烟失败 ({provider}): {type(exc).__name__}: {exc}")
        return None


_SMOKE_FNS = {
    "asr": _smoke_score_asr,
    "translate": _smoke_score_translate,
    "tts": _smoke_score_tts,
}


def _smoke_score(task: str, provider: str) -> float | None:
    fn = _SMOKE_FNS.get(task)
    return fn(provider) if fn else None


# ---------------------------------------------------------------------------
# 选择
# ---------------------------------------------------------------------------

def select_device(task: str) -> DeviceInfo:
    """按任务选择执行设备: 降级链 dml -> cpu。

    规则:
    1. 枚举当前 providers; 若有 DmlExecutionProvider 且任务模型就绪 -> dml;
    2. 否则回退 cpu (onnxruntime 必含 CPUExecutionProvider);
    3. score_ms 用所选设备上的实际推理冒烟填充 (失败/缺模型 -> None)。

    Raises:
        ValueError: task 不在 {asr, tts, translate} 内。
    """
    if task not in TASKS:
        raise ValueError(f"未知任务 {task!r}, 可选: {TASKS}")
    devices = enumerate_devices()
    by_provider = {d.provider: d for d in devices}

    # 1) DirectML 优先: 存在且任务模型就绪
    if "dml" in by_provider and _task_model_ready(task):
        score = _smoke_score(task, "DmlExecutionProvider")
        return DeviceInfo(provider="dml", name="DirectML", score_ms=score)

    # 2) CPU 兜底
    score = _smoke_score(task, "CPUExecutionProvider")
    return DeviceInfo(provider="cpu", name="CPU", score_ms=score)
