"""voxsub.translate._http_client —— 极简 OpenAI 兼容 /v1/chat/completions 客户端。

云翻译 (CloudTranslator) 与 llama-server (QwenQualityTranslator) 共用:
两者都只依赖 OpenAI 兼容的 chat completions 接口, 这里统一实现网络层,
避免重复。

仅用标准库 urllib。失败一律抛 OpenAICompatError (调用方转成 TranslationError)。
"""
from __future__ import annotations

import json
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from voxsub.logging_setup import get_logger

logger = get_logger("translate._http_client")


class OpenAICompatError(RuntimeError):
    """OpenAI 兼容端点调用失败 (网络/HTTP/解析)。"""


def chat_completion(
    endpoint: str,
    *,
    messages: list[dict],
    api_key: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_sec: float = 15.0,
) -> str:
    """POST 一条 chat completion, 返回 ``choices[0].message.content`` 文本。

    Args:
        endpoint: 完整 URL, 如 ``http://127.0.0.1:8080/v1/chat/completions``。
        messages: OpenAI 消息列表 [{"role": ..., "content": ...}, ...]。
        api_key: 存在则加 ``Authorization: Bearer`` (本地 llama-server 可不带)。
        model: 云端点必填; 本地 llama-server 可缺省。

    Raises:
        OpenAICompatError: 网络 / HTTP 非 2xx / 响应结构异常。
    """
    payload: dict = {"messages": messages}
    if model:
        payload["model"] = model
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urlrequest.Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                             headers=headers)
    host = (urlparse(endpoint).hostname or "").lower() or "<unknown>"
    try:
        with urlrequest.urlopen(req, timeout=timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:
            pass
        # 只记状态码与域名; header/body 内容一律不落日志 (api_key 在 header 中)
        logger.error("OpenAI 兼容端点非 2xx 响应: code=%s host=%s", exc.code, host)
        raise OpenAICompatError(f"HTTP {exc.code}: {detail}") from exc
    except urlerror.URLError as exc:
        logger.exception("OpenAI 兼容端点网络错误: host=%s", host)
        raise OpenAICompatError(f"网络错误: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        logger.exception("OpenAI 兼容端点超时/IO 错误: host=%s", host)
        raise OpenAICompatError(f"超时/IO: {exc}") from exc
    except ValueError as exc:  # json.loads/decode 失败: 记日志后原样重抛, 不改变既有异常类型
        logger.exception("OpenAI 兼容端点响应解析失败: host=%s", host)
        raise

    try:
        return body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        logger.exception("OpenAI 兼容端点响应结构异常: host=%s", host)
        raise OpenAICompatError(f"响应格式异常: {str(body)[:200]}") from exc
