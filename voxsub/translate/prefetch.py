"""voxsub.translate.prefetch —— 翻译预取引擎 (M4)。

让"说一句翻一句"的感知延迟贴近识别完成 + 防抖窗口: ASR 还在出字时把
已识别的部分文本反复预热翻译, 整句稳定后输出唯一终稿。契约
(DESIGN.md / 任务):
- 部分文本碎片累积, 防抖 800ms 发翻译 (跳过仍在输入的碎片)
- 整句完成后合并/修正, 出**且仅出一次**终稿 (不重复回调)
- 中途预热结果只用于缩短感知延迟, 不直接回调 (避免字幕跳动)

线程安全: 单处理线程调用 (Pipeline 处理线程), 轻量锁保护状态即可。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from voxsub.logging_setup import get_logger

logger = get_logger("translate.prefetch")


class PrefetchEngine:
    """按句子碎片累积 + 防抖触发翻译预热的协调器。

    用法::

        pf = PrefetchEngine(translate_fn, on_final=cb, debounce_ms=800)
        pf.on_partial("你")        # 预热(可选), 不发终稿
        pf.on_partial("你好")       # 仍在输入, 重置防抖计时
        pf.on_final("你好世界")     # 整句完成 → translate_fn → on_final 恰一次
    """

    def __init__(self, translate_fn: Callable[[str, str, str], str],
                 on_final: Optional[Callable[[str, str], None]] = None,
                 debounce_ms: int = 800,
                 final_cooldown_ms: int = 400):
        self._translate = translate_fn
        self._on_final = on_final
        self._debounce_s = debounce_ms / 1000.0
        self._cooldown_s = final_cooldown_ms / 1000.0
        self._lock = threading.Lock()
        self._last_input = 0.0          # 最近一次部分文本到达 (monotonic)
        self._current_final = None      # 已发射的整句 (去重)
        self._last_final_at = 0.0

    # ------------------------------------------------------------------
    def on_partial(self, text: str, src: str = "zh", dst: str = "en") -> None:
        """部分文本到达: 记录到达时刻与前缀 (供防抖/终稿合并判定)。

        预热翻译实际由 Pipeline 在 on_partial 里经 TranslationCache 触发
        (逐碎片直译结果易与终稿冲突, 故引擎不直接回调部分译文)。
        """
        with self._lock:
            self._last_input = time.monotonic()
            self._src, self._dst = src, dst

    def on_final(self, text: str, src: str = "zh", dst: str = "en") -> None:
        """整句完成: 防抖后翻译并回调解算终稿 (同一句只发一次)。"""
        text = (text or "").strip()
        if not text:
            return
        now = time.monotonic()
        with self._lock:
            if self._current_final == text:
                return                       # 同句重复提交, 忽略
            last_input = self._last_input
            self._current_final = text       # 立即占位, 防并发重复发射

        # 距上次部分输入太近 → 视为仍在输入中的尾判, 延迟到稳定窗口
        if now - last_input < self._debounce_s:
            # 冷却后补发 (后台线程等防抖窗口过再发射), 保证终稿必达
            remaining = self._debounce_s - (now - last_input)
            self._later(remaining, self._delayed_final, (text, src, dst))
            return

        self._emit(text, src, dst)

    @staticmethod
    def _later(delay: float, fn, args) -> None:
        """daemon 定时线程 (不阻塞解释器退出; 长时间防抖窗口内进程可正常结束)。"""
        t = threading.Timer(max(delay, 0.0), fn, args=args)
        t.daemon = True
        t.start()

    def _delayed_final(self, text: str, src: str, dst: str) -> None:
        """防抖窗口过后补发的终稿。"""
        with self._lock:
            if self._current_final != text:
                return                       # 已被更新句覆盖
            if time.monotonic() - self._last_final_at < self._cooldown_s:
                self._later(self._cooldown_s, self._delayed_final, (text, src, dst))
                return
        self._emit(text, src, dst)

    def _emit(self, text: str, src: str, dst: str) -> None:
        try:
            translation = self._translate(text, src, dst)
        except Exception:
            # 软降级: 字幕不中断, 原文+标记兜底; 属正常降级路径故用 debug 级别
            logger.debug("终稿翻译失败, 降级为 '原文+[翻译失败]' (src=%s dst=%s)",
                         src, dst, exc_info=True)
            translation = text + " [翻译失败]"
        with self._lock:
            self._last_final_at = time.monotonic()
            self._current_final = None       # 清占位, 允许下句
        if self._on_final and translation:
            self._on_final(text, translation)

    def translate_now(self, text: str, src: str = "zh", dst: str = "en") -> str:
        """绕过防抖立即翻译 (C 模式批量 / flush)。"""
        try:
            return self._translate(text, src, dst)
        except Exception:
            logger.exception("translate_now 立即翻译失败 (src=%s dst=%s)", src, dst)
            raise

    def reset(self) -> None:
        """整段会话结束复位去重状态 (新句子流)。"""
        with self._lock:
            self._current_final = None
            self._last_input = 0.0
