"""Crash-safe application configuration storage.

Configuration is application state shared by the UI and core services.  It
therefore lives at the package root instead of under :mod:`voxsub.ui`; the old
module remains as a compatibility import for third-party integrations.
"""
from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from voxsub.file_io import write_text_atomically
from voxsub.logging_setup import get_logger

logger = get_logger("config_store")
CONFIG_VERSION = 2
_CONFIG_LOCK = threading.RLock()
_URL_KEYS = frozenset({"stt_base_url", "translate_base_url", "base_url", "sentry_dsn"})


def _normalize_scalar(default: Any, value: Any) -> Any:
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        return value if isinstance(value, int) and not isinstance(value, bool) else default
    if isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            return number if math.isfinite(number) else default
        return default
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    return value  # pragma: no cover - schema currently contains JSON scalars


def _valid_base_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _default_config_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "VoxSub" / "config.json"


_DEFAULTS: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "language": "system",       # system | zh | en
    "theme": "system",          # light | dark | system
    "mode": "a",                # a microphone | b system audio | c file | d OCR
    "lang_pair": "zh-en",
    "translate_tier": "fast",   # fast | quality | cloud
    "stt_provider": "local",    # local | cloud
    "asr_model_id": "asr-zipformer-bilingual-fast",
    "asr_tuning_profile": "auto",
    "asr_vad_threshold": 0.35,
    "asr_silence_ms": 650,
    "asr_max_utterance_ms": 12000,
    "asr_beam_paths": 4,
    "asr_max_new_tokens": 512,
    "asr_hotwords": "",
    "asr_context_hold_ms": 1800,
    "asr_live_draft_enabled": True,
    "asr_context_correction": True,
    "asr_filler_mode": "light",
    "translate_model_id": "mt-opus-fast-builtin",
    "ocr_model_id": "ocr-rapidocr-v6-small-builtin",
    "ocr_cache_root": "",
    # 0 means unlimited; otherwise keep this many newest images in each of
    # the physically separate original/translated cache directories.
    "ocr_cache_limit": 15,
    "download_source": "auto",
    # Model files are user-owned data.  An empty root means an installation
    # predates the storage migration and is resolved conservatively.
    "models_root": "",
    "models_root_mode": "",
    "model_storage_initialized": False,
    "release_notes_seen_version": "",
    "stt_api_key": "",
    "stt_base_url": "https://api.openai.com/v1",
    "stt_model": "whisper-1",
    "translate_api_key": "",
    "translate_base_url": "https://api.deepseek.com/v1",
    "translate_model": "deepseek-chat",
    # 0.3.x legacy aliases; kept for migration and external scripts.
    "api_key": "",
    "base_url": "https://api.deepseek.com/v1",
    "model": "",
    "tts_enabled": True,
    "tts_model_id_zh": "tts-icefall-zh-aishell3",
    "tts_model_id_en": "tts-icefall-en-ljspeech-low",
    "mic_device_id": "",
    "loopback_device_id": "",
    "capture_process_id": 0,
    "capture_window_title": "",
    "last_input_file": "",
    "debug_mode": False,
    # Optional Sentry settings.  The DSN is a public project identifier, but
    # it remains local-only and is never included in telemetry payloads.
    "sentry_dsn": "",
    "sentry_environment": "",
    "sentry_build": "",
    "overlay_font_size": 20,
    "overlay_width": 560,
    "overlay_height": 132,
    "overlay_size_customized": False,
    "overlay_display_mode": "bilingual",
    "overlay_content_padding": 18,
    "overlay_line_gap": 6,
    "overlay_opacity": 0.92,
    "overlay_click_through": False,
    "record_with_translation": False,
}


@dataclass(frozen=True)
class ConfigSchema:
    """Runtime schema for persisted settings without a third-party dependency."""

    defaults: Mapping[str, Any]
    choices: Mapping[str, frozenset[Any]]
    ranges: Mapping[str, tuple[float, float]]

    def normalize(self, key: str, value: Any) -> Any:
        if key not in self.defaults:
            raise KeyError(f"未知配置键: {key}")
        default = self.defaults[key]
        if key == "config_version":
            return CONFIG_VERSION

        normalized = _normalize_scalar(default, value)
        allowed = self.choices.get(key)
        if allowed is not None and normalized not in allowed:
            normalized = default
        if key in _URL_KEYS and not _valid_base_url(normalized):
            normalized = default
        bounds = self.ranges.get(key)
        if bounds is not None and isinstance(normalized, (int, float)):
            low, high = bounds
            normalized = max(low, min(high, normalized))
            if isinstance(default, int):
                normalized = int(normalized)
        return normalized

    def normalize_mapping(self, values: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(self.defaults)
        for key in self.defaults:
            if key in values:
                normalized[key] = self.normalize(key, values[key])
        normalized["config_version"] = CONFIG_VERSION
        return normalized


APP_CONFIG_SCHEMA = ConfigSchema(
    defaults=_DEFAULTS,
    choices={
        "language": frozenset({"system", "zh", "en"}),
        "theme": frozenset({"system", "light", "dark"}),
        "mode": frozenset({"a", "b", "c", "d"}),
        "lang_pair": frozenset({"zh-en", "en-zh", "auto-zh", "auto-en"}),
        "translate_tier": frozenset({"fast", "quality", "cloud"}),
        "stt_provider": frozenset({"local", "cloud"}),
        "asr_tuning_profile": frozenset(
            {"auto", "responsive", "balanced", "accuracy", "context", "custom"}),
        "asr_filler_mode": frozenset({"off", "light"}),
        "download_source": frozenset({"auto", "global", "china"}),
        "sentry_environment": frozenset({"", "development", "testing", "production"}),
        "models_root_mode": frozenset({"", "legacy", "install", "custom"}),
        "overlay_display_mode": frozenset({"bilingual", "source", "translation"}),
    },
    ranges={
        "asr_vad_threshold": (0.01, 0.99),
        "asr_silence_ms": (50, 5000),
        "asr_max_utterance_ms": (1000, 120000),
        "asr_beam_paths": (1, 16),
        "asr_max_new_tokens": (32, 4096),
        "asr_context_hold_ms": (200, 4000),
        "capture_process_id": (0, 2_147_483_647),
        "overlay_font_size": (10, 72),
        "overlay_width": (400, 4000),
        "overlay_height": (88, 2000),
        "overlay_content_padding": (8, 64),
        "overlay_line_gap": (0, 40),
        "overlay_opacity": (0.2, 1.0),
        "ocr_cache_limit": (0, 10_000),
    },
)


def _migrate_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Apply ordered migrations to a copy of persisted configuration data."""
    migrated = dict(raw)
    version = raw.get("config_version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        version = 0
    if version < 1:
        if "translate_api_key" not in migrated and migrated.get("api_key"):
            migrated["translate_api_key"] = migrated["api_key"]
        if "translate_base_url" not in migrated and migrated.get("base_url"):
            migrated["translate_base_url"] = migrated["base_url"]
        if "translate_model" not in migrated and migrated.get("model"):
            migrated["translate_model"] = migrated["model"]
    migrated["config_version"] = CONFIG_VERSION
    return migrated


class ConfigStore:
    """Crash-safe schema-validated application configuration store."""

    DEFAULTS: dict[str, Any] = dict(APP_CONFIG_SCHEMA.defaults)

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _default_config_path()

    def load(self) -> dict[str, Any]:
        """Read known keys and merge defaults; never overwrite a bad file."""
        with _CONFIG_LOCK:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, Any]:
        data: dict[str, Any] = dict(self.DEFAULTS)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    migrated = _migrate_config(raw)
                    data = APP_CONFIG_SCHEMA.normalize_mapping(migrated)
                    invalid = [key for key in self.DEFAULTS
                               if key in migrated and data[key] != migrated[key]]
                    if invalid:
                        logger.warning("配置值无效并已安全回落: keys=%s", ",".join(invalid))
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("配置读取失败(%s), 回落默认值", exc)
        return data

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        normalized = APP_CONFIG_SCHEMA.normalize(key, value)
        with _CONFIG_LOCK:
            data = self._load_unlocked()
            data[key] = normalized
            self._save_unlocked(data)

    def update(self, pairs: dict[str, Any]) -> None:
        normalized = {key: APP_CONFIG_SCHEMA.normalize(key, value)
                      for key, value in pairs.items()}
        with _CONFIG_LOCK:
            data = self._load_unlocked()
            data.update(normalized)
            self._save_unlocked(data)

    def save(self, data: dict[str, Any]) -> None:
        """Persist a complete configuration without exposing a partial JSON file."""
        with _CONFIG_LOCK:
            self._save_unlocked(data)

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        normalized = APP_CONFIG_SCHEMA.normalize_mapping(data)
        write_text_atomically(
            self.path,
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


__all__ = ["APP_CONFIG_SCHEMA", "CONFIG_VERSION", "ConfigSchema", "ConfigStore"]
