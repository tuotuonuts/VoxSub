"""本地配置读写（UI 壳层用）。

存储位置遵循 DESIGN.md『数据与存储』：默认
%LOCALAPPDATA%\\VoxSub\\config.json（可通过构造参数覆盖，便于测试用 tmp 路径）。
格式：UTF-8 JSON，indent=2。损坏 / 缺失时回落默认值，不抛异常。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from voxsub.logging_setup import get_logger

logger = get_logger("ui.config_store")


def _default_config_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "VoxSub" / "config.json"


class ConfigStore:
    """极简键值配置存储：读时合并默认值，写时立即落盘。"""

    # 默认配置（与 DESIGN.md 组件清单对应）
    DEFAULTS: dict[str, Any] = {
        "theme": "system",           # light | dark | system
        "mode": "a",                 # a 麦克风同传 | b 系统声音字幕 | c 文件字幕
        "lang_pair": "zh-en",        # 语言对
        "translate_tier": "fast",    # fast 快档 | quality 质量档 | cloud 云 API
        "asr_model_id": "asr-zipformer-bilingual-fast",
        "asr_tuning_profile": "auto", # auto | responsive | balanced | accuracy | custom
        "asr_vad_threshold": 0.35,     # 自定义：语音触发灵敏度
        "asr_silence_ms": 650,         # 自定义：尾部停顿多久切句
        "asr_max_utterance_ms": 12000, # 自定义：单段最长时长
        "asr_beam_paths": 4,           # 自定义：Zipformer 候选路径
        "asr_max_new_tokens": 512,     # 生成式 ASR 单段最大输出
        "asr_hotwords": "",           # 逗号分隔领域词
        "translate_model_id": "mt-opus-fast-builtin",
        "download_source": "auto",  # auto | global | china
        "api_key": "",               # 云 API Key（仅 cloud 档使用）
        "base_url": "https://api.deepseek.com/v1",  # OpenAI 兼容端点（白名单由 M6 落）
        "tts_enabled": True,         # 语音朗读开关
        "mic_device_id": "",        # A 模式麦克风；空=系统默认
        "loopback_device_id": "",   # B 模式输出端点；空=系统默认
        "capture_process_id": 0,     # B 模式目标进程；0=全部系统声音
        "capture_window_title": "", # 仅作 UI 恢复/进程失效提示
        "last_input_file": "",      # C 模式最近选择的音视频
        "debug_mode": False,         # 内置实时调试日志
        "overlay_font_size": 20,     # 字幕浮窗字号
        "overlay_opacity": 0.92,     # 字幕浮窗透明度
        "overlay_click_through": False, # 锁定浮窗并把鼠标点击穿透到底层窗口
        "record_with_translation": False, # A 模式同时保存麦克风 WAV
    }

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _default_config_path()

    # -- 读取 --------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """读配置并合并默认值；文件缺失 / 损坏一律回落默认副本（不落盘、不抛）。"""
        data: dict[str, Any] = dict(self.DEFAULTS)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update({k: v for k, v in raw.items() if k in self.DEFAULTS})
            except (json.JSONDecodeError, OSError) as exc:
                # 损坏配置：保留默认值即可（不覆盖原文件，等用户下次保存）；
                # 设计内行为 → debug 记录, 不打断读取
                logger.debug("配置读取失败(%s), 回落默认值", exc)
        return data

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    # -- 写入 --------------------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        """更新单键并立即落盘（保留未涉及其余键）。"""
        data = self.load()
        data[key] = value
        self.save(data)

    def update(self, pairs: dict[str, Any]) -> None:
        """批量更新并落盘。"""
        data = self.load()
        data.update(pairs)
        self.save(data)

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
