"""设置页（M7 组件清单 #4，独立窗口）。

六个 Tab：
- 翻译：档位单选（快档 / 质量档 / 云 API）+ 云 API Key（QLineEdit password）+
  BaseURL（OpenAI 兼容端点，白名单校验留给 M6）
- 语音：朗读开关（TTS）
- 外观：主题三档（浅色 / 深色 / 跟随系统），改动即时应用（load_theme）
- 关于：版本 / 技术栈信息

通用设置保持即时生效；识别调优采用明确的「保存 / 放弃」事务，
关闭窗口时未保存的调优会被放弃。
"""
from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from voxsub.ui.config_store import ConfigStore
from voxsub.ui.theme import AppTheme, DESIGN_TOKENS, load_theme
from voxsub import __version__ as _PKG_VERSION
from voxsub.logging_setup import get_logger
from voxsub.logging_setup import set_debug_mode

logger = get_logger("ui.settings_window")


class _InfoButton(QToolButton):
    """Hover-only explanation chip for non-technical users."""

    def __init__(self, title: str, explanation: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._title = title
        self._explanation = explanation
        self._tooltip_html = (
            '<div style="width: 320px; white-space: normal;">'
            f"<b>{escape(title)}</b><br>{escape(explanation)}</div>"
        )
        self.setText("i")
        self.setToolTip(self._tooltip_html)
        self.setAccessibleName(f"{title}说明")
        self.setAccessibleDescription(explanation)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(24, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QToolButton { border: 1px solid rgba(127,127,127,0.35); border-radius: 12px;"
            " font-weight: 700; background: transparent; }"
            "QToolButton:hover { border-color: #14B8A6; color: #14B8A6; }"
        )
    def enterEvent(self, event) -> None:  # noqa: N802
        # Show immediately beside the small i chip.  A tooltip keeps the
        # explanation lightweight and never interrupts the settings workflow.
        QToolTip.showText(
            self.mapToGlobal(self.rect().bottomLeft()),
            self._tooltip_html,
            self,
            self.rect(),
            15_000,
        )
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        QToolTip.hideText()
        super().leaveEvent(event)


class SettingsWindow(QWidget):
    """语幕设置页（独立窗口，QTabWidget 六页）。"""

    model_hub_requested = Signal()

    def __init__(
        self,
        store: ConfigStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store or ConfigStore()
        self._loading = True
        self._tuning_snapshot: dict[str, object] = {}
        self._tuning_dirty = False
        self.setObjectName("settingsWindow")
        self.setWindowTitle("设置 — 语幕 VoxSub")
        self.resize(680, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("settingsTabs")
        self.tabs.addTab(self._build_translate_tab(), "翻译")
        self.tabs.addTab(self._build_recognition_tab(), "识别调优")
        self.tabs.addTab(self._build_voice_tab(), "语音")
        self.tabs.addTab(self._build_device_tab(), "设备")
        self.tabs.addTab(self._build_appearance_tab(), "外观")
        self.tabs.addTab(self._build_about_tab(), "关于")
        root.addWidget(self.tabs, 1)

        self._load_from_store()
        self._loading = False

    # ------------------------------------------------------------------
    # Tab 构建
    # ------------------------------------------------------------------
    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(self)
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
        self.tier_quality = QRadioButton("质量档（使用模型广场中选择的专用翻译模型）", card)
        self.tier_cloud = QRadioButton("云 API（OpenAI 兼容端点）", card)
        for rb in (self.tier_fast, self.tier_quality, self.tier_cloud):
            rb.setObjectName("tierRadio")
            box.addWidget(rb)
        self.tier_fast.toggled.connect(lambda on: self._on_tier_changed(on, "fast"))
        self.tier_quality.toggled.connect(lambda on: self._on_tier_changed(on, "quality"))
        self.tier_cloud.toggled.connect(lambda on: self._on_tier_changed(on, "cloud"))
        self.model_hub_btn = QPushButton("打开模型广场", card)
        self.model_hub_btn.setObjectName("secondaryButton")
        self.model_hub_btn.clicked.connect(self.model_hub_requested.emit)
        box.addWidget(self.model_hub_btn)
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

    def _tuning_row(self, form: QFormLayout, label: str, control: QWidget,
                    explanation: str) -> None:
        holder = QWidget(self)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(control, 1)
        row.addWidget(_InfoButton(label, explanation, holder), 0)
        form.addRow(label, holder)

    def _build_recognition_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        intro_card, intro = self._card("识别模型调优")
        note = QLabel(
            "这里调整的是模型如何听、何时断句以及一次考虑多少候选，"
            "不是重新训练模型。没有 AI 背景时保持“自动”即可。",
            intro_card,
        )
        note.setObjectName("secondaryLabel")
        note.setWordWrap(True)
        intro.addWidget(note)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        self.asr_profile_combo = QComboBox(intro_card)
        self.asr_profile_combo.setObjectName("inputBox")
        self.asr_profile_combo.setMinimumHeight(42)
        for label, value in (
            ("自动（按当前模型选择）", "auto"),
            ("响应优先", "responsive"),
            ("均衡", "balanced"),
            ("准确优先", "accuracy"),
            ("自定义", "custom"),
        ):
            self.asr_profile_combo.addItem(label, value)
        self._tuning_row(
            form, "调优预设", self.asr_profile_combo,
            "自动会根据 Zipformer 或 Qwen3-ASR 选择合适参数。响应优先出字更快，"
            "但更容易把一句话切碎；准确优先会多等一会儿，通常上下文更完整。",
        )

        self.vad_threshold_spin = QDoubleSpinBox(intro_card)
        self.vad_threshold_spin.setRange(0.01, 0.99)
        self.vad_threshold_spin.setSingleStep(0.01)
        self.vad_threshold_spin.setDecimals(2)
        self.vad_threshold_spin.setObjectName("inputBox")
        self._tuning_row(
            form, "语音灵敏度", self.vad_threshold_spin,
            "数值越低，轻声和远处说话越容易被听见，但键盘声、风扇声也更可能被当成语音；"
            "数值越高，环境噪声更少，但小声说话可能漏掉。普通室内建议 0.30–0.50。",
        )

        self.silence_spin = QSpinBox(intro_card)
        self.silence_spin.setRange(50, 5000)
        self.silence_spin.setSingleStep(10)
        self.silence_spin.setSuffix(" ms")
        self.silence_spin.setObjectName("inputBox")
        self._tuning_row(
            form, "停顿多久断句", self.silence_spin,
            "说话停顿达到这个时间后，当前一句才会送去识别。调小会更快出字幕，"
            "适合语速快、照稿念的视频；但 50–150ms 很容易把换气切成碎句。"
            "调大会保留更多上下文，但字幕会晚一些。",
        )

        self.max_segment_spin = QDoubleSpinBox(intro_card)
        self.max_segment_spin.setRange(1.0, 120.0)
        self.max_segment_spin.setSingleStep(0.5)
        self.max_segment_spin.setDecimals(1)
        self.max_segment_spin.setSuffix(" 秒")
        self.max_segment_spin.setObjectName("inputBox")
        self._tuning_row(
            form, "单句最长时长", self.max_segment_spin,
            "遇到长时间不停顿的讲话时，达到这个长度会强制切一次，防止一直没有字幕。"
            "太短会从句子中间截断；太长会增加等待时间和内存占用。Qwen3-ASR 建议 10–20 秒。",
        )

        self.beam_paths_spin = QSpinBox(intro_card)
        self.beam_paths_spin.setRange(1, 16)
        self.beam_paths_spin.setObjectName("inputBox")
        self._tuning_row(
            form, "识别候选数", self.beam_paths_spin,
            "主要影响 Zipformer。数值越大，模型会比较更多可能的文字，可能更准确，"
            "但会更耗 CPU；Qwen3-ASR 当前使用自己的生成方式，这一项对它不生效。",
        )

        self.max_tokens_spin = QSpinBox(intro_card)
        self.max_tokens_spin.setRange(32, 4096)
        self.max_tokens_spin.setSingleStep(32)
        self.max_tokens_spin.setObjectName("inputBox")
        self._tuning_row(
            form, "单句最大文字量", self.max_tokens_spin,
            "主要影响 Qwen3-ASR / Fun-ASR。数值太小会让很长的一句在结尾被截断；"
            "调得更大只是允许输出更长，不会自动提高准确率，并可能让异常片段生成更久。",
        )

        self.hotwords_edit = QLineEdit(intro_card)
        self.hotwords_edit.setObjectName("inputBox")
        self.hotwords_edit.setPlaceholderText("例如：VoxSub, 肿瘤免疫, 张三")
        self._tuning_row(
            form, "常用词 / 专有名词", self.hotwords_edit,
            "把人名、产品名、医学或游戏术语用英文逗号分开。模型会更留意这些读音相近的词。"
            "不要粘贴整段文章，词太多反而可能干扰普通内容。",
        )
        intro.addLayout(form)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.tuning_state_label = QLabel("未修改", intro_card)
        self.tuning_state_label.setObjectName("secondaryLabel")
        action_row.addWidget(self.tuning_state_label, 1)
        self.tuning_reset_btn = QPushButton("恢复自动", intro_card)
        self.tuning_reset_btn.setObjectName("secondaryButton")
        self.tuning_reset_btn.clicked.connect(self._reset_asr_tuning)
        self.tuning_discard_btn = QPushButton("放弃更改", intro_card)
        self.tuning_discard_btn.setObjectName("ghostButton")
        self.tuning_discard_btn.clicked.connect(self._discard_asr_tuning)
        self.tuning_save_btn = QPushButton("保存调优", intro_card)
        self.tuning_save_btn.setObjectName("primaryButton")
        self.tuning_save_btn.clicked.connect(self._save_asr_tuning)
        for button in (self.tuning_reset_btn, self.tuning_discard_btn,
                       self.tuning_save_btn):
            button.setMinimumHeight(40)
            action_row.addWidget(button)
        intro.addLayout(action_row)
        lay.addWidget(intro_card)
        lay.addStretch(1)

        self.asr_profile_combo.currentIndexChanged.connect(self._on_asr_profile_changed)
        for spin in (self.vad_threshold_spin, self.silence_spin,
                     self.max_segment_spin, self.beam_paths_spin,
                     self.max_tokens_spin):
            spin.valueChanged.connect(self._mark_asr_tuning_dirty)
        self.hotwords_edit.textChanged.connect(self._mark_asr_tuning_dirty)
        return page

    def _build_voice_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        card, box = self._card("语音朗读")
        self.tts_switch = QCheckBox("朗读译文（本地 TTS；失败自动降级为仅字幕）", card)
        self.tts_switch.setMinimumHeight(44)
        box.addWidget(self.tts_switch)
        self.tts_switch.toggled.connect(self._on_tts_toggled)
        lay.addWidget(card)
        lay.addStretch(1)
        return page

    def _build_device_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        audio_card, audio_box = self._card("实时音频设备")
        mic_label = QLabel("麦克风输入（A 模式）", audio_card)
        mic_label.setObjectName("fieldLabel")
        self.mic_combo = QComboBox(audio_card)
        self.mic_combo.setObjectName("inputBox")
        self.mic_combo.setMinimumHeight(44)
        output_label = QLabel("系统输出（B 模式监听全部声音时）", audio_card)
        output_label.setObjectName("fieldLabel")
        self.output_combo = QComboBox(audio_card)
        self.output_combo.setObjectName("inputBox")
        self.output_combo.setMinimumHeight(44)
        audio_box.addWidget(mic_label)
        audio_box.addWidget(self.mic_combo)
        audio_box.addWidget(output_label)
        audio_box.addWidget(self.output_combo)
        lay.addWidget(audio_card)

        app_card, app_box = self._card("应用声音隔离")
        note = QLabel(
            "选择窗口后，只捕获该应用及其子进程的声音；其它系统声音不会进入字幕。"
            "选择「全部系统声音」时使用上面的输出设备。",
            app_card,
        )
        note.setObjectName("secondaryLabel")
        note.setWordWrap(True)
        self.process_combo = QComboBox(app_card)
        self.process_combo.setObjectName("inputBox")
        self.process_combo.setMinimumHeight(44)
        self.refresh_devices_btn = QPushButton("刷新设备与窗口", app_card)
        self.refresh_devices_btn.setObjectName("secondaryButton")
        self.refresh_devices_btn.setMinimumHeight(44)
        self.refresh_devices_btn.clicked.connect(self.refresh_devices)
        app_box.addWidget(note)
        app_box.addWidget(self.process_combo)
        app_box.addWidget(self.refresh_devices_btn)
        lay.addWidget(app_card)
        lay.addStretch(1)

        self.mic_combo.currentIndexChanged.connect(self._on_devices_changed)
        self.output_combo.currentIndexChanged.connect(self._on_devices_changed)
        self.process_combo.currentIndexChanged.connect(self._on_devices_changed)
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
            ("隐私", "默认音频仅在内存处理；仅当你打开“同时录音”时保存本地 WAV"),
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
        debug_label = QLabel("开发与排障", card)
        debug_label.setObjectName("fieldLabel")
        self.debug_switch = QCheckBox("启用内置调试模式（实时显示详细日志）", card)
        self.debug_switch.setMinimumHeight(44)
        self.debug_switch.toggled.connect(self._on_debug_toggled)
        box.addSpacing(8)
        box.addWidget(debug_label)
        box.addWidget(self.debug_switch)
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
        self.api_key_edit.editingFinished.connect(self.save_cloud_credentials)
        self.base_url_edit.editingFinished.connect(self.save_cloud_credentials)
        self._set_cloud_enabled(tier == "cloud")

        tuning = self._tuning_from_config(cfg)
        self._apply_tuning_to_controls(tuning)
        self._tuning_snapshot = dict(tuning)
        self._set_tuning_dirty(False)

        self.tts_switch.setChecked(bool(cfg.get("tts_enabled", True)))
        self.debug_switch.setChecked(bool(cfg.get("debug_mode", False)))

        self.refresh_devices()

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

    @staticmethod
    def _asr_preset_values(profile: str) -> dict[str, float | int] | None:
        return {
            "responsive": {"vad": 0.45, "silence": 350, "max_s": 6.0,
                           "beam": 2, "tokens": 256},
            "balanced": {"vad": 0.35, "silence": 650, "max_s": 12.0,
                         "beam": 4, "tokens": 512},
            "accuracy": {"vad": 0.25, "silence": 900, "max_s": 20.0,
                         "beam": 6, "tokens": 512},
        }.get(profile)

    @staticmethod
    def _tuning_from_config(cfg: dict) -> dict[str, object]:
        return {
            "asr_tuning_profile": str(cfg.get("asr_tuning_profile", "auto")),
            "asr_vad_threshold": float(cfg.get("asr_vad_threshold", 0.35)),
            "asr_silence_ms": int(cfg.get("asr_silence_ms", 650)),
            "asr_max_utterance_ms": int(cfg.get("asr_max_utterance_ms", 12000)),
            "asr_beam_paths": int(cfg.get("asr_beam_paths", 4)),
            "asr_max_new_tokens": int(cfg.get("asr_max_new_tokens", 512)),
            "asr_hotwords": str(cfg.get("asr_hotwords", "")),
        }

    def _collect_asr_tuning(self) -> dict[str, object]:
        return {
            "asr_tuning_profile": str(self.asr_profile_combo.currentData() or "auto"),
            "asr_vad_threshold": float(self.vad_threshold_spin.value()),
            "asr_silence_ms": int(self.silence_spin.value()),
            "asr_max_utterance_ms": int(round(self.max_segment_spin.value() * 1000)),
            "asr_beam_paths": int(self.beam_paths_spin.value()),
            "asr_max_new_tokens": int(self.max_tokens_spin.value()),
            "asr_hotwords": self.hotwords_edit.text().strip(),
        }

    def _apply_tuning_to_controls(self, values: dict[str, object]) -> None:
        was_loading = self._loading
        self._loading = True
        try:
            profile = str(values.get("asr_tuning_profile", "auto"))
            idx = self.asr_profile_combo.findData(profile)
            self.asr_profile_combo.setCurrentIndex(max(0, idx))
            self.vad_threshold_spin.setValue(float(values.get("asr_vad_threshold", 0.35)))
            self.silence_spin.setValue(int(values.get("asr_silence_ms", 650)))
            self.max_segment_spin.setValue(
                int(values.get("asr_max_utterance_ms", 12000)) / 1000.0)
            self.beam_paths_spin.setValue(int(values.get("asr_beam_paths", 4)))
            self.max_tokens_spin.setValue(int(values.get("asr_max_new_tokens", 512)))
            self.hotwords_edit.setText(str(values.get("asr_hotwords", "")))
            self._on_asr_profile_changed(self.asr_profile_combo.currentIndex())
        finally:
            self._loading = was_loading

    def _set_tuning_dirty(self, dirty: bool) -> None:
        self._tuning_dirty = bool(dirty)
        if not hasattr(self, "tuning_state_label"):
            return
        self.tuning_state_label.setText(
            "有未保存的更改" if dirty else "已保存")
        self.tuning_save_btn.setEnabled(dirty)
        self.tuning_discard_btn.setEnabled(dirty)

    def _mark_asr_tuning_dirty(self, *_args) -> None:
        if not self._loading:
            self._set_tuning_dirty(self._collect_asr_tuning() != self._tuning_snapshot)

    def _on_asr_profile_changed(self, _index: int) -> None:
        profile = str(self.asr_profile_combo.currentData() or "auto")
        values = self._asr_preset_values(profile)
        if values is not None:
            controls = (
                (self.vad_threshold_spin, values["vad"]),
                (self.silence_spin, values["silence"]),
                (self.max_segment_spin, values["max_s"]),
                (self.beam_paths_spin, values["beam"]),
                (self.max_tokens_spin, values["tokens"]),
            )
            for control, value in controls:
                control.blockSignals(True)
                control.setValue(value)
                control.blockSignals(False)
        custom = profile == "custom"
        for control in (self.vad_threshold_spin, self.silence_spin,
                        self.max_segment_spin, self.beam_paths_spin,
                        self.max_tokens_spin):
            control.setEnabled(custom)
        self._mark_asr_tuning_dirty()

    def _save_asr_tuning(self, *_args) -> None:
        if self._loading:
            return
        values = self._collect_asr_tuning()
        self._store.update(values)
        self._tuning_snapshot = dict(values)
        self._set_tuning_dirty(False)
        self.tuning_state_label.setText("已保存 · 下次开始时生效")

    def _discard_asr_tuning(self) -> None:
        values = self._tuning_from_config(self._store.load())
        self._apply_tuning_to_controls(values)
        self._tuning_snapshot = dict(values)
        self._set_tuning_dirty(False)

    def _reset_asr_tuning(self) -> None:
        self.asr_profile_combo.setCurrentIndex(self.asr_profile_combo.findData("auto"))
        for control, value in (
            (self.vad_threshold_spin, 0.35),
            (self.silence_spin, 650),
            (self.max_segment_spin, 12.0),
            (self.beam_paths_spin, 4),
            (self.max_tokens_spin, 512),
        ):
            control.blockSignals(True)
            control.setValue(value)
            control.blockSignals(False)
        self.hotwords_edit.clear()
        self._mark_asr_tuning_dirty()

    def _on_debug_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._store.set("debug_mode", bool(checked))
        set_debug_mode(bool(checked))

    @staticmethod
    def _select_data(combo: QComboBox, value) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    def refresh_devices(self) -> None:
        """重新枚举麦克风、输出端点和可见应用窗口。"""
        cfg = self._store.load()
        for combo in (self.mic_combo, self.output_combo, self.process_combo):
            combo.blockSignals(True)
            combo.clear()
        self.mic_combo.addItem("系统默认麦克风", "")
        self.output_combo.addItem("系统默认输出", "")
        self.process_combo.addItem("全部系统声音", 0)
        try:
            from voxsub.audio import list_loopbacks, list_microphones

            for info in list_microphones():
                self.mic_combo.addItem(info.name, info.id)
            for info in list_loopbacks():
                self.output_combo.addItem(info.name, info.id)
        except Exception:
            logger.exception("枚举音频设备失败")
        try:
            from voxsub.process_audio import list_capture_targets

            for target in list_capture_targets():
                self.process_combo.addItem(target.label, target.pid)
        except Exception:
            logger.exception("枚举应用窗口失败")

        mic_id = str(cfg.get("mic_device_id", ""))
        output_id = str(cfg.get("loopback_device_id", ""))
        process_id = int(cfg.get("capture_process_id", 0) or 0)
        self._select_data(self.mic_combo, mic_id)
        self._select_data(self.output_combo, output_id)
        self._select_data(self.process_combo, process_id)
        if process_id and self.process_combo.currentData() != process_id:
            title = str(cfg.get("capture_window_title", "") or f"PID {process_id}")
            self.process_combo.addItem(f"已退出：{title}", process_id)
            self.process_combo.setCurrentIndex(self.process_combo.count() - 1)
        for combo in (self.mic_combo, self.output_combo, self.process_combo):
            combo.blockSignals(False)
        if not self._loading:
            self._on_devices_changed()

    def _on_devices_changed(self, _index: int = -1) -> None:
        if self._loading:
            return
        self._store.update({
            "mic_device_id": str(self.mic_combo.currentData() or ""),
            "loopback_device_id": str(self.output_combo.currentData() or ""),
            "capture_process_id": int(self.process_combo.currentData() or 0),
            "capture_window_title": (self.process_combo.currentText()
                                     if int(self.process_combo.currentData() or 0) else ""),
        })

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

    def closeEvent(self, event) -> None:  # noqa: N802
        # Recognition tuning is transactional: X/Alt+F4 always abandons the
        # draft unless the explicit Save button was used.
        self._discard_asr_tuning()
        self.save_cloud_credentials()
        super().closeEvent(event)
