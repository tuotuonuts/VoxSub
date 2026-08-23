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
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from voxsub.audio import (
    AudioSource,
    CHUNK_FRAMES,
    LoopbackSource,
    MicSource,
    list_loopbacks,
    list_microphones,
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
from voxsub.contextual_text import ContextualSegment, ContextualTextProcessor
from voxsub.file_transcriber import FileAudioDecoder, FileRecognizer
from voxsub.language_guard import guard_text, normalize_language
from voxsub.logging_setup import get_logger
from voxsub.recording import WaveSessionRecorder
from voxsub.realtime_builder import RealtimeBuildSpec, build_realtime_components
from voxsub.subtitles import SubtitleExporter, SubtitleLine
from voxsub.tts_worker import TTSWorker

logger = get_logger("pipeline")

_PAUSE_MARKER = object()
_CAPTURE_QUEUE_MAX = 20_000       # 10 minutes at 30 ms/chunk
_RECOGNITION_QUEUE_MAX = 16       # complete utterance waveforms can be large
_CONTEXT_QUEUE_MAX = 128          # decoded acoustic fragments awaiting semantics
_TRANSLATION_QUEUE_MAX = 128      # text only, but must never grow forever


@dataclass(frozen=True)
class _QueuedAudio:
    """A VAD-complete waveform plus its enqueue timestamp for diagnostics."""
    audio: np.ndarray
    queued_at: float


@dataclass(frozen=True)
class _QueuedText:
    """A recognized acoustic fragment plus its first-ready timestamp."""
    text: str
    queued_at: float


@dataclass
class _CaptureMetrics:
    started: float
    last_heartbeat: float
    chunks: int = 0
    peak: float = 0.0
    warned_silence: bool = False

    def observe(self, chunk: np.ndarray) -> None:
        self.chunks += 1
        if chunk.size:
            self.peak = max(self.peak, float(np.max(np.abs(chunk))))


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


class PipelineState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


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
        self._state_lock = threading.RLock()
        self._state = PipelineState.IDLE
        self._provider = provider
        self._models_dir = Path(models) if models else models_dir()
        self._mode = "a"
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
        self._queue: queue.Queue = queue.Queue(maxsize=_CAPTURE_QUEUE_MAX)
        self._recognition_queue: queue.Queue = queue.Queue(
            maxsize=_RECOGNITION_QUEUE_MAX)
        self._context_queue: queue.Queue[_QueuedText] = queue.Queue(
            maxsize=_CONTEXT_QUEUE_MAX)
        self._translation_queue: queue.Queue[str] = queue.Queue(
            maxsize=_TRANSLATION_QUEUE_MAX)
        self._translation_times: dict[str, deque[float]] = defaultdict(deque)
        self._metrics_lock = threading.Lock()
        self._recognition_input_done = threading.Event()
        self._context_input_done = threading.Event()
        self._translation_input_done = threading.Event()
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._source: AudioSource | None = None

        self._cb_utterance: list[Callable[[str, str], None]] = []
        self._cb_partial: list[Callable[[str], None]] = []
        self._cb_status: list[Callable[[str], None]] = []

        self._asr_tuning: dict = {"profile": "auto", "hotwords": ""}
        self._is_generative = False
        self._is_cloud_stt = False
        self._context_processor: ContextualTextProcessor | None = None
        self._recording_enabled = False
        self._recordings_dir: Path | None = None
        self._recorder: WaveSessionRecorder | None = None
        self._last_recording_path: Path | None = None
        self._tts_worker: TTSWorker | None = None

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

    @property
    def state(self) -> PipelineState:
        with self._state_lock:
            return self._state

    def _set_state(self, state: PipelineState) -> None:
        with self._state_lock:
            previous, self._state = self._state, state
        if previous != state:
            logger.info("Pipeline 生命周期: %s -> %s", previous.value, state.value)

    @property
    def _running(self) -> bool:
        """Compatibility view for integrations that used the old private flag."""
        return self.is_running()

    @_running.setter
    def _running(self, value: bool) -> None:
        self._set_state(PipelineState.RUNNING if value else PipelineState.IDLE)

    def set_mode(self, mode: str) -> None:
        if mode in ("a", "b", "c") and not self._running:
            self._mode = mode

    def set_langs(self, src: str, dst: str) -> None:
        normalized = (normalize_language(src), normalize_language(dst))
        changed = normalized != (self._src_lang, self._dst_lang)
        self._src_lang, self._dst_lang = normalized
        if changed and not self._running:
            self._asr = None
            self._seg = None
            self._context_processor = None
        logger.info("语言约束更新: source=%s target=%s", self._src_lang, self._dst_lang)

    def set_input_file(self, path: str | Path) -> None:
        self._in_path = Path(path)

    def set_tts(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._tts_enabled:
            return
        self._tts_enabled = enabled
        if self._running and self._mode != "c":
            if enabled:
                self._start_tts_worker()
            else:
                self._stop_tts_worker()

    def set_models_dir(self, path: str | Path) -> None:
        """Switch model storage between runs and discard path-bound caches."""
        if self._running:
            raise RuntimeError("识别运行中，无法切换模型目录")
        new_root = Path(path)
        if new_root.resolve() == self._models_dir.resolve():
            return

        old_root = self._models_dir
        old_cloud, old_translator = self._cloud_stt, self._translator
        self._stop_tts_worker()
        self._models_dir = new_root
        self._asr = None
        self._cloud_stt = None
        self._vad = None
        self._seg = None
        self._context_processor = None
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
        self._context_processor = None
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
        self._context_processor = None

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
        self._context_processor = None

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
        self._put_or_stop(
            self._queue,
            _PAUSE_MARKER,
            "识别队列已满，无法安全暂停；任务已停止",
        )
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
        return self.state in {
            PipelineState.STARTING,
            PipelineState.RUNNING,
            PipelineState.STOPPING,
        }

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
        try:
            text = guard_text(text, self._src_lang, kind="STT partial")
        except ValueError:
            logger.debug("临时 STT 结果被语言约束过滤: source=%s text=%r",
                         self._src_lang, str(text)[:160])
            return
        if not text:
            return
        for cb in self._cb_partial:
            try:
                cb(text)
            except Exception:
                logger.exception("临时字幕回调异常: %r", cb)

    def _on_asr_partial(self, text: str) -> None:
        processor = self._context_processor
        self._emit_partial(processor.preview(text) if processor is not None else text)

    # ---- 组件构造 ----
    def _ensure_translator(self) -> None:
        if self._translator is None:
            self._translator, self._trans_kind = _load_translator(
                self._requested_trans_kind, self._translator_config)

    def _start_tts_worker(self) -> None:
        if not self._tts_enabled or self._mode == "c":
            return
        if self._tts_worker is None:
            self._tts_worker = TTSWorker(
                self._models_dir,
                external_stop=self._stop_evt,
            )
        self._tts_worker.start()

    def _stop_tts_worker(self) -> None:
        worker, self._tts_worker = self._tts_worker, None
        if worker is not None:
            worker.stop()

    def _put_or_stop(self, target: queue.Queue, item: object, message: str) -> None:
        """Bound queue growth and make overload visible instead of losing data."""
        try:
            target.put(item, timeout=0.5)
        except queue.Full as exc:
            self._stop_evt.set()
            self._emit_status(message)
            raise RuntimeError(message) from exc

    @staticmethod
    def _drain_queue(target: queue.Queue) -> None:
        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                return

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
            "context": {"vad_threshold": 0.32, "silence_ms": 500,
                        "max_utterance_ms": 18000, "beam_paths": 6},
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
            "context_enabled": profile == "context",
            "context_hold_ms": max(200, min(4000, int(
                self._asr_tuning.get("context_hold_ms", 1800)))),
            "context_correction": bool(
                self._asr_tuning.get("context_correction", True)),
            "filler_mode": (
                str(self._asr_tuning.get("filler_mode", "light"))
                if str(self._asr_tuning.get("filler_mode", "light")) in
                {"off", "light"} else "light"
            ),
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
        self._context_processor = None
        self._is_generative = False
        self._is_cloud_stt = False
        cloud_stt = self._requested_stt_provider == "cloud"
        from voxsub.model_catalog import get_model
        from voxsub.router import select_device

        generative_model = get_model(self._requested_asr_model_id)
        generative = cloud_stt or bool(
            generative_model and
            generative_model.runtime != "sherpa-streaming-transducer")
        tuning = self._effective_asr_tuning(generative)
        context_processor = (
            ContextualTextProcessor(
                source_lang=self._src_lang,
                hotwords=tuning["hotwords"],
                filler_mode=tuning["filler_mode"],
                correction_enabled=tuning["context_correction"],
                hold_ms=tuning["context_hold_ms"],
                defer_incomplete=generative,
            )
            if tuning["context_enabled"] else None
        )
        components = build_realtime_components(
            RealtimeBuildSpec(
                self._models_dir, self._requested_stt_provider, self._stt_config,
                self._requested_asr_model_id, self._provider, self._src_lang, tuning,
                generative,
            ),
            queue_audio=self._queue_generative_audio,
            on_sentence=self._on_sentence,
            on_partial=self._on_asr_partial,
            ensure_vad=ensure_bundled_vad,
            vad_factory=WindowVAD,
            asr_factory=create_asr,
            cloud_factory=CloudSTT,
            audio_segmenter_factory=AudioUtteranceSegmenter,
            streaming_segmenter_factory=UtteranceSegmenter,
            select_device=select_device,
            semantic_boundary=(
                context_processor.should_defer_endpoint
                if context_processor is not None and not generative else None),
        )
        self._asr = components.asr
        self._cloud_stt = components.cloud_stt
        self._vad = components.vad
        self._seg = components.segmenter
        self._context_processor = context_processor
        self._is_generative = components.generative
        self._is_cloud_stt = components.cloud
        logger.info(
            "STT 调优生效: provider=%s profile=%s generative=%s vad=%.2f silence=%dms max=%dms beam=%d context=%s",
            "cloud" if components.cloud else "local",
            self._asr_tuning.get("profile", "auto"), components.generative,
            tuning["vad_threshold"], tuning["silence_ms"],
            tuning["max_utterance_ms"], tuning["beam_paths"],
            tuning["context_enabled"],
        )
        self._ensure_translator()

    def _queue_generative_audio(self, audio: np.ndarray) -> None:
        """VAD callback: retain every utterance for the dedicated decoder."""
        arr = np.asarray(audio, dtype=np.float32)
        logger.info("VAD 语音段入队: audio_ms=%.1f queue=%d",
                    arr.size * 1000.0 / SAMPLE_RATE,
                    self._recognition_queue.qsize() + 1)
        self._put_or_stop(
            self._recognition_queue,
            _QueuedAudio(arr, time.monotonic()),
            "识别后端持续落后，音频分段缓存已满；任务已停止，请切换更轻量的识别模型",
        )

    def _on_sentence(self, text: str) -> None:
        """Recognition callback: validate and route an acoustic fragment."""
        text = str(text or "").strip()
        if not text:
            return
        try:
            text = guard_text(text, self._src_lang, kind="STT")
        except ValueError as exc:
            logger.warning("STT 结果被语言约束拦截: source=%s text=%r reason=%s",
                           self._src_lang, text[:160], exc)
            self._emit_status("识别到其他语言，已忽略当前片段")
            return
        queued_at = time.monotonic()
        if self._context_processor is not None:
            logger.info(
                "STT 片段入上下文队列: chars=%d queue=%d",
                len(text), self._context_queue.qsize() + 1,
            )
            self._put_or_stop(
                self._context_queue,
                _QueuedText(text, queued_at),
                "上下文处理持续积压，字幕缓存已满；任务已停止",
            )
            return
        self._queue_translation(text, queued_at)

    def _queue_translation(self, text: str, queued_at: float) -> None:
        with self._metrics_lock:
            self._translation_times[text].append(queued_at)
        logger.info("STT 终句入翻译队列: chars=%d lang=%s->%s queue=%d",
                    len(text), self._src_lang, self._dst_lang,
                    self._translation_queue.qsize() + 1)
        try:
            self._put_or_stop(
                self._translation_queue,
                text,
                "翻译后端持续落后，字幕缓存已满；任务已停止，请切换更轻量的翻译模型",
            )
        except RuntimeError:
            with self._metrics_lock:
                timestamps = self._translation_times.get(text)
                if timestamps:
                    timestamps.pop()
                    if not timestamps:
                        self._translation_times.pop(text, None)
            raise

    def _context_loop(self) -> None:
        """Stabilize semantic fragments before the translation worker sees them."""
        processor = self._context_processor
        if processor is None:
            self._translation_input_done.set()
            return
        pending_since: float | None = None
        try:
            while (not self._context_input_done.is_set() or
                   not self._context_queue.empty()):
                try:
                    item = self._context_queue.get(timeout=0.1)
                except queue.Empty:
                    segments = processor.poll()
                else:
                    segments, pending_since = self._consume_context_item(
                        processor, item, pending_since)
                if segments:
                    self._commit_context_segments(
                        segments, pending_since or time.monotonic())
                    pending_since = None
            trailing = processor.flush()
            if trailing:
                self._commit_context_segments(
                    trailing, pending_since or time.monotonic())
        except Exception as exc:
            logger.exception("智能上下文处理线程失败")
            self._emit_status(f"上下文处理错误: {exc}")
            self._set_state(PipelineState.FAILED)
            self._stop_evt.set()
        finally:
            self._translation_input_done.set()

    def _consume_context_item(
        self,
        processor: ContextualTextProcessor,
        item: _QueuedText,
        pending_since: float | None,
    ) -> tuple[list[ContextualSegment], float | None]:
        expired = processor.poll(now=item.queued_at)
        if expired:
            self._commit_context_segments(
                expired, pending_since or item.queued_at)
            pending_since = None
        if not processor.pending_text:
            pending_since = item.queued_at
        segments = processor.submit(item.text, now=item.queued_at)
        if not segments and processor.pending_text:
            self._emit_partial(processor.pending_text)
        if not segments and not processor.pending_text:
            pending_since = None
        return segments, pending_since

    def _commit_context_segments(
        self,
        segments: list[ContextualSegment],
        queued_at: float,
    ) -> None:
        for segment in segments:
            if segment.corrections or segment.fillers_removed:
                logger.info(
                    "上下文文本已稳定: raw=%r final=%r corrections=%s fillers=%d",
                    segment.raw_text[:160], segment.text[:160],
                    segment.corrections, segment.fillers_removed,
                )
            self._queue_translation(segment.text, queued_at)

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
        can_speak = False
        try:
            translation = self._translator.translate(text, self._src_lang, self._dst_lang)
            if self._trans_kind is not None:
                translation = guard_text(
                    translation, self._dst_lang, kind="translation")
            request_ms = (time.perf_counter() - started) * 1000.0
            logger.info("翻译完成: src_chars=%d dst_chars=%d queue_wait_ms=%s request_ms=%.1f",
                        len(text), len(translation),
                         f"{queue_wait_ms:.1f}" if queue_wait_ms is not None else "na",
                         request_ms)
            can_speak = True
        except Exception as exc:
            request_ms = (time.perf_counter() - started) * 1000.0
            logger.error("翻译失败: src_chars=%d queue_wait_ms=%s request_ms=%.1f error=%s",
                         len(text),
                         f"{queue_wait_ms:.1f}" if queue_wait_ms is not None else "na",
                         request_ms, exc, exc_info=True)
            translation = text + " 〔翻译失败〕"
            self._emit_status("翻译失败(已保留原文)")
        self._emit_utterance(text, translation)
        worker = self._tts_worker
        if can_speak and worker is not None:
            worker.submit(translation, self._dst_lang)
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
                audio, queued_at = self._recognition_item(item)
                backlog = self._recognition_queue.qsize()
                if backlog:
                    logger.info("STT 处理积压: segments=%d（音频已完整保留）", backlog)
                stt_started = time.perf_counter()
                wait_ms = ((time.monotonic() - queued_at) * 1000.0
                            if queued_at is not None else None)
                text = self._decode_recognition_audio(audio)
                if text is None:
                    continue
                decode_ms = (time.perf_counter() - stt_started) * 1000.0
                self._log_recognition(audio, text, wait_ms, decode_ms)
                if text:
                    self._on_sentence(text)
        except Exception as exc:
            logger.exception("生成式 ASR 解码线程失败")
            self._emit_status(f"识别处理错误: {exc}")
            self._set_state(PipelineState.FAILED)
            self._stop_evt.set()
        finally:
            if self._context_processor is not None:
                self._context_input_done.set()
            else:
                self._translation_input_done.set()

    @staticmethod
    def _recognition_item(item) -> tuple[np.ndarray, float | None]:
        if isinstance(item, _QueuedAudio):
            return item.audio, item.queued_at
        # Keep integrations that enqueue raw PCM compatible with the queue.
        return np.asarray(item, dtype=np.float32), None

    def _decode_recognition_audio(self, audio: np.ndarray) -> str | None:
        client = self._cloud_stt if self._is_cloud_stt else self._asr
        if client is None:
            kind = "云 STT 客户端" if self._is_cloud_stt else "本地 STT 执行器"
            raise RuntimeError(f"{kind}未初始化")
        try:
            if self._is_cloud_stt:
                return client.transcribe_samples(
                    audio, source_lang=self._src_lang).strip()
            stream = client.create_stream()
            client.feed(stream, audio)
            text = client.decode(stream).strip()
            client.reset(stream)
            return text
        except Exception as exc:
            kind = "云" if self._is_cloud_stt else "本地"
            logger.error(
                "%s STT 片段失败: audio_ms=%.1f error=%s", kind,
                audio.size * 1000.0 / SAMPLE_RATE, exc, exc_info=True,
            )
            self._emit_status(f"{kind} STT 请求失败，已跳过当前片段")
            return None

    def _log_recognition(self, audio: np.ndarray, text: str,
                         wait_ms: float | None, decode_ms: float) -> None:
        runtime = ("cloud-stt" if self._is_cloud_stt else
                   getattr(self._asr, "runtime", "local-stt"))
        provider = ("cloud" if self._is_cloud_stt else
                    getattr(self._asr, "provider", "cpu"))
        logger.info(
            "STT 片段完成: runtime=%s provider=%s audio_ms=%.1f wait_ms=%s "
            "decode_ms=%.1f chars=%d",
            runtime, provider, audio.size * 1000.0 / SAMPLE_RATE,
            f"{wait_ms:.1f}" if wait_ms is not None else "na",
            decode_ms, len(text),
        )

    # ---- 启停 ----
    def start(self) -> None:
        if self.is_running():
            return
        # 清理上次异常退出留下的线程引用与旧音频块。
        self._threads = [t for t in self._threads if t.is_alive()]
        if self._threads:
            names = ", ".join(t.name for t in self._threads)
            raise RuntimeError(f"上一任务仍在安全收尾（{names}），请稍后再开始")
        self._drain_queue(self._queue)
        self._drain_queue(self._recognition_queue)
        self._drain_queue(self._context_queue)
        self._drain_queue(self._translation_queue)
        with self._metrics_lock:
            self._translation_times.clear()
        self._stop_evt.clear()
        self._pause_evt.clear()
        self._recognition_input_done.clear()
        self._context_input_done.clear()
        self._translation_input_done.clear()
        self._set_state(PipelineState.STARTING)
        self._emit_status("启动中…")
        try:
            new_threads = (self._new_file_threads() if self._mode == "c"
                           else self._new_realtime_threads())
            self._set_state(PipelineState.RUNNING)
            self._threads.extend(new_threads)
            for thread in new_threads:
                thread.start()
            self._emit_status("处理中…" if self._mode == "c" else "正在连接音频设备…")
        except Exception as exc:
            self._set_state(PipelineState.FAILED)
            self._stop_evt.set()
            self._stop_tts_worker()
            recorder, self._recorder = self._recorder, None
            if recorder is not None:
                recorder.close()
            logger.exception("Pipeline 启动失败: mode=%s", self._mode)
            self._emit_status(f"启动失败: {exc}")
            raise

    def _new_file_threads(self) -> list[threading.Thread]:
        if self._in_path is None or not self._in_path.exists():
            raise FileNotFoundError("请先选择要处理的音频或视频文件")
        return [threading.Thread(
            target=self._run_file_mode, name="pipeline-file", daemon=True)]

    def _new_realtime_threads(self) -> list[threading.Thread]:
        self._build_real_time()
        if self._context_processor is not None:
            self._context_processor.reset()
        self._start_tts_worker()
        if self._mode == "a" and self._recording_enabled:
            self._recorder = WaveSessionRecorder(self._recordings_dir)
            self._last_recording_path = self._recorder.path
        threads = [
            threading.Thread(target=self._capture_loop,
                             name="pipeline-capture", daemon=True),
            threading.Thread(target=self._process_loop,
                             name="pipeline-process", daemon=True),
        ]
        if self._is_generative:
            threads.append(threading.Thread(
                target=self._recognition_loop,
                name="pipeline-recognize", daemon=True,
            ))
        if self._context_processor is not None:
            threads.append(threading.Thread(
                target=self._context_loop,
                name="pipeline-context", daemon=True,
            ))
        threads.append(threading.Thread(
            target=self._translation_loop,
            name="pipeline-translate", daemon=True,
        ))
        return threads

    def stop(self) -> None:
        if not self.is_running() and not any(t.is_alive() for t in self._threads):
            return
        self._set_state(PipelineState.STOPPING)
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
        self._set_state(PipelineState.IDLE)
        self._stop_tts_worker()
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
            metrics = _CaptureMetrics(started, started)
            while not self._stop_evt.is_set():
                chunk = source.read_chunk()
                if chunk is None:
                    if not self._stop_evt.is_set():
                        raise RuntimeError("音频设备意外停止输出")
                    break
                metrics.observe(chunk)
                self._accept_capture_chunk(chunk)
                self._report_capture_health(metrics, name)
        except Exception as exc:
            logger.exception("音频采集失败: mode=%s", self._mode)
            self._emit_status(f"音频设备错误: {exc}")
            self._set_state(PipelineState.FAILED)
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

    def _accept_capture_chunk(self, chunk: np.ndarray) -> None:
        if self._pause_evt.is_set():
            # Drain WASAPI while paused, but omit samples from recording/ASR.
            return
        if self._recorder is not None:
            self._recorder.write(chunk)
        try:
            self._queue.put(chunk, timeout=0.5)
        except queue.Full as exc:
            raise RuntimeError(
                "识别持续落后超过 10 分钟；为避免静默丢音已停止任务，"
                "请改用更轻量模型或硬件加速"
            ) from exc

    def _report_capture_health(self, metrics: _CaptureMetrics, name: str) -> None:
        now = time.monotonic()
        if now - metrics.last_heartbeat >= 1.0:
            backlog_s = self._queue.qsize() * CHUNK_FRAMES / SAMPLE_RATE
            logger.debug(
                "音频心跳: chunks=%d peak=%.6f queue=%d backlog=%.2fs",
                metrics.chunks, metrics.peak, self._queue.qsize(), backlog_s,
            )
            if backlog_s >= 5.0:
                logger.warning("识别暂时落后 %.1fs，音频仍完整缓冲、未丢失", backlog_s)
            metrics.last_heartbeat = now
        if (now - metrics.started >= 4.0 and metrics.peak < 1e-5 and
                not metrics.warned_silence):
            metrics.warned_silence = True
            logger.warning("音频已连接但持续静音: source=%s", name)
            self._emit_status("未检测到声音，请检查设备或播放内容")

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
            self._set_state(PipelineState.FAILED)
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
                elif self._context_processor is not None:
                    self._context_input_done.set()
                else:
                    self._translation_input_done.set()

    # ---- C 模式 (文件 → 双语字幕) ----
    def _run_file_mode(self) -> None:
        if self._in_path is None or not self._in_path.exists():
            self._emit_status("文件不存在")
            self._set_state(PipelineState.FAILED)
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
            self._set_state(PipelineState.FAILED)
        finally:
            if wav_path is not None:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("临时音频删除失败: %s", wav_path, exc_info=True)
            if self.state is not PipelineState.FAILED:
                self._set_state(PipelineState.IDLE)

    def _transcribe_file(self, path: Path) -> tuple[list[SubtitleLine], Optional[Path]]:
        """Decode one file, then delegate recognition to the selected backend."""
        pcm, wav_path = FileAudioDecoder.decode(path)
        if self._requested_stt_provider == "cloud":
            return self._recognize_cloud_file(pcm), wav_path
        return self._recognize_streaming(pcm), wav_path

    def _recognize_streaming(self, pcm: np.ndarray) -> list[SubtitleLine]:
        """Build local runtimes and delegate pure file recognition."""
        self._build_real_time()
        self._ensure_translator()
        return FileRecognizer.local(
            pcm,
            vad=self._vad,
            asr=self._asr,
            translator=self._translator,
            source_lang=self._src_lang,
            target_lang=self._dst_lang,
            validate_translation=self._trans_kind is not None,
        )

    def _recognize_cloud_file(self, pcm: np.ndarray) -> list[SubtitleLine]:
        """Build cloud runtimes and delegate VAD-split recognition."""
        self._build_real_time()
        if self._cloud_stt is None or self._vad is None:
            raise RuntimeError("云 STT 未正确初始化")
        self._ensure_translator()
        return FileRecognizer.cloud(
            pcm,
            vad=self._vad,
            cloud_stt=self._cloud_stt,
            translator=self._translator,
            source_lang=self._src_lang,
            target_lang=self._dst_lang,
            tuning=self._effective_asr_tuning(generative=True),
            validate_translation=self._trans_kind is not None,
        )

    # ---- srt / vtt / txt 导出 (模块级函数, 便于单测) ----
    @staticmethod
    def _fmt_ts(ms: int) -> str:
        return SubtitleExporter.format_timestamp(ms)

    @staticmethod
    def write_srt(lines: list[SubtitleLine], out: Path, dur_ms: int = 1500) -> None:
        SubtitleExporter.write_srt(lines, out, duration_ms=dur_ms)

    @staticmethod
    def write_vtt(lines: list[SubtitleLine], out: Path) -> None:
        SubtitleExporter.write_vtt(lines, out)

    @staticmethod
    def write_txt(lines: list[SubtitleLine], out: Path) -> None:
        SubtitleExporter.write_txt(lines, out)
