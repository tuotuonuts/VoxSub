"""Crash-safe application configuration storage.

Configuration is application state shared by the UI and core services.  It
therefore lives at the package root instead of under :mod:`voxsub.ui`; the old
module remains as a compatibility import for third-party integrations.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from voxsub.language_registry import LANGUAGE_PAIRS
from voxsub.file_io import sanitize_for_json, write_text_atomically
from voxsub.logging_setup import get_logger

logger = get_logger("config_store")
CONFIG_VERSION = 2
_CONFIG_LOCK = threading.RLock()
_URL_KEYS = frozenset({"stt_base_url", "translate_base_url", "base_url", "sentry_dsn"})


class ConfigVersionTooNew(RuntimeError):
    """磁盘上的配置版本比当前程序支持的更新。

    这种情况下**必须拒绝**读取改写：旧程序按自己的 schema 归一化再写回，
    会把新版本写入的字段悄悄抹掉，用户看到的是"设置莫名其妙丢了"。
    宁可让功能暂时不可用并给出明确提示，也不做破坏性降级。
    """


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
    "file_translation_mode": "dual",
    "speech_model_id": "speech-granite-4-1b",
    # 默认档 = 智能上下文（context）。它开启上下文纠错，是当前主推的识别方式。
    # 下面的四个数值必须与 pipeline._effective_asr_tuning 里 context 预设一致，
    # 否则新装用户看到的"默认值"和实际生效值会对不上。
    "asr_tuning_profile": "context",
    "asr_vad_threshold": 0.32,
    "asr_silence_ms": 500,
    "asr_max_utterance_ms": 18000,
    "asr_beam_paths": 6,
    "asr_max_new_tokens": 512,
    "asr_hotwords": "",
    "asr_context_hold_ms": 1800,
    "asr_live_draft_enabled": True,
    "asr_auxiliary_preview_enabled": False,
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
    "log_limit_mb": 50,  # total voxsub.log rotations; stored in binary MB
    "log_limit_unit": "MB",
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
    "overlay_glass_enabled": False,
    "overlay_glass_strength": 50,
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
        "log_limit_unit": frozenset({"MB", "GB"}),
        "mode": frozenset({"a", "b", "c", "d"}),
        "lang_pair": frozenset(LANGUAGE_PAIRS),
        "translate_tier": frozenset({"fast", "quality", "cloud"}),
        "stt_provider": frozenset({"local", "cloud"}),
        "asr_tuning_profile": frozenset(
            {"auto", "responsive", "balanced", "accuracy", "context", "custom"}),
        "asr_filler_mode": frozenset({"off", "light"}),
        "file_translation_mode": frozenset({"dual", "single"}),
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
        "overlay_glass_strength": (0, 100),
        "ocr_cache_limit": (0, 10_000),
        "log_limit_mb": (10, 10240),
    },
)


def _config_version_of(raw: Mapping[str, Any]) -> int:
    """从原始配置里读出 ``config_version``。缺字段/类型不对一律当 0（最老的版本）。"""
    version = raw.get("config_version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        return 0
    return version


def _migrate_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Apply ordered migrations to a copy of persisted configuration data.

    **只迁移已知的旧版本。**遇到比当前程序更新的版本不做任何改写，
    而是抛 :class:`ConfigVersionTooNew` —— 静默降级比报错危险得多。
    """
    migrated = dict(raw)
    version = _config_version_of(raw)
    if version > CONFIG_VERSION:
        raise ConfigVersionTooNew(
            f"配置文件版本为 {version}，当前程序只支持到 {CONFIG_VERSION}。"
            "已拒绝读取与保存，避免把设置降级写坏。"
            "请升级到较新版本的程序，或先备份config.json后手动处理。"
        )
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
        # 只读锁：磁盘上的配置比程序新时置上，之后所有写入都拒绝。
        self._locked_reason = ""
        # 磁盘上存在、但当前 schema 不认识的字段。保存时原样带回，
        # 不执行、也不抹掉（工作单 §3.5）。
        self._unknown_fields: dict[str, Any] = {}
        self._corrupt_backed_up = False

    def load(self) -> dict[str, Any]:
        """Read known keys and merge defaults; never overwrite a bad file."""
        with _CONFIG_LOCK:
            return self._load_unlocked()

    def load_raw(self) -> dict[str, Any]:
        """读取**未经归一化**的原始配置（诊断用）。

        与 :meth:`load` 的区别：这里是磁盘上的原样内容，包含当前版本不认识的
        字段。给"配置到底怎么了"这类排查用，不参与业务逻辑。
        """
        with _CONFIG_LOCK:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return raw if isinstance(raw, dict) else {}

    @property
    def locked_reason(self) -> str:
        """非空表示配置处于只读保护状态（版本比程序新）。"""
        return self._locked_reason

    def _load_unlocked(self) -> dict[str, Any]:
        data: dict[str, Any] = dict(self.DEFAULTS)
        self._locked_reason = ""
        self._unknown_fields = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    known = set(self.DEFAULTS)
                    self._unknown_fields = {
                        key: value for key, value in raw.items()
                        if key not in known and key != "config_version"
                    }
                    migrated = _migrate_config(raw)
                    data = APP_CONFIG_SCHEMA.normalize_mapping(migrated)
                    invalid = [key for key in self.DEFAULTS
                               if key in migrated and data[key] != migrated[key]]
                    if invalid:
                        logger.warning("配置值无效并已安全回落: keys=%s", ",".join(invalid))
            except ConfigVersionTooNew as exc:
                # 不降级、不覆盖：保持只读，把原因原样告诉上层。
                self._locked_reason = str(exc)
                logger.error("配置版本过新，已转入只读保护: %s", exc)
                return dict(self.DEFAULTS)
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("配置读取失败(%s), 回落默认值", exc)
                self._backup_corrupt_config()
        return data

    def _backup_corrupt_config(self) -> None:
        """读不动的配置先留一份再回落。

        不备份的后果：下次保存会把它整个覆盖掉，用户的设置就永久没了，
        而且没人知道曾经有过内容。
        """
        if self._corrupt_backed_up:
            return
        self._corrupt_backed_up = True
        try:
            if not self.path.exists():
                return
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            shutil.copy2(self.path, backup)
            logger.warning("配置文件无法解析，已备份到 %s 并回落默认值", backup)
        except OSError as error:  # pragma: no cover - 依赖具体权限环境
            logger.debug("配置文件备份失败: %s", error)

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
        """Persist a complete configuration without exposing a partial JSON file.

        **先读一次再写**：`save` 是公开写入口，如果直接进 `_save_unlocked`，
        那么"磁盘上是未来版本"这件事就不知道（`_locked_reason` 还是空的），
        于是它会把新版本的配置按旧 schema 归一化后写回 —— 正是缺陷 #12 要禁止的
        静默降级。`set`/`update` 一直是先 load 再写，只有 save 漏了这一步。
        """
        with _CONFIG_LOCK:
            self._load_unlocked()  # 探测版本与未知字段，必要时进入只读保护
            self._save_unlocked(data)

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        # 只读保护：磁盘上的配置比程序新时，任何写入都拒绝。
        # 先前这里会照写不误 —— 结果是把新版本的字段悄悄抹掉（降级写坏）。
        if self._locked_reason:
            raise ConfigVersionTooNew(self._locked_reason)

        # 洗掉不成对的代理字符：这类码元无法编码成 UTF-8，会让整次写入抛
        # UnicodeEncodeError（用户看到 "set_config 失败: ... surrogates not allowed"，
        # 设置没保存）。来源通常是 Windows 窗口标题 —— 见 file_io.sanitize_text。
        normalized = APP_CONFIG_SCHEMA.normalize_mapping(data)
        clean = sanitize_for_json(normalized)
        if clean != normalized:
            # 真出现才记一条：说明还有别的入口在往里塞非法码元，值得知道。
            logger.warning("配置含不成对的代理字符，已替换为 U+FFFD（该码元无法写入 UTF-8）")

        # 未知字段原样带回：它们可能是更新的版本写的，当前版本不认识，
        # 但也**不该**因为一次保存就被抹掉（工作单 §3.5）。
        payload = sanitize_for_json({**self._unknown_fields, **clean})
        write_text_atomically(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


__all__ = ["APP_CONFIG_SCHEMA", "CONFIG_VERSION", "ConfigSchema", "ConfigStore",
           "ConfigVersionTooNew"]
