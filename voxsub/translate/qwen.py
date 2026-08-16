"""voxsub.translate.qwen —— 质量档翻译器: Qwen2.5-1.5B-Instruct Q4_K_M GGUF。

技术路线 (DESIGN.md 决策 #9, 2026-08-17 查证): Qwen2.5 ONNX 权重在 HF 全部
gated(401) → 弃 onnxruntime-genai, 改 **llama-cpp-python + 官方 GGUF**。
目标 2-5s/句。

并发安全: 单个 Llama 实例并非线程安全, 这里用一个互斥锁串行化所有
translate 调用 (实时字幕是低并发流, 一次一句, 锁足够)。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from .base import TranslationError, Translator


def _default_models_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "models"


#: (src,dst) → 指令模板中的语言名 (与模型指令兼容的常见写法)
_LANG_NAME = {
    "zh": "Chinese",
    "en": "English",
}

#: 互翻指令模板。保留原文含义, 输出仅译文 (不解释、不加引号)。
_PROMPT_TEMPLATES = {
    ("zh", "en"): "Translate the following Chinese text to English. Reply with only the English translation, no explanations or quotes.\nChinese: {text}\nEnglish:",
    ("en", "zh"): "Translate the following English text to Chinese. Reply with only the Chinese translation, no explanations or quotes.\nEnglish: {text}\nChinese:",
}


class QwenQualityTranslator(Translator):
    """质量档: llama-cpp-python 加载 Qwen GGUF, 中英互翻。"""

    name = "qwen-quality"
    langs = ("zh", "en")
    local = True

    def __init__(self, model_path: Path | str | None = None,
                 n_ctx: int = 2048, n_threads: int = 4,
                 max_tokens: int = 128, temperature: float = 0.2,
                 fast_mode: bool = True):
        self._model_path = Path(model_path) if model_path else (
            _default_models_dir() / "llm" / "qwen2.5-1.5b-instruct-q4_k_m.gguf")
        self._n_ctx = n_ctx
        self._n_threads = n_threads
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._fast_mode = fast_mode          # True=max_tokens 更小(草稿档)
        self._llm = None
        self._lock = threading.Lock()
        self._open_err: str | None = None

    # ------------------------------------------------------------------
    def _ensure_loaded(self):
        if self._llm is not None:
            return
        if not self._model_path.exists():
            raise TranslationError(
                f"质量档模型缺失: {self._model_path} (请用 scripts/model_fetch.py 下载)")
        try:
            from llama_cpp import Llama
        except ImportError as exc:  # pragma: no cover - 依赖缺失, 冒烟测试 catch
            raise TranslationError(f"未安装 llama-cpp-python: {exc}") from exc
        mt = 64 if self._fast_mode else self._max_tokens
        self._llm = Llama(
            model_path=str(self._model_path),
            n_ctx=self._n_ctx,
            n_threads=self._n_threads,
            n_threads_batch=self._n_threads,
            verbose=False,
        )
        self._eff_max_tokens = mt

    # ------------------------------------------------------------------
    def translate(self, text: str, src_lang: str, dst_lang: str, *,
                  timeout_ms: int = 15000) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        tpl = _PROMPT_TEMPLATES.get((src_lang, dst_lang))
        if tpl is None:
            raise TranslationError(f"质量档不支持语言对 {(src_lang, dst_lang)}")
        prompt = tpl.format(text=text)
        try:
            with self._lock:
                self._ensure_loaded()
                out = self._llm.create_completion(
                    prompt, max_tokens=self._eff_max_tokens,
                    temperature=self._temperature, stop=["\n"], echo=False)
            raw = out["choices"][0]["text"].strip()
            return _clean(raw)
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError(f"qwen 推理失败: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._llm = None   # 释放底层 llama 模型 (gc)

    def health(self) -> str:
        try:
            self._ensure_loaded()
        except TranslationError as e:
            return str(e)
        return "ok"


def _clean(text: str) -> str:
    """去掉模型偶发的引号包裹 / 前后缀。"""
    text = text.strip()
    for q in ('"', "'", "「」", "『』"):
        if len(q) == 1 and text.startswith(q) and text.endswith(q) and len(text) > 1:
            text = text[1:-1].strip()
            break
    # 去除常见噪声前缀 (如 "English:", "翻译:")
    for pref in ("English:", "Chinese:", "Translation:", "译文:", "翻译:"):
        if text.lower().startswith(pref.lower()) and len(text) > len(pref):
            text = text[len(pref):].strip()
    return text
