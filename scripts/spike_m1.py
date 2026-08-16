#!/usr/bin/env python
"""M1-spike: 验证三项关键依赖在本机可用 (适配 sherpa-onnx 1.13.5 新 API)。

1. soundcard 录音设备枚举（麦克风 + loopback 虚拟输入）
2. onnxruntime 可用 Execution Provider（CPU / DirectML）
3. sherpa-onnx 流式 ASR + VAD 模型加载与一次识别冒烟

sherpa-onnx 1.13.5 API 要点（2026-08 spike 实测）:
- config 类(OnlineRecognizerConfig 等)已移除, 改用工厂: OnlineRecognizer.from_transducer(...)
- VAD 改 VadModel.create(VadModelConfig), 无 segments 管理, 需自行按窗口调用 is_speech
- stream.accept_waveform(sample_rate, waveform) —— 参数顺序为 (sr, wav)
- 模型路径必须 str, 不接受 Path
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

MODELS_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "VoxSub" / "models"


def enum_audio_devices() -> dict:
    """枚举麦克风(含 loopback)与扬声器。soundcard 0.4.6: loopback 在 microphones 侧。"""
    import soundcard as sc

    mics = [d.name for d in sc.all_microphones(include_loopback=True)]
    spk = [d.name for d in sc.all_speakers()]
    # 打印全部设备 id, 定位 loopback 的真实命名形态 (Windows 上未必含 "loopback" 字样)
    mic_ids = [(d.id, d.name) for d in sc.all_microphones(include_loopback=True)]
    return {
        "microphones": mics,
        "speakers": spk,
        "has_mic": len(mics) > 0,
        "mic_id_name_pairs": mic_ids,
        "loopback_candidates": [n for _, n in mic_ids if "loopback" in n.lower()],
    }


def enum_providers() -> dict:
    """onnxruntime 可用执行提供器 (期望含 DmlExecutionProvider 且未安装纯 CPU 覆盖包)。"""
    import onnxruntime as ort

    return {
        "providers": list(ort.get_available_providers()),
        "version": ort.__version__,
    }


def _find_onnx(model_dir: Path, pattern: str) -> Path:
    """按文件名模式定位 onnx 模型文件, 找不到则报清晰错误。"""
    hits = sorted(model_dir.glob(pattern))
    if not hits:
        raise FileNotFoundError(f"在 {model_dir} 中未找到匹配 {pattern} 的模型文件")
    return hits[0]


def asr_vad_smoke() -> dict:
    """加载流式 ASR + silero VAD, 合成音频喂一次, 验证管线不崩溃且能触发语音检测。"""
    import sherpa_onnx

    vad_dir = MODELS_DIR / "vad"
    asr_dir = MODELS_DIR / "asr"

    # ---- VAD (1.13.5: VadModel.create(VadModelConfig)) ----
    vad_cfg = sherpa_onnx.VadModelConfig(
        silero_vad=sherpa_onnx.SileroVadModelConfig(
            model=str(_find_onnx(vad_dir, "*.onnx")),
            threshold=0.5,
            min_silence_duration=0.5,
            min_speech_duration=0.25,
            window_size=512,
            max_speech_duration=10,
        ),
        sample_rate=16000,
        num_threads=2,
        provider="cpu",
    )
    vad = sherpa_onnx.VadModel.create(vad_cfg)

    # ---- ASR (1.13.5: OnlineRecognizer.from_transducer) ----
    tokens = asr_dir / "tokens.txt"
    if not tokens.exists():
        raise FileNotFoundError(f"缺少 tokens.txt: {tokens}")
    recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(tokens),
        encoder=str(_find_onnx(asr_dir, "*encoder*.onnx")),
        decoder=str(_find_onnx(asr_dir, "*decoder*.onnx")),
        joiner=str(_find_onnx(asr_dir, "*joiner*.onnx")),
        decoding_method="greedy_search",
        provider="cpu",
        num_threads=2,
    )

    # ---- 合成测试音频: 1.5s 静音 + 2s 低频正弦(触发VAD) + 1.5s 静音 ----
    sr = 16000
    silence1 = np.zeros(int(1.5 * sr), dtype=np.float32)
    t = np.arange(int(2.0 * sr), dtype=np.float32) / sr
    tone = 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
    silence2 = np.zeros(int(1.5 * sr), dtype=np.float32)
    samples = np.concatenate([silence1, tone, silence2])

    # VAD 冒烟: 按窗口喂入, is_speech 状态计数（新 API 无 segments, 自行统计切换次数）
    win = vad.window_size()  # window_size 是方法不是属性 (1.13.5)
    prev, transitions, speech_windows = False, 0, 0
    for i in range(0, len(samples) - win + 1, win):
        cur = bool(vad.is_speech(samples[i:i + win]))
        if cur != prev:
            transitions += 1
            prev = cur
        speech_windows += cur
    vad.reset()

    # ASR 冒烟: 整段解码, 验证解码循环 (1.13.5: accept_waveform(sr, wav))
    stream = recognizer.create_stream()
    stream.accept_waveform(sr, samples)
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    result = recognizer.get_result(stream)

    return {
        "vad_transitions": transitions,
        "vad_speech_windows": speech_windows,
        "asr_text": result,
        "loaded_ok": True,
    }


def main() -> None:
    report: dict = {"audio": None, "providers": None, "asr_vad": None}
    errors: dict = {}

    for name, fn in (("audio", enum_audio_devices),
                     ("providers", enum_providers),
                     ("asr_vad", asr_vad_smoke)):
        try:
            report[name] = fn()
        except Exception as exc:  # noqa: BLE001 -- spike 报告型脚本, 全部捕获
            errors[name] = f"{type(exc).__name__}: {exc}"

    print(json.dumps({"report": report, "errors": errors}, ensure_ascii=False, indent=2))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()