#!/usr/bin/env python
"""一次性 API 试错: 找出 sherpa-onnx 1.13.5 的 OnlineRecognizer/VadModel 正确构造方式。"""
import numpy as np
import sherpa_onnx as so
from pathlib import Path

MP = Path(__file__).resolve().parents[1] / ".spike_models"
# 实际模型在 LOCALAPPDATA, 从环境取
import os
MP = Path(os.environ["LOCALAPPDATA"]) / "VoxSub" / "models"
VAD = MP / "vad" / "silero_vad_v5.onnx"
ASR = MP / "asr"
enc = str(next(ASR.glob("*encoder*.onnx")))
dec = str(next(ASR.glob("*decoder*.onnx")))
join = str(next(ASR.glob("*joiner*.onnx")))
tok = str(ASR / "tokens.txt")

# ---- 尝试 1: from_transducer 工厂 ----
try:
    r = so.OnlineRecognizer.from_transducer(
        tokens=tok, encoder=enc, decoder=dec, joiner=join,
        decoding_method="greedy_search", provider="cpu", num_threads=2)
    print("T1 from_transducer: OK")
    stream = r.create_stream()
    sr = 16000
    wav = np.zeros(sr, dtype=np.float32)
    stream.accept_waveform(wav, sr)
    while r.is_ready(stream):
        r.decode_stream(stream)
    print("   decode-loop OK, result:", repr(r.get_result(stream)))
except Exception as e:
    print(f"T1 from_transducer: FAIL {type(e).__name__}: {e}")

# ---- 尝试 2: 直构 kwargs ----
try:
    r2 = so.OnlineRecognizer(
        tokens=tok, encoder=enc, decoder=dec, joiner=join,
        decoding_method="greedy_search", provider="cpu", num_threads=2)
    print("T2 OnlineRecognizer(...): OK")
except Exception as e:
    print(f"T2 OnlineRecognizer(...): FAIL {type(e).__name__}: {e}")

# ---- 尝试 3: VadModel 构造 ----
try:
    v = so.VadModel(silero_vad_model=VAD)
    print("T3 VadModel(silero_vad_model=): OK", v)
except Exception as e:
    print(f"T3 VadModel(silero_vad_model=): FAIL {type(e).__name__}: {e}")
try:
    v = so.VadModel(model=VAD)
    print("T4 VadModel(model=): OK", v)
except Exception as e:
    print(f"T4 VadModel(model=): FAIL {type(e).__name__}: {e}")
try:
    v = so.VadModel.create(silero_vad_model=VAD)
    print("T5 VadModel.create(silero_vad_model=): OK", v)
except Exception as e:
    print(f"T5 VadModel.create(...): FAIL {type(e).__name__}: {e}")
try:
    v = so.VadModel(silero_vad=so.SileroVadModelConfig(model=VAD))
    print("T6 VadModel(silero_vad=SileroVadModelConfig): OK", v)
except Exception as e:
    print(f"T6 VadModel(silero_vad=...): FAIL {type(e).__name__}: {e}")