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
import time
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
from voxsub.ui.selection_controls import (  # noqa: E402
    PillChoiceButton,
    RoundRadioButton,
    ToggleSwitch,
)
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
    from voxsub.ui.i18n import language_manager

    language_manager.set_language("zh")
    yield app
    language_manager.set_language("zh")


def _wait_until(qapp, predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    qapp.processEvents()
    assert predicate(), "timed out waiting for asynchronous UI operation"


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

    def test_checked_radio_indicator_keeps_its_round_geometry(self):
        qss = build_qss("dark")
        checked = qss.split("QRadioButton::indicator:checked {", 1)[1].split("}", 1)[0]
        assert "width: 18px;" in checked
        assert "height: 18px;" in checked
        assert "border-radius: 9px;" in checked

    def test_checked_filter_pill_keeps_capsule_geometry(self):
        qss = build_qss("dark")
        checked = qss.split("QPushButton#filterPill:checked {", 1)[1].split("}", 1)[0]
        assert "min-height: 34px;" in checked
        assert "border-radius: 17px;" in checked
        checked_hover = qss.split(
            "QPushButton#filterPill:checked:hover,", 1
        )[1].split("}", 1)[0]
        assert "border-radius: 17px;" in checked_hover

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
        assert cycle_mode("c") == "d"
        assert cycle_mode("d") == "a"

    def test_invalid_falls_back(self):
        assert cycle_mode("zz") == "a"


# ===========================================================================
# 5. ConfigStore 读写（tmp 路径）
# ===========================================================================
class TestConfigStore:
    def test_ui_import_is_compatibility_alias(self):
        from voxsub.config_store import ConfigStore as CoreConfigStore

        assert ConfigStore is CoreConfigStore

    def test_defaults_when_missing(self, tmp_path):
        store = ConfigStore(tmp_path / "cfg" / "config.json")
        data = store.load()
        assert data["language"] == "system"
        assert data["mode"] == "a"
        assert data["theme"] == "system"
        assert data["translate_tier"] == "fast"
        assert data["tts_enabled"] is True
        assert data["tts_model_id_zh"] == "tts-icefall-zh-aishell3"
        assert data["tts_model_id_en"] == "tts-icefall-en-ljspeech-low"
        assert data["mic_device_id"] == ""
        assert data["loopback_device_id"] == ""
        assert data["capture_process_id"] == 0
        assert data["debug_mode"] is False
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

    def test_ocr_peer_mode_is_a_valid_persisted_choice(self, tmp_path):
        store = ConfigStore(tmp_path / "config.json")

        store.set("mode", "d")

        assert ConfigStore(store.path).get("mode") == "d"

    def test_legacy_cloud_keys_migrate_to_translation_side(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            '{"api_key":"legacy-key","base_url":"https://legacy.example/v1"}',
            encoding="utf-8",
        )
        data = ConfigStore(path).load()
        assert data["translate_api_key"] == "legacy-key"
        assert data["translate_base_url"] == "https://legacy.example/v1"
        assert data["stt_api_key"] == ""

    def test_corrupt_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{ 这不是 json !!!", encoding="utf-8")
        store = ConfigStore(path)
        data = store.load()
        assert data["mode"] == "a"
        assert data["theme"] == "system"

    def test_invalid_types_and_ranges_are_safely_normalized(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            '{"theme":"neon","overlay_font_size":"huge",'
            '"overlay_opacity":9,"capture_process_id":-3}',
            encoding="utf-8",
        )

        data = ConfigStore(path).load()

        assert data["theme"] == "system"
        assert data["overlay_font_size"] == 20
        assert data["overlay_opacity"] == 1.0
        assert data["capture_process_id"] == 0

    def test_context_tuning_schema_accepts_new_mode_and_normalizes_values(
        self, tmp_path
    ):
        store = ConfigStore(tmp_path / "config.json")
        store.update({
            "asr_tuning_profile": "context",
            "asr_context_hold_ms": 9999,
            "asr_live_draft_enabled": False,
            "asr_context_correction": False,
            "asr_filler_mode": "aggressive",
        })

        data = store.load()

        assert data["asr_tuning_profile"] == "context"
        assert data["asr_context_hold_ms"] == 4000
        assert data["asr_live_draft_enabled"] is False
        assert data["asr_context_correction"] is False
        assert data["asr_filler_mode"] == "light"

    def test_unknown_keys_are_rejected(self, tmp_path):
        store = ConfigStore(tmp_path / "config.json")

        with pytest.raises(KeyError, match="未知配置键"):
            store.set("temporary_feature_flag", True)

    def test_non_finite_numbers_and_malformed_urls_fall_back(self, tmp_path):
        store = ConfigStore(tmp_path / "config.json")

        store.update({
            "overlay_opacity": float("nan"),
            "stt_base_url": "not a URL",
        })

        data = store.load()
        assert data["overlay_opacity"] == ConfigStore.DEFAULTS["overlay_opacity"]
        assert data["stt_base_url"] == ConfigStore.DEFAULTS["stt_base_url"]

    def test_save_creates_parent_dirs(self, tmp_path):
        store = ConfigStore(tmp_path / "deep" / "nested" / "config.json")
        store.set("mode", "b")
        assert config_store.ConfigStore(tmp_path / "deep" / "nested" / "config.json").get("mode") == "b"
        assert not list(store.path.parent.glob(f".{store.path.name}.*.part"))


# ===========================================================================
# 6. Pipeline stub 契约（仅显式测试注入）
# ===========================================================================
class TestPipelineStub:
    def test_stub_requires_explicit_test_escape_hatch(self, monkeypatch):
        monkeypatch.setattr(pipeline_client, "_RealPipeline", None)
        with pytest.raises(RuntimeError, match="核心识别管线无法加载"):
            get_pipeline()
        p = get_pipeline(allow_stub=True)
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
        progress: list[tuple[int, int, str]] = []
        p.on_progress(lambda done, total, stage: progress.append((done, total, stage)))

        p.start()
        assert p.is_running() is True
        assert statuses[-1] == "拾音中"
        p.stop()
        assert p.is_running() is False
        assert statuses[-1] == "待机"

        p.set_mode("c")
        assert p.mode == "c"
        p._emit_utterance("你好", "Hello")  # noqa: SLF001
        p._emit_progress(25, 100, "正在识别音视频")  # noqa: SLF001
        assert utters == [("你好", "Hello")]
        assert progress == [(25, 100, "正在识别音视频")]

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
            assert set(win.mode_cards) == {"a", "b", "c", "d"}
            assert all(isinstance(c, ModeCard) for c in win.mode_cards.values())
            assert win.lang_combo.count() == 4
            assert win.lang_combo.itemText(0) == "中 → 英"
            assert isinstance(win.subtitle_list, SubtitleList)
            assert win.subtitle_list.count() == 0
            # CTA 初始态
            assert win.cta.is_running() is False
        finally:
            win.close()
            win.deleteLater()

    def test_secondary_pages_are_embedded_and_navigate_as_a_stack(self, qapp, tmp_path):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget
        from voxsub.model_catalog import ModelMarketplace
        from voxsub.ui.model_hub_window import ModelHubWindow
        from voxsub.ui.settings_window import SettingsWindow

        store = ConfigStore(tmp_path / "config.json")
        win = self._make_win(tmp_path)
        settings = SettingsWindow(store=store)
        hub = ModelHubWindow(
            store=store,
            marketplace=ModelMarketplace(tmp_path / "models"),
        )

        class FakeOcrWorkspace(QWidget):
            def __init__(self):
                super().__init__()
                self.prepare_calls = 0

            def prepare_live(self):
                self.prepare_calls += 1

        ocr = FakeOcrWorkspace()
        win.install_in_app_pages(settings, hub, ocr)
        try:
            win.show()
            win.show_settings_page()
            qapp.processEvents()
            assert settings.parent() is win._page_stack  # noqa: SLF001
            assert not settings.isWindow()
            assert win._page_layer.isVisible()  # noqa: SLF001
            assert win._page_blur.blurRadius() == 12  # noqa: SLF001

            # Embedded navigation must preserve the main window's fullscreen state.
            win.showFullScreen()
            qapp.processEvents()
            win.show_settings_page()
            qapp.processEvents()
            assert win.windowState() & Qt.WindowState.WindowFullScreen

            settings.model_hub_btn.click()
            qapp.processEvents()
            assert hub.parent() is win._page_stack  # noqa: SLF001
            assert win._page_stack.currentWidget() is hub  # noqa: SLF001
            assert len(win._page_history) == 1  # noqa: SLF001

            win.close_in_app_page()
            assert win._page_stack.currentWidget() is settings  # noqa: SLF001
            win.close_in_app_page()
            assert not win._page_layer.isVisible()  # noqa: SLF001
            assert win._page_blur.blurRadius() == 0  # noqa: SLF001

            win.set_mode("d")
            assert win._workspace_stack.currentWidget() is ocr  # noqa: SLF001
            assert win.pair_label.text() == "翻译方向"
            assert win.lang_combo.isVisible()
            assert not win.cta.isVisible()
            assert ocr.prepare_calls == 1
            win.set_lang_pair("en-zh")
            assert store.get("lang_pair") == "en-zh"

            win.set_mode("a")
            assert win._workspace_stack.currentWidget() is win._translation_workspace  # noqa: SLF001
        finally:
            win.close()
            win.deleteLater()
            settings.deleteLater()
            hub.deleteLater()
            ocr.deleteLater()

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
            _wait_until(qapp, lambda: not win._pipeline_busy)  # noqa: SLF001
            assert win.pipeline.is_running() is True
            assert win.cta.is_running() is True
            assert win.status_light.text.text() == "拾音中"
            # 字幕流入
            win._on_utterance("你好", "Hello")  # noqa: SLF001
            win._on_utterance("世界", "World")
            assert win.subtitle_list.count() == 2
            # 停止
            win._toggle_run()  # noqa: SLF001
            _wait_until(qapp, lambda: not win._pipeline_busy)  # noqa: SLF001
            assert win.pipeline.is_running() is False
            assert win.cta.is_running() is False
            assert win.status_light.text.text() == "待机"
        finally:
            win.close()
            win.deleteLater()

    def test_live_draft_replaces_one_bilingual_row_before_final_commit(
        self, qapp, tmp_path
    ):
        from PySide6.QtWidgets import QLabel

        win = self._make_win(tmp_path)
        try:
            win._on_draft("欢迎", "Welcome")  # noqa: SLF001
            partial_row = win.subtitle_list._partial_row  # noqa: SLF001
            win._on_draft("欢迎使用", "Welcome to")  # noqa: SLF001

            assert win.subtitle_list.count() == 0
            assert win.subtitle_list._partial_row is partial_row  # noqa: SLF001
            assert win.subtitle_list._partial_src.text() == "欢迎使用"  # noqa: SLF001
            assert win.subtitle_list._partial_dst.text() == "Welcome to"  # noqa: SLF001

            win._on_utterance("欢迎使用 VoxSub。", "Welcome to VoxSub.")  # noqa: SLF001
            assert win.subtitle_list.count() == 1
            assert win.subtitle_list._partial_row is None  # noqa: SLF001
            rows = win.subtitle_list.findChildren(QLabel, "dstText")
            assert any(label.text() == "Welcome to VoxSub." for label in rows)
        finally:
            win.close()
            win.deleteLater()

    def test_four_peer_mode_cards_do_not_overlap_at_minimum_size(
        self, qapp, tmp_path
    ):
        from itertools import combinations

        from PySide6.QtCore import QRect

        win = self._make_win(tmp_path)
        try:
            win.resize(win.minimumSize())
            win.show()
            qapp.processEvents()
            card_rects = []
            for card in win.mode_cards.values():
                card_rect = card.geometry()
                top_left = card.parentWidget().mapTo(win, card_rect.topLeft())
                card_rects.append(QRect(top_left, card_rect.size()))
            assert all(
                not first.intersects(second)
                for first, second in combinations(card_rects, 2)
            )
            pair_top = win.pair_label.parentWidget().mapTo(
                win, win.pair_label.geometry().topLeft()).y()
            assert max(rect.bottom() for rect in card_rects) < pair_top
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
            win.set_lang_pair("auto-zh")
            assert win.current_lang_pair() == "auto-zh"
            assert win._store.get("lang_pair") == "auto-zh"  # noqa: SLF001
        finally:
            win.close()
            win.deleteLater()

    @pytest.mark.parametrize(
        ("stt_provider", "translate_tier", "expected_translator"),
        [
            ("local", "fast", "opus-fast"),
            ("cloud", "fast", "opus-fast"),
            ("local", "cloud", "cloud"),
            ("cloud", "quality", "qwen-quality"),
        ],
    )
    def test_pipeline_config_keeps_stt_and_translation_independent(
        self, qapp, tmp_path, stt_provider, translate_tier, expected_translator
    ):
        store = ConfigStore(tmp_path / "config.json")
        store.update({
            "models_root": str(tmp_path / "models"),
            "stt_provider": stt_provider,
            "translate_tier": translate_tier,
            "stt_api_key": "stt-key",
            "stt_base_url": "https://api.openai.com/v1",
            "stt_model": "whisper-1",
            "translate_api_key": "translate-key",
            "translate_base_url": "https://api.deepseek.com/v1",
            "translate_model": "deepseek-chat",
        })
        pipeline = _PipelineStub()
        win = MainWindow(store=store, pipeline=pipeline)
        try:
            win._apply_pipeline_config()  # noqa: SLF001
            assert pipeline.models_dir == Path(store.get("models_root"))
            assert pipeline.stt[0] == stt_provider
            assert pipeline.translator[0] == expected_translator
            assert pipeline.stt[1]["stt_api_key"] == "stt-key"
            assert pipeline.translator[1]["translate_api_key"] == "translate-key"
            assert pipeline.asr_tuning["context_hold_ms"] == 1800
            assert pipeline.asr_tuning["live_draft_enabled"] is True
            assert pipeline.asr_tuning["context_correction"] is True
            assert pipeline.asr_tuning["filler_mode"] == "light"
            assert pipeline.tts_enabled is True
            assert pipeline.tts_model_ids == {
                "zh": "tts-icefall-zh-aishell3",
                "en": "tts-icefall-en-ljspeech-low",
            }
        finally:
            win.close()
            win.deleteLater()

    def test_file_mode_uses_right_workspace_import_card(self, qapp, tmp_path):
        win = self._make_win(tmp_path)
        try:
            assert win.file_panel.parent().objectName() == "subtitlePanel"
            assert win.file_panel.isHidden()
            win.set_mode("c")
            assert not win.file_panel.isHidden()
            assert win.workspace_title.text() == "文件字幕"
            assert "选择文件后" in win.subtitle_list._empty_hint.text()  # noqa: SLF001
            assert win.file_progress.isTextVisible() is False
            win._on_file_progress(25, 100, "正在识别音视频")  # noqa: SLF001
            assert win.file_progress.value() == 25
            assert "正在识别音视频" in win.file_progress_label.text()
            assert not win.file_progress.isHidden()
        finally:
            win.close()
            win.deleteLater()

    def test_main_overlay_font_buttons_have_real_text_room(self, qapp, tmp_path):
        win = self._make_win(tmp_path)
        try:
            for button in (win.overlay_font_down_btn, win.overlay_font_up_btn):
                # Compatibility hooks remain for integrations, but adjustment
                # controls must no longer appear in the main workspace.
                assert button.objectName() == "compactGhostButton"
                assert button.isHidden()
                assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 16
                assert button.sizeHint().width() <= button.width()
        finally:
            win.close()
            win.deleteLater()

    def test_main_and_locked_overlay_controls_stay_in_sync(self, qapp, tmp_path):
        from PySide6.QtCore import Qt
        from voxsub.ui.subtitle_overlay import SubtitleOverlay

        store = ConfigStore(tmp_path / "config.json")
        win = MainWindow(store=store, pipeline=_PipelineStub())
        overlay = SubtitleOverlay(store=store)
        win.attach_overlay(overlay)
        try:
            win.overlay_font_up_btn.click()
            assert overlay.font_size() == 22
            assert store.get("overlay_font_size") == 22
            win.overlay_lock_btn.click()
            assert overlay.is_click_through()
            assert win.overlay_lock_btn.text() == "解锁浮窗"
            overlay._poll_locked_hover(overlay.frameGeometry().center())  # noqa: SLF001
            assert overlay._locked_panel.isVisible()  # noqa: SLF001
            assert overlay._locked_panel.parent() is None  # noqa: SLF001
            assert not (overlay._locked_panel.windowFlags()  # noqa: SLF001
                        & Qt.WindowType.WindowTransparentForInput)
            overlay._locked_panel.unlock.click()  # noqa: SLF001
            assert not overlay.is_click_through()
            assert win.overlay_lock_btn.text() == "锁定浮窗"
        finally:
            overlay.close()
            overlay.deleteLater()
            win.close()
            win.deleteLater()

    def test_open_overlay_button_is_single_instance_and_silent_when_visible(
            self, qapp, tmp_path):
        from voxsub.ui.subtitle_overlay import SubtitleOverlay

        store = ConfigStore(tmp_path / "config.json")
        win = MainWindow(store=store, pipeline=_PipelineStub())
        overlay = SubtitleOverlay(store=store)
        win.attach_overlay(overlay)
        try:
            assert win.overlay_open_btn.text() == "打开浮窗"
            assert overlay.isHidden()
            win.overlay_open_btn.click()
            qapp.processEvents()
            assert overlay.isVisible()
            first_overlay = win._overlay  # noqa: SLF001
            win.overlay_open_btn.click()
            win.overlay_open_btn.click()
            assert win._overlay is first_overlay  # noqa: SLF001
            assert overlay.isVisible()
        finally:
            overlay.close()
            overlay.deleteLater()
            win.close()
            win.deleteLater()

    def test_file_picker_persists_and_updates_pipeline(self, qapp, tmp_path, monkeypatch):
        import voxsub.ui.main_window as main_window

        video = tmp_path / "meeting.mp4"
        video.write_bytes(b"placeholder")
        win = self._make_win(tmp_path)
        monkeypatch.setattr(main_window, "choose_open_file", lambda *a, **k: str(video))
        try:
            assert win.select_input_file()
            assert win._store.get("last_input_file") == str(video)  # noqa: SLF001
            assert win.pipeline.input_file == str(video)
            assert win.file_name_label.text() == "meeting.mp4"
        finally:
            win.close()
            win.deleteLater()

    def test_conversation_is_selectable_saveable_and_clearable(self, qapp, tmp_path):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel

        win = self._make_win(tmp_path)
        out = tmp_path / "conversation.txt"
        try:
            win._on_utterance("你好", "Hello")  # noqa: SLF001
            labels = win.subtitle_list.findChildren(QLabel, "srcText")
            assert labels
            assert labels[0].textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
            assert win.save_conversation(out)
            assert out.read_text(encoding="utf-8") == "你好\nHello"
            win.clear_conversation()
            assert win.subtitle_list.count() == 0
            assert win._conversation == []  # noqa: SLF001
        finally:
            win.close()
            win.deleteLater()

    def test_interactive_conversation_export_runs_in_a_worker(self, qapp, tmp_path, monkeypatch):
        import voxsub.ui.main_window as main_window

        win = self._make_win(tmp_path)
        out = tmp_path / "conversation.txt"
        try:
            win._on_utterance("你好", "Hello")  # noqa: SLF001
            monkeypatch.setattr(
                main_window,
                "choose_save_file",
                lambda *a, **k: (str(out), "纯文本 (*.txt)"),
            )
            original_write = win._write_conversation_snapshot  # noqa: SLF001

            def _slow_write(snapshot, destination):
                time.sleep(0.15)
                return original_write(snapshot, destination)

            monkeypatch.setattr(win, "_write_conversation_snapshot", _slow_write)
            started = time.monotonic()
            assert win._begin_save_conversation()  # noqa: SLF001
            assert time.monotonic() - started < 0.08
            assert not win.save_conversation_btn.isEnabled()
            _wait_until(qapp, lambda: not win._session_export_busy)  # noqa: SLF001
            assert out.read_text(encoding="utf-8") == "你好\nHello"
            assert win.save_conversation_btn.isEnabled()
        finally:
            win.close()
            win.deleteLater()

    def test_microphone_recording_controls_pause_resume_finish(self, qapp, tmp_path):
        win = self._make_win(tmp_path)
        try:
            win.record_switch.setChecked(True)
            win._toggle_run()  # start recording flow  # noqa: SLF001
            _wait_until(qapp, lambda: not win._pipeline_busy)  # noqa: SLF001
            assert win.pipeline.is_running()
            assert win.cta.state() == "recording"
            assert not win.finish_record_btn.isHidden()
            win._toggle_run()  # pause  # noqa: SLF001
            assert win.pipeline.is_paused()
            assert win.cta.state() == "paused"
            win._toggle_run()  # resume  # noqa: SLF001
            assert not win.pipeline.is_paused()
            assert win.cta.state() == "recording"
            win._finish_recording()  # noqa: SLF001
            assert win._pipeline_busy  # noqa: SLF001
            assert not win.save_conversation_btn.isEnabled()
            _wait_until(qapp, lambda: not win._pipeline_busy)  # noqa: SLF001
            assert not win.pipeline.is_running()
            assert win.cta.state() == "idle"
        finally:
            win.close()
            win.deleteLater()

    def test_slow_stop_never_blocks_ui_and_save_waits_for_final_subtitles(
        self, qapp, tmp_path
    ):
        class _SlowPipeline(_PipelineStub):
            def stop(self) -> None:
                time.sleep(0.15)
                super().stop()

        win = MainWindow(
            store=ConfigStore(tmp_path / "config.json"),
            pipeline=_SlowPipeline(),
        )
        out = tmp_path / "after-stop.txt"
        try:
            win._toggle_run()  # noqa: SLF001
            _wait_until(qapp, lambda: not win._pipeline_busy)  # noqa: SLF001
            win._on_utterance("完整一句", "A complete sentence")  # noqa: SLF001
            started = time.monotonic()
            win._toggle_run()  # noqa: SLF001
            assert time.monotonic() - started < 0.08
            assert win._pipeline_busy  # noqa: SLF001
            assert not win.save_conversation(out)
            assert not out.exists()
            _wait_until(qapp, lambda: not win._pipeline_busy)  # noqa: SLF001
            assert win.save_conversation(out)
            assert out.exists()
        finally:
            win.close()
            win.deleteLater()

    def test_slow_start_never_blocks_qt_event_loop(self, qapp, tmp_path):
        class _SlowStartPipeline(_PipelineStub):
            def start(self) -> None:
                time.sleep(0.15)
                super().start()

        win = MainWindow(
            store=ConfigStore(tmp_path / "config.json"),
            pipeline=_SlowStartPipeline(),
        )
        try:
            started = time.monotonic()
            win._toggle_run()  # noqa: SLF001
            assert time.monotonic() - started < 0.08
            assert win._pipeline_busy  # noqa: SLF001
            assert win.cta.state() == "starting"
            assert not win.cta.isEnabled()
            _wait_until(qapp, lambda: not win._pipeline_busy)  # noqa: SLF001
            assert win.pipeline.is_running()
        finally:
            win.pipeline.stop()
            win.close()
            win.deleteLater()

    def test_finish_recording_then_save_is_serialized_without_freeze(
        self, qapp, tmp_path
    ):
        class _SlowFinishPipeline(_PipelineStub):
            def stop(self) -> None:
                time.sleep(0.15)
                super().stop()

        win = MainWindow(
            store=ConfigStore(tmp_path / "config.json"),
            pipeline=_SlowFinishPipeline(),
        )
        output = tmp_path / "recording-session.txt"
        try:
            win.record_switch.setChecked(True)
            win._toggle_run()  # noqa: SLF001
            _wait_until(qapp, lambda: not win._pipeline_busy)  # noqa: SLF001
            win._on_utterance("尾句", "Final sentence")  # noqa: SLF001
            started = time.monotonic()
            win._finish_recording()  # noqa: SLF001
            assert time.monotonic() - started < 0.08
            assert win._pipeline_busy  # noqa: SLF001
            assert not win.save_conversation(output)
            assert not output.exists()
            _wait_until(qapp, lambda: not win._pipeline_busy)  # noqa: SLF001
            assert win.save_conversation(output)
            assert output.read_text(encoding="utf-8") == "尾句\nFinal sentence"
        finally:
            win.close()
            win.deleteLater()


# ===========================================================================
# 8. 字幕浮窗冒烟（offscreen）
# ===========================================================================
class TestSubtitleOverlay:
    def test_construct_and_subtitles(self, qapp, tmp_path):
        from PySide6.QtCore import Qt
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
            ov.show()
            qapp.processEvents()
            before_src_height = ov.src_label.fontMetrics().height()
            before_dst_height = ov.dst_label.fontMetrics().height()
            ov.change_font_size(+2)
            qapp.processEvents()
            assert ov._font_size == 22  # noqa: SLF001
            assert ov._font_value_label.text() == "22"  # noqa: SLF001
            assert ov.src_label.fontMetrics().height() > before_src_height
            assert ov.dst_label.fontMetrics().height() > before_dst_height
            assert "font-size: 18pt" in ov.src_label.styleSheet()
            assert "font-size: 22pt" in ov.dst_label.styleSheet()
            ov.change_font_size(-99)  # 下限 10
            assert ov._font_size == 10  # noqa: SLF001
            ov.set_overlay_opacity(0.5)
            # setWindowOpacity 内部量化到 8bit（127/255≈0.498），容差放宽
            assert ov.windowOpacity() == pytest.approx(0.5, abs=0.01)
            assert ov._store.get("overlay_font_size") == 10  # noqa: SLF001
            for button in (ov._font_down_btn, ov._font_up_btn):  # noqa: SLF001
                assert button.sizeHint().width() <= button.width()
            ov._font_up_btn.click()  # noqa: SLF001
            assert ov.font_size() == 12
            ov._font_down_btn.click()  # noqa: SLF001
            assert ov.font_size() == 10
            ov.change_font_size(+100)
            assert ov.font_size() == 72
            # The body remains native click-through, while a separate hover
            # control island provides the only in-place unlock action.
            ov.set_click_through(True)
            assert ov.is_click_through()
            assert ov.windowFlags() & Qt.WindowType.WindowTransparentForInput
            ov._poll_locked_hover(ov.frameGeometry().center())  # noqa: SLF001
            assert ov._locked_panel.isVisible()  # noqa: SLF001
            assert ov._locked_panel.layout().count() == 1  # noqa: SLF001
            assert ov._locked_panel.layout().sizeHint().width() <= ov._locked_panel.width()  # noqa: SLF001
            assert ov._locked_panel.unlock.sizeHint().width() <= ov._locked_panel.unlock.width()  # noqa: SLF001
            ov._locked_panel.unlock.click()  # noqa: SLF001
            assert not ov.is_click_through()
            assert not (ov.windowFlags() & Qt.WindowType.WindowTransparentForInput)
            assert not ov.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        finally:
            ov.close()
            ov.deleteLater()

    def test_unlocked_overlay_can_resize_and_persist_size(self, qapp, tmp_path):
        from PySide6.QtCore import QPoint
        from voxsub.ui.subtitle_overlay import SubtitleOverlay

        store = ConfigStore(tmp_path / "config.json")
        ov = SubtitleOverlay(store=store)
        try:
            ov.show()
            qapp.processEvents()
            start = ov.frameGeometry()
            ov._manual_size = True  # noqa: SLF001
            ov._resize_edges = (False, True, False, True)  # noqa: SLF001
            ov._resize_start_geometry = start  # noqa: SLF001
            ov._resize_start_pos = start.bottomRight()  # noqa: SLF001
            ov._resize_from_global(start.bottomRight() + QPoint(80, 40))  # noqa: SLF001
            ov._persist_size()  # noqa: SLF001
            assert ov.width() == start.width() + 80
            assert ov.height() == start.height() + 40
            assert store.get("overlay_width") == ov.width()
            assert store.get("overlay_height") == ov.height()

            # Locked mode must not alter the size controls: only unlock exists.
            ov.set_click_through(True)
            ov._poll_locked_hover(ov.frameGeometry().center())  # noqa: SLF001
            assert ov._locked_panel.layout().count() == 1  # noqa: SLF001
        finally:
            ov.close()
            ov.deleteLater()

    def test_display_mode_and_spacing_controls_persist(self, qapp, tmp_path):
        from voxsub.ui.subtitle_overlay import SubtitleOverlay

        store = ConfigStore(tmp_path / "config.json")
        ov = SubtitleOverlay(store=store)
        try:
            ov.set_subtitles("Source line", "译文")
            ov.show()
            qapp.processEvents()

            ov.set_display_mode("translation")
            assert ov.display_mode() == "translation"
            assert ov.src_label.isHidden()
            assert not ov.dst_label.isHidden()
            assert store.get("overlay_display_mode") == "translation"
            assert ov._display_actions["translation"].isChecked()  # noqa: SLF001
            ov._display_actions["translation"].trigger()  # noqa: SLF001
            assert ov._display_actions["translation"].isChecked()  # noqa: SLF001

            ov.set_display_mode("source")
            assert not ov.src_label.isHidden()
            assert ov.dst_label.isHidden()
            ov.set_display_mode("bilingual")
            assert not ov.src_label.isHidden()
            assert not ov.dst_label.isHidden()

            original_padding = ov.content_padding()
            original_gap = ov.line_gap()
            ov.change_content_padding(+2)
            assert ov.content_padding() == original_padding + 2
            assert ov.line_gap() == round(
                original_gap / original_padding * ov.content_padding())
            assert ov._box.contentsMargins().top() == ov.content_padding()  # noqa: SLF001
            assert ov._box.contentsMargins().bottom() == ov.content_padding()  # noqa: SLF001
            assert ov._content_box.spacing() == ov.line_gap()  # noqa: SLF001

            ov.change_content_padding(-100)
            assert ov.content_padding() == 8
            assert ov._box.contentsMargins().left() == 8  # noqa: SLF001
            assert store.get("overlay_content_padding") == 8
            ov.change_content_padding(+100)
            assert ov.content_padding() == 64

            ov.change_line_gap(-100)
            assert ov.line_gap() == 0
            assert ov._content_box.spacing() == 0  # noqa: SLF001
            assert store.get("overlay_line_gap") == 0
            ov.change_line_gap(+100)
            assert ov.line_gap() == 40
            assert ov._content_box.spacing() == 40  # noqa: SLF001
        finally:
            ov.close()
            ov.deleteLater()

    def test_long_subtitles_scroll_without_resizing_overlay(self, qapp, tmp_path):
        from voxsub.ui.subtitle_overlay import SubtitleOverlay

        ov = SubtitleOverlay(store=ConfigStore(tmp_path / "config.json"))
        try:
            ov.resize(420, 110)
            ov.show()
            qapp.processEvents()
            original_size = ov.size()
            long_source = "A long source sentence stays inside the chosen window. " * 40
            long_translation = "很长的译文会在用户指定的浮窗中换行并允许滚动查看。" * 40

            ov.set_subtitles(long_source, long_translation)
            _wait_until(
                qapp,
                lambda: ov._content_scroll.verticalScrollBar().maximum() > 0,  # noqa: SLF001
            )
            bar = ov._content_scroll.verticalScrollBar()  # noqa: SLF001
            assert ov.size() == original_size
            assert bar.value() == bar.minimum()

            ov.set_partial(long_source * 2, long_translation * 2)
            _wait_until(qapp, lambda: bar.value() == bar.maximum())
            assert ov.size() == original_size
            assert bar.maximum() > 0

            ov.move(300, 300)
            base_top_margin = ov._box.contentsMargins().top()  # noqa: SLF001
            ov._set_toolbar_visible(True)  # noqa: SLF001
            assert ov._toolbar.parent() is None  # noqa: SLF001
            assert ov._box.contentsMargins().top() == base_top_margin  # noqa: SLF001
            assert ov._toolbar.frameGeometry().bottom() <= ov.frameGeometry().top()  # noqa: SLF001
            ov._spacing_btn.click()  # noqa: SLF001
            assert ov._spacing_controls.isVisible()  # noqa: SLF001
            ov.set_click_through(True)
            assert ov._toolbar.isHidden()  # noqa: SLF001
            assert ov._box.contentsMargins().top() == ov.content_padding()  # noqa: SLF001
            ov._poll_locked_hover(ov.frameGeometry().center())  # noqa: SLF001
            assert ov._locked_panel.isVisible()  # noqa: SLF001
            assert ov._locked_panel.layout().count() == 1  # noqa: SLF001
        finally:
            ov.close()
            ov.deleteLater()


# ===========================================================================
# 9. 设置页冒烟（offscreen）
# ===========================================================================
class TestSettingsWindow:
    def test_system_language_setting_resolves_current_system_language(self, monkeypatch):
        import voxsub.ui.i18n as i18n

        i18n.language_manager.set_language("zh")
        monkeypatch.setattr(i18n, "system_language", lambda: "en")
        try:
            assert i18n.language_manager.set_language("system") == "en"
            assert i18n.language_manager.setting == "system"
            assert i18n.language_manager.language == "en"
        finally:
            i18n.language_manager.set_language("zh")

    def test_bilingual_ui_follows_setting_and_refreshes_open_windows(self, qapp, tmp_path):
        from voxsub.ui.i18n import language_manager
        from voxsub.ui.settings_window import SettingsWindow

        store = ConfigStore(tmp_path / "config.json")
        assert store.get("language") == "system"
        sw = SettingsWindow(store=store)
        try:
            language_manager.set_language("en")
            assert sw.windowTitle() == "Settings - VoxSub"
            assert sw.tabs.tabText(0) == "Translation"
            assert sw.tabs.tabText(1) == "Recognition tuning"
            assert sw.tabs.tabText(4) == "Storage & models"
            assert sw.language_combo.currentText() == "Follow system"
            sw.language_combo.setCurrentIndex(2)
            assert store.get("language") == "en"
            assert language_manager.language == "en"
            language_manager.set_language("zh")
            assert sw.windowTitle() == "设置 — 语幕 VoxSub"
            assert sw.tabs.tabText(0) == "翻译"
        finally:
            sw.close()

    def test_language_change_refreshes_all_open_windows(self, qapp, tmp_path):
        from PySide6.QtWidgets import QLabel
        from voxsub.model_catalog import HardwareProfile, ModelMarketplace
        from voxsub.ui.diagnostics_window import DiagnosticsWindow
        from voxsub.ui.i18n import language_manager
        from voxsub.ui.model_hub_window import ModelHubWindow
        from voxsub.ui.settings_window import SettingsWindow
        from voxsub.ui.subtitle_overlay import SubtitleOverlay

        class _FakeDiag:
            @staticmethod
            def run_self_check():
                return [{"check": "模型完整性", "status": "ok", "detail": "406 条登记全部就绪"}]

        language_manager.set_language("zh")
        store = ConfigStore(tmp_path / "config.json")
        profile = HardwareProfile("test cpu", 8, 16, 32.0, "RTX 4060", 8.0, "CUDA")
        win = MainWindow(store=store, pipeline=_PipelineStub())
        overlay = SubtitleOverlay(store=store)
        settings = SettingsWindow(store=store, overlay=overlay)
        hub = ModelHubWindow(
            store=store,
            marketplace=ModelMarketplace(tmp_path / "models"),
            profile=profile,
        )
        diagnostics = DiagnosticsWindow(store=store, diagnostics_module=_FakeDiag)
        try:
            language_manager.set_language("en")
            qapp.processEvents()
            assert win.windowTitle() == "VoxSub"
            assert win.source_hint.text() == "Input: microphone selected in Settings"
            assert overlay._lock_btn.text() == "Lock"  # noqa: SLF001
            assert overlay._locked_panel.unlock.text() == "Unlock"  # noqa: SLF001
            assert settings.tabs.tabText(3) == "Devices"
            assert settings.language_combo.itemText(0) == "Follow system"
            assert hub.windowTitle() == "Model Hub - VoxSub"
            assert hub.source_combo.itemText(0) == "Auto benchmark and switch"
            assert "8 cores" in hub.hardware_detail.text()
            assert "Discrete GPU" in hub.hardware_detail.text()
            assert diagnostics.windowTitle() == "Diagnostics - VoxSub"
            assert diagnostics.tabs.tabText(0) == "Self-check"
            texts = [label.text() for label in diagnostics.findChildren(QLabel)]
            assert "Model integrity" in texts
            assert any("registered entries are ready" in text for text in texts)
        finally:
            language_manager.set_language("zh")
            for widget in (diagnostics, hub, settings, overlay, win):
                widget.close()
                widget.deleteLater()

    def test_all_choice_controls_use_stable_variants(self, qapp, tmp_path):
        from PySide6.QtWidgets import QCheckBox, QRadioButton
        from voxsub.ui.settings_window import SettingsWindow

        sw = SettingsWindow(store=ConfigStore(tmp_path / "config.json"))
        try:
            radios = sw.findChildren(QRadioButton)
            switches = sw.findChildren(QCheckBox)
            assert len(radios) == 8
            assert all(isinstance(radio, RoundRadioButton) for radio in radios)
            assert switches
            assert all(isinstance(switch, ToggleSwitch) for switch in switches)
            for control in (*radios, *switches):
                assert control.sizeHint().width() >= (
                    control.fontMetrics().horizontalAdvance(control.text()) + 20
                )
        finally:
            sw.close()
            sw.deleteLater()

    def test_construct_and_tier_persist(self, qapp, tmp_path):
        from voxsub.ui.settings_window import SettingsWindow

        path = tmp_path / "config.json"
        store = ConfigStore(path)
        sw = SettingsWindow(store=store)
        try:
            assert sw.tabs.count() == 7
            assert sw.tabs.tabText(1) == "识别调优"
            assert sw.tabs.tabText(3) == "设备"
            assert sw.tabs.tabText(4) == "存储与模型"
            # 切换云档 → 输入框可用 + 落盘
            sw.tier_cloud.setChecked(True)
            assert store.get("translate_tier") == "cloud"
            assert sw.api_key_edit.isEnabled()
            assert sw.base_url_edit.isEnabled()
            sw.api_key_edit.setText("sk-abc")
            sw.save_cloud_credentials()
            assert store.get("api_key") == "sk-abc"
            sw.stt_cloud_radio.setChecked(True)
            sw.stt_api_key_edit.setText("stt-key")
            sw.stt_model_edit.setText("whisper-test")
            sw.save_cloud_credentials()
            assert sw.current_stt_provider() == "cloud"
            assert store.get("stt_api_key") == "stt-key"
            assert store.get("stt_model") == "whisper-test"
            assert "云 STT" in sw.cloud_mode_summary.text()
            assert "云翻译" in sw.cloud_mode_summary.text()
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

    def test_overlay_controls_sync_to_live_overlay(self, qapp, tmp_path):
        from voxsub.ui.settings_window import SettingsWindow
        from voxsub.ui.subtitle_overlay import SubtitleOverlay

        store = ConfigStore(tmp_path / "config.json")
        overlay = SubtitleOverlay(store=store)
        sw = SettingsWindow(store=store, overlay=overlay)
        try:
            assert sw.overlay_font_spin.minimum() == 10
            assert sw.overlay_font_spin.maximum() == 72
            sw.overlay_font_spin.setValue(26)
            assert overlay.font_size() == 26
            assert store.get("overlay_font_size") == 26

            sw.overlay_font_spin.setValue(72)
            assert overlay.font_size() == 72
            assert store.get("overlay_font_size") == 72

            sw.overlay_opacity_spin.setValue(70)
            assert overlay.windowOpacity() == pytest.approx(0.70, abs=0.01)
            assert store.get("overlay_opacity") == pytest.approx(0.70)

            sw.overlay_lock_switch.setChecked(True)
            assert overlay.is_click_through()
            assert store.get("overlay_click_through") is True
        finally:
            sw.close()
            sw.deleteLater()
            overlay.close()
            overlay.deleteLater()

    def test_overlay_spinbox_arrows_match_tuning_style_and_both_step(
        self, qapp, tmp_path
    ):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QAbstractSpinBox
        from voxsub.ui.settings_window import SettingsWindow

        sw = SettingsWindow(store=ConfigStore(tmp_path / "config.json"))
        try:
            sw.tabs.setCurrentIndex(5)
            sw.show()
            qapp.processEvents()
            sw.overlay_font_spin.setValue(30)
            sw.overlay_opacity_spin.setValue(70)
            for spin in (sw.overlay_font_spin, sw.overlay_opacity_spin):
                assert spin.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
                before = spin.value()
                up = spin._voxsub_step_up_btn  # noqa: SLF001
                down = spin._voxsub_step_down_btn  # noqa: SLF001
                assert up.objectName() == "spinStepButton"
                assert down.objectName() == "spinStepButton"
                assert up.isVisible() and down.isVisible()
                QTest.mouseClick(up, Qt.MouseButton.LeftButton)
                assert spin.value() == before + spin.singleStep()
                QTest.mouseClick(down, Qt.MouseButton.LeftButton)
                assert spin.value() == before
        finally:
            sw.close()
            sw.deleteLater()

    def test_sentry_settings_save_through_ui(self, qapp, tmp_path, monkeypatch):
        import voxsub.ui.settings_window as settings_window
        from voxsub.ui.settings_window import SettingsWindow

        calls = []
        monkeypatch.setattr(settings_window, "reload_error_reporting",
                            lambda: calls.append(True))
        monkeypatch.setattr(settings_window, "is_error_reporting_enabled",
                            lambda: True)
        store = ConfigStore(tmp_path / "config.json")
        sw = SettingsWindow(store=store)
        try:
            sw.sentry_dsn_edit.setText("https://public@example.invalid/1")
            sw.sentry_environment_combo.setCurrentIndex(
                sw.sentry_environment_combo.findData("production"))
            sw.sentry_build_edit.setText("desktop-1")
            sw.sentry_save_btn.click()

            assert calls == [True]
            assert store.get("sentry_dsn") == "https://public@example.invalid/1"
            assert store.get("sentry_environment") == "production"
            assert store.get("sentry_build") == "desktop-1"
            assert "启用" in sw.sentry_status_label.text()
        finally:
            sw.close()
            sw.deleteLater()

    def test_asr_tuning_has_hover_info_transaction_and_wide_ranges(
        self, qapp, tmp_path, monkeypatch
    ):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QEnterEvent
        from PySide6.QtWidgets import QToolButton
        import voxsub.ui.settings_window as settings_module
        from voxsub.ui.settings_window import SettingsWindow

        store = ConfigStore(tmp_path / "config.json")
        sw = SettingsWindow(store=store)
        try:
            info = [b for b in sw.findChildren(QToolButton) if b.text() == "i"]
            assert len(info) == 11
            assert all(len(button.toolTip()) >= 20 for button in info)
            shown: list[tuple] = []

            class _TipSpy:
                @staticmethod
                def showText(*args):  # noqa: N802
                    shown.append(args)

                @staticmethod
                def hideText():  # noqa: N802
                    pass

            monkeypatch.setattr(settings_module, "QToolTip", _TipSpy)
            point = QPointF(1, 1)
            info[0].enterEvent(QEnterEvent(point, point, point))
            assert shown and "自动" in str(shown[0][1])
            # Clicking i no longer opens a blocking QMessageBox.
            info[0].click()
            assert sw.silence_spin.minimum() == 50
            assert sw.silence_spin.maximum() == 5000
            assert sw.max_segment_spin.maximum() == 120.0
            sw.asr_profile_combo.setCurrentIndex(sw.asr_profile_combo.findData("custom"))
            sw.silence_spin.setValue(80)
            sw.hotwords_edit.setText("VoxSub,肿瘤免疫")
            assert sw._tuning_dirty  # noqa: SLF001
            # Draft does not touch disk until explicit save.
            assert store.get("asr_tuning_profile") == "auto"
            assert store.get("asr_silence_ms") == 650
            sw.tuning_save_btn.click()
            assert store.get("asr_tuning_profile") == "custom"
            assert store.get("asr_silence_ms") == 80
            assert store.get("asr_hotwords") == "VoxSub,肿瘤免疫"
            sw.silence_spin.setValue(1230)
            sw.close()
            assert store.get("asr_silence_ms") == 80
            assert sw.silence_spin.value() == 80
        finally:
            sw.close()
            sw.deleteLater()

    def test_smart_context_mode_is_additive_and_saves_its_own_controls(
        self, qapp, tmp_path
    ):
        from voxsub.ui.settings_window import SettingsWindow

        store = ConfigStore(tmp_path / "config.json")
        sw = SettingsWindow(store=store)
        try:
            context_index = sw.asr_profile_combo.findData("context")
            assert context_index >= 0
            sw.asr_profile_combo.setCurrentIndex(context_index)
            assert not sw.silence_spin.isEnabled()
            assert sw.context_hold_spin.isEnabled()
            assert sw.live_draft_switch.isEnabled()
            assert sw.live_draft_switch.isChecked()
            assert sw.context_correction_switch.isEnabled()
            assert sw.filler_mode_combo.isEnabled()

            sw.context_hold_spin.setValue(2200)
            sw.live_draft_switch.setChecked(False)
            sw.context_correction_switch.setChecked(False)
            sw.filler_mode_combo.setCurrentIndex(
                sw.filler_mode_combo.findData("off"))
            sw.tuning_save_btn.click()
            assert store.get("asr_tuning_profile") == "context"
            assert store.get("asr_context_hold_ms") == 2200
            assert store.get("asr_live_draft_enabled") is False
            assert store.get("asr_context_correction") is False
            assert store.get("asr_filler_mode") == "off"

            # Existing presets remain selectable and do not expose context-only knobs.
            sw.asr_profile_combo.setCurrentIndex(
                sw.asr_profile_combo.findData("balanced"))
            assert not sw.context_hold_spin.isEnabled()
            assert not sw.context_correction_switch.isEnabled()
            assert not sw.live_draft_switch.isEnabled()
            assert not sw.filler_mode_combo.isEnabled()
        finally:
            sw.close()
            sw.deleteLater()

    def test_asr_tuning_spinbox_up_and_down_arrows_both_step(self, qapp, tmp_path):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QAbstractSpinBox
        from voxsub.ui.settings_window import SettingsWindow

        sw = SettingsWindow(store=ConfigStore(tmp_path / "config.json"))
        try:
            sw.tabs.setCurrentIndex(1)
            sw.asr_profile_combo.setCurrentIndex(
                sw.asr_profile_combo.findData("custom"))
            sw.show()
            qapp.processEvents()
            for spin in (
                sw.vad_threshold_spin,
                sw.silence_spin,
                sw.max_segment_spin,
                sw.beam_paths_spin,
                sw.max_tokens_spin,
            ):
                assert spin.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
                before = spin.value()
                up = spin._voxsub_step_up_btn  # noqa: SLF001
                down = spin._voxsub_step_down_btn  # noqa: SLF001
                assert up.isVisible() and down.isVisible()
                QTest.mousePress(up, Qt.MouseButton.LeftButton)
                qapp.processEvents()
                assert up.isDown()
                QTest.mouseRelease(up, Qt.MouseButton.LeftButton)
                assert spin.value() == pytest.approx(before + spin.singleStep())
                QTest.mouseClick(down, Qt.MouseButton.LeftButton)
                assert spin.value() == pytest.approx(before)

            sw.asr_profile_combo.setCurrentIndex(
                sw.asr_profile_combo.findData("context"))
            spin = sw.context_hold_spin
            assert spin.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
            before = spin.value()
            QTest.mouseClick(
                spin._voxsub_step_up_btn, Qt.MouseButton.LeftButton)  # noqa: SLF001
            assert spin.value() == before + spin.singleStep()
            QTest.mouseClick(
                spin._voxsub_step_down_btn, Qt.MouseButton.LeftButton)  # noqa: SLF001
            assert spin.value() == before
        finally:
            sw.close()
            sw.deleteLater()

    def test_storage_folder_picker_is_async_and_avoids_windows_shell(
        self, qapp, tmp_path
    ):
        import time
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QFileDialog
        from voxsub.ui.settings_window import SettingsWindow

        store = ConfigStore(tmp_path / "config.json")
        store.set("models_root", str(tmp_path / "models"))
        sw = SettingsWindow(store=store)
        try:
            started = time.monotonic()
            sw._choose_models_folder()  # noqa: SLF001
            assert time.monotonic() - started < 0.2
            QTest.qWait(10)
            qapp.processEvents()
            dialog = sw._storage_dialog  # noqa: SLF001
            assert dialog is not None
            assert dialog.isVisible()
            assert dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
            assert dialog.fileMode() == QFileDialog.FileMode.Directory
            dialog.reject()
            QTest.qWait(10)
            qapp.processEvents()
            assert sw._storage_dialog is None  # noqa: SLF001
            assert sw.change_models_folder_btn.isEnabled()
        finally:
            sw.close()
            sw.deleteLater()

    def test_model_migration_result_does_not_wait_in_gui_thread(
        self, qapp, tmp_path, monkeypatch
    ):
        import time
        from PySide6.QtTest import QTest
        import voxsub.ui.settings_window as settings_module
        from voxsub.model_storage import MigrationResult
        from voxsub.ui.settings_window import SettingsWindow

        store = ConfigStore(tmp_path / "config.json")
        store.set("models_root", str(tmp_path / "old-models"))
        destination = tmp_path / "new-models"
        sw = SettingsWindow(store=store)
        try:
            def fake_migrate(source, target):
                time.sleep(0.08)
                return MigrationResult(source, target, moved_paths=1)

            monkeypatch.setattr(settings_module, "migrate_models", fake_migrate)
            started = time.monotonic()
            sw._begin_model_migration(destination, switch_default=True)  # noqa: SLF001
            assert time.monotonic() - started < 0.05
            assert sw.can_close_application() is False
            deadline = time.monotonic() + 2.0
            while sw._storage_worker is not None and time.monotonic() < deadline:  # noqa: SLF001
                QTest.qWait(10)
                qapp.processEvents()
            assert sw._storage_worker is None  # noqa: SLF001
            assert sw.can_close_application() is True
            assert store.get("models_root") == str(destination)
        finally:
            sw.close()
            sw.deleteLater()
    def test_tts_switch(self, qapp, tmp_path):
        from voxsub.ui.settings_window import SettingsWindow

        sw = SettingsWindow(store=ConfigStore(tmp_path / "config.json"))
        changes: list[tuple[bool, str, str]] = []
        sw.tts_settings_changed.connect(
            lambda enabled, zh, en: changes.append((enabled, zh, en)))
        try:
            sw.tts_switch.setChecked(False)
            assert sw._store.get("tts_enabled") is False  # noqa: SLF001
            assert changes == [(
                False,
                "tts-icefall-zh-aishell3",
                "tts-icefall-en-ljspeech-low",
            )]
        finally:
            sw.close()
            sw.deleteLater()

    def test_tts_model_choices_use_installed_catalog_voices(self, qapp, tmp_path):
        from voxsub.ui.settings_window import SettingsWindow

        models = tmp_path / "models"
        store = ConfigStore(tmp_path / "config.json")
        store.set("models_root", str(models))
        required = {
            "tts/zh": ("model.onnx", "tokens.txt", "lexicon.txt"),
            "tts/en": ("model.onnx", "tokens.txt", "espeak-ng-data/phontab"),
            "tts/vits-melo-zh-en": ("model.onnx", "tokens.txt", "lexicon.txt"),
        }
        for directory, relatives in required.items():
            for relative in relatives:
                target = models / directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"ready")

        sw = SettingsWindow(store=store)
        changes: list[tuple[bool, str, str]] = []
        sw.tts_settings_changed.connect(
            lambda enabled, zh, en: changes.append((enabled, zh, en)))
        try:
            assert sw.tts_zh_model_combo.isEnabled()
            assert sw.tts_en_model_combo.isEnabled()
            assert sw.tts_zh_model_combo.currentData() == "tts-icefall-zh-aishell3"
            assert sw.tts_en_model_combo.currentData() == "tts-icefall-en-ljspeech-low"
            bilingual = sw.tts_zh_model_combo.findData("tts-melo-zh-en")
            assert bilingual >= 0
            sw.tts_zh_model_combo.setCurrentIndex(bilingual)
            assert store.get("tts_model_id_zh") == "tts-melo-zh-en"
            assert changes[-1][1] == "tts-melo-zh-en"
        finally:
            sw.close()
            sw.deleteLater()
    def test_device_and_process_selection_persist(self, qapp, tmp_path, monkeypatch):
        import voxsub.audio as audio
        import voxsub.process_audio as process_audio
        from voxsub.audio import AudioDeviceInfo
        from voxsub.process_audio import CaptureTarget
        from voxsub.ui.settings_window import SettingsWindow

        class _Device:
            def __init__(self, device_id: str, name: str):
                self.id, self.name = device_id, name

        mic = _Device("mic-1", "USB 麦克风")
        output = _Device("out-1", "会议耳机")
        monkeypatch.setattr(audio, "list_microphones",
                            lambda: [AudioDeviceInfo(mic.name, "mic", mic)])
        monkeypatch.setattr(audio, "list_loopbacks",
                            lambda: [AudioDeviceInfo(output.name, "loopback", output)])
        monkeypatch.setattr(process_audio, "list_capture_targets",
                            lambda: [CaptureTarget(4242, "Meeting.exe", "周会")])

        store = ConfigStore(tmp_path / "config.json")
        sw = SettingsWindow(store=store)
        try:
            sw.mic_combo.setCurrentIndex(1)
            sw.output_combo.setCurrentIndex(1)
            sw.process_combo.setCurrentIndex(1)
            assert store.get("mic_device_id") == "mic-1"
            assert store.get("loopback_device_id") == "out-1"
            assert store.get("capture_process_id") == 4242
            assert "Meeting.exe" in store.get("capture_window_title")
        finally:
            sw.close()
            sw.deleteLater()


# ===========================================================================
# 10. 模型广场（硬件推荐 / 性能排序 / 下载源）
# ===========================================================================
class TestModelHubWindow:
    def test_construct_sorted_cards_and_four_level_legend(self, qapp, tmp_path):
        from PySide6.QtWidgets import QLabel

        from voxsub.model_catalog import CATALOG, HardwareProfile, ModelMarketplace
        from voxsub.ui.model_hub_window import ModelHubWindow

        profile = HardwareProfile("test cpu", 8, 16, 32.0, "RTX 4060", 8.0, "CUDA")
        hub = ModelHubWindow(
            store=ConfigStore(tmp_path / "config.json"),
            marketplace=ModelMarketplace(tmp_path / "models"),
            profile=profile,
        )
        try:
            assert hub.windowTitle().startswith("模型广场")
            assert len(hub._cards) == len(CATALOG) >= 11  # noqa: SLF001
            scores = [card.model.quality_score for card in hub._cards.values()]  # noqa: SLF001
            assert scores == sorted(scores, reverse=True)
            texts = [label.text() for label in hub.findChildren(QLabel)]
            for level in ("不推荐", "较为推荐", "推荐", "满载"):
                assert level in texts
            assert hub.source_combo.count() == 3
            assert all(not card.progress.isTextVisible() for card in hub._cards.values())  # noqa: SLF001
            assert all(isinstance(button, PillChoiceButton)
                       for button in hub.filter_buttons.values())
            assert set(hub.filter_buttons) == {
                "all", "asr", "translate", "tts", "ocr"}
            hub.set_filter("tts")
            assert set(hub._cards) == {  # noqa: SLF001
                "tts-melo-zh-en",
                "tts-icefall-zh-aishell3",
                "tts-icefall-en-ljspeech-low",
            }
            assert all(button.isCheckable() for button in hub.filter_buttons.values())
            assert all(card.npu_badge.text().startswith("NPU ")
                       for card in hub._cards.values())  # noqa: SLF001
            assert all(card.npu_badge.toolTip()
                       for card in hub._cards.values())  # noqa: SLF001
        finally:
            hub.close()
            hub.deleteLater()


# ===========================================================================
# 11. 诊断页骨架（真实 voxsub.diagnostics 已由 M8 实现 → 渲染结果卡；
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
