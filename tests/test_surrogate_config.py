"""代理字符（surrogate）导致配置写不进去的测试。

## 覆盖的用户报告

日志里的第三条报错：

    set_config 失败: UnicodeEncodeError: 'utf-8' codec can't encode character
    '\\udc96' in position 1517: surrogates not allowed

现象：用户在设置页挑了"应用声音隔离"的目标应用，设置**没保存**，
日志里多一条看不懂的回溯。

## 根因链

1. Windows 窗口标题是 UTF-16，**允许**出现不成对的代理字符；
   `recap.discovery.list_windows()` 用 `GetWindowTextW` + ctypes 取值，
   拿到的 Python str 会原样带着它（Python 允许 str 里有孤立代理）。
2. 该标题经 IPC 到前端，用户选中后由 `set_config` 写回
   `capture_window_title`。
3. `config_store` 把整份配置 `json.dumps(...).encode("utf-8")` 落盘 ——
   孤立代理在 UTF-8 里没有任何合法表示 → 整次写入失败。

## 修法

- 源头：`process_audio.list_capture_targets()` 洗标题。
- 兜底：`config_store._save_unlocked()` 洗整份配置（保证配置文件永远写得进去）。
- 工具：`file_io.sanitize_text` / `sanitize_for_json`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voxsub.file_io import sanitize_for_json, sanitize_text  # noqa: E402

# 孤立代理：低代理（0xDC00–0xDFFF）与高代理（0xD800–0xDBFF）各来一个
LONE_LOW = "\udc96"
LONE_HIGH = "\ud800"
# 合法的一对代理（U+1F600 😀）—— **不能**被当成非法码元洗掉
VALID_PAIR = "\U0001f600"


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("VOXSUB_ROOT", str(ROOT))
    return tmp_path


# ------------------------------------------------------------------ 工具函数


class TestSanitizeText:
    def test_replaces_lone_low_surrogate(self):
        result = sanitize_text(f"标题{LONE_LOW}结尾")
        assert result == "标题\ufffd结尾"
        assert result.encode("utf-8")  # 不再抛

    def test_replaces_lone_high_surrogate(self):
        assert sanitize_text(f"a{LONE_HIGH}b") == "a\ufffdb"

    def test_keeps_valid_astral_characters(self):
        """成对的代理（真·emoji）是合法字符，必须原样保留。

        一刀切删掉 0xD800–0xDFFF 会把 emoji 也毁了。
        """
        text = f"字幕{VALID_PAIR}测试"
        assert sanitize_text(text) == text
        assert VALID_PAIR in sanitize_text(text)

    def test_clean_text_returned_unchanged(self):
        text = "语幕 VoxSub — 正常标题"
        assert sanitize_text(text) == text

    def test_empty_and_none_safe(self):
        assert sanitize_text("") == ""
        assert sanitize_text(None) is None  # type: ignore[arg-type]

    def test_result_is_utf8_encodable_for_every_surrogate(self):
        """穷举所有孤立代理码位，确认洗完都能编码。"""
        for code in range(0xD800, 0xE000):
            text = f"x{chr(code)}y"
            assert sanitize_text(text).encode("utf-8")


class TestSanitizeForJson:
    def test_nested_structures(self):
        result = sanitize_for_json({"a": [f"x{LONE_LOW}", {"b": f"y{LONE_HIGH}"}]})
        assert result == {"a": ["x\ufffd", {"b": "y\ufffd"}]}
        json.dumps(result).encode("utf-8")

    def test_non_string_values_untouched(self):
        payload = {"n": 1, "f": 1.5, "b": True, "none": None, "list": [1, 2]}
        assert sanitize_for_json(payload) == payload

    def test_clean_payload_is_unchanged(self):
        payload = {"title": "正常", "nested": {"k": ["a", "b"]}}
        assert sanitize_for_json(payload) == payload


# ------------------------------------------------------------------ 配置写入


class TestConfigWrite:
    def test_lone_surrogate_no_longer_breaks_write(self, isolated_config):
        """带孤立代理的值必须能落盘（此前整次写入抛 UnicodeEncodeError）。"""
        from voxsub.config_store import ConfigStore

        store = ConfigStore()
        store.update({"capture_window_title": f"msedge.exe — 标题{LONE_LOW}"})

        written = json.loads(store.path.read_text(encoding="utf-8"))
        assert "\ufffd" in written["capture_window_title"]
        assert store.load()["capture_window_title"] == written["capture_window_title"]

    def test_write_does_not_lose_other_keys(self, isolated_config):
        """关键：坏值不能让整次写入失败 —— 同一次调用里的其它设置也要保住。"""
        from voxsub.config_store import ConfigStore

        store = ConfigStore()
        store.update({"capture_window_title": f"坏{LONE_LOW}", "mode": "b"})

        written = json.loads(store.path.read_text(encoding="utf-8"))
        assert written["mode"] == "b", "同一次写入的其它键不该丢"

    def test_config_file_always_utf8_decodable(self, isolated_config):
        from voxsub.config_store import ConfigStore

        store = ConfigStore()
        store.update({"capture_window_title": f"{LONE_HIGH}{LONE_LOW}混合"})
        store.path.read_bytes().decode("utf-8")  # 不抛即通过


# ------------------------------------------------------- 源头：窗口标题清洗


class TestWindowTitleSource:
    """`list_capture_targets` 必须洗标题 —— 这是非法码元进系统的入口。"""

    class _FakeWindow:
        def __init__(self, pid: int, title: str) -> None:
            self.pid = pid
            self.title = title

    def _patch_windows(self, monkeypatch, windows):
        """把 recap.discovery.list_windows 换成返回给定窗口的替身。

        recap 是可选的第三方包，测试环境不一定装了 —— 用 sys.modules 注入
        一个假模块，避免测试依赖它是否安装。
        """
        import types

        from voxsub import process_audio

        module = types.ModuleType("recap.discovery")
        module.list_windows = lambda: windows  # type: ignore[attr-defined]
        package = types.ModuleType("recap")
        package.discovery = module  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "recap", package)
        monkeypatch.setitem(sys.modules, "recap.discovery", module)
        monkeypatch.setattr(process_audio.os, "getpid", lambda: 999999)
        return process_audio

    def test_title_with_lone_surrogate_is_sanitized(self, monkeypatch):
        import os as real_os

        me = real_os.getpid()
        process_audio = self._patch_windows(
            monkeypatch, [self._FakeWindow(me, f"真实标题{LONE_LOW}")]
        )

        targets = process_audio.list_capture_targets()
        assert targets, "应至少返回一个目标"
        for target in targets:
            target.window_title.encode("utf-8")  # 不抛即通过
            assert LONE_LOW not in target.window_title

    def test_clean_title_untouched(self, monkeypatch):
        import os as real_os

        me = real_os.getpid()
        process_audio = self._patch_windows(
            monkeypatch, [self._FakeWindow(me, "干净的标题")]
        )
        targets = process_audio.list_capture_targets()
        assert targets[0].window_title == "干净的标题"

    def test_own_process_excluded(self, monkeypatch):
        """回归：清洗不能影响原有过滤（排除自身 PID）。"""
        import os as real_os

        me = real_os.getpid()
        process_audio = self._patch_windows(
            monkeypatch, [self._FakeWindow(me, "自己"), self._FakeWindow(me, "自己2")]
        )
        monkeypatch.setattr(process_audio.os, "getpid", lambda: me)
        assert process_audio.list_capture_targets() == []
