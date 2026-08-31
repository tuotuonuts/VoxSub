"""OpenAI-compatible HTTP clients shared by cloud translation and cloud STT.

The translation client uses ``/v1/chat/completions`` while cloud STT uses
``/v1/audio/transcriptions``.  Both stay on the standard library network path.

仅用标准库 urllib。失败一律抛 OpenAICompatError (调用方转成 TranslationError)。
"""
from __future__ import annotations

import json
import uuid
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse, urlunparse

from voxsub.logging_setup import get_logger

logger = get_logger("translate._http_client")


class OpenAICompatError(RuntimeError):
    """OpenAI 兼容端点调用失败 (网络/HTTP/解析)。"""


def normalize_api_base(base_url: str) -> str:
    """Normalize a provider base URL to a single ``.../v1`` prefix.

    The settings UI accepts both ``https://host`` and ``https://host/v1``.
    Keeping normalization here prevents the old ``/v1/v1`` endpoint bug and
    lets chat and audio endpoints share exactly the same convention.
    """
    raw = str(base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"
    return urlunparse(parsed._replace(path=path, query="", fragment="")).rstrip("/")


def chat_completion(
    endpoint: str,
    *,
    messages: list[dict],
    api_key: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    repeat_penalty: float | None = None,
    max_tokens: int | None = None,
    stop: list[str] | None = None,
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
    if top_p is not None:
        payload["top_p"] = top_p
    if top_k is not None:
        payload["top_k"] = top_k
    if repeat_penalty is not None:
        payload["repeat_penalty"] = repeat_penalty
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if stop:
        payload["stop"] = [str(item) for item in stop if str(item)]

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


def audio_transcription(
    endpoint: str,
    *,
    audio_bytes: bytes,
    filename: str = "voxsub.wav",
    api_key: str | None = None,
    model: str,
    language: str | None = None,
    response_format: str = "json",
    timeout_sec: float = 15.0,
) -> str:
    """POST an audio file to an OpenAI-compatible transcription endpoint."""
    boundary = f"----VoxSubBoundary{uuid.uuid4().hex}"
    boundary_bytes = boundary.encode("ascii")
    chunks: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        chunks.extend((
            b"--" + boundary_bytes + b"\r\n",
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
            str(value).encode("utf-8"),
            b"\r\n",
        ))

    add_field("model", model)
    add_field("response_format", response_format)
    if language and language != "auto":
        add_field("language", language)
    chunks.extend((
        b"--" + boundary_bytes + b"\r\n",
        (f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
         "Content-Type: audio/wav\r\n\r\n").encode("utf-8"),
        bytes(audio_bytes),
        b"\r\n--" + boundary_bytes + b"--\r\n",
    ))
    payload = b"".join(chunks)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(payload)),
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urlrequest.Request(endpoint, data=payload, headers=headers)
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
        logger.error("音频转写端点非 2xx 响应: code=%s host=%s", exc.code, host)
        raise OpenAICompatError(f"HTTP {exc.code}: {detail}") from exc
    except urlerror.URLError as exc:
        logger.exception("音频转写端点网络错误: host=%s", host)
        raise OpenAICompatError(f"网络错误: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        logger.exception("音频转写端点超时/IO 错误: host=%s", host)
        raise OpenAICompatError(f"超时/IO: {exc}") from exc
    except ValueError as exc:
        logger.exception("音频转写端点响应解析失败: host=%s", host)
        raise OpenAICompatError("响应不是有效 JSON") from exc

    if isinstance(body, dict):
        text = body.get("text")
        if isinstance(text, str):
            return text.strip()
        nested = body.get("data")
        if isinstance(nested, dict) and isinstance(nested.get("text"), str):
            return nested["text"].strip()
    logger.error("音频转写端点响应结构异常: host=%s", host)
    raise OpenAICompatError(f"响应格式异常: {str(body)[:200]}")
