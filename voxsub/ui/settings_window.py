"""设置页（M7 组件清单 #4，独立窗口）。

四个 Tab：
- 翻译：档位单选（快档 / 质量档 / 云 API）+ 云 API Key（QLineEdit password）+
  BaseURL（OpenAI 兼容端点，白名单校验留给 M6）
- 语音：朗读开关（TTS）
- 外观：主题三档（浅色 / 深色 / 跟随系统），改动即时应用（load_theme）
- 关于：版本 / 技术栈信息

所有改动立即写入 ConfigStore（不设保存按钮的瞬时一致性；另提供「重置默认」）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from voxsub.ui.config_store import ConfigStore
from voxsub.ui.theme import AppTheme, DESIGN_TOKENS, load_theme
from voxsub import __version__ as _PKG_VERSION


class SettingsWindow(QWidget):
    """语幕设置页（独立窗口，QTabWidget 四页）。"""

    def __init__(
        self,
        store: ConfigStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store or ConfigStore()
        self.setObjectName("settingsWindow")
        self.setWindowTitle("设置 — 语幕 VoxSub")
        self.resize(640, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("settingsTabs")
        self.tabs.addTab(self._build_translate_tab(), "翻译")
        self.tabs.addTab(self._build_voice_tab(), "语音")
        self.tabs.addTab(self._build_appearance_tab(), "外观")
        self.tabs.addTab(self._build_about_tab(), "关于")
        root.addWidget(self.tabs, 1)

        self._load_from_store()

    # ------------------------------------------------------------------
    # Tab 构建
    # ------------------------------------------------------------------
    def _card(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        card = QWidget(self)
        card.setObjectName("settingsCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(10)
        head = QLabel(title, card)
        head.setObjectName("sectionTitle")
        lay.addWidget(head)
        return card, lay

    def _build_translate_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        card, box = self._card("翻译档位")
        self.tier_fast = QRadioButton("快档（本地 OPUS-MT，<0.5s/句）", card)
        self.tier_quality = QRadioButton("质量档（本地 Qwen 1.5B，2-5s/句）", card)
        self.tier_cloud = QRadioButton("云 API（OpenAI 兼容端点）", card)
        for rb in (self.tier_fast, self.tier_quality, self.tier_cloud):
            rb.setObjectName("tierRadio")
            box.addWidget(rb)
        self.tier_fast.toggled.connect(lambda on: self._on_tier_changed(on, "fast"))
        self.tier_quality.toggled.connect(lambda on: self._on_tier_changed(on, "quality"))
        self.tier_cloud.toggled.connect(lambda on: self._on_tier_changed(on, "cloud"))
        lay.addWidget(card)

        cloud_card, cloud_box = self._card("云 API 配置（仅云档生效）")
        api_label = QLabel("API Key", cloud_card)
        api_label.setObjectName("fieldLabel")
        self.api_key_edit = QLineEdit(cloud_card)
        self.api_key_edit.setObjectName("inputBox")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        self.api_key_edit.setEnabled(False)
        url_label = QLabel("BaseURL（OpenAI 兼容）", cloud_card)
        url_label.setObjectName("fieldLabel")
        self.base_url_edit = QLineEdit(cloud_card)
        self.base_url_edit.setObjectName("inputBox")
        self.base_url_edit.setPlaceholderText("https://api.deepseek.com/v1")
        self.base_url_edit.setEnabled(False)
        cloud_box.addWidget(api_label)
        cloud_box.addWidget(self.api_key_edit)
        cloud_box.addWidget(url_label)
        cloud_box.addWidget(self.base_url_edit)
        lay.addWidget(cloud_card)
        lay.addStretch(1)
        return page

    def _build_voice_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        card, box = self._card("语音朗读")
        self.tts_switch = QRadioButton("朗读译文（本地 TTS；失败自动降级为仅字幕）", card)
        box.addWidget(self.tts_switch)
        self.tts_switch.toggled.connect(self._on_tts_toggled)
        lay.addWidget(card)
        lay.addStretch(1)
        return page

    def _build_appearance_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        card, box = self._card("主题")
        self.theme_light = QRadioButton("浅色", card)
        self.theme_dark = QRadioButton("深色", card)
        self.theme_system = QRadioButton("跟随系统", card)
        for rb in (self.theme_light, self.theme_dark, self.theme_system):
            box.addWidget(rb)
        # 色板预览
        swatch_row = QHBoxLayout()
        t = DESIGN_TOKENS
        for name, color in (
            ("基底", t["light"]["bg_base"]),
            ("surface", t["dark"]["surface_1"]),
            ("accent", t["dark"]["accent"]),
        ):
            sw = QLabel(name, card)
            sw.setObjectName("fieldLabel")
            dot = QLabel("■", card)
            dot.setStyleSheet(f"color: {color}; font-size: 12px;")
            swatch_row.addWidget(dot)
            swatch_row.addWidget(sw)
            swatch_row.addSpacing(8)
        swatch_row.addStretch(1)
        box.addLayout(swatch_row)
        self.theme_light.toggled.connect(lambda on: self._on_theme_changed(on, AppTheme.LIGHT))
        self.theme_dark.toggled.connect(lambda on: self._on_theme_changed(on, AppTheme.DARK))
        self.theme_system.toggled.connect(lambda on: self._on_theme_changed(on, AppTheme.SYSTEM))
        lay.addWidget(card)
        lay.addStretch(1)
        return page

    def _build_about_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)
        card, box = self._card("关于")
        lines = [
            ("语幕 VoxSub", "一款面向大众的实时翻译字幕工具（Windows）"),
            (f"版本", f"ui {__import__('voxsub.ui', fromlist=['__version__']).__version__} · 核心包 {_PKG_VERSION}"),
            ("技术栈", "Python 3.11 · PySide6 · QFluentWidgets · sherpa-onnx"),
            ("隐私", "音频仅存在于内存流水线，不落盘录音"),
        ]
        for key, val in lines:
            row = QHBoxLayout()
            k = QLabel(key, card)
            k.setObjectName("fieldLabel")
            v = QLabel(val, card)
            v.setWordWrap(True)
            row.addWidget(k, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(v, 1)
            box.addLayout(row)
        lay.addWidget(card)
        lay.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # 状态装载 / 变更
    # ------------------------------------------------------------------
    def _load_from_store(self) -> None:
        cfg = self._store.load()
        tier = cfg.get("translate_tier", "fast")
        target = {"fast": self.tier_fast, "quality": self.tier_quality, "cloud": self.tier_cloud}
        rb = target.get(tier, self.tier_fast)
        rb.setChecked(True)
        self.api_key_edit.setText(cfg.get("api_key", ""))
        self.base_url_edit.setText(cfg.get("base_url", ""))
        self._set_cloud_enabled(tier == "cloud")

        self.tts_switch.setChecked(bool(cfg.get("tts_enabled", True)))

        theme = cfg.get("theme", "system")
        theme_target = {
            "light": self.theme_light,
            "dark": self.theme_dark,
            "system": self.theme_system,
        }
        theme_target.get(theme, self.theme_system).setChecked(True)

    def _on_tier_changed(self, checked: bool, tier: str) -> None:
        if not checked:
            return
        self._store.set("translate_tier", tier)
        self._set_cloud_enabled(tier == "cloud")

    def _set_cloud_enabled(self, enabled: bool) -> None:
        self.api_key_edit.setEnabled(enabled)
        self.base_url_edit.setEnabled(enabled)

    def _on_tts_toggled(self, checked: bool) -> None:
        self._store.set("tts_enabled", bool(checked))

    def _on_theme_changed(self, checked: bool, theme: AppTheme) -> None:
        if not checked:
            return
        self._store.set("theme", theme.value)
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            load_theme(app, theme)

    # 测试 / 程序化访问入口
    def current_tier(self) -> str:
        if self.tier_fast.isChecked():
            return "fast"
        if self.tier_quality.isChecked():
            return "quality"
        if self.tier_cloud.isChecked():
            return "cloud"
        return "fast"

    def save_cloud_credentials(self) -> None:
        """保存云 API Key / BaseURL（QLineEdit 失焦或设置页关闭时由 app.py 调用）。"""
        self._store.update(
            {"api_key": self.api_key_edit.text().strip(), "base_url": self.base_url_edit.text().strip()}
        )