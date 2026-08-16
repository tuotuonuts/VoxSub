"""Pipeline (M6) 测试: 状态机 / 导出格式 / 真机 C 模式与 A 模式启停。"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from voxsub.pipeline import Pipeline, SubtitleLine


# ---------- 工具 ----------

class FakeSource:
    """假音频源: 反复吐出 16k 静音块, 直到 stop。"""

    sample_rate = 16000

    def __init__(self) -> None:
        self._stop = False

    def start(self) -> None:
        self._stop = False

    def read_chunk(self):
        if self._stop:
            return None
        return np.zeros(480, dtype=np.float32)

    def stop(self) -> None:
        self._stop = True

    def close(self) -> None:
        pass


def _test_wav() -> Path:
    """取模型包自带的真实中文语音样本 (M1 下载的 asr 包内含 test_wavs)。"""
    d = Path(os.environ["LOCALAPPDATA"]) / "VoxSub" / "models" / "asr" / "test_wavs"
    return d / "0.wav"


# ---------- 导出格式 ----------

def test_fmt_ts() -> None:
    assert Pipeline._fmt_ts(0) == "00:00:00,000"
    assert Pipeline._fmt_ts(61_500) == "00:01:01,500"
    assert Pipeline._fmt_ts(3_661_234) == "01:01:01,234"


def test_write_srt_content(tmp_path: Path) -> None:
    lines = [SubtitleLine(text="你好", translation="Hello", ts_ms=0),
             SubtitleLine(text="世界", translation="World", ts_ms=2000)]
    out = tmp_path / "t.srt"
    Pipeline.write_srt(lines, out)
    content = out.read_text(encoding="utf-8-sig")
    assert "1\n00:00:00,000 --> 00:00:01,500\n你好\nHello" in content
    assert "2\n00:00:02,000 --> 00:00:05,000\n世界\nWorld" in content  # 末句 3s


def test_write_vtt_and_txt(tmp_path: Path) -> None:
    lines = [SubtitleLine(text="你好", translation="Hello", ts_ms=0)]
    vtt = tmp_path / "t.vtt"
    txt = tmp_path / "t.txt"
    Pipeline.write_vtt(lines, vtt)
    Pipeline.write_txt(lines, txt)
    assert vtt.read_text(encoding="utf-8").startswith("WEBVTT")
    assert txt.read_text(encoding="utf-8") == "你好\tHello"


# ---------- 状态机 ----------

def test_mode_validation() -> None:
    p = Pipeline()
    p.set_mode("c")
    assert p.mode == "c"
    p.set_mode("invalid")
    assert p.mode == "c"  # 非法值被忽略


def test_start_stop_with_fake_source(monkeypatch) -> None:
    """A 模式伪源启停: 线程不崩, 回调能收到(静音无识别结果, 仅验证生命周期)。"""
    import time
    p = Pipeline()
    msgs: list[str] = []
    p.on_status(msgs.append)
    monkeypatch.setattr(p, "_make_source", lambda: FakeSource())
    p.start()
    assert p.is_running()
    time.sleep(1.0)
    p.stop()
    assert not p.is_running()
    assert any("启动" in m or "运行" in m or "停止" in m for m in msgs)


# ---------- C 模式 (真实模型) ----------

@pytest.mark.integration
def test_file_mode_real_wav(tmp_path: Path) -> None:
    """真实中文 wav → 分句识别 → srt 导出, 全链路可用。"""
    wav = _test_wav()
    if not wav.exists():
        pytest.skip("缺少 test_wavs 样本")
    p = Pipeline()
    lines, _ = p._transcribe_file(wav)
    assert len(lines) >= 1
    assert all(ln.text.strip() for ln in lines)
    # 时间戳单调递增
    ts = [ln.ts_ms for ln in lines]
    assert ts == sorted(ts)
    # 导出
    out = tmp_path / "out.srt"
    Pipeline.write_srt(lines, out)
    assert out.exists() and out.stat().st_size > 0


@pytest.mark.integration
def test_file_mode_translation_fallback(tmp_path: Path) -> None:
    """翻译模块未就绪(或失败)时, 导出不阻塞: 译文带标记。"""
    wav = _test_wav()
    if not wav.exists():
        pytest.skip("缺少 test_wavs 样本")
    p = Pipeline()

    class _Broken:
        def translate(self, *a, **k):
            raise RuntimeError("boom")

    p._translator = _Broken()  # 注入故障翻译器模拟 M4 缺失
    lines, _ = p._transcribe_file(wav)
    assert all("翻译失败" in ln.translation or ln.translation == "" for ln in lines)