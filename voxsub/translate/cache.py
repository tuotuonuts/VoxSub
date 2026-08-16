"""voxsub.translate.cache —— 翻译结果 LRU 缓存。

契约 (DESIGN.md「翻译层契约/实时性机制」):
- key = (norm_text, src, dst), norm_text 为空白归一化后的小写文本
- 上限 2000 条, 超限淘汰最久未使用项
- 线程安全 (Pipeline 逐句并发调用, 需加锁)

纯逻辑无外部依赖, 便于单元测试。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional


def normalize_key(text: str) -> str:
    """归一化: 折叠空白 + 转小写。短句/标点差异造成的重复命中即靠此。"""
    return " ".join(text.lower().split())


class TranslationCache:
    """LRU 缓存: 同一句原文在同一语言对下只翻译一次, 重复出现的字幕零延迟。"""

    def __init__(self, max_items: int = 2000) -> None:
        self._max = max(1, int(max_items))
        self._data: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, text: str, src_lang: str, dst_lang: str) -> Optional[str]:
        """命中返回译文并标记为最近使用; 未命中返回 None。"""
        key = (normalize_key(text), src_lang, dst_lang)
        with self._lock:
            val = self._data.get(key)
            if val is not None:
                self._data.move_to_end(key)  # LRU: 提到队尾(最近)
            return val

    def put(self, text: str, src_lang: str, dst_lang: str, translation: str) -> None:
        """写入译文; 超上限淘汰最久未用项。"""
        if not text or not translation:
            return
        key = (normalize_key(text), src_lang, dst_lang)
        with self._lock:
            self._data[key] = translation
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)  # 移除最久未用(队首)

    def size(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return self.size()