"""语幕 VoxSub —— audio 采集模块（M2）。

Windows 音频采集层: 麦克风 (MicSource) 与系统声音 (LoopbackSource)。
对所有下游 (VAD/ASR 流水线) 统一输出 float32 一维、16000 Hz 单声道音频块。

底层依赖 soundcard 0.4.6 (Windows 走 mediafoundation.py 的 WASAPI 封装)。实测要点:

- 枚举: `sc.all_microphones(include_loopback=True)` 返回的列表前部是 loopback
  "虚拟麦克风" (每个扬声器端点一个, `isloopback=True`, 设备 id 与对应扬声器
  完全相同, 形如 ``{0.0.0.00000000}.{GUID}``), 后部才是真实麦克风
  (``{0.0.1.00000000}.{GUID}``)。loopback 的 *名字就是扬声器名*, 本机 6 个
  全部不含 "loopback" 字样 —— 区分 loopback 必须用 ``isloopback`` 属性,
  绝不能按名字关键词过滤 (DESIGN.md 中"按名字过滤"的写法已过时, 会全部漏掉)。
- 重采样: ``Recorder(samplerate=16000)`` 由 WASAPI 层自动完成 (AUTOCONVERTPCM
  标志, 实测可用)。本模块另备 ``resample_16k()`` numpy 线性插值作兜底工具。
- ``record(numframes)`` 返回 (frames, channels) float32 数组, 此处 channels=1。
- 采集生命周期: ``Recorder`` 是上下文管理器, ``__enter__`` = Start capture,
  ``__exit__`` = Stop + 释放 COM 指针; ``__enter__`` 只能进入一次。
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import NamedTuple, Optional

import numpy as np

import soundcard as sc

#: 流水线统一采样率 (Hz)
SAMPLE_RATE: int = 16000
#: 单块帧数 (480 = 30ms @16k)。VAD 窗口 512 样本, 30ms 块粒度适中。
CHUNK_FRAMES: int = 480


class AudioDeviceInfo(NamedTuple):
    """单个音频输入设备信息; device 为 soundcard 设备对象, 可直接传给 AudioSource。"""

    name: str
    kind: str            # "mic" | "loopback"
    device: object       # soundcard _Microphone 设备对象


def _enum_mics(include_loopback: bool) -> list:
    """枚举底层麦克风列表 (含/不含 loopback 虚拟麦克风)。"""
    return sc.all_microphones(include_loopback=include_loopback)


def list_microphones(include_loopback: bool = True) -> list[AudioDeviceInfo]:
    """列出真实麦克风 (kind="mic")。

    include_loopback=True 时底层枚举同样包含 loopback 虚拟麦克风, 但返回时
    一律按 ``isloopback`` 标志过滤 —— loopback 设备统一由 list_loopbacks() 提供,
    两个列表互不重叠。注意: 不能按名字过滤 (loopback 名字 = 扬声器名)。
    """
    result: list[AudioDeviceInfo] = []
    for dev in _enum_mics(include_loopback=include_loopback):
        if include_loopback and getattr(dev, "isloopback", False):
            continue  # 虚拟扬声器端点, 不算真实麦克风
        result.append(AudioDeviceInfo(name=dev.name, kind="mic", device=dev))
    return result


def list_loopbacks() -> list[AudioDeviceInfo]:
    """列出系统声音 loopback 设备 (kind="loopback"), 与扬声器端点一一对应。

    soundcard 把每个扬声器包成 isloopback=True 的"虚拟麦克风"; 其设备 id
    与对应扬声器完全相同。判断一律用 ``getattr(dev, 'isloopback', False)``。
    """
    result: list[AudioDeviceInfo] = []
    for dev in _enum_mics(include_loopback=True):
        if getattr(dev, "isloopback", False):
            result.append(AudioDeviceInfo(name=dev.name, kind="loopback", device=dev))
    return result


def resample_16k(samples: np.ndarray, src_rate: int) -> np.ndarray:
    """numpy 线性插值重采样到 16 kHz, 返回 float32 一维数组。

    正常采集路径由 soundcard/WASAPI 自动重采样, 本函数是兜底工具
    (例如未来任意采样率的文件提轨输入)。src_rate 已是 16000 时原样返回。
    """
    samples = np.asarray(samples, dtype=np.float32)
    if src_rate == SAMPLE_RATE:
        return samples
    n_out = int(round(len(samples) * SAMPLE_RATE / src_rate))
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


class AudioSource(ABC):
    """音频源抽象基类: 对下游统一输出 float32 一维 16 kHz 单声道块。"""

    sample_rate: int = SAMPLE_RATE

    @abstractmethod
    def start(self) -> None:
        """打开设备并开始采集。重复调用无副作用 (幂等)。"""

    @abstractmethod
    def read_chunk(self) -> Optional[np.ndarray]:
        """读取一块音频 (~30ms)。未启动或已停止时返回 None。"""

    @abstractmethod
    def stop(self) -> None:
        """停止采集。可随时从其它线程调用 (内部有锁, 后续 UI 线程会调)。"""

    @abstractmethod
    def close(self) -> None:
        """释放全部资源, 之后不可再用。"""


class _SoundcardSource(AudioSource):
    """基于 soundcard Recorder 的通用实现 (MicSource / LoopbackSource 共用)。

    生命周期: start() 创建 Recorder 并 ``__enter__`` (启动 WASAPI capture);
    stop()  ``__exit__`` (停止 + 释放 COM 指针); close() = stop()。
    线程安全: 一把锁保护 start/stop/read_chunk —— read_chunk 持锁期间
    record() 至多阻塞一块时长 (~几十 ms), 其它线程调 stop() 至多等这么久。
    """

    def __init__(self, device: object | None, chunk_frames: int = CHUNK_FRAMES,
                 allow_failover: bool = False) -> None:
        self._device = device
        self._chunk_frames = int(chunk_frames)
        self._allow_failover = allow_failover   # 默认设备打不开时自动换可用设备
        self._recorder = None            # soundcard _Recorder | None
        self._lock = threading.Lock()

    def _make_recorder_for(self, device: object):
        """为指定设备创建并启动 Recorder (16k 单声道; WASAPI 自动重采样)。"""
        rec = device.recorder(
            samplerate=self.sample_rate,
            channels=1,
            blocksize=self._chunk_frames,
        )
        rec.__enter__()  # Start capture (Recorder 是上下文管理器)
        return rec

    # -- AudioSource 实现 --
    def start(self) -> None:
        with self._lock:
            if self._recorder is not None:
                return  # 已启动, 幂等
            last_err: Exception | None = None
            for dev in self._device_iter():
                try:
                    self._recorder = self._make_recorder_for(dev)
                    self._device = dev
                    return
                except Exception as exc:  # 该设备不可用 (如 WAVEFORMATEX 旧格式)
                    last_err = exc
                    continue
            raise RuntimeError(
                f"无法打开任何音频输入设备 (共尝试 {self._candidate_count()} 个): {last_err}"
            ) from last_err

    def _device_iter(self):
        """候选设备: 首选默认设备; 允许 failover 时依次尝试其它可用设备。"""
        if self._device is not None:
            yield self._device
        if self._allow_failover:
            for info in self._failover_candidates():
                if info.device is not self._device:
                    yield info.device

    # 子类覆写: failover 候选列表 (list[AudioDeviceInfo])
    def _failover_candidates(self) -> list:
        return []

    def _candidate_count(self) -> int:
        n = 1 if self._device is not None else 0
        if self._allow_failover:
            n += len(self._failover_candidates())
        return n

    def read_chunk(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._recorder is None:
                return None  # 未启动或已停止
            data = self._recorder.record(self._chunk_frames)
        # record() 返回 (frames, channels) float32; channels=1 拍平为一维
        return np.asarray(data, dtype=np.float32).reshape(-1)

    def stop(self) -> None:
        with self._lock:
            rec, self._recorder = self._recorder, None
            if rec is None:
                return
            try:
                rec.__exit__(None, None, None)  # Stop + 释放 COM 指针
            except Exception:
                # 设备被拔除/变更时 COM 释放可能抛错; 清理路径不向上扩散
                pass

    def close(self) -> None:
        self.stop()


def _default_loopback_device() -> object:
    """默认 loopback 设备: 与系统默认扬声器同一端点者优先 (会议/网课场景
    用户听得见的就是默认扬声器), 找不到则退回列表第一个。"""
    loops = list_loopbacks()
    if not loops:
        raise RuntimeError("本机未发现 loopback 设备 (没有可用扬声器?)")
    try:
        default_id = sc.default_speaker().id
    except Exception:
        default_id = None
    for lp in loops:
        if lp.device.id == default_id:
            return lp.device
    return loops[0].device


class MicSource(_SoundcardSource):
    """默认麦克风采集源 (对话场景)。

    device 可传 AudioDeviceInfo.device 或 soundcard 麦克风对象;
    缺省使用系统默认麦克风。注意本机有 WAVEFORMATEX 旧格式麦克风
    (soundcard 0.4.6 会断言拒绝), 若默认麦克风恰好打不开, 无参构造时
    会自动回退到第一个可用的真实麦克风; 显式传 device 则失败即报错。
    """

    def __init__(self, device: object | None = None,
                 chunk_frames: int = CHUNK_FRAMES) -> None:
        if device is None:
            device = sc.default_microphone()
        super().__init__(device, chunk_frames, allow_failover=True)

    def _failover_candidates(self) -> list:
        return list_microphones()


class LoopbackSource(_SoundcardSource):
    """系统声音采集源 (WASAPI loopback, 会议/网课"对方声音"场景)。

    device 可传 AudioDeviceInfo.device 或 soundcard 麦克风对象;
    缺省时自动选择与系统默认扬声器对应的 loopback 端点; 打不开时
    回退到第一个可用的 loopback。显式传 device 则失败即报错。
    """

    def __init__(self, device: object | None = None,
                 chunk_frames: int = CHUNK_FRAMES) -> None:
        if device is None:
            device = _default_loopback_device()
        super().__init__(device, chunk_frames, allow_failover=True)

    def _failover_candidates(self) -> list:
        return list_loopbacks()