"""voxsub/audio 模块测试（M2）。

分层:
- 单元测试: 用 fake 设备对象验证 isloopback 过滤逻辑 / resample / 抽象基类约束
  (不依赖真机)。
- 真机测试: 枚举测试 (麦克风/loopback 数量与类型)。
- 集成测试 (@pytest.mark.integration): 本机有声卡, 默认全部执行。
  - loopback 闭环: 默认扬声器播放合成正弦, 匹配端点的 loopback 必须采到信号,
    停止播放后恢复静音 —— 这是 M2 最关键的验收。
  - MicSource: start/read_chunk/stop/close 生命周期冒烟。

运行: cd VoxSub && unset PYTHONPATH PYTHONHOME && .venv/Scripts/python.exe -m pytest tests/test_audio.py -v
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

# 保证 `python -m pytest` / 裸 `pytest` 都能 import 到项目根下的 voxsub 包
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import soundcard as sc  # noqa: E402

import voxsub.audio as audio  # noqa: E402
from voxsub.audio import (  # noqa: E402
    AudioDeviceInfo,
    AudioSource,
    LoopbackSource,
    MicSource,
    list_loopbacks,
    list_microphones,
    resample_16k,
)


# ---------------------------------------------------------------------------
# 单元: fake 设备过滤逻辑 (不依赖真机)
# ---------------------------------------------------------------------------
class _FakeMic:
    """模拟 soundcard _Microphone: isloopback 属性可选 (None=老设备无此属性)。"""

    def __init__(self, name: str, isloopback: bool | None = None) -> None:
        self.name = name
        self.id = f"fake-{name}"
        if isloopback is not None:
            self.isloopback = isloopback


class _FakeSC:
    """模拟 soundcard 模块: 复刻 all_microphones(include_loopback=...) 行为。"""

    def __init__(self, devices: list) -> None:
        self._devices = devices

    def all_microphones(self, include_loopback: bool = False) -> list:
        if not include_loopback:
            return [d for d in self._devices if not getattr(d, "isloopback", False)]
        return list(self._devices)


@pytest.fixture
def fake_sc(monkeypatch):
    devices = [
        _FakeMic("Speaker A (loopback 字样)", isloopback=True),
        # 关键场景: 名字完全不含 "loopback", 必须靠 isloopback 属性识别
        _FakeMic("Speaker B", isloopback=True),
        _FakeMic("Real Mic 1", isloopback=False),
        # 防御: 某些设备可能没有 isloopback 属性, 应视为真实麦克风
        _FakeMic("Legacy Mic", None),
    ]
    monkeypatch.setattr(audio, "sc", _FakeSC(devices))
    return devices


def test_list_microphones_filters_loopback_by_flag(fake_sc) -> None:
    """list_microphones 只返回真实麦克风, loopback(含名字无关键字者)必须被过滤。"""
    mics = list_microphones()
    names = [m.name for m in mics]
    assert all(m.kind == "mic" for m in mics)
    assert all(isinstance(m, AudioDeviceInfo) for m in mics)
    assert "Real Mic 1" in names
    assert "Legacy Mic" in names
    # 两个 loopback 都不能混进麦克风列表 —— 即使名字里带 "loopback" 字样
    assert "Speaker A (loopback 字样)" not in names
    assert "Speaker B" not in names


def test_list_loopbacks_keeps_only_isloopback(fake_sc) -> None:
    """list_loopbacks 只保留 isloopback=True 的虚拟麦克风。"""
    loops = list_loopbacks()
    assert [m.name for m in loops] == ["Speaker A (loopback 字样)", "Speaker B"]
    assert all(m.kind == "loopback" for m in loops)
    assert all(getattr(m.device, "isloopback", False) for m in loops)


def test_list_microphones_param_false_skips_loopback_enum(fake_sc) -> None:
    """include_loopback=False 时连枚举都不带 loopback。"""
    mics = list_microphones(include_loopback=False)
    assert [m.name for m in mics] == ["Real Mic 1", "Legacy Mic"]
    assert all(m.kind == "mic" for m in mics)


def test_audio_source_is_abstract() -> None:
    """AudioSource 是抽象基类, 不能直接实例化。"""
    with pytest.raises(TypeError):
        AudioSource()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# 单元: resample_16k 兜底重采样 (numpy 线性插值)
# ---------------------------------------------------------------------------
def test_resample_16k_identity() -> None:
    """16k 输入原样返回。"""
    x = np.sin(2 * np.pi * 440 * np.arange(1600) / 16000).astype(np.float32)
    y = resample_16k(x, 16000)
    assert y.dtype == np.float32
    assert y.shape == x.shape
    assert np.allclose(y, x, atol=1e-6)


def test_resample_16k_upsample_8k() -> None:
    """8k -> 16k: 长度翻倍且波形保持 (与参考正弦高度相关)。"""
    x = np.sin(2 * np.pi * 440 * np.arange(800) / 8000).astype(np.float32)
    y = resample_16k(x, 8000)
    ref = np.sin(2 * np.pi * 440 * np.arange(1600) / 16000).astype(np.float32)
    assert y.dtype == np.float32
    assert y.shape == (1600,)
    assert float(np.corrcoef(y, ref)[0, 1]) > 0.99


def test_resample_16k_downsample_48k() -> None:
    """48k -> 16k: 长度缩到 1/3。"""
    x = np.sin(2 * np.pi * 440 * np.arange(4800) / 48000).astype(np.float32)
    y = resample_16k(x, 48000)
    assert y.dtype == np.float32
    assert y.shape == (1600,)


# ---------------------------------------------------------------------------
# 真机: 设备枚举
# ---------------------------------------------------------------------------
def test_list_microphones_real_nonempty() -> None:
    """本机真实麦克风枚举: 非空, 全部 kind=mic, 无 loopback 混入。"""
    mics = list_microphones()
    assert len(mics) > 0
    assert all(m.kind == "mic" for m in mics)
    assert all(not getattr(m.device, "isloopback", False) for m in mics)


def test_list_loopbacks_real_paired_with_speakers() -> None:
    """本机 loopback 枚举: 非空, 全部 kind=loopback, 与扬声器端点一一对应。

    soundcard 的 loopback 是"虚拟麦克风", 其设备 id 与对应扬声器完全相同,
    因此 loopback 集合 != 空 且 == 扬声器 id 集合。
    """
    loops = list_loopbacks()
    assert len(loops) > 0
    assert all(m.kind == "loopback" for m in loops)
    assert all(getattr(m.device, "isloopback", False) for m in loops)
    assert {m.device.id for m in loops} == {s.id for s in sc.all_speakers()}


# ---------------------------------------------------------------------------
# 集成: 真机声卡闭环 (本机有声卡, 默认执行)
# ---------------------------------------------------------------------------
def _pick_loopback_for(spk) -> AudioDeviceInfo:
    """选与指定扬声器同一端点的 loopback, 找不到退到第一个。"""
    loops = list_loopbacks()
    assert loops, "本机无 loopback 设备, 无法闭环"
    for lp in loops:
        if lp.device.id == spk.id:
            return lp
    return loops[0]


@pytest.mark.integration
def test_loopback_closure_sine() -> None:
    """loopback 闭环验收: 默认扬声器播 2s 440Hz 正弦, 匹配端点的 loopback
    必须采到 (功率 > 1e-4); 停止播放后采集窗口恢复静音 (< 1e-3)。"""
    sr = 16000
    duration = 2.0
    t = np.arange(int(sr * duration), dtype=np.float32) / sr
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    spk = sc.default_speaker()
    loop = _pick_loopback_for(spk)
    src = LoopbackSource(device=loop.device)
    src.start()
    try:
        # 预热: 冲掉启动/缓冲里的历史数据, 避免误判
        for _ in range(3):
            src.read_chunk()

        # 后台线程播放 —— play() 会阻塞到 2s 数据全部送入设备缓冲
        player = threading.Thread(
            target=lambda: spk.play(tone.copy(), samplerate=sr), daemon=True
        )
        player.start()

        chunks = []
        t_end = time.perf_counter() + duration + 0.35
        while time.perf_counter() < t_end:
            c = src.read_chunk()
            if c is not None:
                # read_chunk 契约: float32 一维
                assert c.ndim == 1 and c.dtype == np.float32
                chunks.append(c)
        assert chunks, "loopback 采集窗口内未读到任何块"
        sig = np.concatenate(chunks)
        power = float(np.mean(sig.astype(np.float64) ** 2))
        assert power > 1e-4, f"loopback 未采到播放信号, power={power:.2e}"

        player.join(timeout=10)
        assert src.sample_rate == 16000

        # 停止播放后: 等待尾部声音排出, 再采 0.8s 应静音
        time.sleep(0.4)
        sil_chunks = []
        t_end = time.perf_counter() + 0.8
        while time.perf_counter() < t_end:
            c = src.read_chunk()
            if c is not None:
                sil_chunks.append(c)
        sil = np.concatenate(sil_chunks) if sil_chunks else np.zeros(480, np.float32)
        sil_power = float(np.mean(sil.astype(np.float64) ** 2))
        assert sil_power < 1e-3, f"停止播放后非静音, power={sil_power:.2e}"
    finally:
        src.stop()
        src.close()


@pytest.mark.integration
def test_loopback_chunk_format() -> None:
    """LoopbackSource 输出格式契约: 1D float32 / 480 帧 / 16k 属性。"""
    loops = list_loopbacks()
    assert loops, "本机无 loopback 设备"
    src = LoopbackSource(device=loops[0].device)
    src.start()
    try:
        c = src.read_chunk()
        assert c is not None
        assert c.ndim == 1
        assert c.dtype == np.float32
        assert c.shape[0] == 480
        assert src.sample_rate == 16000
    finally:
        src.stop()
        src.close()
    assert src.read_chunk() is None  # 停止后 read_chunk 返回 None


@pytest.mark.integration
def test_mic_source_smoke() -> None:
    """MicSource 生命周期冒烟: start/read_chunk/stop/close 不崩, 块格式正确。"""
    src = MicSource()
    try:
        src.start()
    except RuntimeError as exc:
        # CI/沙箱会以不同 Windows 身份运行，常没有麦克风隐私授权或输入设备。
        # 这属于主机能力而非生命周期契约失败；实际错误可见性由 Pipeline 单测覆盖。
        pytest.skip(f"当前测试环境不可访问麦克风: {exc}")
    try:
        chunks = [src.read_chunk() for _ in range(10)]  # ~0.3s
        live = [c for c in chunks if c is not None]
        assert live, "MicSource 生命周期内应持续产出音频块"
        for c in live:
            assert c.ndim == 1
            assert c.dtype == np.float32
        assert src.sample_rate == 16000
    finally:
        src.stop()
        src.close()
    assert src.read_chunk() is None  # 停止后 read_chunk 返回 None
