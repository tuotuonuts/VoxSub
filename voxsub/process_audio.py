"""Windows 按应用/窗口捕获：WASAPI process loopback → 16k mono 流。

底层使用 Windows 10 2004+ 的 ``AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK``。
目标 PID 的子进程树会一并捕获，适用于 Chrome/Edge 等多进程应用；其它应用和
系统提示音不会进入 VoxSub。音频只进入内存队列，不落盘。
"""
from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from voxsub.audio import AudioSource, CHUNK_FRAMES, SAMPLE_RATE, resample_16k
from voxsub.logging_setup import get_logger

logger = get_logger("process_audio")


@dataclass(frozen=True)
class CaptureTarget:
    """一个可选择的可见窗口及其所属进程。"""

    pid: int
    process_name: str
    window_title: str

    @property
    def label(self) -> str:
        title = self.window_title.strip()
        return f"{self.process_name} — {title}" if title else self.process_name


def list_capture_targets() -> list[CaptureTarget]:
    """枚举可见顶层窗口，按 PID 去重并排除 VoxSub 自身。"""
    try:
        from recap.discovery import list_windows
    except ImportError:
        logger.warning("recap-capture 未安装，无法枚举按应用捕获目标")
        return []

    try:
        import psutil
    except ImportError:
        psutil = None

    by_pid: dict[int, CaptureTarget] = {}
    for win in list_windows():
        pid = int(getattr(win, "pid", 0) or 0)
        title = str(getattr(win, "title", "") or "").strip()
        if pid <= 0 or pid == os.getpid() or not title:
            continue
        process_name = f"PID {pid}"
        if psutil is not None:
            try:
                process_name = psutil.Process(pid).name()
            except (psutil.Error, OSError):
                pass
        candidate = CaptureTarget(pid, process_name, title)
        current = by_pid.get(pid)
        # 同进程多个窗口时保留信息量更高的标题；进程树仍只需一个 PID。
        if current is None or len(candidate.window_title) > len(current.window_title):
            by_pid[pid] = candidate
    return sorted(by_pid.values(), key=lambda x: (x.process_name.casefold(), x.window_title.casefold()))


class _StreamingRecapCapture:
    """把 recap 的 process-loopback 捕获器改造成内存 PCM 队列。"""

    def __init__(self, process_id: int, output: "queue.Queue[np.ndarray]") -> None:
        from recap.audio import AudioCapture

        owner = self

        class _Capture(AudioCapture):
            def __init__(self) -> None:
                fd, path = tempfile.mkstemp(prefix="voxsub-process-", suffix=".wav")
                os.close(fd)
                self._voxsub_temp = Path(path)
                self.capture_error: Exception | None = None
                self._mono_buffer = np.zeros(0, dtype=np.float32)
                super().__init__(self._voxsub_temp, process_id=process_id)

            def _capture_loop(self) -> None:
                try:
                    self._capture_loop_impl()
                except Exception as exc:  # 在线程内保存，start() 在调用线程上报告
                    self.capture_error = exc
                    logger.exception("按应用音频捕获线程失败: pid=%d", process_id)
                    self._format_event.set()
                    self._started_event.set()
                finally:
                    self._running = False

            def _drain_packets(self, capture_client, bytes_per_frame: int) -> None:
                silent_flag = 0x2
                while True:
                    packet_size = capture_client.GetNextPacketSize()
                    if packet_size == 0:
                        break
                    data_ptr, num_frames, flags, _, _ = capture_client.GetBuffer()
                    try:
                        if num_frames <= 0:
                            continue
                        if flags & silent_flag:
                            mono = np.zeros(num_frames, dtype=np.float32)
                        else:
                            import ctypes

                            size = num_frames * bytes_per_frame
                            raw = bytes((ctypes.c_char * size).from_address(data_ptr))
                            if self._is_float:
                                samples = np.frombuffer(raw, dtype="<f4").astype(np.float32)
                            elif self._bits_per_sample == 16:
                                samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                            elif self._bits_per_sample == 32:
                                samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
                            else:
                                raise RuntimeError(f"不支持的进程音频位深: {self._bits_per_sample}")
                            channels = max(1, int(self._channels))
                            mono = (samples.reshape(-1, channels).mean(axis=1)
                                    if channels > 1 else samples.reshape(-1))
                        mono = resample_16k(mono, int(self._sample_rate))
                        data = (np.concatenate([self._mono_buffer, mono])
                                if self._mono_buffer.size else mono)
                        full = (data.size // CHUNK_FRAMES) * CHUNK_FRAMES
                        for i in range(0, full, CHUNK_FRAMES):
                            chunk = data[i:i + CHUNK_FRAMES].astype(np.float32, copy=True)
                            try:
                                output.put_nowait(chunk)
                            except queue.Full:
                                try:
                                    output.get_nowait()
                                    output.put_nowait(chunk)
                                except queue.Empty:
                                    pass
                        self._mono_buffer = data[full:].copy()
                    finally:
                        capture_client.ReleaseBuffer(num_frames)

        self.capture = _Capture()

    def cleanup(self) -> None:
        try:
            self.capture._voxsub_temp.unlink(missing_ok=True)  # noqa: SLF001
        except OSError:
            logger.debug("进程捕获临时 WAV 删除失败", exc_info=True)


class ProcessLoopbackSource(AudioSource):
    """只捕获指定 PID 及其子进程树的输出声音。"""

    def __init__(self, process_id: int, chunk_frames: int = CHUNK_FRAMES) -> None:
        if int(process_id) <= 0:
            raise ValueError("process_id 必须为正整数")
        self.process_id = int(process_id)
        self._chunk_frames = int(chunk_frames)
        # The Pipeline capture thread continuously drains this queue, including
        # while the user pauses.  Keep it unbounded so COM callback bursts never
        # discard the beginning of a word; the downstream pipeline owns the
        # explicit 10-minute memory safety cap.
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self._backend: Optional[_StreamingRecapCapture] = None
        self._running = False
        self._lock = threading.Lock()

    @property
    def device_name(self) -> str:
        return f"应用进程 PID {self.process_id}"

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            backend = _StreamingRecapCapture(self.process_id, self._queue)
            backend.capture.start()
            self._backend = backend

        # 激活是异步 COM 回调；在采集线程外等待，失败能回到 Pipeline/UI。
        if not backend.capture.wait_started(timeout=15.0):
            self.stop()
            raise RuntimeError("按应用音频捕获启动超时")
        if backend.capture.capture_error is not None:
            exc = backend.capture.capture_error
            self.stop()
            raise RuntimeError(f"按应用音频捕获启动失败: {exc}") from exc
        self._running = True
        logger.info("按应用音频捕获已启动: pid=%d rate=%d channels=%d",
                    self.process_id, backend.capture.sample_rate, backend.capture.channels)

    def read_chunk(self) -> Optional[np.ndarray]:
        if not self._running:
            return None
        try:
            return self._queue.get(timeout=0.2)
        except queue.Empty:
            # 目标暂时没有 render stream 时 Windows 不一定投递静音包；保持源存活。
            return np.zeros(self._chunk_frames, dtype=np.float32)

    def stop(self) -> None:
        with self._lock:
            backend, self._backend = self._backend, None
            self._running = False
        if backend is not None:
            backend.capture.stop()
            backend.capture.wait(timeout=5.0)
            backend.cleanup()
            logger.info("按应用音频捕获已停止: pid=%d", self.process_id)

    def close(self) -> None:
        self.stop()
