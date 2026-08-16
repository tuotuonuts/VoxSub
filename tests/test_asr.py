"""voxsub.asr 模块测试 —— 全部基于真实模型与真实中文语音素材 (test_wavs)。

运行前提 (本机):
- 模型位于 %LOCALAPPDATA%/VoxSub/models/{asr,vad}/ (DESIGN.md 模型目录约定)
- python 命令需先 `unset PYTHONPATH PYTHONHOME` 再调 .venv/Scripts/python.exe
  (Hermes 注入的 PYTHONPATH 会污染 import, 见 requirements.txt / STATUS.md)

模型不存在时测试整体 skip (其他机器克隆本仓库也能收集通过)。
"""
from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np
import pytest

from voxsub.asr import StreamingASR, UtteranceSegmenter, WindowVAD

MODELS_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "VoxSub" / "models"
ASR_DIR = MODELS_DIR / "asr"
VAD_MODEL = MODELS_DIR / "vad" / "silero_vad_v5.onnx"
TEST_WAVS = ASR_DIR / "test_wavs"
SAMPLE_RATE = 16000

# 真实素材缺失时整体跳过 (本机保证存在, 此处仅为跨机器健壮性)
_REQUIRED = [ASR_DIR / "tokens.txt", VAD_MODEL, TEST_WAVS / "1.wav", TEST_WAVS / "3.wav"]
_MISSING = [str(p) for p in _REQUIRED if not p.exists()]
if _MISSING:
    pytest.skip(f"缺少模型/素材, 跳过 asr 实机测试: {_MISSING}", allow_module_level=True)


def load_wav16k(path: Path) -> np.ndarray:
    """读取 wav 为 float32 mono 16k。

    采样率/声道不合规时自动适配: 多声道取均值, 非 16k 用 numpy 线性插值重采样。
    """
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    if sr != SAMPLE_RATE:
        x_old = np.linspace(0.0, 1.0, len(data), endpoint=False)
        x_new = np.linspace(0.0, 1.0, int(len(data) * SAMPLE_RATE / sr), endpoint=False)
        data = np.interp(x_new, x_old, data).astype(np.float32)
    return data


@pytest.fixture(scope="module")
def asr() -> StreamingASR:
    """流式识别器: 模块级共享 (模型加载 ~1s, 各测试间流对象相互独立, 可安全复用)。"""
    return StreamingASR(ASR_DIR, provider="cpu", num_threads=2)


@pytest.fixture
def vad() -> WindowVAD:
    """VAD: 函数级隔离 (内部状态机有记忆, 避免测试间串扰)。"""
    return WindowVAD(str(VAD_MODEL))


# ---------------------------------------------------------------------------
# WindowVAD
# ---------------------------------------------------------------------------

def test_window_vad_detects_real_speech(vad: WindowVAD) -> None:
    """真实中文语音 wav 逐窗口喂入, 必须出现过 is_speech=True。"""
    wav = load_wav16k(TEST_WAVS / "1.wav")
    win = vad.window_size
    # 注意: 本机实测 window_size 为 576 (sherpa 1.13.5 对 silero v5 的窗口做
    # 了对齐, 并非文档常写的 512) —— 所以一律以 vad.window_size 属性为准,
    # 绝不硬编码窗口长度。仅断言其为合法正数且为偶数 (silero 要求)。
    assert win > 0 and win % 2 == 0
    seen = False
    for i in range(0, len(wav) - win + 1, win):
        if vad.is_speech(wav[i:i + win]):
            seen = True
            break
    vad.reset()  # 状态机复位, 保证后续测试干净
    assert seen, "真实语音 wav 应至少触发一个语音窗口"


def test_window_vad_pure_silence_all_false(vad: WindowVAD) -> None:
    """纯静音数组逐窗口喂入, 必须全部为 False。"""
    win = vad.window_size
    silence = np.zeros(win * 60, dtype=np.float32)  # ~2s 静音, 余量充足
    flags = [vad.is_speech(silence[i:i + win]) for i in range(0, len(silence) - win + 1, win)]
    vad.reset()
    assert not any(flags), "纯静音不应触发语音检测"


def test_window_vad_rejects_wrong_window_size(vad: WindowVAD) -> None:
    """窗口长度必须恰为 window_size, 否则应报清晰错误 (而非底层 VAD 崩溃/静默)。"""
    with pytest.raises(ValueError):
        vad.is_speech(np.zeros(vad.window_size - 1, dtype=np.float32))
    vad.reset()


# ---------------------------------------------------------------------------
# StreamingASR
# ---------------------------------------------------------------------------

def test_streaming_asr_recognizes_real_wav(asr: StreamingASR) -> None:
    """整段真实中文语音送入流式识别器, 必须解码出非空文本。"""
    wav = load_wav16k(TEST_WAVS / "1.wav")
    stream = asr.create_stream()
    asr.feed(stream, wav)
    text = asr.decode(stream)
    assert text.strip() != "", f"真实语音应识别出文本, 得到空串 ({TEST_WAVS / '1.wav'})"


def test_streaming_asr_partial_result_nonempty_after_feed(asr: StreamingASR) -> None:
    """feed 后立即 get_result 应能拿到累计文本 (feed 内部增量解码契约)。"""
    wav = load_wav16k(TEST_WAVS / "0.wav")
    stream = asr.create_stream()
    # 只喂前 3 秒, 且按 0.5s 块喂入, 模拟流式节奏
    prefix = wav[: SAMPLE_RATE * 3]
    for i in range(0, len(prefix), SAMPLE_RATE // 2):
        asr.feed(stream, prefix[i:i + SAMPLE_RATE // 2])
    partial = asr.get_result(stream)
    assert partial.strip() != "", "feed 3s 真实语音后应已有部分识别结果"


# ---------------------------------------------------------------------------
# UtteranceSegmenter
# ---------------------------------------------------------------------------

def test_segmenter_real_wav_single_callback(asr: StreamingASR, vad: WindowVAD) -> None:
    """一段真实中文语音以 480 样本块喂入 (故意跨 512 窗切块), 必须只回调一次非空文本。"""
    calls: list[str] = []
    seg = UtteranceSegmenter(asr, vad, on_utterance=calls.append, min_silence_ms=500)
    wav = load_wav16k(TEST_WAVS / "1.wav")
    chunk = 480
    for i in range(0, len(wav), chunk):
        seg.feed(wav[i:i + chunk])
    seg.flush()  # 文件尾静音已自然触发, flush 兜底不应产生第二次回调
    assert len(calls) == 1, f"一段语音应只回调一次, 实际 {len(calls)} 次: {calls}"
    assert calls[0].strip() != "", "回调文本不应为空"


def test_segmenter_silence_no_callback(asr: StreamingASR, vad: WindowVAD) -> None:
    """纯静音喂入不应触发任何回调。"""
    calls: list[str] = []
    seg = UtteranceSegmenter(asr, vad, on_utterance=calls.append, min_silence_ms=500)
    seg.feed(np.zeros(SAMPLE_RATE, dtype=np.float32))  # 1s 静音
    seg.feed(np.zeros(SAMPLE_RATE * 2, dtype=np.float32))  # 再 2s, 总静音 3s >> 500ms
    seg.flush()
    assert calls == [], f"静音不应回调, 实际: {calls}"


def test_segmenter_flush_forces_callback(asr: StreamingASR, vad: WindowVAD) -> None:
    """尾静音未达阈值时 flush() 必须强制结束当前段并回调累计文本。"""
    calls: list[str] = []
    seg = UtteranceSegmenter(asr, vad, on_utterance=calls.append, min_silence_ms=500)
    wav = load_wav16k(TEST_WAVS / "3.wav")
    seg.feed(wav[: SAMPLE_RATE * 2])  # 只喂前 2s 语音, 不喂尾静音
    assert calls == [], "静音未达阈值前不应提前回调"
    seg.flush()
    assert len(calls) == 1, f"flush 应强制回调一次, 实际 {len(calls)} 次: {calls}"
    assert calls[0].strip() != "", "flush 回调文本不应为空"


def test_segmenter_flush_when_idle_no_callback(asr: StreamingASR, vad: WindowVAD) -> None:
    """静音态 (无活跃流) 下 flush() 不应产生回调。"""
    calls: list[str] = []
    seg = UtteranceSegmenter(asr, vad, on_utterance=calls.append, min_silence_ms=500)
    seg.flush()
    seg.flush()  # 连调两次也要安全
    assert calls == []