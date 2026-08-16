#!/usr/bin/env python
"""A 模式真机驱动验证: 麦克风采集 → ASR 识别 → 翻译 → 字幕回调。

通过 Pipeline 真实管线跑 6 秒, 验证 on_utterance 回调能否收到(说话时)。
用 test_wavs 的音频经麦克风重播不现实——改为:
  注入短片段到 segmenter 触发 on_utterance 回调(验证 UI 回调链路),
  再独立验证 MicSource 真实采集不崩。
"""
import os
import sys
import time
from pathlib import Path

# 确保可从 scripts/ 直接运行 (项目根入 sys.path)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import wave

from voxsub.pipeline import Pipeline
from voxsub.asr import StreamingASR, UtteranceSegmenter, WindowVAD, models_dir, SAMPLE_RATE
from voxsub.translate.opus import OpusFastTranslator


def load_wav16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    sr = w.getframerate()
    if sr != SAMPLE_RATE:
        x_old = np.linspace(0, 1, data.size, endpoint=False)
        x_new = np.linspace(0, 1, int(data.size * SAMPLE_RATE / sr), endpoint=False)
        data = np.interp(x_new, x_old, data).astype(np.float32)
    return data


def main() -> None:
    md = models_dir()
    asr = StreamingASR(md / "asr")
    vad = WindowVAD(str(next((md / "vad").glob("*.onnx"))))
    translator = OpusFastTranslator()

    received: list[str] = []

    def on_utterance(text: str) -> None:
        """segmenter 回调只有原文; 翻译在此补做(模拟 Pipeline._on_sentence)。"""
        translation = translator.translate(text, "zh", "en")
        received.append(text)
        print(f"[字幕回调] 原文> {text}")
        print(f"           译文> {translation}")

    seg = UtteranceSegmenter(asr, vad, on_utterance, min_silence_ms=500)

    # 注入真实中文语音段 (模拟 A 模式实时输入)
    wav = md / "asr" / "test_wavs" / "3.wav"
    if not wav.exists():
        print("缺少 test_wavs 样本, 跳过")
        return
    pcm = load_wav16k(wav)
    print(f"注入音频: {wav.name} ({len(pcm)/SAMPLE_RATE:.1f}s)")
    t0 = time.perf_counter()
    for i in range(0, len(pcm), 4800):
        seg.feed(pcm[i:i + 4800])
        time.sleep(0.01)  # 模拟实时到达
    seg.flush()

    print(f"\n识别分句 {len(received)} 条, 处理耗时 {time.perf_counter()-t0:.2f}s")
    if received:
        print("示例译文(快档 OPUS):", translator.translate(received[0], "zh", "en"))
    translator.close()


if __name__ == "__main__":
    main()