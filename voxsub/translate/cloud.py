"""voxsub.translate.cloud —— 云翻译器: OpenAI 兼容端点 (用户自配 key)。

配置 (从 config 读取, 缺省回退环境变量):
- api_key / DEEPSEEK_API_KEY
- base_url / DEEPSEEK_BASE_URL   (缺省 https://api.deepseek.com)
- model / DEEPSEEK_MODEL         (缺省 deepseek-chat)

安全约束 (DESIGN.md): 仅允许用户显式配置的端点。白名单域名机制 ——
base_url 的主机必须命中 allowlist, 否则拒绝 (防止任意端点被进程调用)。
请求体为 OpenAI /v1/chat/completions 兼容格式, 用标准库 urllib。
失败 (网络/HTTP 非 2xx/解析) 一律抛 TranslationError, 由调用方降级。
"""
from __future__ import annotations

import json
import os
import threading
from urllib.parse import urlparse

from voxsub.logging_setup import get_logger
from voxsub.language_guard import language_name, normalize_language

from ._http_client import OpenAICompatError, chat_completion, normalize_api_base
from .base import TranslationError, Translator, parse_translation_batch

logger = get_logger("translate.cloud")

#: 允许的 OpenAI 兼容端点主机白名单 (安全边界)
DEFAULT_ALLOWLIST = {
    "api.deepseek.com",
    "api.openai.com",
    "api.moonshot.cn",
    "dashscope.aliyuncs.com",
    "api.groq.com",
    "api.mistral.ai",
    "api.siliconflow.cn",
    "open.bigmodel.cn",
    # 回环地址始终放行: 本地 llama-server (质量档) 与本地 mock 端点测试
    # 都挂在 127.0.0.1/localhost, 属于进程自身可控边界, 不构成越权调用。
    "127.0.0.1",
    "localhost",
    "::1",
}


def _cfg_get(config, key: str, env: str | None = None, default=None):
    """从 config (dict/obj) 取值, 缺省回退环境变量与默认值。"""
    if config is not None:
        if isinstance(config, dict):
            v = config.get(key)
        else:
            v = getattr(config, key, None)
        if v:
            return v
    if env:
        ev = os.environ.get(env)
        if ev:
            return ev
    return default


class CloudTranslator(Translator):
    """云档: 把句子交给 OpenAI 兼容 /v1/chat/completions 端点翻译。"""

    name = "cloud"
    langs = ("zh", "en")
    local = False

    def __init__(self, config=None, *, allowlist: set[str] | None = None,
                 timeout_ms: int = 15000):
        # New settings keep translation credentials separate from STT.  The
        # legacy keys remain fallbacks so existing 0.3.x configs keep working.
        self._api_key = (_cfg_get(config, "translate_api_key") or
                         _cfg_get(config, "api_key", "DEEPSEEK_API_KEY"))
        base_url = (_cfg_get(config, "translate_base_url") or
                    _cfg_get(config, "base_url", "DEEPSEEK_BASE_URL",
                             "https://api.deepseek.com/v1"))
        self._base_url = normalize_api_base(base_url)
        self._model = (str(_cfg_get(config, "translate_model") or
                           _cfg_get(config, "model", "DEEPSEEK_MODEL",
                                    "deepseek-chat")))
        self._allowlist = allowlist if allowlist is not None else DEFAULT_ALLOWLIST
        self._timeout = timeout_ms / 1000.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _validate_endpoint(self) -> str:
        parsed = urlparse(self._base_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme in {"http", "https"} and host in self._allowlist:
            endpoint = f"{self._base_url}/chat/completions"
            return endpoint
        # 只记 host/scheme, 不记完整 base_url (防止带 query 的 URL 泄漏)
        logger.warning("云翻译端点拒绝: host=%s 不在白名单内 (scheme=%s), 不发起调用",
                       host, parsed.scheme)
        raise TranslationError(
            f"端点基址 {self._base_url!r} 不在白名单 {sorted(self._allowlist)} 内, "
            f"拒绝云翻译。请只使用受信任的 OpenAI 兼容服务。")

    def ready(self) -> bool:
        """凭据 + 端点白名单是否就绪 (供 list_available / 设置页开关)。"""
        if not self._api_key:
            return False
        try:
            self._validate_endpoint()
            return True
        except TranslationError:
            return False

    # ------------------------------------------------------------------
    def translate(self, text: str, src_lang: str, dst_lang: str, *,
                  timeout_ms: int = 15000) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        if not self._api_key:
            logger.debug("云翻译未配置 api_key, 按未就绪处理 (正常软降级)")
            raise TranslationError("云翻译未配置 DEEPSEEK_API_KEY")
        # ``self._timeout`` is already expressed in seconds.  Convert only an
        # explicit per-call millisecond override; otherwise a previous
        # double-conversion could turn the default 15 seconds into 15 ms.
        if timeout_ms == 15000:
            effective_timeout = self._timeout
        else:
            effective_timeout = max(1.0, int(timeout_ms) / 1000.0)
        endpoint = self._validate_endpoint()
        src_lang = normalize_language(src_lang)
        dst_lang = normalize_language(dst_lang)
        lang_hint = (
            f"The source text is {language_name(src_lang)}. "
            f"Translate only from {language_name(src_lang)} to {language_name(dst_lang)}. "
            "Do not translate into, detect as, or add any other language."
        )
        messages = [
            {"role": "system",
             "content": ("You are a professional translator. " + lang_hint +
                         " Reply with only the translation, no explanations.")},
            {"role": "user", "content": text},
        ]
        try:
            with self._lock:
                out = chat_completion(
                    endpoint, messages=messages, api_key=self._api_key,
                    model=self._model, temperature=0.2,
                    timeout_sec=effective_timeout)
        except OpenAICompatError as exc:
            # 只记 host 与 model; api_key 只在 Authorization header 中出现, 绝不落日志
            logger.exception("云翻译 API 调用失败 (host=%s, model=%s)",
                             (urlparse(self._base_url).hostname or "").lower(),
                             self._model)
            raise TranslationError(str(exc)) from exc
        return out

    def translate_many(
        self, texts: list[str], src_lang: str, dst_lang: str, *,
        timeout_ms: int = 15000,
    ) -> list[str]:
        """Translate an OCR line batch with one cloud request."""
        sources = [str(text or "").strip() for text in texts]
        if not sources:
            return []
        if len(sources) == 1:
            return [self.translate(
                sources[0], src_lang, dst_lang, timeout_ms=timeout_ms)]
        if not self._api_key:
            raise TranslationError("云翻译未配置 DEEPSEEK_API_KEY")
        endpoint = self._validate_endpoint()
        src_lang = normalize_language(src_lang)
        dst_lang = normalize_language(dst_lang)
        payload = json.dumps(sources, ensure_ascii=False, separators=(",", ":"))
        messages = [
            {"role": "system", "content": (
                "You are a professional machine-translation engine. Return only "
                "the valid JSON array requested by the user, with no prose.")},
            {"role": "user", "content": (
                f"Translate every JSON item from {language_name(src_lang)} to "
                f"{language_name(dst_lang)}. Return exactly {len(sources)} strings "
                "in the same order. Do not change the array length.\n" + payload)},
        ]
        effective_timeout = (
            self._timeout if timeout_ms == 15000
            else max(1.0, int(timeout_ms) / 1000.0)
        )
        try:
            with self._lock:
                raw = chat_completion(
                    endpoint, messages=messages, api_key=self._api_key,
                    model=self._model, temperature=0.1,
                    timeout_sec=effective_timeout)
            return parse_translation_batch(raw, len(sources))
        except OpenAICompatError as exc:
            logger.exception(
                "云 OCR 批量翻译失败 (host=%s, model=%s)",
                (urlparse(self._base_url).hostname or "").lower(), self._model)
            raise TranslationError(str(exc)) from exc

    def close(self) -> None:
        pass  # 无长连接, 无需释放
