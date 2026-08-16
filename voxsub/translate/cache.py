"""voxsub.translate.cache —— 翻译结果 LRU 缓存 (M4)。

契约 (DESIGN.md): key=(norm_text, src, dst), 上限 2000 条; 重复出现的
字幕/短句零延迟。文本归一化: 去首尾空白、折叠内部连续空白、统一大小写
(中文不影响; 英文大小写归一避免重复缓存)。
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Optional


def normalize_text(text: str) -> str:
    """缓存键用文本归一化 (对译义无影响)。"""
    if text is None:
        return ""
    s = " ".join(str(text).split())   # 折叠空白 + 去首尾
    return s.lower()


class TranslationCache:
    """线程安全 LRU 缓存。"""

    def __init__(self, max_size: int = 2000):
        self._max = max(int(max_size), 1)
        self._store: OrderedDict[tuple, str] = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, text: str, src_lang: str, dst_lang: str) -> tuple:
        return (normalize_text(text), src_lang, dst_lang)

    def get(self, text: str, src_lang: str, dst_lang: str) -> Optional[str]:
        """命中返回译文 (刷新为最近使用), 未命中返回 None。"""
        key = self._key(text, src_lang, dst_lang)
        with self._lock:
            v = self._store.get(key)
            if v is None:
                return None
            self._store.move_to_end(key)
            return v

    def put(self, text: str, src_lang: str, dst_lang: str, result: str) -> None:
        """写入一条结果; 超上限时淘汰最久未用。"""
        if not result:
            return
        key = self._key(text, src_lang, dst_lang)
        with self._lock:
            self._store[key] = result
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    # ---------------- 命中率统计 (诊断页可选) ----------------
    def stats(self) -> dict:
        return {"size": len(self._store), "max": self._max}
