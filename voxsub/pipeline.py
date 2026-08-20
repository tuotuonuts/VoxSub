"""voxsub.pipeline —— 三模式编排 (M6)。

线程模型 (DESIGN.md「Pipeline 编排设计」):
  [采集线程] audio.read_chunk() 循环 ──queue──▶ [处理线程] segmenter.feed() → asr
      ──on_utterance(原文)──▶ translate ──▶ 订阅回调 (queue 桥接, 推理线程绝不直接碰 UI)

集成安全: 翻译模块 (M4) 未落地时用 _NoopTranslator 占位(原文直通+标记),
Pipeline 全链路不因缺模块崩溃 —— 翻译就绪后由 TranslatorFactory 注入。
"""
from __future__ import annotations

import queue
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from voxsub import __version__
from voxsub.audio import (
    AudioSource,
    CHUNK_FRAMES,
    LoopbackSource,
    MicSource,
    list_loopbacks,
    list_microphones,
    resample_16k,
)
from voxsub.asr import (
    AudioUtteranceSegmenter,
    UtteranceSegmenter,
    WindowVAD,
    create_asr,
    models_dir,
    SAMPLE_RATE,
)
from voxsub.bootstrap_models import ensure_bundled_vad
from voxsub.cloud_stt import CloudSTT
from voxsub.file_io import write_text_atomically
from voxsub.logging_setup import get_logger
from voxsub.recording import WaveSessionRecorder

logger = get_logger("pipeline")

_PAUSE_MARKER = object()


# ---------- 数据模型 ----------

@dataclass
class SubtitleLine:
    """一条字幕: 原文 + 译文 + 相对时间戳(ms, C 模式有效)。"""
    text: str
    translation: str = ""
    ts_ms: int = 0
    is_final: bool = True


@dataclass(frozen=True)
class _QueuedAudio:
    """A VAD-complete waveform plus its enqueue timestamp for diagnostics."""
    audio: np.ndarray
    queued_at: float


# ---------- 翻译占位 (M4 就绪后替换) ----------

class _NoopTranslator:
    """M4 翻译层未安装时的容错占位: 原文直通并加标记, 保证管线不中断。"""

    name = "noop"
    langs = ("zh", "en")

    def translate(self, text: str, src_lang: str, dst_lang: str, *,
                  timeout_ms: int = 15000) -> str:
        return f"{text} 〔翻译待装〕"

    def close(self) -> None:
        pass

    def health(self) -> str:
        return "翻译模块未安装 (M4 待集成)"


def _load_translator(kind: str = "opus-fast", config=None) -> object:
    """按用户选择延迟加载翻译层；失败时保留原文，不让管线崩溃。"""
    try:
        from voxsub.translate.factory import TranslatorFactory  # type: ignore[import-not-found]
        try:
            return TranslatorFactory.create(kind, config), kind
        except Exception as exc:
            logger.error("翻译档位 %s 创建失败，退回原文显示: %s", kind, exc,
                         exc_info=True)
            return _NoopTranslator(), None
    except ImportError as exc:
        logger.debug("翻译层未安装, 用占位实现: %s", exc)
        return _NoopTranslator(), None


# ---------- Pipeline ----------

class Pipeline:
    """三模式实时/离线翻译管线 (契约见 DESIGN.md「Pipeline 契约」)。"""

    def __init__(self, provider: str = "auto", models: Optional[Path] = None) -> None:
        self._provider = provider
        self._models_dir = Path(models) if models else models_dir()
        self._mode = "a"
        self._running = False
        self._in_path: Optional[Path] = None          # C 模式输入文件
        self._src_lang, self._dst_lang = "zh", "en"   # 默认中→英
        self._tts_enabled = False
        self._mic_device_id = ""
        self._loopback_device_id = ""
        self._capture_process_id = 0
        self._capture_window_title = ""
        self._requested_stt_provider = "local"
        self._stt_config = None
        self._requested_trans_kind = "opus-fast"
        self._requested_asr_model_id = "asr-zipformer-bilingual-fast"
        self._translator_config = None

        # 10 minutes of 30 ms chunks is a hard memory safety cap, not a normal
        # latency policy.  We never silently discard captured speech.
        self._queue: queue.Queue = queue.Queue(maxsize=20_000)
        self._recognition_queue: queue.Queue = queue.Queue()
        self._translation_queue: queue.Queue[str] = queue.Queue()
        self._translation_times: dict[str, deque[float]] = defaultdict(deque)
        self._metrics_lock = threading.Lock()
        self._recognition_input_done = threading.Event()
        self._translation_input_done = threading.Event()
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._source: AudioSource | None = None
        self._state_lock = threading.Lock()

        self._cb_utterance: list[Callable[[str, str], None]] = []
        self._cb_partial: list[Callable[[str], None]] = []
        self._cb_status: list[Callable[[str], None]] = []

        self._asr_tuning: dict = {"profile": "auto", "hotwords": ""}
        self._is_generative = False
        self._is_cloud_stt = False
        self._recording_enabled = False
        self._recordings_dir: Path | None = None
        self._recorder: WaveSessionRecorder | None = None
        self._last_recording_path: Path | None = None

        # 惰性组件 (首次 start 时构建)
        self._asr = None
        self._cloud_stt = None
        self._vad = None
        self._seg = None
        self._translator = None
        self._trans_kind = None

    # ---- 配置 ----
    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode in ("a", "b", "c") and not self._running:
            self._mode = mode

    def set_langs(self, src: str, dst: str) -> None:
        self._src_lang, self._dst_lang = src, dst

    def set_input_file(self, path: str | Path) -> None:
        self._in_path = Path(path)

    def set_tts(self, enabled: bool) -> None:
        self._tts_enabled = enabled

    def set_models_dir(self, path: str | Path) -> None:
        """Switch model storage between runs and discard path-bound caches."""
        if self._running:
            raise RuntimeError("识别运行中，无法切换模型目录")
        new_root = Path(path)
        if new_root.resolve() == self._models_dir.resolve():
            return

        old_root = self._models_dir
        old_cloud, old_translator = self._cloud_stt, self._translator
        self._models_dir = new_root
        self._asr = None
        self._cloud_stt = None
        self._vad = None
        self._seg = None
        self._translator = None
        self._trans_kind = None
        self._is_cloud_stt = False
        self._is_generative = False
        for label, component in (("云 STT", old_cloud), ("翻译器", old_translator)):
            if component is None:
                continue
            try:
                component.close()
            except Exception:
                logger.debug("切换模型目录时关闭旧%s失败", label, exc_info=True)
        logger.info("Pipeline 模型目录已切换: old=%s new=%s", old_root, new_root)

    def set_audio_devices(self, mic_device_id: str = "",
                          loopback_device_id: str = "") -> None:
        if self._running:
            return
        self._mic_device_id = str(mic_device_id or "")
        self._loopback_device_id = str(loopback_device_id or "")

    def set_capture_process(self, process_id: int = 0, window_title: str = "") -> None:
        if self._running:
            return
        self._capture_process_id = max(0, int(process_id or 0))
        self._capture_window_title = str(window_title or "")

    def set_stt(self, provider: str = "local", config=None) -> None:
        """Select the speech-to-text side independently from translation."""
        if self._running:
            return
        normalized = "cloud" if str(provider or "").lower() == "cloud" else "local"
        snapshot = dict(config) if isinstance(config, dict) else config
        changed = normalized != self._requested_stt_provider or snapshot != self._stt_config
        self._requested_stt_provider = normalized
        self._stt_config = snapshot
        if not changed:
            return
        if self._cloud_stt is not None:
            try:
                self._cloud_stt.close()
            except Exception:
                logger.debug("关闭旧云 STT 失败", exc_info=True)
        self._cloud_stt = None
        self._asr = None
        self._vad = None
        self._seg = None
        self._is_cloud_stt = False
        self._is_generative = False

    def set_translator(self, kind: str, config=None) -> None:
        """选择翻译档位；下一次 start 前立即替换旧实例。"""
        if self._running:
            return
        normalized = kind if kind in ("opus-fast", "qwen-quality", "cloud") else "opus-fast"
        changed = normalized != self._requested_trans_kind or config != self._translator_config
        self._requested_trans_kind = normalized
        self._translator_config = config
        if changed and self._translator is not None:
            try:
                self._translator.close()
            except Exception:
                logger.debug("关闭旧翻译器失败", exc_info=True)
            self._translator = None
            self._trans_kind = None

    def set_asr_model(self, model_id: str) -> None:
        """Select a catalog ASR model for the next run."""
        if self._running:
            return
        normalized = str(model_id or "asr-zipformer-bilingual-fast")
        if normalized == self._requested_asr_model_id:
            return
        self._requested_asr_model_id = normalized
        # sherpa recognizers own native state; replace only while stopped.
        self._asr = None
        self._vad = None
        self._seg = None

    def set_asr_tuning(self, tuning: dict | None = None) -> None:
        """Apply inference/segmentation tuning on the next run.

        This is deliberately not model-weight training.  It controls VAD,
        sentence boundaries, decoder budget, beam width and domain hotwords.
        """
        if self._running:
            return
        normalized = dict(tuning or {})
        if normalized == self._asr_tuning:
            return
        self._asr_tuning = normalized
        self._asr = None
        self._vad = None
        self._seg = None

    def set_recording(self, enabled: bool, directory: str | Path | None = None) -> None:
        """Enable microphone recording alongside translation for the next run."""
        if self._running:
            return
        self._recording_enabled = bool(enabled)
        self._recordings_dir = Path(directory) if directory else None

    def pause(self) -> None:
        """Pause microphone recording and translation without closing the device."""
        if not self._running or self._mode == "c" or self._pause_evt.is_set():
            return
        self._pause_evt.set()
        # The marker is ordered after all already-captured chunks.  The process
        # worker flushes the current phrase so audio from both sides of a long
        # pause is never glued into one sentence.
        self._queue.put(_PAUSE_MARKER)
        self._emit_status("已暂停 · 点击继续恢复录音与翻译")

    def resume(self) -> None:
        if not self._running or not self._pause_evt.is_set():
            return
        self._pause_evt.clear()
        self._emit_status("拾音中")

    def is_paused(self) -> bool:
        return self._pause_evt.is_set()

    @property
    def last_recording_path(self) -> Path | None:
        return self._last_recording_path

    def is_running(self) -> bool:
        return self._running

    # ---- 回调 (UI 订阅) ----
    def on_utterance(self, cb: Callable[[str, str], None]) -> None:
        self._cb_utterance.append(cb)

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._cb_status.append(cb)

    def on_partial(self, cb: Callable[[str], None]) -> None:
        self._cb_partial.append(cb)

    def _emit_status(self, msg: str) -> None:
        logger.info("Pipeline 状态: %s", msg)
        for cb in self._cb_status:
            try:
                cb(msg)
            except Exception:
                logger.exception("状态回调异常: %r", cb)

    def _emit_utterance(self, text: str, translation: str) -> None:
        logger.info("字幕已生成: src_chars=%d dst_chars=%d", len(text), len(translation))
        for cb in self._cb_utterance:
            try:
                cb(text, translation)
            except Exception:
                logger.exception("字幕回调异常: %r", cb)

    def _emit_partial(self, text: str) -> None:
        for cb in self._cb_partial:
            try:
                cb(text)
            except Exception:
                logger.exception("临时字幕回调异常: %r", cb)

    # ---- 组件构造 ----
    def _ensure_translator(self) -> None:
        if self._translator is None:
            self._translator, self._trans_kind = _load_translator(
                self._requested_trans_kind, self._translator_config)

    def _effective_asr_tuning(self, generative: bool) -> dict:
        """Resolve friendly presets to concrete values with safe bounds."""
        profile = str(self._asr_tuning.get("profile", "auto"))
        presets = {
            "responsive": {"vad_threshold": 0.45, "silence_ms": 350,
                           "max_utterance_ms": 6000, "beam_paths": 2},
            "balanced": {"vad_threshold": 0.35, "silence_ms": 650,
                         "max_utterance_ms": 12000, "beam_paths": 4},
            "accuracy": {"vad_threshold": 0.25, "silence_ms": 900,
                         "max_utterance_ms": 20000, "beam_paths": 6},
        }
        if profile == "auto":
            values = ({"vad_threshold": 0.35, "silence_ms": 700,
                       "max_utterance_ms": 12000, "beam_paths": 4}
                      if generative else
                      {"vad_threshold": 0.5, "silence_ms": 350,
                       "max_utterance_ms": 4500, "beam_paths": 4})
        elif profile in presets:
            values = dict(presets[profile])
        else:
            values = {
                "vad_threshold": float(self._asr_tuning.get("vad_threshold", 0.35)),
                "silence_ms": int(self._asr_tuning.get("silence_ms", 650)),
                "max_utterance_ms": int(self._asr_tuning.get("max_utterance_ms", 12000)),
                "beam_paths": int(self._asr_tuning.get("beam_paths", 4)),
            }
        values.update({
            "vad_threshold": max(0.01, min(0.99, float(values["vad_threshold"]))),
            "silence_ms": max(50, min(5000, int(values["silence_ms"]))),
            "max_utterance_ms": max(1000, min(120000, int(values["max_utterance_ms"]))),
            "beam_paths": max(1, min(16, int(values["beam_paths"]))),
            "max_new_tokens": max(32, min(4096, int(
                self._asr_tuning.get("max_new_tokens", 512)))),
            "hotwords": str(self._asr_tuning.get("hotwords", "")).strip(),
        })
        return values

    def _build_real_time(self) -> None:
        """构建 A/B 模式实时组件 (惰性, 只建一次)。"""
        cloud_ready = self._requested_stt_provider == "cloud" and self._cloud_stt is not None
        local_ready = self._requested_stt_provider != "cloud" and self._asr is not None
        if (self._vad is not None and self._seg is not None and
                (cloud_ready or local_ready)):
            self._ensure_translator()
            return
        # A failed model load must not leave a recognizer behind.  Otherwise a
        # retry skips setup and starts a process thread with ``_seg is None``.
        old_cloud = self._cloud_stt
        if old_cloud is not None:
            try:
                old_cloud.close()
            except Exception:
                logger.debug("重建实时链路前关闭旧云 STT 失败", exc_info=True)
        self._asr = None
        self._cloud_stt = None
        self._vad = None
        self._seg = None
        self._is_generative = False
        self._is_cloud_stt = False
        vad_dir = self._models_dir / "vad"
        vad_model = ensure_bundled_vad(self._models_dir)
        if vad_model is None:
            vad_model = next(vad_dir.glob("*.onnx"), None)
        if vad_model is None:
            raise FileNotFoundError(
                f"缺少基础 VAD 模型。请重新安装 VoxSub {__version__} "
                "或在模型目录中修复 VAD。"
            )
        from voxsub.model_catalog import get_model

        selected_model = get_model(self._requested_asr_model_id)
        cloud_stt = self._requested_stt_provider == "cloud"
        generative = cloud_stt or bool(
            selected_model and selected_model.runtime != "sherpa-streaming-transducer")
        tuning = self._effective_asr_tuning(generative)
        # Build VAD first: cloud STT and local generative ASR both use it, and
        # a failed recognizer construction must not leave a half-ready chain.
        vad = WindowVAD(str(vad_model), threshold=tuning["vad_threshold"])
        asr = None
        cloud_client = None
        if cloud_stt:
            cloud_client = CloudSTT(self._stt_config)
            if not cloud_client.ready():
                raise RuntimeError(
                    "云 STT 尚未就绪，请填写独立的 STT API Key、BaseURL 和模型名"
                )
            seg = AudioUtteranceSegmenter(
                vad,
                self._queue_generative_audio,
                min_silence_ms=tuning["silence_ms"],
                max_utterance_ms=tuning["max_utterance_ms"],
            )
        else:
            asr_provider = self._provider
            if asr_provider == "auto":
                from voxsub.router import select_device

                route = select_device("asr", benchmark=False)
                asr_provider = route.provider if route.provider in {"cuda", "coreml"} else "cpu"
                logger.info("ASR 自动路由: device=%s runtime_provider=%s",
                            route.name, asr_provider)
            asr = create_asr(
                self._requested_asr_model_id,
                self._models_dir,
                provider=asr_provider,
                num_threads=min(4, max(1, os.cpu_count() or 1)),
                source_lang=self._src_lang,
                tuning=tuning,
            )
            logger.info("本地 STT 执行器: runtime=%s provider=%s threads=%d",
                        getattr(asr, "runtime", type(asr).__name__),
                        getattr(asr, "provider", asr_provider),
                        min(4, max(1, os.cpu_count() or 1)))
            if generative:
                seg = AudioUtteranceSegmenter(
                    vad,
                    self._queue_generative_audio,
                    min_silence_ms=tuning["silence_ms"],
                    max_utterance_ms=tuning["max_utterance_ms"],
                )
            else:
                seg = UtteranceSegmenter(
                    asr,
                    vad,
                    self._on_sentence,
                    min_silence_ms=tuning["silence_ms"],
                    max_utterance_ms=tuning["max_utterance_ms"],
                    on_partial=self._emit_partial,
                    partial_interval_ms=360,
                )
        self._asr = asr
        self._cloud_stt = cloud_client
        self._vad = vad
        self._seg = seg
        self._is_generative = generative
        self._is_cloud_stt = cloud_stt
        logger.info(
            "STT 调优生效: provider=%s profile=%s generative=%s vad=%.2f silence=%dms max=%dms beam=%d",
            "cloud" if cloud_stt else "local",
            self._asr_tuning.get("profile", "auto"), generative,
            tuning["vad_threshold"], tuning["silence_ms"],
            tuning["max_utterance_ms"], tuning["beam_paths"],
        )
        self._ensure_translator()

    def _queue_generative_audio(self, audio: np.ndarray) -> None:
        """VAD callback: retain every utterance for the dedicated decoder."""
        arr = np.asarray(audio, dtype=np.float32)
        logger.info("VAD 语音段入队: audio_ms=%.1f queue=%d",
                    arr.size * 1000.0 / SAMPLE_RATE,
                    self._recognition_queue.qsize() + 1)
        self._recognition_queue.put(_QueuedAudio(arr, time.monotonic()))

    def _on_sentence(self, text: str) -> None:
        """Recognition callback: queue every sentence for translation."""
        text = str(text or "").strip()
        if not text:
            return
        queued_at = time.monotonic()
        with self._metrics_lock:
            self._translation_times[text].append(queued_at)
        logger.info("STT 终句入翻译队列: chars=%d lang=%s->%s queue=%d",
                    len(text), self._src_lang, self._dst_lang,
                    self._translation_queue.qsize() + 1)
        self._translation_queue.put(text)

    def _take_translation_timestamp(self, text: str) -> float | None:
        with self._metrics_lock:
            timestamps = self._translation_times.get(text)
            if not timestamps:
                return None
            value = timestamps.popleft()
            if not timestamps:
                self._translation_times.pop(text, None)
            return value

    def _translate_sentence(self, text: str, queued_at: float | None = None) -> None:
        """翻译单句并回调 UI；只在翻译工作线程调用。"""
        self._emit_status("翻译中…")
        started = time.perf_counter()
        queue_wait_ms = ((time.monotonic() - queued_at) * 1000.0
                         if queued_at is not None else None)
        try:
            translation = self._translator.translate(text, self._src_lang, self._dst_lang)
            request_ms = (time.perf_counter() - started) * 1000.0
            logger.info("翻译完成: src_chars=%d dst_chars=%d queue_wait_ms=%s request_ms=%.1f",
                        len(text), len(translation),
                        f"{queue_wait_ms:.1f}" if queue_wait_ms is not None else "na",
                        request_ms)
        except Exception as exc:
            request_ms = (time.perf_counter() - started) * 1000.0
            logger.error("翻译失败: src_chars=%d queue_wait_ms=%s request_ms=%.1f error=%s",
                         len(text),
                         f"{queue_wait_ms:.1f}" if queue_wait_ms is not None else "na",
                         request_ms, exc, exc_info=True)
            translation = text + " 〔翻译失败〕"
            self._emit_status("翻译失败(已保留原文)")
        self._emit_utterance(text, translation)
        if self._running:
            self._emit_status(
                "已暂停 · 点击继续恢复录音与翻译"
                if self._pause_evt.is_set() else "拾音中"
            )

    def _translation_loop(self) -> None:
        """翻译与 ASR 解码解耦，慢模型不得堵住音频/VAD 线程。"""
        warmup = getattr(self._translator, "warmup", None)
        if callable(warmup):
            logger.info("翻译工作线程开始预热")
            warmup()
        while not self._translation_input_done.is_set() or not self._translation_queue.empty():
            try:
                text = self._translation_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._translate_sentence(text, self._take_translation_timestamp(text))

    def _recognition_loop(self) -> None:
        """Decode VAD-complete waveforms without ever blocking audio capture."""
        try:
            while (not self._recognition_input_done.is_set() or
                   not self._recognition_queue.empty()):
                try:
                    item = self._recognition_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if isinstance(item, _QueuedAudio):
                    audio = item.audio
                    queued_at = item.queued_at
                else:
                    # Keep tests and integrations that enqueue raw PCM arrays
                    # compatible with the queue contract.
                    audio = np.asarray(item, dtype=np.float32)
                    queued_at = None
                backlog = self._recognition_queue.qsize()
                if backlog:
                    logger.info("STT 处理积压: segments=%d（音频已完整保留）", backlog)
                stt_started = time.perf_counter()
                wait_ms = ((time.monotonic() - queued_at) * 1000.0
                            if queued_at is not None else None)
                if self._is_cloud_stt:
                    if self._cloud_stt is None:
                        raise RuntimeError("云 STT 客户端未初始化")
                    try:
                        text = self._cloud_stt.transcribe_samples(
                            audio, source_lang=self._src_lang).strip()
                    except Exception as exc:
                        # A single network failure must not kill the capture
                        # and translation workers; later segments can recover.
                        logger.error("云 STT 片段失败: %s", exc, exc_info=True)
                        self._emit_status("云 STT 请求失败，已跳过当前片段")
                        continue
                else:
                    try:
                        stream = self._asr.create_stream()
                        self._asr.feed(stream, audio)
                        text = self._asr.decode(stream).strip()
                        self._asr.reset(stream)
                    except Exception as exc:
                        logger.error("本地 STT 片段失败: audio_ms=%.1f error=%s",
                                     audio.size * 1000.0 / SAMPLE_RATE, exc,
                                     exc_info=True)
                        self._emit_status("本地 STT 请求失败，已跳过当前片段")
                        continue
                decode_ms = (time.perf_counter() - stt_started) * 1000.0
                logger.info(
                    "STT 片段完成: runtime=%s provider=%s audio_ms=%.1f wait_ms=%s "
                    "decode_ms=%.1f chars=%d",
                    getattr(self._asr, "runtime", "cloud-stt") if not self._is_cloud_stt
                    else "cloud-stt",
                    getattr(self._asr, "provider", "cloud") if not self._is_cloud_stt
                    else "cloud",
                    audio.size * 1000.0 / SAMPLE_RATE,
                    f"{wait_ms:.1f}" if wait_ms is not None else "na",
                    decode_ms, len(text))
                if text:
                    self._on_sentence(text)
        except Exception as exc:
            logger.exception("生成式 ASR 解码线程失败")
            self._emit_status(f"识别处理错误: {exc}")
            self._running = False
            self._stop_evt.set()
        finally:
            self._translation_input_done.set()

    # ---- 启停 ----
    def start(self) -> None:
        if self._running:
            return
        # 清理上次异常退出留下的线程引用与旧音频块。
        self._threads = [t for t in self._threads if t.is_alive()]
        if self._threads:
            names = ", ".join(t.name for t in self._threads)
            raise RuntimeError(f"上一任务仍在安全收尾（{names}），请稍后再开始")
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self._recognition_queue.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self._translation_queue.get_nowait()
            except queue.Empty:
                break
        with self._metrics_lock:
            self._translation_times.clear()
        self._stop_evt.clear()
        self._pause_evt.clear()
        self._recognition_input_done.clear()
        self._translation_input_done.clear()
        self._emit_status("启动中…")
        try:
            if self._mode == "c":
                if self._in_path is None or not self._in_path.exists():
                    raise FileNotFoundError("请先选择要处理的音频或视频文件")
                new_threads = [threading.Thread(
                    target=self._run_file_mode, name="pipeline-file", daemon=True)]
            else:
                self._build_real_time()
                if self._mode == "a" and self._recording_enabled:
                    self._recorder = WaveSessionRecorder(self._recordings_dir)
                    self._last_recording_path = self._recorder.path
                new_threads = [
                    threading.Thread(target=self._capture_loop,
                                     name="pipeline-capture", daemon=True),
                    threading.Thread(target=self._process_loop,
                                     name="pipeline-process", daemon=True),
                ]
                if self._is_generative:
                    new_threads.append(threading.Thread(
                        target=self._recognition_loop,
                        name="pipeline-recognize", daemon=True,
                    ))
                new_threads.append(threading.Thread(
                    target=self._translation_loop,
                    name="pipeline-translate", daemon=True,
                ))
            self._running = True
            self._threads.extend(new_threads)
            for thread in new_threads:
                thread.start()
            self._emit_status("处理中…" if self._mode == "c" else "正在连接音频设备…")
        except Exception as exc:
            self._running = False
            self._stop_evt.set()
            recorder, self._recorder = self._recorder, None
            if recorder is not None:
                recorder.close()
            logger.exception("Pipeline 启动失败: mode=%s", self._mode)
            self._emit_status(f"启动失败: {exc}")
            raise

    def stop(self) -> None:
        if not self._running and not any(t.is_alive() for t in self._threads):
            return
        self._stop_evt.set()
        source = self._source
        if source is not None:
            try:
                source.stop()
            except Exception:
                logger.debug("主动停止音频源失败", exc_info=True)
        # capture → process(flush) → translate 的拥有关系必须保持；处理线程是
        # segmenter 唯一拥有者，UI 线程绝不能再次 flush/reset 原生 sherpa 流。
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=8.0)
        self._threads = [t for t in self._threads if t.is_alive()]
        self._running = False
        if self._last_recording_path is not None and self._recording_enabled:
            self._emit_status(f"已停止 · 录音已保存：{self._last_recording_path}")
        else:
            self._emit_status("已停止")

    # ---- A/B 模式线程 ----
    def _make_source(self) -> AudioSource:
        if self._mode == "b":
            if self._capture_process_id > 0:
                from voxsub.process_audio import ProcessLoopbackSource

                return ProcessLoopbackSource(self._capture_process_id)
            if self._loopback_device_id:
                device = self._find_device(list_loopbacks(), self._loopback_device_id,
                                           "输出设备")
                return LoopbackSource(device=device)
            # 不再错误选择列表第一个；LoopbackSource() 会匹配系统默认扬声器。
            return LoopbackSource()
        if self._mic_device_id:
            device = self._find_device(list_microphones(), self._mic_device_id, "麦克风")
            return MicSource(device=device)
        return MicSource()

    @staticmethod
    def _find_device(devices: list, device_id: str, label: str) -> object:
        for info in devices:
            if str(getattr(info.device, "id", "")) == device_id:
                return info.device
        raise RuntimeError(f"已选择的{label}当前不可用，请在设置中重新选择")

    def _capture_loop(self) -> None:
        try:
            source = self._make_source()
            self._source = source
            source.start()
            name = str(getattr(source, "device_name", type(source).__name__))
            logger.info("实时采集开始: mode=%s source=%s", self._mode, name)
            self._emit_status(f"拾音中 · {name}")
            started = time.monotonic()
            last_heartbeat = started
            chunks = 0
            peak = 0.0
            warned_silence = False
            while not self._stop_evt.is_set():
                chunk = source.read_chunk()
                if chunk is None:
                    if not self._stop_evt.is_set():
                        raise RuntimeError("音频设备意外停止输出")
                    break
                chunks += 1
                if chunk.size:
                    peak = max(peak, float(np.max(np.abs(chunk))))
                if self._pause_evt.is_set():
                    # Keep draining the device so WASAPI does not build its own
                    # stale buffer.  A phone-recorder pause intentionally omits
                    # these samples from both the WAV and transcription.
                    continue
                recorder = self._recorder
                if recorder is not None:
                    recorder.write(chunk)
                try:
                    self._queue.put(chunk, timeout=0.5)
                except queue.Full:
                    raise RuntimeError(
                        "识别持续落后超过 10 分钟；为避免静默丢音已停止任务，"
                        "请改用更轻量模型或硬件加速"
                    )
                elapsed = time.monotonic() - started
                now = time.monotonic()
                if now - last_heartbeat >= 1.0:
                    backlog_s = self._queue.qsize() * CHUNK_FRAMES / SAMPLE_RATE
                    logger.debug("音频心跳: chunks=%d peak=%.6f queue=%d backlog=%.2fs",
                                 chunks, peak, self._queue.qsize(), backlog_s)
                    if backlog_s >= 5.0:
                        logger.warning("识别暂时落后 %.1fs，音频仍完整缓冲、未丢失",
                                       backlog_s)
                    last_heartbeat = now
                if elapsed >= 4.0 and peak < 1e-5 and not warned_silence:
                    warned_silence = True
                    logger.warning("音频已连接但持续静音: source=%s", name)
                    self._emit_status("未检测到声音，请检查设备或播放内容")
        except Exception as exc:
            logger.exception("音频采集失败: mode=%s", self._mode)
            self._emit_status(f"音频设备错误: {exc}")
            self._running = False
            self._stop_evt.set()
        finally:
            recorder, self._recorder = self._recorder, None
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    logger.exception("结束同传录音失败")
            source = self._source
            self._source = None
            if source is not None:
                try:
                    source.stop()
                    source.close()
                except Exception:
                    logger.debug("释放音频源失败", exc_info=True)

    def _process_loop(self) -> None:
        try:
            seg = self._seg
            while not self._stop_evt.is_set() or not self._queue.empty():
                try:
                    chunk = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if seg is None:
                    raise RuntimeError("识别分句器未初始化，已取消本次任务")
                if chunk is _PAUSE_MARKER:
                    seg.flush()
                    continue
                seg.feed(chunk)
        except Exception as exc:
            logger.exception("ASR/VAD 处理线程失败")
            self._emit_status(f"识别处理错误: {exc}")
            self._running = False
            self._stop_evt.set()
        finally:
            try:
                if self._seg is not None:
                    self._seg.flush()
            except Exception:
                logger.exception("处理线程 flush 尾句失败")
            finally:
                if self._is_generative:
                    self._recognition_input_done.set()
                else:
                    self._translation_input_done.set()

    # ---- C 模式 (文件 → 双语字幕) ----
    def _run_file_mode(self) -> None:
        if self._in_path is None or not self._in_path.exists():
            self._emit_status("文件不存在")
            self._running = False
            return
        wav_path: Optional[Path] = None
        try:
            self._emit_status(f"正在提取/读取音频: {self._in_path.name}")
            self._ensure_translator()
            warmup = getattr(self._translator, "warmup", None)
            if callable(warmup):
                logger.info("文件模式开始预热翻译引擎")
                warmup()
            lines, wav_path = self._transcribe_file(self._in_path)
            out = self._in_path.with_suffix(".srt")
            self.write_srt(lines, out)
            self._emit_status(f"完成 → {out}")
            self._emit_utterance(f"已导出 {len(lines)} 条字幕", str(out))
        except Exception as exc:
            logger.error("文件处理失败 path=%s: %s", self._in_path, exc, exc_info=True)
            self._emit_status(f"文件处理失败: {exc}")
        finally:
            if wav_path is not None:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("临时音频删除失败: %s", wav_path, exc_info=True)
            self._running = False

    def _transcribe_file(self, path: Path) -> tuple[list[SubtitleLine], Optional[Path]]:
        """对文件离线识别: 支持 .wav 直读(采样率自动转 16k), 其他格式需 ffmpeg。

        返回 (字幕行列表, 中间 wav 路径)。分句按 VAD, 时间戳按样本计数精确。
        """
        wav_path: Optional[Path] = None
        if path.suffix.lower() == ".wav":
            pcm, sr = self._read_wav(path)
        else:
            ffmpeg = self._find_ffmpeg()
            if ffmpeg is None:
                raise RuntimeError("视频/压缩音频提取组件 ffmpeg 未随程序安装")
            with tempfile.NamedTemporaryFile(prefix="voxsub-extract-", suffix=".wav",
                                             delete=False) as tmp:
                wav_path = Path(tmp.name)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(path), "-vn", "-ar", "16000", "-ac", "1",
                 "-c:a", "pcm_s16le", str(wav_path)],
                check=False, capture_output=True, creationflags=flags,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"无法从文件提取音频: {detail[-500:]}")
            pcm, sr = self._read_wav(wav_path)

        if sr != SAMPLE_RATE:
            pcm = resample_16k(pcm, sr)
        if self._requested_stt_provider == "cloud":
            return self._recognize_cloud_file(pcm), wav_path
        return self._recognize_streaming(pcm), wav_path

    @staticmethod
    def _find_ffmpeg() -> Optional[Path]:
        """定位随包 ffmpeg，其次使用系统 PATH。"""
        candidates: list[Path] = []
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            candidates.extend([exe_dir / "tools" / "ffmpeg.exe", exe_dir / "ffmpeg.exe"])
            bundle = getattr(sys, "_MEIPASS", None)
            if bundle:
                candidates.append(Path(bundle) / "tools" / "ffmpeg.exe")
        candidates.extend([
            Path(__file__).resolve().parent.parent / "tools" / "ffmpeg.exe",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) /
            "VoxSub" / "tools" / "ffmpeg.exe",
        ])
        found = shutil.which("ffmpeg")
        if found:
            candidates.append(Path(found))
        return next((p for p in candidates if p.is_file()), None)

    @staticmethod
    def _read_wav(path: Path) -> tuple[np.ndarray, int]:
        """读 16-bit PCM wav 为 float32 mono + 采样率。"""
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            n_ch, width = w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width == 1:
            data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif width == 2:
            data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif width == 3:
            packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            vals = (packed[:, 0].astype(np.int32) |
                    (packed[:, 1].astype(np.int32) << 8) |
                    (packed[:, 2].astype(np.int32) << 16))
            vals = np.where(vals & 0x800000, vals - 0x1000000, vals)
            data = vals.astype(np.float32) / 8388608.0
        elif width == 4:
            data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            raise RuntimeError(f"不支持的 WAV 位深: {width * 8} bit")
        if n_ch > 1:
            data = data.reshape(-1, n_ch).mean(axis=1)  # 混音到单声道
        return data, sr

    def _recognize_streaming(self, pcm: np.ndarray) -> list[SubtitleLine]:
        """整段流式识别 + VAD 分句 + 时间戳(样本计数)。"""
        self._build_real_time()
        vad, asr = self._vad, self._asr

        lines: list[SubtitleLine] = []
        stream = asr.create_stream()
        seg_start_sample: Optional[int] = None
        silence = 0
        min_silence = int(SAMPLE_RATE * 0.5)
        win = vad.window_size

        for i in range(0, pcm.size - win + 1, win):
            chunk = pcm[i:i + win]
            if vad.is_speech(chunk):
                if seg_start_sample is None:
                    seg_start_sample = i
                silence = 0
                asr.feed(stream, chunk)
            elif seg_start_sample is not None:
                asr.feed(stream, chunk)
                silence += win
                if silence >= min_silence:
                    text = asr.decode(stream).strip()
                    if text:
                        lines.append(SubtitleLine(text=text, ts_ms=int(seg_start_sample * 1000 / SAMPLE_RATE)))
                    asr.reset(stream)
                    seg_start_sample = None
                    silence = 0
                    vad.reset()
        # 尾段
        if seg_start_sample is not None:
            text = asr.decode(stream).strip()
            if text:
                lines.append(SubtitleLine(text=text, ts_ms=int(seg_start_sample * 1000 / SAMPLE_RATE)))

        # 批量翻译 (stub 未就绪时原文直通, 不阻塞导出)
        self._ensure_translator()
        for ln in lines:
            try:
                ln.translation = self._translator.translate(ln.text, self._src_lang, self._dst_lang)
            except Exception:
                ln.translation = ln.text + " 〔翻译失败〕"
        return lines

    def _recognize_cloud_file(self, pcm: np.ndarray) -> list[SubtitleLine]:
        """VAD-split file audio and send finalized segments to cloud STT."""
        self._build_real_time()
        if self._cloud_stt is None or self._vad is None:
            raise RuntimeError("云 STT 未正确初始化")
        vad = self._vad
        tuning = self._effective_asr_tuning(generative=True)
        min_silence = int(SAMPLE_RATE * tuning["silence_ms"] / 1000)
        max_utterance = int(SAMPLE_RATE * tuning["max_utterance_ms"] / 1000)
        win = vad.window_size
        segments: list[tuple[int, np.ndarray]] = []
        current: list[np.ndarray] = []
        current_samples = 0
        start_sample: int | None = None
        silence = 0

        def finish() -> None:
            nonlocal current, current_samples, start_sample, silence
            if current and start_sample is not None:
                segments.append((start_sample, np.concatenate(current)))
            current = []
            current_samples = 0
            start_sample = None
            silence = 0
            vad.reset()

        for i in range(0, pcm.size - win + 1, win):
            chunk = pcm[i:i + win]
            speech = vad.is_speech(chunk)
            if speech:
                if start_sample is None:
                    start_sample = i
                current.append(chunk.copy())
                current_samples += len(chunk)
                silence = 0
            elif start_sample is not None:
                current.append(chunk.copy())
                current_samples += len(chunk)
                silence += win
                if silence >= min_silence or current_samples >= max_utterance:
                    finish()
        if start_sample is not None:
            finish()

        lines: list[SubtitleLine] = []
        for start, audio in segments:
            try:
                text = self._cloud_stt.transcribe_samples(
                    audio, source_lang=self._src_lang).strip()
            except Exception as exc:
                logger.error("云 STT 文件片段失败: %s", exc, exc_info=True)
                continue
            if text:
                lines.append(SubtitleLine(
                    text=text, ts_ms=int(start * 1000 / SAMPLE_RATE)))

        self._ensure_translator()
        for line in lines:
            try:
                line.translation = self._translator.translate(
                    line.text, self._src_lang, self._dst_lang)
            except Exception:
                line.translation = line.text + " 〔翻译失败〕"
        return lines

    # ---- srt / vtt / txt 导出 (模块级函数, 便于单测) ----
    @staticmethod
    def _fmt_ts(ms: int) -> str:
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms2 = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"

    @classmethod
    def write_srt(cls, lines: list[SubtitleLine], out: Path, dur_ms: int = 1500) -> None:
        """写 srt: 序号 + 时间轴(每句固定 1.5s 展示) + 双语两行。"""
        body = []
        for idx, ln in enumerate(lines, start=1):
            # 每条展示 dur_ms; 末句放宽到 3s, 便于阅读
            end = ln.ts_ms + (3000 if idx == len(lines) else dur_ms)
            body.append(f"{idx}\n{cls._fmt_ts(ln.ts_ms)} --> {cls._fmt_ts(end)}\n"
                        f"{ln.text}\n{ln.translation}\n")
        write_text_atomically(out, "\n".join(body), encoding="utf-8-sig")

    @classmethod
    def write_vtt(cls, lines: list[SubtitleLine], out: Path) -> None:
        """写 vtt (WebVTT)。"""
        body = ["WEBVTT\n"]
        for idx, ln in enumerate(lines, start=1):
            body.append(f"{cls._fmt_ts(ln.ts_ms).replace(',', '.')} --> "
                        f"{cls._fmt_ts(ln.ts_ms + 3000).replace(',', '.')}\n"
                        f"{ln.text}\n{ln.translation}\n")
        write_text_atomically(out, "\n".join(body), encoding="utf-8")

    @classmethod
    def write_txt(cls, lines: list[SubtitleLine], out: Path) -> None:
        """写纯文本: 原文 ⇄ 译文, tab 分隔。"""
        write_text_atomically(
            out,
            "\n".join(f"{ln.text}\t{ln.translation}" for ln in lines),
            encoding="utf-8",
        )
