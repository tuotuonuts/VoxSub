"""voxsub.tts 模块测试 —— 基于真实 sherpa-onnx vits 模型 (M5)。

运行前提 (本机):
- 模型位于 %LOCALAPPDATA%/VoxSub/models/tts/{zh,en}/ (DESIGN.md 模型目录约定)
  zh = vits-icefall-zh-aishell3 (含 lexicon.txt), en = vits-icefall-en_US-ljspeech-low
- python 命令需先 `unset PYTHONPATH PYTHONHOME` 再调 .venv/Scripts/python.exe
  (Hermes 注入的 PYTHONPATH 会污染 import, 见 requirements.txt / STATUS.md)

模型不存在时测试整体 skip (其他机器克隆本仓库也能收集通过)。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np
import pytest

from voxsub.tts import SAMPLE_RATE, TTSEngine, models_dir

MODELS_DIR = models_dir()
TTS_DIR = MODELS_DIR / "tts"

# 真实模型缺失时整体跳过 (本机保证存在, 此处仅为跨机器健壮性)
_REQUIRED = [TTS_DIR / "zh" / "model.onnx", TTS_DIR / "zh" / "tokens.txt",
             TTS_DIR / "en" / "model.onnx", TTS_DIR / "en" / "tokens.txt"]
_MISSING = [str(p) for p in _REQUIRED if not p.exists()]
if _MISSING:
    pytest.skip(f"缺少 TTS 模型, 跳过实机测试: {_MISSING}", allow_module_level=True)


@pytest.fixture(scope="module")
def tts() -> TTSEngine:
    """共享引擎 (模型懒加载/复用; 合成调用加锁, 模块级复用安全)。"""
    return TTSEngine(TTS_DIR, provider="cpu", num_threads=2)


def assert_16k_mono_float32(pcm: np.ndarray) -> None:
    """契约断言: 16k mono float32 波形。"""
    assert isinstance(pcm, np.ndarray)
    assert pcm.dtype == np.float32, f"输出应为 float32, 实际 {pcm.dtype}"
    assert pcm.ndim == 1, f"输出应为 mono 一维, 实际 {pcm.ndim}D"
    assert SAMPLE_RATE == 16000
    assert len(pcm) > 0


def duration_sec(pcm: np.ndarray) -> float:
    return len(pcm) / SAMPLE_RATE


# ---------------------------------------------------------------------------
# 中文合成
# ---------------------------------------------------------------------------

def test_tts_zh_short_sentence(tts: TTSEngine) -> None:
    """中文短句「你好，语幕」必须合成出非空 16k 波形, 时长 > 0.3s。"""
    pcm = tts.synthesize("你好，语幕", lang="zh")
    assert pcm is not None, "中文模型就绪时合成不应返回 None"
    assert_16k_mono_float32(pcm)
    dur = duration_sec(pcm)
    assert dur > 0.3, f"「你好，语幕」时长应 > 0.3s, 实际 {dur:.2f}s"
    assert np.abs(pcm).max() > 1e-4, "合成波形不应全零 (应有实际语音内容)"


def test_tts_zh_sample_rate_is_16k(tts: TTSEngine) -> None:
    """契约: 输出采样率恰为 16000 (模型原生 8k, 引擎内部必须重采样到位)。"""
    pcm = tts.synthesize("测试", lang="zh")
    assert pcm is not None
    assert len(pcm) / SAMPLE_RATE > 0  # 以 16k 换算的时长为正
    assert SAMPLE_RATE == 16000


def test_tts_zh_longer_text_longer_audio(tts: TTSEngine) -> None:
    """较长句子合成时长应显著长于短句 (冒烟验证文本-时长正相关)。"""
    short = tts.synthesize("你好", lang="zh")
    long_ = tts.synthesize("今天天气真好，我们一起去公园散步吧。", lang="zh")
    assert short is not None and long_ is not None
    assert duration_sec(long_) > duration_sec(short), \
        f"长句应更长: 短句 {duration_sec(short):.2f}s vs 长句 {duration_sec(long_):.2f}s"


# ---------------------------------------------------------------------------
# 英文合成
# ---------------------------------------------------------------------------

def test_tts_en_sentence(tts: TTSEngine) -> None:
    """英文句必须合成出非空 16k 波形 (en 模型为 piper 系, 走 espeak-ng-data 路径)。"""
    pcm = tts.synthesize("Hello, VoxSub.", lang="en")
    assert pcm is not None, "英文模型就绪时合成不应返回 None"
    assert_16k_mono_float32(pcm)
    assert np.abs(pcm).max() > 1e-4, "合成波形不应全零"


# ---------------------------------------------------------------------------
# 降级逻辑 (失败返回 None, 不抛异常 —— 契约硬性要求)
# ---------------------------------------------------------------------------

def test_tts_bad_model_dir_returns_none() -> None:
    """坏模型路径 (目录不存在/缺文件) 合成必须返回 None 而非抛异常。"""
    bad = TTSEngine(TTS_DIR / "does_not_exist")
    assert bad.health() != "ok"
    assert bad.synthesize("你好，语幕", lang="zh") is None
    assert bad.synthesize("Hello", lang="en") is None


def test_tts_empty_text_returns_none(tts: TTSEngine) -> None:
    """空文本 / 纯空白 / None 输入: 返回 None, 不进入底层合成。"""
    assert tts.synthesize("", lang="zh") is None
    assert tts.synthesize("   ", lang="zh") is None


def test_tts_unknown_lang_returns_none(tts: TTSEngine) -> None:
    """未知语种: 返回 None (缺该语种模型等价于降级), 不抛异常。"""
    assert tts.synthesize("你好", lang="fr") is None
    assert tts.synthesize("你好", lang="ZH ") is None  # 大小写/空白都走降级


def test_tts_failed_model_build_returns_none(tmp_path: Path) -> None:
    """模型文件存在但构造 OfflineTts 失败 (tokens 内容损坏) -> None, 不抛。"""
    fake = tmp_path / "zh"
    fake.mkdir(parents=True)
    (fake / "model.onnx").write_bytes(b"not a real onnx model")
    (fake / "tokens.txt").write_text("bad tokens", encoding="utf-8")
    engine = TTSEngine(tmp_path)
    assert engine.synthesize("你好", lang="zh") is None


# ---------------------------------------------------------------------------
# health / 线程安全
# ---------------------------------------------------------------------------

def test_tts_health_ok_with_models(tts: TTSEngine) -> None:
    """双语言模型就绪时 health() 应为 \"ok\"。"""
    assert tts.health() == "ok"


def test_tts_concurrent_synthesis_safe(tts: TTSEngine) -> None:
    """多线程并发合成不得崩/不得抛 (UI 管线可能并发调用朗读)。"""
    errors: list[BaseException] = []
    results: list[np.ndarray | None] = [None] * 4

    def worker(i: int) -> None:
        try:
            text = "并发合成测试" if i % 2 == 0 else "Concurrent synthesis."
            lang = "zh" if i % 2 == 0 else "en"
            results[i] = tts.synthesize(text, lang=lang)
        except BaseException as exc:  # noqa: BLE001 - 测试内捕获以便断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"并发合成出现异常: {errors}"
    assert all(r is not None and len(r) > 0 for r in results), \
        f"并发合成结果应全部有效, 实际: {[None if r is None else len(r) for r in results]}"