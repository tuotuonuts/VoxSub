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
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from .base import TranslationError, Translator

#: 允许的 OpenAI 兼容端点主机白名单 (安全边界)
DEFAULT_ALLOWLIST = {
    "api.deepseek.com",
    "api.openai.com",
    "api.moonshot.cn",
    "dashscope.aliyuncs.com",
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
        self._api_key = _cfg_get(config, "api_key", "DEEPSEEK_API_KEY")
        base_url = _cfg_get(config, "base_url", "DEEPSEEK_BASE_URL",
                            "https://api.deepseek.com")
        self._base_url = base_url.rstrip("/")
        self._model = _cfg_get(config, "model", "DEEPSEEK_MODEL", "deepseek-chat")
        self._allowlist = allowlist if allowlist is not None else DEFAULT_ALLOWLIST
        self._timeout = timeout_ms / 1000.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _validate_endpoint(self) -> str:
        parsed = urlparse(self._base_url)
        host = (parsed.hostname or "").lower()
        if host in self._allowlist:
            endpoint = f"{self._base_url}/v1/chat/completions"
            return endpoint
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
            raise TranslationError("云翻译未配置 DEEPSEEK_API_KEY")
        endpoint = self._validate_endpoint()
        lang_hint = {
            ("zh", "en"): "Translate to English.",
            ("en", "zh"): "Translate to Chinese.",
        }.get((src_lang, dst_lang), f"Translate from {src_lang} to {dst_lang}.")
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system",
                 "content": "You are a professional translator. " + lang_hint +
                            " Reply with only the translation, no explanations."},
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(
            endpoint, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._api_key}"})
        try:
            with self._lock:
                with urlrequest.urlopen(req, timeout=max(timeout_ms, 1) / 1000.0) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:200]
            except Exception:
                pass
            raise TranslationError(f"云翻译 HTTP {exc.code}: {detail}") from exc
        except urlerror.URLError as exc:
            raise TranslationError(f"云翻译网络错误: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise TranslationError(f"云翻译超时/IO: {exc}") from exc
        try:
            out = body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationError(f"云翻译响应格式异常: {body}") from exc
        return out

    def close(self) -> None:
        pass  # 无长连接, 无需释放
