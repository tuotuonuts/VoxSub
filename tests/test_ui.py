"""voxsub.ui 模块测试（M7 壳层）。

分层：
- 纯逻辑单测：设计令牌表逐项存在性 / QSS 渲染非空 / 模式轮换函数 /
  ConfigStore 读写（tmp 路径，不碰真实 %LOCALAPPDATA%）/ Pipeline stub 契约
- 主题切换单测：resolve_theme_name（fake darkdetect 注入）/ load_theme 应用
  （DARK / LIGHT / SYSTEM 三档，QT_QPA_PLATFORM=offscreen 无头运行）
- 组件冒烟（offscreen）：MainWindow / SubtitleOverlay / SettingsWindow /
  DiagnosticsWindow 可构造、关键交互路径不崩

运行: cd VoxSub && unset PYTHONPATH PYTHONHOME && .venv/Scripts/python.exe -m pytest tests/test_ui.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 无头运行：必须在任何 Qt 构造前设置（QApplication 读取该变量）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from unittest import mock  # noqa: E402

from voxsub.ui import config_store, pipeline_client  # noqa: E402
from voxsub.ui.config_store import ConfigStore  # noqa: E402
from voxsub.ui.main_window import MainWindow, ModeCard, SubtitleList, cycle_mode  # noqa: E402
from voxsub.ui.pipeline_client import _PipelineStub, get_pipeline  # noqa: E402
from voxsub.ui.theme import (  # noqa: E402
    AppTheme,
    DESIGN_TOKENS,
    build_qss,
    load_theme,
    resolve_theme_name,
)


# ---------------------------------------------------------------------------
# 共享 QApplication（模块级单例，offscreen）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    load_theme(app, AppTheme.DARK)
    yield app


# ===========================================================================
# 1. 设计令牌表（DESIGN.md 逐项校验）
# ===========================================================================
class TestDesignTokens:
    def test_both_themes_present(self):
        assert set(DESIGN_TOKENS) == {"dark", "light"}

    def test_dark_token_values_match_design(self):
        d = DESIGN_TOKENS["dark"]
        # 基底 / surface 分层
        assert d["bg_base"] == "#050505"
        assert d["surface_1"] == "#131313"
        assert d["surface_2"] == "#1A1A1A"
        # 文本主 / 次
        assert d["text_primary"] == "#F2F2F2"
        assert d["text_secondary"] == "#9CA3AF"
        # border 白 8%
        assert d["border"] == "rgba(255,255,255,0.08)"
        # accent 唯一 teal
        assert d["accent"] == "#14B8A6"
        assert d["accent_deep"] == "#0D9488"
        # 语义低饱和三色
        assert d["success"] == "#34D399"
        assert d["warning"] == "#FBBF24"
        assert d["error"] == "#F87171"

    def test_light_token_values_match_design(self):
        l = DESIGN_TOKENS["light"]
        assert l["bg_base"] == "#F7F7F5"
        assert l["surface_1"] == "#FFFFFF"
        assert l["surface_2"] == "#F2F2F2"
        assert l["text_primary"] == "#1A1A1A"
        assert l["text_secondary"] == "#6B7280"
        assert l["border"] == "rgba(0,0,0,0.08)"
        # accent 两档同值
        assert l["accent"] == DESIGN_TOKENS["dark"]["accent"] == "#14B8A6"

    def test_radius_spacing_and_font_tokens(self):
        for name in ("dark", "light"):
            t = DESIGN_TOKENS[name]
            assert t["radius_capsule"] == "999px"
            assert t["radius_card"] == "16px"
            assert t["radius_input"] == "10px"
            assert t["radius_dialog"] == "20px"
            assert t["radius_shell"] == "32px"
            assert t["radius_inner"] == "24px"
            assert t["spacing"] == "4px"
            # 字体栈：禁 Inter/Roboto/Arial
            forbidden = ("Inter", "Roboto", "Arial")
            assert not any(f in t["font_family"] for f in forbidden)
            assert "Cascadia Code" in t["font_mono"]


# ===========================================================================
# 2. QSS 渲染
# ===========================================================================
class TestQss:
    def test_qss_non_empty_and_tokenized(self):
        for name in ("dark", "light"):
            qss = build_qss(name)
            assert len(qss) > 1000, f"{name} QSS 过短"
            assert f"/* ============ 语幕 VoxSub" in qss
            # 令牌已替换：模板中不应残留 @ 占位符
            assert "@" not in qss.replace("@theme_name", "")

    def test_qss_contains_accent_and_radius(self):
        for name in ("dark", "light"):
            qss = build_qss(name)
            assert "#14B8A6" in qss
            assert "border-radius" in qss
            assert "border: 1px solid rgba(20,184,166," in qss or "rgba(20,184,166" in qss

    def test_qss_theme_specific_colors(self):
        assert "#050505" in build_qss("dark")
        assert "#F7F7F5" in build_qss("light")


# ===========================================================================
# 3. 主题切换函数
# ===========================================================================
class TestThemeSwitching:
    def test_resolve_fixed_themes(self):
        assert resolve_theme_name(AppTheme.DARK) == "dark"
        assert resolve_theme_name(AppTheme.LIGHT) == "light"

    def test_resolve_system_uses_detector(self):
        class _FakeDark:
            @staticmethod
            def theme():
                return "Dark"

        class _FakeLight:
            @staticmethod
            def theme():
                return "Light"

        assert resolve_theme_name(AppTheme.SYSTEM, _FakeDark.theme) == "dark"
        assert resolve_theme_name(AppTheme.SYSTEM, _FakeLight.theme) == "light"
        # 取不到值时保守回落浅色
        assert resolve_theme_name(AppTheme.SYSTEM, lambda: None) == "light"

    def test_app_theme_enum_values(self):
        assert AppTheme.DARK.value == "dark"
        assert AppTheme.LIGHT.value == "light"
        assert AppTheme.SYSTEM.value == "system"

    def test_load_theme_applies_qss(self, qapp, monkeypatch):
        # SYSTEM 档：fake darkdetect → dark
        import sys as _sys

        class _FakeDarkDetect:
            @staticmethod
            def theme():
                return "Dark"

        monkeypatch.setitem(_sys.modules, "darkdetect", _FakeDarkDetect)
        load_theme(qapp, AppTheme.SYSTEM)
        ss = qapp.styleSheet()
        assert ss and "#050505" in ss
        # 深/浅档直接生效
        load_theme(qapp, AppTheme.DARK)
        assert qapp.styleSheet() and "#050505" in qapp.styleSheet()
        load_theme(qapp, AppTheme.LIGHT)
        assert qapp.styleSheet() and "#F7F7F5" in qapp.styleSheet()


# ===========================================================================
# 4. 模式轮换
# ===========================================================================
class TestCycleMode:
    def test_cycles(self):
        assert cycle_mode("a") == "b"
        assert cycle_mode("b") == "c"
        assert cycle_mode("c") == "a"

    def test_invalid_falls_back(self):
        assert cycle_mode("zz") == "a"


# ===========================================================================
# 5. ConfigStore 读写（tmp 路径）
# ===========================================================================
class TestConfigStore:
    def test_defaults_when_missing(self, tmp_path):
        store = ConfigStore(tmp_path / "cfg" / "config.json")
        data = store.load()
        assert data["mode"] == "a"
        assert data["theme"] == "system"
        assert data["translate_tier"] == "fast"
        assert data["tts_enabled"] is True
        assert not store.path.exists()  # 只读不落盘

    def test_set_get_roundtrip_and_persist(self, tmp_path):
        path = tmp_path / "config.json"
        store = ConfigStore(path)
        store.set("theme", "dark")
        store.set("translate_tier", "cloud")
        store.update({"api_key": "sk-test-123", "base_url": "https://example.com/v1"})
        # 新实例读取（模拟重启）
        reloaded = ConfigStore(path)
        data = reloaded.load()
        assert data["theme"] == "dark"
        assert data["translate_tier"] == "cloud"
        assert data["api_key"] == "sk-test-123"
        assert data["base_url"] == "https://example.com/v1"
        # 未动的键保留默认
        assert data["lang_pair"] == "zh-en"

    def test_corrupt_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{ 这不是 json !!!", encoding="utf-8")
        store = ConfigStore(path)
        data = store.load()
        assert data["mode"] == "a"
        assert data["theme"] == "system"

    def test_save_creates_parent_dirs(self, tmp_path):
        store = ConfigStore(tmp_path / "deep" / "nested" / "config.json")
        store.set("mode", "b")
        assert config_store.ConfigStore(tmp_path / "deep" / "nested" / "config.json").get("mode") == "b"


# ===========================================================================
# 6. Pipeline stub 契约（DESIGN.md M6）
# ===========================================================================
class TestPipelineStub:
    def test_stub_is_used_when_pipeline_missing(self, monkeypatch):
        monkeypatch.setattr(pipeline_client, "_RealPipeline", None)
        p = get_pipeline()
        assert isinstance(p, _PipelineStub)

    def test_real_pipeline_auto_selected_when_available(self):
        """M6 voxsub.pipeline 已实现：get_pipeline() 应返回真实 Pipeline 且契约齐备。"""
        import voxsub.pipeline

        assert pipeline_client._HAS_PIPELINE  # noqa: SLF001
        p = get_pipeline()
        assert isinstance(p, voxsub.pipeline.Pipeline)
        for attr in ("mode", "start", "stop", "set_mode", "on_utterance", "on_status", "is_running"):
            assert callable(getattr(p, attr)) or isinstance(getattr(p, attr), (str, bool)), attr
        assert p.mode in ("a", "b", "c")

    def test_stub_contract(self):
        p = _PipelineStub()
        assert p.mode == "a"
        assert p.is_running() is False

        statuses: list[str] = []
        utters: list[tuple[str, str]] = []
        p.on_status(statuses.append)
        p.on_utterance(lambda s, d: utters.append((s, d)))

        p.start()
        assert p.is_running() is True
        assert statuses[-1] == "拾音中"
        p.stop()
        assert p.is_running() is False
        assert statuses[-1] == "待机"

        p.set_mode("c")
        assert p.mode == "c"
        p._emit_utterance("你好", "Hello")  # noqa: SLF001
        assert utters == [("你好", "Hello")]

    def test_stub_mode_validation(self):
        p = _PipelineStub()
        p.set_mode("x")
        assert p.mode == "a"


# ===========================================================================
# 7. 主窗冒烟（offscreen；显式注入 _PipelineStub 保持单测封闭，不碰真机采集）
# ===========================================================================
class TestMainWindow:
    @staticmethod
    def _make_win(tmp_path):
        return MainWindow(store=ConfigStore(tmp_path / "config.json"), pipeline=_PipelineStub())

    def test_construct_and_widgets(self, qapp, tmp_path):
        win = self._make_win(tmp_path)
        try:
            assert win.windowTitle() == "语幕 VoxSub"
            assert set(win.mode_cards) == {"a", "b", "c"}
            assert all(isinstance(c, ModeCard) for c in win.mode_cards.values())
            assert win.lang_combo.count() == 2
            assert win.lang_combo.itemText(0) == "中 → 英"
            assert isinstance(win.subtitle_list, SubtitleList)
            assert win.subtitle_list.count() == 0
            # CTA 初始态
            assert win.cta.is_running() is False
        finally:
            win.close()
            win.deleteLater()

    def test_mode_card_selection_highlights(self, qapp, tmp_path):
        win = self._make_win(tmp_path)
        try:
            win.set_mode("b")
            assert win.mode_cards["a"].is_active() is False
            assert win.mode_cards["b"].is_active() is True
            assert win.mode_cards["c"].is_active() is False
            assert win.current_mode() == "b"
            # 同步到 pipeline + 配置
            assert win.pipeline.mode == "b"
            assert win._store.get("mode") == "b"  # noqa: SLF001
            # 轮换
            assert win.cycle_mode() == "c"
            assert win.mode_cards["c"].is_active() is True
        finally:
            win.close()
            win.deleteLater()

    def test_start_stop_and_subtitle_stream(self, qapp, tmp_path):
        win = self._make_win(tmp_path)
        try:
            win._toggle_run()  # noqa: SLF001
            assert win.pipeline.is_running() is True
            assert win.cta.is_running() is True
            assert win.status_light.text.text() == "拾音中"
            # 字幕流入
            win._on_utterance("你好", "Hello")  # noqa: SLF001
            win._on_utterance("世界", "World")
            assert win.subtitle_list.count() == 2
            # 停止
            win._toggle_run()  # noqa: SLF001
            assert win.pipeline.is_running() is False
            assert win.cta.is_running() is False
            assert win.status_light.text.text() == "待机"
        finally:
            win.close()
            win.deleteLater()

    def test_lang_pair(self, qapp, tmp_path):
        win = self._make_win(tmp_path)
        try:
            win.set_lang_pair("en-zh")
            assert win.current_lang_pair() == "en-zh"
            assert win._store.get("lang_pair") == "en-zh"  # noqa: SLF001
            # 模拟用户下拉切换
            idx = win.lang_combo.findText("中 → 英")
            win.lang_combo.setCurrentIndex(idx)
            assert win.current_lang_pair() == "zh-en"
            win.set_lang_pair("bogus")  # 非法值回落 zh-en
            assert win.current_lang_pair() == "zh-en"
        finally:
            win.close()
            win.deleteLater()


# ===========================================================================
# 8. 字幕浮窗冒烟（offscreen）
# ===========================================================================
class TestSubtitleOverlay:
    def test_construct_and_subtitles(self, qapp, tmp_path):
        from voxsub.ui.subtitle_overlay import SubtitleOverlay

        ov = SubtitleOverlay(store=ConfigStore(tmp_path / "config.json"))
        try:
            assert ov.windowFlags() & ov.windowFlags().WindowStaysOnTopHint
            ov.set_subtitles("こんにちは", "你好")
            assert ov.src_label.text() == "こんにちは"
            assert ov.dst_label.text() == "你好"
            # 历史
            ov.set_subtitles("再见", "Goodbye")
            assert len(ov._history) == 2  # noqa: SLF001
            # 字号 / 透明度调节
            ov.change_font_size(+2)
            assert ov._font_size == 22  # noqa: SLF001
            ov.change_font_size(-99)  # 下限 14
            assert ov._font_size == 14  # noqa: SLF001
            ov.set_overlay_opacity(0.5)
            # setWindowOpacity 内部量化到 8bit（127/255≈0.498），容差放宽
            assert ov.windowOpacity() == pytest.approx(0.5, abs=0.01)
        finally:
            ov.close()
            ov.deleteLater()


# ===========================================================================
# 9. 设置页冒烟（offscreen）
# ===========================================================================
class TestSettingsWindow:
    def test_construct_and_tier_persist(self, qapp, tmp_path):
        from voxsub.ui.settings_window import SettingsWindow

        path = tmp_path / "config.json"
        store = ConfigStore(path)
        sw = SettingsWindow(store=store)
        try:
            assert sw.tabs.count() == 4
            # 切换云档 → 输入框可用 + 落盘
            sw.tier_cloud.setChecked(True)
            assert store.get("translate_tier") == "cloud"
            assert sw.api_key_edit.isEnabled()
            assert sw.base_url_edit.isEnabled()
            sw.api_key_edit.setText("sk-abc")
            sw.save_cloud_credentials()
            assert store.get("api_key") == "sk-abc"
            # 切回快档 → 输入框禁用
            sw.tier_fast.setChecked(True)
            assert store.get("translate_tier") == "fast"
            assert sw.api_key_edit.isEnabled() is False
            # 重新实例化能恢复选中态
            sw2 = SettingsWindow(store=ConfigStore(path))
            assert sw2.current_tier() == "fast"
            sw2.close()
        finally:
            sw.close()
            sw.deleteLater()

    def test_theme_change_applies(self, qapp, tmp_path):
        from voxsub.ui.settings_window import SettingsWindow

        sw = SettingsWindow(store=ConfigStore(tmp_path / "config.json"))
        try:
            sw.theme_dark.setChecked(True)
            assert sw._store.get("theme") == "dark"  # noqa: SLF001
            assert "#050505" in qapp.styleSheet()
            sw.theme_light.setChecked(True)
            assert "#F7F7F5" in qapp.styleSheet()
            # 容器恢复深色，避免影响后续用例
            from voxsub.ui.theme import load_theme as _lt

            _lt(qapp, AppTheme.DARK)
        finally:
            sw.close()
            sw.deleteLater()

    def test_tts_switch(self, qapp, tmp_path):
        from voxsub.ui.settings_window import SettingsWindow

        sw = SettingsWindow(store=ConfigStore(tmp_path / "config.json"))
        try:
            sw.tts_switch.setChecked(False)
            assert sw._store.get("tts_enabled") is False  # noqa: SLF001
        finally:
            sw.close()
            sw.deleteLater()


# ===========================================================================
# 10. 诊断页骨架（真实 voxsub.diagnostics 已由 M8 实现 → 渲染结果卡；
#     模块缺失分支 → 占位文案）
# ===========================================================================
class TestDiagnosticsWindow:
    def test_placeholder_when_module_missing(self, qapp):
        from PySide6.QtWidgets import QLabel

        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        dw = DiagnosticsWindow(diagnostics_module=None)  # 模拟 M8 模块缺失
        try:
            texts = [lbl.text() for lbl in dw.findChildren(QLabel)]
            assert any("尚未实现" in t for t in texts)
            assert any("run_self_check" in t for t in texts)
        finally:
            dw.close()
            dw.deleteLater()

    def test_renders_result_cards_from_real_contract(self, qapp):
        from PySide6.QtWidgets import QLabel

        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        class _FakeDiag:
            @staticmethod
            def run_self_check():
                return [
                    {"check": "模型完整性", "status": "ok", "detail": "406 条就绪"},
                    {"check": "ORT providers", "status": "warn", "detail": "仅有 CPU"},
                    {"check": "ASR 冒烟", "status": "fail", "detail": "模型缺失"},
                ]

        dw = DiagnosticsWindow(diagnostics_module=_FakeDiag)
        try:
            texts = [lbl.text() for lbl in dw.findChildren(QLabel)]
            assert "✅" in texts and "⚠️" in texts and "❌" in texts
            assert any("模型完整性" in t for t in texts)
            assert len(dw._results) == 3  # noqa: SLF001
        finally:
            dw.close()
            dw.deleteLater()

    def test_real_diagnostics_module_detected(self, qapp):
        """M8 已实现：自动探测应命中真实模块（不做重自检，仅验证探测与渲染骨架）。"""
        import voxsub.diagnostics  # noqa: F401

        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        with mock.patch.object(DiagnosticsWindow, "_render", lambda self: None):
            dw = DiagnosticsWindow()
        try:
            assert dw._module is voxsub.diagnostics  # noqa: SLF001
        finally:
            dw.close()
            dw.deleteLater()