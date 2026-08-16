"""Pipeline 契约接入点（M7 ← M6 依赖面）。

DESIGN.md『Pipeline 契约（M6，UI 唯一依赖面）』:
    class Pipeline:
        mode: str                       # "a" | "b" | "c"
        def start(self) -> None
        def stop(self) -> None
        def set_mode(self, mode: str) -> None
        def on_utterance(self, cb: Callable[[str, str], None]) -> None  # (原文, 译文)
        def on_status(self, cb: Callable[[str], None]) -> None          # 状态文本
        def is_running(self) -> bool

M6 尚未实现：本模块在 import 失败时提供鸭子类型 _PipelineStub，让 UI 壳层可以
完整联调（按钮启停 / 模式切换 / 回调接线），真实音频/翻译/字幕流由 M6 集成时
无缝替换 —— UI 只面向 get_pipeline() 返回的对象。
"""
from __future__ import annotations

from typing import Callable

try:  # M6 就绪后可 import；未就绪时走 stub
    from voxsub.pipeline import Pipeline as _RealPipeline  # type: ignore[import-not-found]
    _REAL_PIPELINE_NAME = getattr(_RealPipeline, "__module__", "voxsub.pipeline")
    _HAS_PIPELINE = True
except Exception:  # ModuleNotFoundError 等 —— M6 未实现
    _RealPipeline = None  # type: ignore[assignment]
    _HAS_PIPELINE = False


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

    # -- 契约实现 ----------------------------------------------------------
    def start(self) -> None:
        if not self._running:
            self._running = True
            self._emit_status("拾音中")

    def stop(self) -> None:
        if self._running:
            self._running = False
            self._emit_status("待机")

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in ("a", "b", "c") else "a"

    def on_utterance(self, cb: Callable[[str, str], None]) -> None:
        self._utterance_cb = cb

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._status_cb = cb

    def is_running(self) -> bool:
        return self._running

    # -- 内部辅助（UI 联调用：模拟一条字幕进入流） ------------------------
    def _emit_utterance(self, src: str, dst: str) -> None:
        if self._utterance_cb is not None:
            self._utterance_cb(src, dst)

    def _emit_status(self, text: str) -> None:
        if self._status_cb is not None:
            self._status_cb(text)


def get_pipeline() -> object:
    """返回 Pipeline 实例：优先真实实现（M6），否则 stub。"""
    if _RealPipeline is not None:
        return _RealPipeline()
    return _PipelineStub()


__all__ = ["get_pipeline", "_PipelineStub", "_HAS_PIPELINE"]