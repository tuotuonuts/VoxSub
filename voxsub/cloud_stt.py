"""Cloud speech-to-text client for OpenAI-compatible transcription APIs."""
from __future__ import annotations

import io
import os
import wave
from urllib.parse import urlparse

import numpy as np

from voxsub.language_guard import normalize_language
from voxsub.logging_setup import get_logger
from voxsub.translate._http_client import (
    OpenAICompatError,
    audio_transcription,
    normalize_api_base,
)
from voxsub.translate.base import TranslationError
from voxsub.translate.cloud import DEFAULT_ALLOWLIST

logger = get_logger("cloud_stt")


def _cfg_get(config, key: str, env: str | None = None, default=None):
    if config is not None:
        if isinstance(config, dict):
            value = config.get(key)
        else:
            value = getattr(config, key, None)
        if value:
            return value
    if env:
        value = os.environ.get(env)
        if value:
            return value
    return default


def samples_to_wav(samples: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Encode mono float32 PCM into a small 16-bit WAV upload."""
    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm = np.clip(values, -1.0, 1.0)
    raw = (pcm * 32767.0).astype("<i2", copy=False).tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(raw)
    return output.getvalue()


class CloudSTT:
    """Sentence-level cloud STT over ``/v1/audio/transcriptions``.

    VAD and sentence boundaries remain local so the capture thread never waits
    on the network.  The caller submits one finalized audio segment at a time.
    """

    name = "cloud-stt"
    local = False

    def __init__(self, config=None, *, allowlist: set[str] | None = None,
                 timeout_ms: int = 12_000):
        self._api_key = _cfg_get(config, "stt_api_key", "VOXSUB_STT_API_KEY")
        self._base_url = normalize_api_base(_cfg_get(
            config, "stt_base_url", "VOXSUB_STT_BASE_URL",
            "https://api.openai.com/v1",
        ))
        self._model = str(_cfg_get(
            config, "stt_model", "VOXSUB_STT_MODEL", "whisper-1",
        ))
        self._allowlist = allowlist if allowlist is not None else DEFAULT_ALLOWLIST
        self._timeout = max(1.0, int(timeout_ms) / 1000.0)

    def _validate_endpoint(self) -> str:
        parsed = urlparse(self._base_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme in {"http", "https"} and host in self._allowlist:
            return f"{self._base_url}/audio/transcriptions"
        logger.warning("云 STT 端点拒绝: host=%s scheme=%s", host, parsed.scheme)
        raise TranslationError(
            f"云 STT 端点 {self._base_url!r} 不在受信任白名单内，已拒绝调用。"
        )

    def ready(self) -> bool:
        if not self._api_key or not self._model:
            return False
        try:
            self._validate_endpoint()
            return True
        except TranslationError:
            return False

    def transcribe(self, audio_bytes: bytes, *, source_lang: str = "auto",
                   filename: str = "voxsub.wav", timeout_ms: int = 12_000) -> str:
        if not self._api_key:
            raise TranslationError("云 STT 未配置 API Key")
        endpoint = self._validate_endpoint()
        effective_timeout = self._timeout if timeout_ms == 12_000 else max(
            1.0, int(timeout_ms) / 1000.0)
        source_lang = normalize_language(source_lang)
        try:
            return audio_transcription(
                endpoint,
                audio_bytes=audio_bytes,
                filename=filename,
                api_key=self._api_key,
                model=self._model,
                language=source_lang,
                timeout_sec=effective_timeout,
            )
        except OpenAICompatError as exc:
            logger.exception("云 STT 调用失败 (host=%s model=%s)",
                             (urlparse(self._base_url).hostname or "").lower(),
                             self._model)
            raise TranslationError(str(exc)) from exc

    def transcribe_samples(self, samples: np.ndarray, *, source_lang: str = "auto",
                           timeout_ms: int = 12_000) -> str:
        return self.transcribe(
            samples_to_wav(samples), source_lang=source_lang,
            timeout_ms=timeout_ms,
        )

    def close(self) -> None:
        pass


__all__ = ["CloudSTT", "samples_to_wav"]
