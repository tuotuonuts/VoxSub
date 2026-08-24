"""voxsub.translate.base —— Translator 抽象基类 (M4 契约)。

契约见 DESIGN.md「翻译层契约（M4）」::

    class Translator(ABC):
        name: str
        langs: tuple[str, ...]
        local: bool
        def translate(self, text, src_lang, dst_lang, *, timeout_ms=15000) -> str
        def close(self) -> None
        def health(self) -> str

时间/失败约定 (与 PrefetchEngine 配合):
- translate 抛 TranslationError 表示单句失败; 调用方负责重试 1 次后降级
  (保留原文 + 字幕标记 [翻译失败])。
- 连续 3 句失败由调用方弹提示, 不崩管道。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import json


class TranslationError(RuntimeError):
    """单句翻译失败。由调用方 (pipeline / PrefetchEngine) 捕获并降级。"""


def parse_translation_batch(output: str, expected_count: int) -> list[str]:
    """Parse a model's JSON-array response without accepting extra prose."""
    text = str(output or "").strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise TranslationError("批量翻译没有返回 JSON 数组")
    try:
        value = json.loads(text[start:end + 1])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TranslationError("批量翻译返回的 JSON 无法解析") from exc
    if not isinstance(value, list) or len(value) != max(0, int(expected_count)):
        raise TranslationError(
            f"批量翻译返回数量不匹配: expected={expected_count} "
            f"actual={len(value) if isinstance(value, list) else 'not-list'}"
        )
    results = [str(item or "").strip() for item in value]
    if any(not item for item in results):
        raise TranslationError("批量翻译包含空译文")
    return results


class Translator(ABC):
    """翻译层抽象 —— 快档/质量档/云三实现共用契约。"""

    name: str = "base"
    langs: tuple[str, ...] = ()          # 支持的语言代码 (如 ("zh", "en"))
    local: bool = False                  # True = 离线可用

    @abstractmethod
    def translate(self, text: str, src_lang: str, dst_lang: str, *,
                  timeout_ms: int = 15000) -> str:
        """把 text 从 src_lang 翻译到 dst_lang, 返回译文。

        Raises:
            TranslationError: 本次翻译失败 (网络/模型/超时), 调用方降级。
        """

    @abstractmethod
    def close(self) -> None:
        """释放底层资源 (推理会话 / 模型 / 网络句柄)。幂等。"""

    def health(self) -> str:
        """返回 \"ok\" 或缺陷描述 (供诊断页展示)。"""
        return "ok"

    # ---- 便捷: 语言对是否受支持 ----
    def supports(self, src_lang: str, dst_lang: str) -> bool:
        return src_lang in self.langs and dst_lang in self.langs

    def __enter__(self) -> "Translator":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
