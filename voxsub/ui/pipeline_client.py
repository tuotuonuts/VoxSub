"""Pipeline 契约接入点（M7 ← M6 依赖面）。

DESIGN.md『Pipeline 契约（M6，UI 唯一依赖面）』:
    class Pipeline:
        mode: str                       # "a" | "b" | "c"
        def start(self) -> None
        def stop(self) -> bool
        def set_mode(self, mode: str) -> None
        def on_utterance(self, cb: Callable[[str, str], None]) -> None  # (原文, 译文)
        def on_status(self, cb: Callable[[str], None]) -> None          # 状态文本
        def is_running(self) -> bool

``_PipelineStub`` 保留给显式注入的 UI 单测。生产路径必须返回真实 Pipeline；
导入故障会直接暴露给启动流程，不能伪装成“正在识别”。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from voxsub.logging_setup import get_logger

logger = get_logger("ui.pipeline_client")

_PIPELINE_IMPORT_ERROR: Exception | None = None

try:
    from voxsub.pipeline import Pipeline as _RealPipeline  # type: ignore[import-not-found]
    _REAL_PIPELINE_NAME = getattr(_RealPipeline, "__module__", "voxsub.pipeline")
    _HAS_PIPELINE = True
except Exception as exc:  # Keep the original import failure for user diagnostics.
    logger.exception("真实 Pipeline 导入失败，应用不能继续进入识别模式")
    _RealPipeline = None  # type: ignore[assignment]
    _HAS_PIPELINE = False
    _PIPELINE_IMPORT_ERROR = exc


class _PipelineStub:
    """鸭子类型 Pipeline：实现 DESIGN 契约接口，仅供 UI 壳层联调。

    行为：维护 mode / running，回调注册即存即用；start/stop 触发 on_status
    状态文本（与主窗状态灯语汇一致：待机 / 拾音中 / 推理中）。
    """

    def __init__(self, mode: str = "a") -> None:
        self.mode = mode if mode in ("a", "b", "c") else "a"
        self._running = False
        self._utterance_cb: Callable[[str, str], None] | None = None
        self._status_cb: Callable[[str], None] | None = None
        self._partial_cb: Callable[[str], None] | None = None
        self._draft_cb: Callable[[str, str], None] | None = None
        self.langs = ("zh", "en")
        self.input_file = ""
        self.tts_enabled = False
        self.tts_model_ids = {
            "zh": "tts-icefall-zh-aishell3",
            "en": "tts-icefall-en-ljspeech-low",
        }
        self.models_dir: Path | None = None
        self.audio_devices = ("", "")
        self.capture_process = (0, "")
        self.stt = ("local", None)
        self.translator = ("opus-fast", None)
        self.asr_model_id = "asr-zipformer-bilingual-fast"
        self.asr_tuning = {"profile": "auto"}
        self.recording_enabled = False
        self._paused = False
        self.last_recording_path = None

    # -- 契约实现 ----------------------------------------------------------
    def start(self) -> None:
        if not self._running:
            self._running = True
            self._paused = False
            self._emit_status("拾音中")

    def stop(self) -> bool:
        if self._running:
            self._running = False
            self._paused = False
            self._emit_status("待机")
        return True

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in ("a", "b", "c") else "a"

    def set_langs(self, src: str, dst: str) -> None:
        self.langs = (src, dst)

    def set_input_file(self, path: str) -> None:
        self.input_file = str(path)

    def set_tts(self, enabled: bool) -> None:
        self.tts_enabled = bool(enabled)

    def set_tts_models(self, model_ids: dict[str, str] | None = None) -> None:
        self.tts_model_ids = dict(model_ids or {})

    def set_models_dir(self, path: str | Path) -> None:
        if self._running:
            raise RuntimeError("识别运行中，无法切换模型目录")
        self.models_dir = Path(path)

    def set_audio_devices(self, mic_device_id: str = "", loopback_device_id: str = "") -> None:
        self.audio_devices = (mic_device_id, loopback_device_id)

    def set_capture_process(self, process_id: int = 0, window_title: str = "") -> None:
        self.capture_process = (int(process_id), window_title)

    def set_translator(self, kind: str, config=None) -> None:
        self.translator = (kind, config)

    def set_stt(self, provider: str = "local", config=None) -> None:
        self.stt = (str(provider), config)

    def set_asr_model(self, model_id: str) -> None:
        self.asr_model_id = str(model_id)

    def set_asr_tuning(self, tuning: dict | None = None) -> None:
        self.asr_tuning = dict(tuning or {})

    def set_recording(self, enabled: bool, directory=None) -> None:
        self.recording_enabled = bool(enabled)

    def pause(self) -> None:
        if self._running:
            self._paused = True
            self._emit_status("已暂停 · 点击继续恢复录音与翻译")

    def resume(self) -> None:
        if self._running:
            self._paused = False
            self._emit_status("拾音中")

    def is_paused(self) -> bool:
        return self._paused

    def on_utterance(self, cb: Callable[[str, str], None]) -> None:
        self._utterance_cb = cb

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._status_cb = cb

    def on_partial(self, cb: Callable[[str], None]) -> None:
        self._partial_cb = cb

    def on_draft(self, cb: Callable[[str, str], None]) -> None:
        self._draft_cb = cb

    def is_running(self) -> bool:
        return self._running

    # -- 内部辅助（UI 联调用：模拟一条字幕进入流） ------------------------
    def _emit_utterance(self, src: str, dst: str) -> None:
        if self._utterance_cb is not None:
            self._utterance_cb(src, dst)

    def _emit_partial(self, src: str, dst: str = "") -> None:
        if self._partial_cb is not None:
            self._partial_cb(src)
        if self._draft_cb is not None:
            self._draft_cb(src, dst)

    def _emit_status(self, text: str) -> None:
        if self._status_cb is not None:
            self._status_cb(text)


def get_pipeline(*, allow_stub: bool = False) -> object:
    """Return the real Pipeline, with an explicit test-only stub escape hatch."""
    if _RealPipeline is not None:
        return _RealPipeline()
    if allow_stub:
        return _PipelineStub()
    raise RuntimeError(
        "核心识别管线无法加载；请在诊断页面检查运行时依赖和日志。"
    ) from _PIPELINE_IMPORT_ERROR


__all__ = ["get_pipeline", "_PipelineStub", "_HAS_PIPELINE"]
