"""voxsub.router —— 设备路由 (M8): 枚举 + 实测计分 + 降级链。

契约见 DESIGN.md「设备路由与诊断契约」:

    class DeviceInfo(NamedTuple):
        provider: str            # "cpu" | "dml" | "cuda" | "npu"
        name: str
        score_ms: float | None   # 实测延迟; None=未测

    def enumerate_devices() -> list[DeviceInfo]   # onnxruntime providers 枚举
    def select_device(task) -> DeviceInfo         # task ∈ {asr, tts, translate}

优先级: 独立 GPU -> NPU -> 核显 -> CPU。物理设备存在但当前模型运行时
不支持时会跳过并记录原因，不把 CPU 冒充成硬件加速。
score_ms 用实际推理冒烟计时填充:
- asr       : sherpa-onnx 流式识别器对真实短音频 (test_wavs/1.wav, 缺则合成音)
              做一次 feed+decode, 计时;
- translate : ORT 加载 OPUS-MT encoder 并做一次 dummy forward (token id 输入),
              计时 (模型缺失或推理失败 -> None);
- tts       : sherpa OfflineTts 合成 "测试", 计时 (模型缺失 -> None)。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import NamedTuple

import numpy as np

from voxsub.logging_setup import get_logger
from voxsub.hardware import HardwareProfile, detect_hardware
from voxsub.model_catalog import ModelMarketplace, get_model
from voxsub.model_storage import resolve_models_root

logger = get_logger("router")

#: provider 内部名 -> 展示名 (DeviceInfo.name 直接取展示名)
_PROVIDER_NAMES = {
    "DmlExecutionProvider": "DirectML",
    "CPUExecutionProvider": "CPU",
    "CUDAExecutionProvider": "CUDA",
    "TensorrtExecutionProvider": "TensorRT",
    "CoreMLExecutionProvider": "CoreML",
    "NPUExecutionProvider": "NPU",
    "QNNExecutionProvider": "Qualcomm QNN",
    "VitisAIExecutionProvider": "AMD VitisAI",
    "OpenVINOExecutionProvider": "Intel OpenVINO",
}

TASKS = ("asr", "tts", "translate")


class DeviceInfo(NamedTuple):
    provider: str          # "cpu" | "dml" | "cuda" | "npu"
    name: str
    score_ms: float | None  # 实测延迟; None=未测
    kind: str = ""         # gpu | npu | igpu | cpu
    raw_provider: str = ""


def models_dir() -> Path:
    """Return VoxSub's configured, upgrade-safe model root."""
    return resolve_models_root()


def _norm_provider(raw: str) -> str:
    """onnxruntime provider 内部名 -> 契约 provider 短名。"""
    table = {
        "DmlExecutionProvider": "dml",
        "CPUExecutionProvider": "cpu",
        "CUDAExecutionProvider": "cuda",
        "TensorrtExecutionProvider": "cuda",
        "CoreMLExecutionProvider": "coreml",
        "NPUExecutionProvider": "npu",
        "QNNExecutionProvider": "npu",
        "VitisAIExecutionProvider": "npu",
        "OpenVINOExecutionProvider": "openvino",
    }
    return table.get(raw, raw.lower().replace("executionprovider", ""))


def _display_name(raw: str) -> str:
    return _PROVIDER_NAMES.get(raw, raw)


def _device_kind(raw: str, profile: HardwareProfile,
                 hardware_type: str = "") -> str:
    """Normalize ORT's provider/device metadata to a physical device class."""
    if hardware_type == "gpu":
        return "gpu" if profile.has_discrete_gpu else "igpu"
    if hardware_type == "npu":
        return "npu"
    if hardware_type:
        return "cpu"
    if raw in {"CUDAExecutionProvider", "TensorrtExecutionProvider"}:
        return "gpu"
    if raw in {"QNNExecutionProvider", "VitisAIExecutionProvider",
               "NPUExecutionProvider"}:
        return "npu"
    if raw == "OpenVINOExecutionProvider":
        if profile.has_npu_runtime and "openvino" in profile.npu_provider.casefold():
            return "npu"
        return "igpu" if profile.has_integrated_gpu else "cpu"
    if raw == "DmlExecutionProvider":
        return "gpu" if profile.has_discrete_gpu else "igpu"
    return "cpu"


def _ep_device_details(ort, profile: HardwareProfile) -> list[DeviceInfo]:
    get_ep_devices = getattr(ort, "get_ep_devices", None)
    if not get_ep_devices:
        return []
    devices: list[DeviceInfo] = []
    seen: set[tuple[str, str]] = set()
    try:
        for item in get_ep_devices():
            raw = str(item.ep_name)
            hardware_type = str(item.device.type).rsplit(".", 1)[-1].lower()
            kind = _device_kind(raw, profile, hardware_type)
            key = (raw, kind)
            if key in seen:
                continue
            seen.add(key)
            vendor = str(getattr(item.device, "vendor", "") or "").strip()
            short = "npu" if kind == "npu" else _norm_provider(raw)
            label = f"{vendor + ' ' if vendor else ''}{_display_name(raw)}"
            devices.append(DeviceInfo(short, label, None, kind, raw))
    except Exception:
        logger.debug("ORT EP 设备明细读取失败，改用 provider 列表", exc_info=True)
        return []
    return devices


def _provider_devices(providers: list[str], profile: HardwareProfile,
                      detailed: list[DeviceInfo]) -> list[DeviceInfo]:
    devices = list(detailed)
    detailed_providers = {device.raw_provider for device in detailed}
    for raw in providers:
        if raw in detailed_providers:
            continue
        kind = _device_kind(raw, profile)
        short = "npu" if kind == "npu" else _norm_provider(raw)
        devices.append(DeviceInfo(short, _display_name(raw), None, kind, raw))
    return devices


def enumerate_devices() -> list[DeviceInfo]:
    """Enumerate registered ORT EP devices and classify their hardware."""
    import onnxruntime as ort

    providers = ort.get_available_providers()
    logger.info("枚举 ORT 执行提供器: %s", ", ".join(providers) if providers else "(无)")
    profile = detect_hardware()
    details = _ep_device_details(ort, profile)
    devices = _provider_devices(providers, profile, details)
    return sorted(devices, key=_device_priority)


def _device_priority(device: DeviceInfo) -> tuple[int, str]:
    kind = device.kind or ({"cuda": "gpu", "dml": "gpu", "npu": "npu",
                            "cpu": "cpu"}.get(device.provider, "igpu"))
    return ({"gpu": 0, "npu": 1, "igpu": 2, "cpu": 3}.get(kind, 4), device.name)


def _raw_provider(device: DeviceInfo) -> str:
    if device.raw_provider:
        return device.raw_provider
    return {
        "cuda": "CUDAExecutionProvider", "dml": "DmlExecutionProvider",
        "npu": "NPUExecutionProvider", "openvino": "OpenVINOExecutionProvider",
        "cpu": "CPUExecutionProvider",
    }.get(device.provider, device.provider)


def _provider_spec(device: DeviceInfo):
    raw = _raw_provider(device)
    kind = device.kind or ("npu" if device.provider == "npu" else "")
    if raw == "QNNExecutionProvider" and kind == "npu":
        return (raw, {"backend_type": "htp"})
    if raw == "OpenVINOExecutionProvider":
        target = "NPU" if kind == "npu" else "GPU" if kind in {"gpu", "igpu"} else "CPU"
        return (raw, {"device_type": target})
    return raw


def _supports_device(task: str, device: DeviceInfo) -> bool:
    raw = _raw_provider(device)
    if task in {"asr", "tts"}:
        # Current sherpa-onnx Python runtime supports CUDA/CoreML/CPU, not
        # DirectML/QNN/Vitis/OpenVINO. Never report those paths as accelerated.
        return raw in {"CUDAExecutionProvider", "CPUExecutionProvider",
                       "CoreMLExecutionProvider"}
    return raw in {
        "CUDAExecutionProvider", "DmlExecutionProvider", "QNNExecutionProvider",
        "VitisAIExecutionProvider", "OpenVINOExecutionProvider",
        "NPUExecutionProvider", "CPUExecutionProvider",
    }


def preferred_onnx_providers(task: str = "translate") -> list:
    """Return usable ORT providers in GPU -> NPU -> iGPU -> CPU order."""
    providers: list = []
    seen: set[str] = set()
    for device in enumerate_devices():
        if not _supports_device(task, device):
            continue
        raw = _raw_provider(device)
        if raw in seen:
            continue
        seen.add(raw)
        providers.append(_provider_spec(device))
    if "CPUExecutionProvider" not in seen:
        providers.append("CPUExecutionProvider")
    return providers


# ---------------------------------------------------------------------------
# 任务模型就绪判定
# ---------------------------------------------------------------------------

def _find_first(directory: Path, pattern: str) -> Path | None:
    hits = sorted(directory.glob(pattern))
    return hits[0] if hits else None


def _bundled_model_dir(model_id: str) -> Path:
    """Find a built-in model in its current or pre-migration folder."""
    root = models_dir()
    model = get_model(model_id)
    if model is None:  # pragma: no cover - a catalog programming error
        return root
    return ModelMarketplace(root).available_model_dir(model)


def _task_model_ready(task: str) -> bool:
    """任务所需模型是否就位 (决定能否走 GPU/DML 冒烟)。"""
    root = models_dir()
    if task == "asr":
        asr_dir = _bundled_model_dir("asr-zipformer-bilingual-fast")
        return (asr_dir / "tokens.txt").exists() and _find_first(asr_dir, "*encoder*.onnx") is not None
    if task == "translate":
        opus_dir = _bundled_model_dir("mt-opus-fast-builtin")
        return _find_first(opus_dir, "**/*encoder*.onnx") is not None
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
        asr_dir = _bundled_model_dir("asr-zipformer-bilingual-fast")
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
        logger.warning("ASR 冒烟计分失败 (provider=%s): %s: %s",
                       provider, type(exc).__name__, exc, exc_info=True)
        return None


def _load_smoke_wav() -> np.ndarray:
    """取一段短音频用于 ASR 冒烟: 优先真实语音, 否则合成 静音+正弦。"""
    sr = 16000
    wav = _find_first(
        _bundled_model_dir("asr-zipformer-bilingual-fast") / "test_wavs", "*.wav")
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
        except Exception as exc:  # noqa: BLE001 -- 素材损坏则走合成
            logger.debug("冒烟音频素材读取失败 (%s): %s, 改用合成音", wav.name, exc)
            pass
    silence = np.zeros(int(0.8 * sr), dtype=np.float32)
    t = np.arange(int(1.0 * sr), dtype=np.float32) / sr
    tone = 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
    return np.concatenate([silence, tone, np.zeros(int(0.4 * sr), dtype=np.float32)])


def _smoke_score_translate(provider) -> float | None:
    """ORT 加载 OPUS-MT encoder + dummy forward 计时 (毫秒)。

    只验证"模型可加载、可推理"这条链路 (完整 seq2seq 解码在 M4 translate 模块,
    此处不做); 模型缺失或加载失败 -> None。
    """
    enc = _find_first(_bundled_model_dir("mt-opus-fast-builtin"), "**/*encoder*.onnx")
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
        logger.warning("translate 冒烟计分失败 (provider=%s): %s: %s",
                       provider, type(exc).__name__, exc, exc_info=True)
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
        logger.warning("TTS 冒烟计分失败 (provider=%s): %s: %s",
                       provider, type(exc).__name__, exc, exc_info=True)
        return None


_SMOKE_FNS = {
    "asr": _smoke_score_asr,
    "translate": _smoke_score_translate,
    "tts": _smoke_score_tts,
}


def _smoke_score(task: str, provider) -> float | None:
    fn = _SMOKE_FNS.get(task)
    return fn(provider) if fn else None


# ---------------------------------------------------------------------------
# 选择
# ---------------------------------------------------------------------------

def select_device(task: str, *, benchmark: bool = True) -> DeviceInfo:
    """Select a genuinely supported device in GPU -> NPU -> iGPU -> CPU order.

    Accelerators are accepted only when the task's runtime supports their EP.
    With ``benchmark=True`` a failed smoke inference falls through to the next
    candidate instead of pretending that the failed accelerator was selected.

    Raises:
        ValueError: task 不在 {asr, tts, translate} 内。
    """
    if task not in TASKS:
        raise ValueError(f"未知任务 {task!r}, 可选: {TASKS}")
    devices = sorted(enumerate_devices(), key=_device_priority)
    model_ready = _task_model_ready(task)
    for device in devices:
        kind = device.kind or ("cpu" if device.provider == "cpu" else "gpu")
        if not _supports_device(task, device):
            logger.info("设备路由跳过: task=%s device=%s 当前运行时不支持",
                        task, device.name)
            continue
        if kind != "cpu" and not model_ready:
            logger.info("设备路由跳过: task=%s device=%s 模型未就绪",
                        task, device.name)
            continue
        score = _smoke_score(task, _provider_spec(device)) if benchmark else None
        if benchmark and kind != "cpu" and score is None:
            logger.warning("设备路由实测失败: task=%s device=%s，尝试下一设备",
                           task, device.name)
            continue
        selected = DeviceInfo(device.provider, device.name, score, kind,
                              _raw_provider(device))
        logger.info("设备路由: task=%s 选择 %s kind=%s score_ms=%s",
                    task, selected.name, kind, score)
        return selected

    # ORT normally always exposes CPU, but keep a deterministic last defense.
    score = _smoke_score(task, "CPUExecutionProvider") if benchmark else None
    logger.warning("设备路由没有可用加速器，task=%s 强制 CPU", task)
    return DeviceInfo("cpu", "CPU", score, "cpu", "CPUExecutionProvider")
