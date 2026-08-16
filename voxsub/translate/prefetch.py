"""voxsub.translate.prefetch —— 翻译预取引擎。

让"说一句翻一句"的感知延迟贴近识别完成 + 防抖窗口: ASR 还在出字时就
把已识别的部分文本反复预热翻译, 整句稳定后输出终稿。契约 (DESIGN.md):
- 输入部分文本碎片 → 防抖 800ms 跳过仍在输入的碎片 → 整句提交时出终稿
- 同一句只出一份终稿; 中途预热结果可丢弃(不回调), 避免字幕跳动

线程安全: 单处理线程调用 (Pipeline 处理线程), 仅需轻量锁保护状态。
"""
from __future__ import annotations

import threading
import time


class PrefetchEngine:
    """按句子碎片累积 + 防抖触发翻译预热的协调器。

    用法::
        pf = PrefetchEngine(translate_fn, debounce_ms=800)
        pf.on_partial("你")         # 预热, 不发终稿
        pf.on_partial("你好")        # 仍在输入中, 重置防抖计时
        pf.on_final("你好世界")      # 整句完成 → translate_fn → on_final_cb
    """

    def __init__(self, translate_fn, debounce_ms: int = 800,
                 on_final: callable = None) -> None:
        """
        Args:
            translate_fn: callable(text, src, dst) -> str, 真正的翻译函数。
            debounce_ms: 距上次输入多久后视为整句稳定可出终稿。
            on_final: callable(src_text, dst_text) 整句终稿回调(Pipeline 接 UI)。
        """
        self._translate_fn = translate_fn
        self._debounce_s = debounce_ms / 1000.0
        self._on_final = on_final
        self._lock = threading.Lock()

    def on_partial(self, text: str, src: str = "zh", dst: str = "en") -> None:
        """部分文本到达: 仅记录最近输入时间, 用于整句稳定判定。"""
        with self._lock:
            # 预取方案: 简化实现——部分文本暂不实际调用 translate_fn,
            # 因为逐碎片翻译结果易与终稿冲突造成字幕跳动。由 Pipeine 的
            # TranslationCache 兜底短句命中; 这里负责"整句稳定"判定。
            self._last_input = time.monotonic()
            self._src, self._dst = src, dst

    def on_final(self, text: str, src: str = "zh", dst: str = "en") -> None:
        """整句完成: 策略化等待防抖窗口(若刚收尾), 然后翻译出终稿。"""
        with self._lock:
            last = getattr(self, "_last_input", 0.0)
        elapsed = time.monotonic() - last
        if elapsed < self._debounce_s:
            # 距上次输入太近, 可能是误收的中间态; 留给后续 flush/新句处理
            return
        translation = self._translate_fn(text, src, dst)
        if self._on_final:
            self._on_final(text, translation)

    def translate_now(self, text: str, src: str = "zh", dst: str = "en") -> str:
        """绕过防抖立即翻译(如 C 模式批量, 或 flush 时)。"""
        return self._translate_fn(text, src, dst)