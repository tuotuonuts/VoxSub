"""设置页（M7 组件清单 #4，可嵌入主窗的二级页面）。

六个 Tab：
- 翻译：STT 来源与翻译来源独立选择，云 STT / 云翻译各自配置模型、Key、BaseURL
- 语音：朗读开关（TTS）
- 外观：主题三档（浅色 / 深色 / 跟随系统），改动即时应用（load_theme）
- 关于：版本 / 技术栈信息

通用设置保持即时生效；识别调优采用明确的「保存 / 放弃」事务，
关闭窗口时未保存的调优会被放弃。
"""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from voxsub.config_store import ConfigStore
from voxsub.error_reporting import (
    is_error_reporting_enabled,
    reload_error_reporting,
)
from voxsub.ui.i18n import (
    LANGUAGE_EN,
    LANGUAGE_SYSTEM,
    LANGUAGE_ZH,
    language_manager,
    retranslate_widget_tree,
    tr,
    translate_existing,
)
from voxsub.ui.selection_controls import RoundRadioButton, ToggleSwitch
from voxsub.ui.theme import AppTheme, DESIGN_TOKENS, load_theme
from voxsub.ui.view_models import RecognitionTuningDraft
from voxsub import __version__ as _PKG_VERSION
from voxsub.logging_setup import get_logger
from voxsub.logging_setup import start_diagnostic_session, stop_diagnostic_session
from voxsub.model_catalog import ModelMarketplace, get_model, models_for_task
from voxsub.model_storage import migrate_models, resolve_models_root
from voxsub.ocr_cache import (
    OcrCacheLocationError,
    cache_from_store,
    resolve_ocr_cache_root,
    validate_ocr_cache_root,
)
from voxsub.ui.release_notes import release_history_text

logger = get_logger("ui.settings_window")


class _ReliableSpinButtonMixin:
    """Use explicit step buttons instead of unreliable native spin subcontrols.

    Qt's Windows style can paint an upper arrow that is not part of the real
    hit-test region after the application stylesheet is applied.  A test that
    clicks the style's reported rectangle therefore passes while a physical
    click does nothing.  Child tool buttons have their own hit targets,
    pressed state and auto-repeat, so their behavior is independent of the
    platform spinbox style.
    """

    def _install_spin_step_buttons(self) -> None:
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._voxsub_step_up_btn = QToolButton(self)
        self._voxsub_step_down_btn = QToolButton(self)
        for button, direction, callback, label in (
            (self._voxsub_step_up_btn, Qt.ArrowType.UpArrow, self.stepUp,
             tr("增加", "Increase")),
            (self._voxsub_step_down_btn, Qt.ArrowType.DownArrow, self.stepDown,
             tr("减小", "Decrease")),
        ):
            button.setObjectName("spinStepButton")
            button.setProperty(
                "stepDirection",
                "up" if direction == Qt.ArrowType.UpArrow else "down",
            )
            button.setArrowType(direction)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(350)
            button.setAutoRepeatInterval(75)
            button.setAccessibleName(label)
            button.setToolTip(label)
            button.clicked.connect(callback)
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.setTextMargins(0, 0, 30, 0)
        self._layout_spin_step_buttons()

    def _layout_spin_step_buttons(self) -> None:
        margin = 3
        width = 28
        available = max(2, self.height() - margin * 2)
        upper_height = available // 2
        lower_height = available - upper_height
        x = max(margin, self.width() - width - margin)
        self._voxsub_step_up_btn.setGeometry(x, margin, width, upper_height)
        self._voxsub_step_down_btn.setGeometry(
            x, margin + upper_height, width, lower_height)
        self._voxsub_step_up_btn.raise_()
        self._voxsub_step_down_btn.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_spin_step_buttons()


class _ReliableSpinBox(_ReliableSpinButtonMixin, QSpinBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._install_spin_step_buttons()


class _ReliableDoubleSpinBox(_ReliableSpinButtonMixin, QDoubleSpinBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._install_spin_step_buttons()


class _InfoButton(QToolButton):
    """Hover-only explanation chip for non-technical users."""

    def __init__(self, title: str, explanation: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._title = title
        self._explanation = explanation
        language_manager.language_changed.connect(self.retranslate_ui)
        self.setText("i")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(24, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("infoButton")
        self.retranslate_ui()

    def retranslate_ui(self, *_args) -> None:
        title = translate_existing(self._title)
        explanation = translate_existing(self._explanation)
        if language_manager.language == LANGUAGE_EN:
            explanation = {
                "调优预设": "Automatic chooses suitable values for Zipformer or Qwen3-ASR. Low latency shows text sooner but may split one sentence; Accuracy waits longer for more context.",
                "语音灵敏度": "Lower values catch quiet or distant speech more easily but may mistake keyboard or fan noise for speech. Higher values reject more noise but may miss quiet speech.",
                "停顿多久断句": "After speech has been quiet for this long, the current sentence is sent for recognition. Lower values reduce latency; higher values preserve more context.",
                "上下文最长等待": "In Smart Context mode, an incomplete phrase can wait this much longer for the speaker to continue. A hard limit always commits the text.",
                "实时双语草稿": "Continuously updates the current source and translation before the selected recognizer commits its corrected final. Turn it off to reduce CPU and memory use while keeping Smart Context segmentation and correction.",
                "上下文保守纠偏": "Uses your common words and repeatedly established recent terms for small auditable corrections. It never freely rewrites or invents content.",
                "语气词清理": "Light cleanup removes only isolated fillers such as um or uh. Meaningful sentence-final particles and the raw recognized text are preserved.",
                "单句最长时长": "If someone speaks without pausing, this limit forces a split so subtitles keep moving. Too short can cut a sentence; too long increases delay and memory use.",
                "识别候选数": "Mainly affects Zipformer. More candidates can improve accuracy but use more CPU. Qwen3-ASR uses its own generation path, so this setting does not affect it.",
                "单句最大文字量": "Mainly affects Qwen3-ASR and Fun-ASR. A value that is too small can truncate long speech; a larger value only permits longer output and may slow abnormal segments.",
                "常用词 / 专有名词": "Separate names, product terms, medical terms, or game terms with commas. The model pays more attention to similar-sounding words; do not paste a whole article.",
            }.get(self._title, explanation)
        self._tooltip_html = (
            '<div style="width: 320px; white-space: normal;">'
            f"<b>{escape(title)}</b><br>{escape(explanation)}</div>"
        )
        self.setToolTip(self._tooltip_html)
        self.setAccessibleName(f"{title}{' explanation' if language_manager.language == LANGUAGE_EN else '说明'}")
        self.setAccessibleDescription(explanation)
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


class _ModelMigrationWorker(QThread):
    """Move a potentially multi-gigabyte model library off the Qt UI thread."""

    completed = Signal(bool, str)

    def __init__(self, source: Path, destination: Path, parent: QWidget) -> None:
        super().__init__(parent)
        self.source = source
        self.destination = destination

    def run(self) -> None:
        try:
            result = migrate_models(self.source, self.destination)
        except Exception as exc:  # noqa: BLE001 - shown as an in-page status
            logger.exception("模型迁移失败: source=%s destination=%s",
                             self.source, self.destination)
            self.completed.emit(False, str(exc))
            return
        self.completed.emit(
            True,
            f"{result.moved_paths} 个项目已迁移，{result.kept_existing_paths} 个同名项目已保留",
        )


class SettingsWindow(QWidget):
    """语幕设置页（可独立构造，也可嵌入主窗，QTabWidget 七页）。"""

    model_hub_requested = Signal()
    overlay_changed = Signal(dict)
    model_storage_changed = Signal(str)
    tts_settings_changed = Signal(bool, str, str)

    @property
    def _tuning_dirty(self) -> bool:
        """Compatibility view for existing UI tests and integrations."""
        return self._tuning_draft.dirty

    @_tuning_dirty.setter
    def _tuning_dirty(self, value: bool) -> None:
        self._tuning_draft.dirty = bool(value)

    def __init__(
        self,
        store: ConfigStore | None = None,
        parent: QWidget | None = None,
        overlay: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store or ConfigStore()
        self._overlay = overlay
        self._loading = True
        self._tuning_draft = RecognitionTuningDraft()
        self._embedded = False
        self._storage_worker: _ModelMigrationWorker | None = None
        self._storage_dialog: QFileDialog | None = None
        self._ocr_cache_dialog: QFileDialog | None = None
        self._storage_change_guard: Callable[[], bool] | None = None
        self._storage_switch_destination: Path | None = None
        self.setObjectName("settingsWindow")
        self.setWindowTitle("设置 — 语幕 VoxSub")
        self.setMinimumSize(640, 520)
        self.resize(760, 700)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 22)
        root.setSpacing(16)

        header = QFrame(self)
        header.setObjectName("windowHeader")
        header_box = QVBoxLayout(header)
        header_box.setContentsMargins(2, 0, 2, 0)
        header_box.setSpacing(3)
        eyebrow = QLabel("VOXSUB  /  PREFERENCES", header)
        eyebrow.setObjectName("eyebrowLabel")
        title = QLabel("设置", header)
        title.setObjectName("windowTitleLabel")
        subtitle = QLabel("把识别、设备和字幕显示调整成适合你的工作方式。", header)
        subtitle.setObjectName("windowSubtitleLabel")
        header_box.addWidget(eyebrow)
        header_box.addWidget(title)
        header_box.addWidget(subtitle)
        root.addWidget(header)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("settingsTabs")
        self.tabs.addTab(self._build_translate_tab(), "翻译")
        self.tabs.addTab(self._build_recognition_tab(), "识别调优")
        self.tabs.addTab(self._build_voice_tab(), "语音")
        self.tabs.addTab(self._build_device_tab(), "设备")
        self.tabs.addTab(self._build_storage_tab(), "存储与模型")
        self.tabs.addTab(self._build_appearance_tab(), "外观")
        self.tabs.addTab(self._build_about_tab(), "关于")
        root.addWidget(self.tabs, 1)

        self._load_from_store()
        self._loading = False
        language_manager.language_changed.connect(self._on_language_changed)
        self._on_language_changed(language_manager.language)

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

    @staticmethod
    def _scroll_page(page: QWidget) -> QScrollArea:
        """Keep dense settings usable on small screens without clipping fields."""
        page.setObjectName("settingsPage")
        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _build_translate_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        stt_card, stt_box = self._card("语音识别来源")
        stt_note = QLabel(
            "本地 STT 不上传音频；云 STT 会把每个语音片段发送到你填写的音频转写接口。"
            "两者可以和下面的本地/云翻译自由组合。",
            stt_card,
        )
        stt_note.setObjectName("secondaryLabel")
        stt_note.setWordWrap(True)
        stt_box.addWidget(stt_note)
        self.stt_local_radio = RoundRadioButton("本地 STT（使用模型广场中的识别模型）", stt_card)
        self.stt_cloud_radio = RoundRadioButton("云 STT（OpenAI 兼容音频转写）", stt_card)
        for rb in (self.stt_local_radio, self.stt_cloud_radio):
            rb.setObjectName("tierRadio")
            stt_box.addWidget(rb)
        self.stt_local_radio.toggled.connect(
            lambda on: self._on_stt_provider_changed(on, "local"))
        self.stt_cloud_radio.toggled.connect(
            lambda on: self._on_stt_provider_changed(on, "cloud"))
        self.model_hub_btn = QPushButton("打开本地模型广场", stt_card)
        self.model_hub_btn.setObjectName("secondaryButton")
        self.model_hub_btn.clicked.connect(self.model_hub_requested.emit)
        stt_box.addWidget(self.model_hub_btn)
        lay.addWidget(stt_card)

        card, box = self._card("翻译来源")
        self.tier_fast = RoundRadioButton("快档（本地 OPUS-MT，<0.5s/句）", card)
        self.tier_quality = RoundRadioButton("质量档（使用模型广场中选择的专用翻译模型）", card)
        self.tier_cloud = RoundRadioButton("云翻译（OpenAI 兼容文本模型）", card)
        for rb in (self.tier_fast, self.tier_quality, self.tier_cloud):
            rb.setObjectName("tierRadio")
            box.addWidget(rb)
        self.tier_fast.toggled.connect(lambda on: self._on_tier_changed(on, "fast"))
        self.tier_quality.toggled.connect(lambda on: self._on_tier_changed(on, "quality"))
        self.tier_cloud.toggled.connect(lambda on: self._on_tier_changed(on, "cloud"))
        lay.addWidget(card)

        translate_card, translate_box = self._card("云翻译配置（仅云翻译生效）")
        translate_form = QFormLayout()
        translate_form.setHorizontalSpacing(14)
        translate_form.setVerticalSpacing(9)
        self.translate_api_key_edit = QLineEdit(translate_card)
        self.translate_api_key_edit.setObjectName("inputBox")
        self.translate_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.translate_api_key_edit.setPlaceholderText("翻译服务 API Key")
        self.translate_base_url_edit = QLineEdit(translate_card)
        self.translate_base_url_edit.setObjectName("inputBox")
        self.translate_base_url_edit.setPlaceholderText("https://api.deepseek.com/v1")
        self.translate_model_edit = QLineEdit(translate_card)
        self.translate_model_edit.setObjectName("inputBox")
        self.translate_model_edit.setPlaceholderText("deepseek-chat")
        translate_form.addRow("API Key", self.translate_api_key_edit)
        translate_form.addRow("BaseURL", self.translate_base_url_edit)
        translate_form.addRow("模型名", self.translate_model_edit)
        translate_box.addLayout(translate_form)
        translate_hint = QLabel(
            "只把识别后的文字发送到翻译接口；可与本地 STT 组合。",
            translate_card,
        )
        translate_hint.setObjectName("secondaryLabel")
        translate_hint.setWordWrap(True)
        translate_box.addWidget(translate_hint)
        lay.addWidget(translate_card)

        stt_cloud_card, stt_cloud_box = self._card("云 STT 配置（仅云 STT 生效）")
        stt_form = QFormLayout()
        stt_form.setHorizontalSpacing(14)
        stt_form.setVerticalSpacing(9)
        self.stt_api_key_edit = QLineEdit(stt_cloud_card)
        self.stt_api_key_edit.setObjectName("inputBox")
        self.stt_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.stt_api_key_edit.setPlaceholderText("音频转写服务 API Key")
        self.stt_base_url_edit = QLineEdit(stt_cloud_card)
        self.stt_base_url_edit.setObjectName("inputBox")
        self.stt_base_url_edit.setPlaceholderText("https://api.openai.com/v1")
        self.stt_model_edit = QLineEdit(stt_cloud_card)
        self.stt_model_edit.setObjectName("inputBox")
        self.stt_model_edit.setPlaceholderText("whisper-1")
        stt_form.addRow("API Key", self.stt_api_key_edit)
        stt_form.addRow("BaseURL", self.stt_base_url_edit)
        stt_form.addRow("模型名", self.stt_model_edit)
        stt_cloud_box.addLayout(stt_form)
        stt_cloud_hint = QLabel(
            "当前按 VAD 切出的语音片段调用 /audio/transcriptions，适合实时字幕的句段模式；"
            "请确认服务商支持音频转写接口。",
            stt_cloud_card,
        )
        stt_cloud_hint.setObjectName("secondaryLabel")
        stt_cloud_hint.setWordWrap(True)
        stt_cloud_box.addWidget(stt_cloud_hint)
        lay.addWidget(stt_cloud_card)

        self.cloud_mode_summary = QLabel("", page)
        self.cloud_mode_summary.setObjectName("statusPill")
        self.cloud_mode_summary.setWordWrap(True)
        lay.addWidget(self.cloud_mode_summary)

        # Compatibility aliases retained for 0.3.x callers and existing user
        # automation; they now point specifically at the translation fields.
        self.api_key_edit = self.translate_api_key_edit
        self.base_url_edit = self.translate_base_url_edit
        self.translate_api_key_edit.editingFinished.connect(self.save_cloud_credentials)
        self.translate_base_url_edit.editingFinished.connect(self.save_cloud_credentials)
        self.translate_model_edit.editingFinished.connect(self.save_cloud_credentials)
        self.stt_api_key_edit.editingFinished.connect(self.save_cloud_credentials)
        self.stt_base_url_edit.editingFinished.connect(self.save_cloud_credentials)
        self.stt_model_edit.editingFinished.connect(self.save_cloud_credentials)
        lay.addStretch(1)
        return self._scroll_page(page)

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
            "不是重新训练模型。该设置同时用于 A 麦克风、B 系统声音和 C 音视频文件模式；"
            "没有 AI 背景时保持“自动”即可。",
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
            ("智能上下文（动态断句）", "context"),
            ("自定义", "custom"),
        ):
            self.asr_profile_combo.addItem(label, value)
        self._tuning_row(
            form, "调优预设", self.asr_profile_combo,
            "自动会根据 Zipformer 或 Qwen3-ASR 选择合适参数。响应优先出字更快，"
            "但更容易把一句话切碎；准确优先会多等一会儿。智能上下文会持续更新"
            "当前句及其译文，完整后再提交。",
        )

        self.vad_threshold_spin = _ReliableDoubleSpinBox(intro_card)
        self.vad_threshold_spin.setRange(0.01, 0.99)
        self.vad_threshold_spin.setSingleStep(0.01)
        self.vad_threshold_spin.setDecimals(2)
        self.vad_threshold_spin.setObjectName("inputBox")
        self._tuning_row(
            form, "语音灵敏度", self.vad_threshold_spin,
            "数值越低，轻声和远处说话越容易被听见，但键盘声、风扇声也更可能被当成语音；"
            "数值越高，环境噪声更少，但小声说话可能漏掉。普通室内建议 0.30–0.50。",
        )

        self.silence_spin = _ReliableSpinBox(intro_card)
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

        self.context_hold_spin = _ReliableSpinBox(intro_card)
        self.context_hold_spin.setRange(200, 4000)
        self.context_hold_spin.setSingleStep(100)
        self.context_hold_spin.setSuffix(" ms")
        self.context_hold_spin.setObjectName("inputBox")
        self._tuning_row(
            form, "上下文最长等待", self.context_hold_spin,
            "仅智能上下文模式生效。检测到句子可能没说完时，允许额外等待这段时间；"
            "到达上限一定会提交，不会无限等待。会议建议 1200–2200ms。",
        )

        self.max_segment_spin = _ReliableDoubleSpinBox(intro_card)
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

        self.beam_paths_spin = _ReliableSpinBox(intro_card)
        self.beam_paths_spin.setRange(1, 16)
        self.beam_paths_spin.setObjectName("inputBox")
        self._tuning_row(
            form, "识别候选数", self.beam_paths_spin,
            "主要影响 Zipformer。数值越大，模型会比较更多可能的文字，可能更准确，"
            "但会更耗 CPU；Qwen3-ASR 当前使用自己的生成方式，这一项对它不生效。",
        )

        self.max_tokens_spin = _ReliableSpinBox(intro_card)
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

        self.live_draft_switch = ToggleSwitch(
            "启用当前句原文与译文动态更新", intro_card)
        self._tuning_row(
            form, "实时双语草稿", self.live_draft_switch,
            "仅智能上下文模式生效。开启后当前句原文和译文会持续更新；"
            "关闭可减少 CPU 和内存占用，但智能断句、上下文纠偏和语气词清理仍然保留。",
        )

        self.context_correction_switch = ToggleSwitch(
            "启用保守纠偏（不自由改写）", intro_card)
        self._tuning_row(
            form, "上下文保守纠偏", self.context_correction_switch,
            "只根据常用词和最近多次出现的词做小范围、可记录的替换；"
            "不会凭空补充数字、人名、否定词或没有听到的内容。",
        )

        self.filler_mode_combo = QComboBox(intro_card)
        self.filler_mode_combo.setObjectName("inputBox")
        self.filler_mode_combo.setMinimumHeight(42)
        self.filler_mode_combo.addItem("关闭（保留原话）", "off")
        self.filler_mode_combo.addItem("轻度（仅独立语气词）", "light")
        self._tuning_row(
            form, "语气词清理", self.filler_mode_combo,
            "轻度只清理独立的“嗯、啊、呃、额、唔”等填充词；句尾语气、疑问和原始识别文本会保留。",
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
                     self.context_hold_spin, self.max_segment_spin, self.beam_paths_spin,
                     self.max_tokens_spin):
            spin.valueChanged.connect(self._mark_asr_tuning_dirty)
        self.hotwords_edit.textChanged.connect(self._mark_asr_tuning_dirty)
        self.live_draft_switch.toggled.connect(self._mark_asr_tuning_dirty)
        self.context_correction_switch.toggled.connect(self._mark_asr_tuning_dirty)
        self.filler_mode_combo.currentIndexChanged.connect(
            self._mark_asr_tuning_dirty)
        return self._scroll_page(page)

    def _build_voice_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        card, box = self._card("语音朗读")
        note = QLabel(
            "智能上下文和实时双语草稿均可使用朗读；为避免反复抢读，只朗读已经定稿的译文。",
            card,
        )
        note.setObjectName("secondaryLabel")
        note.setWordWrap(True)
        box.addWidget(note)
        self.tts_switch = ToggleSwitch("朗读译文（本地 TTS；失败自动降级为仅字幕）", card)
        self.tts_switch.setMinimumHeight(44)
        box.addWidget(self.tts_switch)
        self.tts_switch.toggled.connect(self._on_tts_toggled)

        zh_label = QLabel("中文朗读模型", card)
        zh_label.setObjectName("fieldLabel")
        self.tts_zh_model_combo = QComboBox(card)
        self.tts_zh_model_combo.setObjectName("inputBox")
        self.tts_zh_model_combo.setMinimumHeight(44)
        en_label = QLabel("英文朗读模型", card)
        en_label.setObjectName("fieldLabel")
        self.tts_en_model_combo = QComboBox(card)
        self.tts_en_model_combo.setObjectName("inputBox")
        self.tts_en_model_combo.setMinimumHeight(44)
        box.addWidget(zh_label)
        box.addWidget(self.tts_zh_model_combo)
        box.addWidget(en_label)
        box.addWidget(self.tts_en_model_combo)
        self.tts_zh_model_combo.currentIndexChanged.connect(
            lambda index: self._on_tts_model_changed("zh", index))
        self.tts_en_model_combo.currentIndexChanged.connect(
            lambda index: self._on_tts_model_changed("en", index))

        self.tts_model_hub_btn = QPushButton("打开模型广场管理朗读模型", card)
        self.tts_model_hub_btn.setObjectName("secondaryButton")
        self.tts_model_hub_btn.clicked.connect(self.model_hub_requested.emit)
        box.addWidget(self.tts_model_hub_btn)
        self.tts_status_label = QLabel("", card)
        self.tts_status_label.setObjectName("secondaryLabel")
        self.tts_status_label.setWordWrap(True)
        box.addWidget(self.tts_status_label)
        lay.addWidget(card)
        lay.addStretch(1)
        return self._scroll_page(page)

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
        return self._scroll_page(page)

    def _build_storage_tab(self) -> QWidget:
        """Let users relocate downloaded models without reinstalling them."""
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        card, box = self._card("模型保存位置")
        note = QLabel(
            "识别、翻译、语音模型会按用途整理在这里。更新软件不会清空这个文件夹。",
            card,
        )
        note.setObjectName("cardCaption")
        note.setWordWrap(True)
        box.addWidget(note)

        path_row = QHBoxLayout()
        path_label = QLabel("当前位置", card)
        path_label.setObjectName("fieldLabel")
        self.models_path_value = QLabel(card)
        self.models_path_value.setObjectName("modelStoragePath")
        self.models_path_value.setWordWrap(True)
        self.models_path_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        path_row.addWidget(path_label, 0, Qt.AlignmentFlag.AlignTop)
        path_row.addWidget(self.models_path_value, 1)
        box.addLayout(path_row)

        self.models_path_mode = QLabel(card)
        self.models_path_mode.setObjectName("cardCaption")
        self.models_path_mode.setWordWrap(True)
        box.addWidget(self.models_path_mode)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.open_models_folder_btn = QPushButton("打开文件夹", card)
        self.open_models_folder_btn.setObjectName("secondaryButton")
        self.open_models_folder_btn.clicked.connect(self._open_models_folder)
        self.change_models_folder_btn = QPushButton("更改保存位置", card)
        self.change_models_folder_btn.setObjectName("primaryButton")
        self.change_models_folder_btn.clicked.connect(self._choose_models_folder)
        actions.addWidget(self.open_models_folder_btn)
        actions.addWidget(self.change_models_folder_btn)
        actions.addStretch(1)
        box.addLayout(actions)
        lay.addWidget(card)

        ocr_card, ocr_box = self._card("OCR 图片缓存")
        ocr_note = QLabel(
            "上传/截图原图与译后覆盖图分开保存，绝不写入 C 盘。默认每类保留最近 15 张；设为 0 表示无限保留。",
            ocr_card,
        )
        ocr_note.setObjectName("cardCaption")
        ocr_note.setWordWrap(True)
        ocr_box.addWidget(ocr_note)
        self.ocr_cache_path_value = QLabel(ocr_card)
        self.ocr_cache_path_value.setObjectName("modelStoragePath")
        self.ocr_cache_path_value.setWordWrap(True)
        self.ocr_cache_path_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        ocr_box.addWidget(self.ocr_cache_path_value)
        ocr_form = QFormLayout()
        self.ocr_cache_limit_spin = _ReliableSpinBox(ocr_card)
        self.ocr_cache_limit_spin.setObjectName("inputBox")
        self.ocr_cache_limit_spin.setRange(0, 10_000)
        self.ocr_cache_limit_spin.setSpecialValueText(tr("无限"))
        self.ocr_cache_limit_spin.setSuffix(tr(" 张/类"))
        self.ocr_cache_limit_spin.valueChanged.connect(
            self._on_ocr_cache_limit_changed)
        ocr_form.addRow(tr("每类保留"), self.ocr_cache_limit_spin)
        ocr_box.addLayout(ocr_form)
        ocr_actions = QHBoxLayout()
        self.open_ocr_cache_btn = QPushButton("打开缓存", ocr_card)
        self.open_ocr_cache_btn.setObjectName("secondaryButton")
        self.open_ocr_cache_btn.clicked.connect(self._open_ocr_cache_folder)
        self.change_ocr_cache_btn = QPushButton("更改缓存位置", ocr_card)
        self.change_ocr_cache_btn.setObjectName("secondaryButton")
        self.change_ocr_cache_btn.clicked.connect(self._choose_ocr_cache_folder)
        ocr_actions.addWidget(self.open_ocr_cache_btn)
        ocr_actions.addWidget(self.change_ocr_cache_btn)
        ocr_actions.addStretch(1)
        ocr_box.addLayout(ocr_actions)
        self.ocr_cache_status = QLabel(ocr_card)
        self.ocr_cache_status.setObjectName("secondaryLabel")
        self.ocr_cache_status.setWordWrap(True)
        ocr_box.addWidget(self.ocr_cache_status)
        lay.addWidget(ocr_card)

        import_card, import_box = self._card("迁移已有模型")
        import_note = QLabel(
            "如果以前把模型放在其他磁盘或手动复制过模型，可从这里把它们并入当前位置。"
            "同名文件会保留，避免覆盖已有下载。",
            import_card,
        )
        import_note.setObjectName("cardCaption")
        import_note.setWordWrap(True)
        import_box.addWidget(import_note)
        self.import_models_btn = QPushButton("迁移已有模型", import_card)
        self.import_models_btn.setObjectName("secondaryButton")
        self.import_models_btn.clicked.connect(self._choose_models_to_import)
        import_box.addWidget(self.import_models_btn, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(import_card)

        self.model_storage_status = QLabel(card)
        self.model_storage_status.setObjectName("secondaryLabel")
        self.model_storage_status.setWordWrap(True)
        lay.addWidget(self.model_storage_status)
        lay.addStretch(1)
        return self._scroll_page(page)

    def _build_appearance_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        card, box = self._card("主题")
        self.theme_light = RoundRadioButton("浅色", card)
        self.theme_dark = RoundRadioButton("深色", card)
        self.theme_system = RoundRadioButton("跟随系统", card)
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
        language_row = QHBoxLayout()
        language_label = QLabel("语言", card)
        language_label.setObjectName("fieldLabel")
        self.language_combo = QComboBox(card)
        self.language_combo.setObjectName("inputBox")
        self.language_combo.setMinimumHeight(40)
        self.language_combo.addItem("跟随系统", LANGUAGE_SYSTEM)
        self.language_combo.addItem("简体中文", LANGUAGE_ZH)
        self.language_combo.addItem("English", LANGUAGE_EN)
        self.language_combo.currentIndexChanged.connect(self._on_language_setting_changed)
        language_row.addWidget(language_label)
        language_row.addWidget(self.language_combo, 1)
        box.addLayout(language_row)
        self.theme_light.toggled.connect(lambda on: self._on_theme_changed(on, AppTheme.LIGHT))
        self.theme_dark.toggled.connect(lambda on: self._on_theme_changed(on, AppTheme.DARK))
        self.theme_system.toggled.connect(lambda on: self._on_theme_changed(on, AppTheme.SYSTEM))
        lay.addWidget(card)

        overlay_card, overlay_box = self._card("浮窗显示")
        overlay_note = QLabel(
            "浮窗上的工具条也可以直接调整。这里适合设置一个固定的默认外观，"
            "锁定后仍可在浮窗顶部悬停打开解锁控制。",
            overlay_card,
        )
        overlay_note.setObjectName("cardCaption")
        overlay_note.setWordWrap(True)
        overlay_box.addWidget(overlay_note)

        overlay_form = QFormLayout()
        overlay_form.setHorizontalSpacing(18)
        overlay_form.setVerticalSpacing(10)
        self.overlay_font_spin = _ReliableSpinBox(overlay_card)
        self.overlay_font_spin.setObjectName("inputBox")
        self.overlay_font_spin.setRange(10, 72)
        self.overlay_font_spin.setSingleStep(2)
        self.overlay_font_spin.setSuffix(" pt")
        self.overlay_font_spin.valueChanged.connect(self._on_overlay_font_changed)
        overlay_form.addRow("译文字号", self.overlay_font_spin)

        self.overlay_opacity_spin = _ReliableSpinBox(overlay_card)
        self.overlay_opacity_spin.setObjectName("inputBox")
        self.overlay_opacity_spin.setRange(20, 100)
        self.overlay_opacity_spin.setSingleStep(5)
        self.overlay_opacity_spin.setSuffix(" %")
        self.overlay_opacity_spin.valueChanged.connect(self._on_overlay_opacity_changed)
        overlay_form.addRow("浮窗不透明度", self.overlay_opacity_spin)
        overlay_box.addLayout(overlay_form)

        self.overlay_lock_switch = ToggleSwitch("启动时保持锁定并允许点击穿透", overlay_card)
        self.overlay_lock_switch.setMinimumHeight(40)
        self.overlay_lock_switch.toggled.connect(self._on_overlay_lock_changed)
        overlay_box.addWidget(self.overlay_lock_switch)
        lay.addWidget(overlay_card)
        lay.addStretch(1)
        return self._scroll_page(page)

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
        github_row = QHBoxLayout()
        github_key = QLabel("GitHub", card)
        github_key.setObjectName("fieldLabel")
        github_link = QLabel(
            '<a href="https://github.com/tuotuonuts/VoxSub">github.com/tuotuonuts/VoxSub</a>',
            card,
        )
        github_link.setOpenExternalLinks(True)
        github_link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        github_link.setToolTip("https://github.com/tuotuonuts/VoxSub")
        github_row.addWidget(github_key, 0, Qt.AlignmentFlag.AlignTop)
        github_row.addWidget(github_link, 1)
        box.addLayout(github_row)
        debug_label = QLabel("开发与排障", card)
        debug_label.setObjectName("fieldLabel")
        self.debug_switch = ToggleSwitch("启用内置调试模式（实时显示详细日志）", card)
        self.debug_switch.setMinimumHeight(44)
        self.debug_switch.toggled.connect(self._on_debug_toggled)
        box.addSpacing(8)
        box.addWidget(debug_label)
        box.addWidget(self.debug_switch)
        lay.addWidget(card)

        sentry_card, sentry_box = self._card("诊断上报")
        sentry_note = QLabel(
            "可选：将最新自检报告和本地日志快照发送到 Sentry。上传前会过滤 API Key、"
            "音频、字幕正文、识别文本和私人路径；不填写 DSN 时保持关闭。",
            sentry_card,
        )
        sentry_note.setObjectName("cardCaption")
        sentry_note.setWordWrap(True)
        sentry_box.addWidget(sentry_note)
        sentry_form = QFormLayout()
        sentry_form.setHorizontalSpacing(14)
        sentry_form.setVerticalSpacing(9)
        self.sentry_dsn_edit = QLineEdit(sentry_card)
        self.sentry_dsn_edit.setObjectName("inputBox")
        self.sentry_dsn_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.sentry_dsn_edit.setPlaceholderText("https://<public-key>@<host>/<project-id>")
        self.sentry_environment_combo = QComboBox(sentry_card)
        self.sentry_environment_combo.setObjectName("inputBox")
        self.sentry_environment_combo.setMinimumHeight(40)
        for label, value in (("开发", "development"), ("测试", "testing"),
                             ("生产", "production")):
            self.sentry_environment_combo.addItem(label, value)
        self.sentry_build_edit = QLineEdit(sentry_card)
        self.sentry_build_edit.setObjectName("inputBox")
        self.sentry_build_edit.setPlaceholderText("source")
        sentry_form.addRow("DSN", self.sentry_dsn_edit)
        sentry_form.addRow("环境", self.sentry_environment_combo)
        sentry_form.addRow("构建标识", self.sentry_build_edit)
        sentry_box.addLayout(sentry_form)
        sentry_action = QHBoxLayout()
        self.sentry_status_label = QLabel(sentry_card)
        self.sentry_status_label.setObjectName("secondaryLabel")
        self.sentry_status_label.setWordWrap(True)
        sentry_action.addWidget(self.sentry_status_label, 1)
        self.sentry_save_btn = QPushButton("保存诊断上报设置", sentry_card)
        self.sentry_save_btn.setObjectName("secondaryButton")
        self.sentry_save_btn.clicked.connect(self._save_sentry_settings)
        sentry_action.addWidget(self.sentry_save_btn)
        sentry_box.addLayout(sentry_action)
        lay.addWidget(sentry_card)

        history_card, history_box = self._card("更新日志")
        history_note = QLabel(
            "每次更新后，首次打开应用会看到一次简短说明。这里可以随时回看最近版本的变化。",
            history_card,
        )
        history_note.setObjectName("cardCaption")
        history_note.setWordWrap(True)
        history_box.addWidget(history_note)
        self.release_history_label = QLabel(history_card)
        self.release_history_label.setObjectName("releaseHistory")
        self.release_history_label.setWordWrap(True)
        self.release_history_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        history_box.addWidget(self.release_history_label)
        lay.addWidget(history_card)
        lay.addStretch(1)
        return self._scroll_page(page)

    # ------------------------------------------------------------------
    # 状态装载 / 变更
    # ------------------------------------------------------------------
    def _load_from_store(self) -> None:
        cfg = self._store.load()
        tier = cfg.get("translate_tier", "fast")
        target = {"fast": self.tier_fast, "quality": self.tier_quality, "cloud": self.tier_cloud}
        rb = target.get(tier, self.tier_fast)
        rb.setChecked(True)
        stt_provider = str(cfg.get("stt_provider", "local"))
        stt_target = self.stt_cloud_radio if stt_provider == "cloud" else self.stt_local_radio
        stt_target.setChecked(True)
        self.translate_api_key_edit.setText(str(cfg.get(
            "translate_api_key", cfg.get("api_key", ""))))
        self.translate_base_url_edit.setText(str(cfg.get(
            "translate_base_url", cfg.get("base_url", ""))))
        self.translate_model_edit.setText(str(cfg.get(
            "translate_model", cfg.get("model", "deepseek-chat"))))
        self.stt_api_key_edit.setText(str(cfg.get("stt_api_key", "")))
        self.stt_base_url_edit.setText(str(cfg.get(
            "stt_base_url", "https://api.openai.com/v1")))
        self.stt_model_edit.setText(str(cfg.get("stt_model", "whisper-1")))
        self._set_translate_cloud_enabled(tier == "cloud")
        self._set_stt_cloud_enabled(stt_provider == "cloud")
        self._update_cloud_mode_summary()

        tuning = self._tuning_from_config(cfg)
        self._apply_tuning_to_controls(tuning)
        self._tuning_draft.load(tuning)
        self._set_tuning_dirty(False)

        self._refresh_tts_model_choices(cfg)
        self.tts_switch.setChecked(bool(cfg.get("tts_enabled", True)))
        self.tts_switch.setEnabled(True)
        self._update_tts_status()
        # Legacy persisted debug mode must never silently re-enable DEBUG.
        self.debug_switch.setChecked(False)
        self.sentry_dsn_edit.setText(str(cfg.get("sentry_dsn", "")))
        self.sentry_build_edit.setText(str(cfg.get("sentry_build", "source") or "source"))
        self._select_data(self.sentry_environment_combo,
                          str(cfg.get("sentry_environment", "production") or "production"))
        self._update_sentry_status()
        self.overlay_font_spin.setValue(int(cfg.get("overlay_font_size", 20)))
        self.overlay_opacity_spin.setValue(
            int(round(float(cfg.get("overlay_opacity", 0.92)) * 100)))
        self.overlay_lock_switch.setChecked(bool(cfg.get("overlay_click_through", False)))
        self.ocr_cache_limit_spin.setValue(int(cfg.get("ocr_cache_limit", 15)))
        self._refresh_ocr_cache_storage()
        self._refresh_model_storage()
        if hasattr(self, "release_history_label"):
            self.release_history_label.setText(release_history_text())

        self.refresh_devices()

        theme = cfg.get("theme", "system")
        theme_enum = {
            "light": AppTheme.LIGHT,
            "dark": AppTheme.DARK,
            "system": AppTheme.SYSTEM,
        }.get(str(theme), AppTheme.SYSTEM)
        theme_target = {
            "light": self.theme_light,
            "dark": self.theme_dark,
            "system": self.theme_system,
        }
        theme_target.get(theme, self.theme_system).setChecked(True)
        self._select_data(self.language_combo, str(cfg.get("language", LANGUAGE_SYSTEM)))

    def _on_tier_changed(self, checked: bool, tier: str) -> None:
        if not checked:
            return
        self._store.set("translate_tier", tier)
        self._set_translate_cloud_enabled(tier == "cloud")
        self._update_cloud_mode_summary()

    def _on_stt_provider_changed(self, checked: bool, provider: str) -> None:
        if not checked:
            return
        self._store.set("stt_provider", provider)
        self._set_stt_cloud_enabled(provider == "cloud")
        self._update_cloud_mode_summary()

    def _set_translate_cloud_enabled(self, enabled: bool) -> None:
        for control in (
            self.translate_api_key_edit,
            self.translate_base_url_edit,
            self.translate_model_edit,
        ):
            control.setEnabled(enabled)

    def _set_stt_cloud_enabled(self, enabled: bool) -> None:
        for control in (self.stt_api_key_edit, self.stt_base_url_edit,
                        self.stt_model_edit):
            control.setEnabled(enabled)

    def _set_cloud_enabled(self, enabled: bool) -> None:
        """Backward-compatible alias for the translation cloud controls."""
        self._set_translate_cloud_enabled(enabled)

    def _update_cloud_mode_summary(self) -> None:
        if not hasattr(self, "cloud_mode_summary"):
            return
        stt_cloud = self.stt_cloud_radio.isChecked()
        translate_cloud = self.tier_cloud.isChecked()
        stt_name = "云 STT" if stt_cloud else "本地 STT"
        translate_name = "云翻译" if translate_cloud else (
            "本地质量翻译" if self.tier_quality.isChecked() else "本地快档翻译")
        if stt_cloud != translate_cloud:
            prefix = "混合模式"
        elif stt_cloud:
            prefix = "云端双阶段"
        else:
            prefix = "本地模式"
        self.cloud_mode_summary.setText(
            f"{tr('当前组合', 'Current pipeline')}：{tr(prefix)} · "
            f"{tr(stt_name)} + {tr(translate_name)}"
        )

    def _on_tts_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._store.set("tts_enabled", bool(checked))
        self._update_tts_status()
        self._emit_tts_settings()

    def _refresh_tts_model_choices(self, cfg: dict | None = None) -> None:
        """Show installed catalog voices while preserving a missing selection."""
        config = dict(cfg or self._store.load())
        marketplace = ModelMarketplace(resolve_models_root(self._store))
        was_loading = self._loading
        self._loading = True
        try:
            for lang, combo in (
                ("zh", self.tts_zh_model_combo),
                ("en", self.tts_en_model_combo),
            ):
                combo.blockSignals(True)
                combo.clear()
                current = str(config.get(
                    f"tts_model_id_{lang}",
                    "tts-icefall-zh-aishell3" if lang == "zh"
                    else "tts-icefall-en-ljspeech-low",
                ))
                compatible = [
                    model for model in models_for_task("tts")
                    if lang in model.tts_languages
                ]
                installed = [
                    model for model in compatible
                    if marketplace.is_installed(model)
                ]
                current_model = get_model(current)
                if current_model in compatible and current_model not in installed:
                    combo.addItem(f"{current_model.name}{tr('（未安装）')}", current)
                    item = getattr(combo.model(), "item", lambda _index: None)(0)
                    if item is not None:
                        item.setEnabled(False)
                for model in installed:
                    combo.addItem(model.name, model.id)
                if not compatible:
                    combo.addItem("没有兼容的朗读模型", "")
                elif combo.count() == 0:
                    combo.addItem("尚未安装朗读模型", "")
                selected = combo.findData(current)
                if selected < 0 and installed:
                    selected = combo.findData(installed[0].id)
                combo.setCurrentIndex(max(0, selected))
                combo.setEnabled(bool(installed))
                combo.blockSignals(False)
        finally:
            self._loading = was_loading

    def refresh_tts_model_choices(self) -> None:
        """Public hook used after a Model Hub install or selection change."""
        self._refresh_tts_model_choices()
        self._update_tts_status()
        if not self._loading:
            self._emit_tts_settings()

    def _on_tts_model_changed(self, lang: str, _index: int) -> None:
        if self._loading:
            return
        combo = self.tts_zh_model_combo if lang == "zh" else self.tts_en_model_combo
        model_id = str(combo.currentData() or "")
        model = get_model(model_id)
        if model is None or model.task != "tts" or lang not in model.tts_languages:
            return
        marketplace = ModelMarketplace(resolve_models_root(self._store))
        if not marketplace.is_installed(model):
            return
        self._store.set(f"tts_model_id_{lang}", model.id)
        self._update_tts_status()
        self._emit_tts_settings()

    def _emit_tts_settings(self) -> None:
        cfg = self._store.load()
        self.tts_settings_changed.emit(
            bool(cfg.get("tts_enabled", False)),
            str(cfg.get("tts_model_id_zh", "tts-icefall-zh-aishell3")),
            str(cfg.get("tts_model_id_en", "tts-icefall-en-ljspeech-low")),
        )

    def _update_tts_status(self) -> None:
        if not hasattr(self, "tts_status_label"):
            return
        cfg = self._store.load()
        target = str(cfg.get("lang_pair", "zh-en")).split("-", 1)[-1]
        model_id = str(cfg.get(f"tts_model_id_{target}", ""))
        model = get_model(model_id)
        ready = bool(
            model is not None
            and model.task == "tts"
            and target in model.tts_languages
            and ModelMarketplace(resolve_models_root(self._store)).is_installed(model)
        )
        language = "中文" if target == "zh" else "英文"
        if ready:
            state = tr(
                f"当前{language}译文将使用 {model.name} 朗读。",
                f"The current {'Chinese' if target == 'zh' else 'English'} translation will be read with {model.name}.",
            )
        else:
            state = tr(
                f"尚未安装当前{language}译文所需的朗读模型，请前往模型广场下载。",
                f"No installed voice is available for the current {'Chinese' if target == 'zh' else 'English'} translation. Download one from Model Hub.",
            )
        self.tts_status_label.setText(state)

    def _overlay_call(self, method: str, *args) -> None:
        if self._overlay is None:
            return
        callback = getattr(self._overlay, method, None)
        if callable(callback):
            try:
                callback(*args)
            except Exception:  # pragma: no cover - optional live window bridge
                logger.exception("应用浮窗设置失败: %s", method)

    def _emit_overlay_state(self) -> None:
        self.overlay_changed.emit({
            "font_size": int(self.overlay_font_spin.value()),
            "opacity": self.overlay_opacity_spin.value() / 100.0,
            "click_through": bool(self.overlay_lock_switch.isChecked()),
        })

    def _on_overlay_font_changed(self, value: int) -> None:
        if self._loading:
            return
        current = getattr(self._overlay, "font_size", lambda: value)()
        self._overlay_call("change_font_size", int(value) - int(current))
        self._store.set("overlay_font_size", int(value))
        self._emit_overlay_state()

    def _on_overlay_opacity_changed(self, value: int) -> None:
        if self._loading:
            return
        opacity = max(0.2, min(1.0, value / 100.0))
        self._overlay_call("set_overlay_opacity", opacity)
        self._store.set("overlay_opacity", opacity)
        self._emit_overlay_state()

    def _on_overlay_lock_changed(self, checked: bool) -> None:
        if self._loading:
            return
        self._overlay_call("set_click_through", bool(checked))
        self._store.set("overlay_click_through", bool(checked))
        self._emit_overlay_state()

    @staticmethod
    def _asr_preset_values(profile: str) -> dict[str, float | int] | None:
        return {
            "responsive": {"vad": 0.45, "silence": 350, "max_s": 6.0,
                           "beam": 2, "tokens": 256},
            "balanced": {"vad": 0.35, "silence": 650, "max_s": 12.0,
                         "beam": 4, "tokens": 512},
            "accuracy": {"vad": 0.25, "silence": 900, "max_s": 20.0,
                         "beam": 6, "tokens": 512},
            "context": {"vad": 0.32, "silence": 500, "max_s": 18.0,
                        "beam": 6, "tokens": 768},
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
            "asr_context_hold_ms": int(cfg.get("asr_context_hold_ms", 1800)),
            "asr_live_draft_enabled": bool(
                cfg.get("asr_live_draft_enabled", True)),
            "asr_context_correction": bool(
                cfg.get("asr_context_correction", True)),
            "asr_filler_mode": str(cfg.get("asr_filler_mode", "light")),
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
            "asr_context_hold_ms": int(self.context_hold_spin.value()),
            "asr_live_draft_enabled": bool(
                self.live_draft_switch.isChecked()),
            "asr_context_correction": bool(
                self.context_correction_switch.isChecked()),
            "asr_filler_mode": str(
                self.filler_mode_combo.currentData() or "light"),
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
            self.context_hold_spin.setValue(int(
                values.get("asr_context_hold_ms", 1800)))
            self.live_draft_switch.setChecked(bool(
                values.get("asr_live_draft_enabled", True)))
            self.context_correction_switch.setChecked(bool(
                values.get("asr_context_correction", True)))
            filler_mode = str(values.get("asr_filler_mode", "light"))
            self.filler_mode_combo.setCurrentIndex(max(
                0, self.filler_mode_combo.findData(filler_mode)))
            self._on_asr_profile_changed(self.asr_profile_combo.currentIndex())
        finally:
            self._loading = was_loading

    def _set_tuning_dirty(self, dirty: bool) -> None:
        self._tuning_draft.dirty = bool(dirty)
        if not hasattr(self, "tuning_state_label"):
            return
        self.tuning_state_label.setText(
            tr("有未保存的更改") if dirty else tr("已保存"))
        self.tuning_save_btn.setEnabled(dirty)
        self.tuning_discard_btn.setEnabled(dirty)

    def _mark_asr_tuning_dirty(self, *_args) -> None:
        if not self._loading:
            self._set_tuning_dirty(
                self._tuning_draft.compare(self._collect_asr_tuning()))

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
        context = profile == "context"
        self.context_hold_spin.setEnabled(context)
        self.live_draft_switch.setEnabled(context)
        self.context_correction_switch.setEnabled(context)
        self.filler_mode_combo.setEnabled(context)
        self._mark_asr_tuning_dirty()

    def _save_asr_tuning(self, *_args) -> None:
        if self._loading:
            return
        values = self._collect_asr_tuning()
        self._store.update(values)
        self._tuning_draft.commit(values)
        self._set_tuning_dirty(False)
        self.tuning_state_label.setText(tr("已保存 · 下次开始时生效"))

    def _discard_asr_tuning(self) -> None:
        values = self._tuning_from_config(self._store.load())
        self._apply_tuning_to_controls(values)
        self._tuning_draft.load(values)
        self._set_tuning_dirty(False)

    def _reset_asr_tuning(self) -> None:
        self.asr_profile_combo.setCurrentIndex(self.asr_profile_combo.findData("auto"))
        for control, value in (
            (self.vad_threshold_spin, 0.35),
            (self.silence_spin, 650),
            (self.max_segment_spin, 12.0),
            (self.beam_paths_spin, 4),
            (self.max_tokens_spin, 512),
            (self.context_hold_spin, 1800),
        ):
            control.blockSignals(True)
            control.setValue(value)
            control.blockSignals(False)
        self.hotwords_edit.clear()
        self.live_draft_switch.setChecked(True)
        self.context_correction_switch.setChecked(True)
        self.filler_mode_combo.setCurrentIndex(
            self.filler_mode_combo.findData("light"))
        self._mark_asr_tuning_dirty()

    def _on_debug_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._store.set("debug_mode", bool(checked))
        if checked:
            start_diagnostic_session()
        else:
            stop_diagnostic_session()

    def _update_sentry_status(self) -> None:
        if is_error_reporting_enabled():
            self.sentry_status_label.setText(tr("Sentry 已启用"))
        elif self.sentry_dsn_edit.text().strip():
            self.sentry_status_label.setText(tr("已保存，重载失败；请检查 DSN"))
        else:
            self.sentry_status_label.setText(tr("Sentry 未配置"))

    def _save_sentry_settings(self) -> None:
        if self._loading:
            return
        self._store.update({
            "sentry_dsn": self.sentry_dsn_edit.text().strip(),
            "sentry_environment": str(
                self.sentry_environment_combo.currentData() or "development"),
            "sentry_build": self.sentry_build_edit.text().strip() or "source",
        })
        try:
            reload_error_reporting()
        except Exception:
            logger.exception("重新加载 Sentry 配置失败")
        self._update_sentry_status()

    # ------------------------------------------------------------------
    # 模型存储
    # ------------------------------------------------------------------
    def set_storage_change_guard(self, guard: Callable[[], bool] | None) -> None:
        """Install a guard that reports active Model Hub downloads."""
        self._storage_change_guard = guard

    def _refresh_model_storage(self) -> None:
        root = resolve_models_root(self._store)
        self.models_path_value.setText(str(root))
        mode = str(self._store.get("models_root_mode", "") or "")
        mode_text = {
            "legacy": tr("已保留你升级前使用的位置；需要时可迁移到其他磁盘。",
                         "Your existing model location was kept; move it to another drive whenever you need."),
            "install": tr("新安装默认保存到软件目录下的 Models 文件夹。",
                          "New installations save models in the app's Models folder by default."),
            "custom": tr("使用你自己选择的模型保存位置。",
                         "Models are stored in the location you selected."),
        }.get(mode, tr("模型位置会在更新后保持不变，直到你主动更改。",
                       "The model location stays unchanged across updates until you change it."))
        self.models_path_mode.setText(mode_text)

    def _refresh_ocr_cache_storage(self) -> None:
        try:
            root = resolve_ocr_cache_root(self._store)
            self.ocr_cache_path_value.setText(str(root))
            self.ocr_cache_status.setText(
                tr("原图保存在 originals，译后图片保存在 translated。"))
            self.open_ocr_cache_btn.setEnabled(True)
        except OcrCacheLocationError as exc:
            self.ocr_cache_path_value.setText(tr("尚未配置非 C 盘目录"))
            self.ocr_cache_status.setText(str(exc))
            self.open_ocr_cache_btn.setEnabled(False)

    def _on_ocr_cache_limit_changed(self, value: int) -> None:
        if self._loading:
            return
        self._store.set("ocr_cache_limit", int(value))
        try:
            cache = cache_from_store(self._store)
            cache.prune("original")
            cache.prune("translated")
        except (OSError, OcrCacheLocationError):
            # The status already explains how to choose a valid non-C path.
            logger.debug("OCR 缓存数量设置已保存，当前路径暂不可整理", exc_info=True)

    def _open_ocr_cache_folder(self) -> None:
        try:
            root = resolve_ocr_cache_root(self._store)
            (root / "originals").mkdir(parents=True, exist_ok=True)
            (root / "translated").mkdir(parents=True, exist_ok=True)
        except (OSError, OcrCacheLocationError) as exc:
            self.ocr_cache_status.setText(f"{tr('无法打开 OCR 缓存')}：{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _choose_ocr_cache_folder(self) -> None:
        if self._ocr_cache_dialog is not None:
            return
        try:
            current = resolve_ocr_cache_root(self._store)
        except OcrCacheLocationError:
            current = resolve_models_root(self._store)
        dialog = QFileDialog(
            self.window(), tr("选择非 C 盘 OCR 图片缓存目录"), str(current.parent))
        dialog.setObjectName("ocrCacheFolderDialog")
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.fileSelected.connect(self._on_ocr_cache_folder_selected)
        dialog.finished.connect(
            lambda _result, opened=dialog: self._on_ocr_cache_dialog_finished(opened))
        self._ocr_cache_dialog = dialog
        dialog.open()
        QTimer.singleShot(0, dialog.raise_)
        QTimer.singleShot(0, dialog.activateWindow)

    def _on_ocr_cache_folder_selected(self, selected: str) -> None:
        try:
            root = validate_ocr_cache_root(Path(selected) / "VoxSub-OCR")
            (root / "originals").mkdir(parents=True, exist_ok=True)
            (root / "translated").mkdir(parents=True, exist_ok=True)
        except (OSError, OcrCacheLocationError) as exc:
            self.ocr_cache_status.setText(str(exc))
            return
        self._store.set("ocr_cache_root", str(root))
        self._refresh_ocr_cache_storage()

    def _on_ocr_cache_dialog_finished(self, dialog: QFileDialog) -> None:
        if self._ocr_cache_dialog is dialog:
            self._ocr_cache_dialog = None
        dialog.deleteLater()

    def _set_storage_controls_enabled(self, enabled: bool) -> None:
        for control in (
            self.open_models_folder_btn,
            self.change_models_folder_btn,
            self.import_models_btn,
        ):
            control.setEnabled(enabled)

    def _open_models_folder(self) -> None:
        root = resolve_models_root(self._store)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.model_storage_status.setText(
                tr(f"无法打开模型文件夹：{exc}", f"Could not open the model folder: {exc}"))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _choose_models_folder(self) -> None:
        self._open_storage_folder_dialog(switch_default=True)

    def _choose_models_to_import(self) -> None:
        self._open_storage_folder_dialog(switch_default=False)

    def _open_storage_folder_dialog(self, *, switch_default: bool) -> None:
        """Open a non-native folder chooser without blocking the Qt event loop."""
        if self._storage_dialog is not None or self._storage_worker is not None:
            return
        current = resolve_models_root(self._store)
        title = (
            tr("选择新的模型保存位置", "Choose a new model storage folder")
            if switch_default
            else tr("选择已有模型文件夹", "Choose an existing model folder")
        )
        # The native Windows shell picker caused AppHangB1 on systems with
        # OneDrive/shell extensions.  Qt's own dialog stays inside our event
        # loop, and open() returns immediately instead of nesting exec().
        dialog = QFileDialog(self.window(), title, str(current.parent))
        dialog.setObjectName("modelStorageFolderDialog")
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.fileSelected.connect(
            lambda selected: self._on_storage_folder_selected(
                selected, switch_default=switch_default))
        dialog.finished.connect(
            lambda _result, opened=dialog: self._on_storage_dialog_finished(opened))
        self._storage_dialog = dialog
        self._set_storage_controls_enabled(False)
        self.model_storage_status.setText(
            tr("请选择一个文件夹；关闭选择器不会更改任何文件。",
               "Choose a folder. Closing the picker will not change any files."))
        logger.info("打开模型目录选择器: mode=%s current=%s",
                    "relocate" if switch_default else "import", current)
        dialog.open()
        QTimer.singleShot(0, dialog.raise_)
        QTimer.singleShot(0, dialog.activateWindow)

    def _on_storage_folder_selected(
        self, selected: str, *, switch_default: bool
    ) -> None:
        if not selected:
            return
        logger.info("模型目录已选择: mode=%s selected=%s",
                    "relocate" if switch_default else "import", selected)
        self._begin_model_migration(Path(selected), switch_default=switch_default)

    def _on_storage_dialog_finished(self, dialog: QFileDialog) -> None:
        if dialog is not self._storage_dialog:
            return
        self._storage_dialog = None
        dialog.deleteLater()
        if self._storage_worker is None:
            self._set_storage_controls_enabled(True)

    def _begin_model_migration(self, selected: Path, *, switch_default: bool) -> None:
        if self._storage_worker is not None:
            return
        if self._storage_change_guard is not None and self._storage_change_guard():
            self.model_storage_status.setText(
                tr("请先等待模型广场中的下载完成或取消下载，再迁移模型。",
                   "Wait for or cancel active Model Hub downloads before moving models."))
            return

        active_root = resolve_models_root(self._store)
        source, destination = (active_root, selected) if switch_default else (selected, active_root)
        self._storage_switch_destination = destination if switch_default else None
        self._set_storage_controls_enabled(False)
        self.model_storage_status.setText(
            tr("正在后台迁移模型，请保持应用打开…",
               "Moving models in the background. Keep VoxSub open…"))
        worker = _ModelMigrationWorker(source, destination, self)
        worker.completed.connect(self._on_model_migration_completed)
        worker.finished.connect(self._on_model_migration_finished)
        worker.finished.connect(worker.deleteLater)
        self._storage_worker = worker
        logger.info("模型迁移任务开始: mode=%s source=%s destination=%s",
                    "relocate" if switch_default else "import", source, destination)
        worker.start()

    def _on_model_migration_completed(self, success: bool, detail: str) -> None:
        # The result signal is queued to the GUI thread. Waiting here would
        # block the settings page while QThread is still unwinding, especially
        # for a cross-drive multi-gigabyte move. The finished signal clears the
        # reference after run() has returned.
        switch_to, self._storage_switch_destination = self._storage_switch_destination, None
        if not success:
            self.model_storage_status.setText(
                f"{tr('迁移失败', 'Move failed')}: {detail}")
            return
        if switch_to is not None:
            self._store.update({
                "models_root": str(switch_to),
                "models_root_mode": "custom",
                "model_storage_initialized": True,
            })
        self._refresh_model_storage()
        self._refresh_ocr_cache_storage()
        self.model_storage_status.setText(
            f"{tr('模型迁移完成', 'Model move complete')}: {detail}")
        self.model_storage_changed.emit(str(resolve_models_root(self._store)))

    def _on_model_migration_finished(self) -> None:
        worker = self.sender()
        if worker is self._storage_worker:
            self._storage_worker = None
            if self._storage_dialog is None:
                self._set_storage_controls_enabled(True)
            logger.info("模型迁移工作线程已结束")

    def _storage_worker_is_running(self) -> bool:
        return self._storage_worker is not None and self._storage_worker.isRunning()

    def can_close_application(self) -> bool:
        """Prevent Qt from destroying a live migration thread on app exit."""
        if self._storage_dialog is not None:
            self._storage_dialog.reject()
        if not self._storage_worker_is_running():
            return True
        self.model_storage_status.setText(
            tr("模型仍在后台迁移，请等待完成后再退出应用。",
               "Models are still moving. Wait for completion before exiting VoxSub."))
        logger.warning("迁移进行中，已阻止应用退出以保护模型文件")
        return False

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
        retranslate_widget_tree(self)

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

    def _on_language_setting_changed(self, _index: int) -> None:
        if self._loading:
            return
        value = self.language_combo.currentData() or LANGUAGE_SYSTEM
        self._store.set("language", value)
        language_manager.set_language(value)

    def _on_language_changed(self, _language: str) -> None:
        retranslate_widget_tree(self)
        self.ocr_cache_limit_spin.setSpecialValueText(tr("无限"))
        self.ocr_cache_limit_spin.setSuffix(tr(" 张/类"))
        self._update_cloud_mode_summary()
        self._set_tuning_dirty(self._tuning_dirty)
        self._refresh_model_storage()
        self._refresh_ocr_cache_storage()
        self._update_tts_status()
        self._update_sentry_status()
        if hasattr(self, "release_history_label"):
            self.release_history_label.setText(release_history_text())

    def set_embedded(self, embedded: bool = True) -> None:
        """Switch between the legacy top-level presentation and page embedding."""
        self._embedded = embedded
        if embedded:
            self.setMinimumSize(0, 0)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        else:
            self.setMinimumSize(640, 520)
            self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def prepare_for_page_leave(self) -> None:
        """Apply the same discard/save boundary used by closing the old window."""
        self._discard_asr_tuning()
        self.save_cloud_credentials()

    # 测试 / 程序化访问入口
    def current_tier(self) -> str:
        if self.tier_fast.isChecked():
            return "fast"
        if self.tier_quality.isChecked():
            return "quality"
        if self.tier_cloud.isChecked():
            return "cloud"
        return "fast"

    def current_stt_provider(self) -> str:
        return "cloud" if self.stt_cloud_radio.isChecked() else "local"

    def save_cloud_credentials(self) -> None:
        """保存 STT 与翻译两套云 API 凭据（失焦或关窗时调用）。"""
        self._store.update(
            {
                "stt_api_key": self.stt_api_key_edit.text().strip(),
                "stt_base_url": self.stt_base_url_edit.text().strip(),
                "stt_model": self.stt_model_edit.text().strip(),
                "translate_api_key": self.translate_api_key_edit.text().strip(),
                "translate_base_url": self.translate_base_url_edit.text().strip(),
                "translate_model": self.translate_model_edit.text().strip(),
                # Keep old keys synchronized for scripts and 0.3.x tooling.
                "api_key": self.translate_api_key_edit.text().strip(),
                "base_url": self.translate_base_url_edit.text().strip(),
                "model": self.translate_model_edit.text().strip(),
            }
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._storage_dialog is not None:
            self._storage_dialog.reject()
        if self._storage_worker_is_running():
            # Never let Qt destroy a live QThread. The move cannot be safely
            # interrupted in the middle of a file operation, so keep this
            # page alive until it reports completion instead of aborting.
            self.model_storage_status.setText(
                tr("模型仍在后台迁移，完成后才可以关闭此页面。",
                   "Model migration is still running; this page will close when it finishes."))
            event.ignore()
            return
        # Recognition tuning is transactional: X/Alt+F4 always abandons the
        # draft unless the explicit Save button was used.
        self.prepare_for_page_leave()
        super().closeEvent(event)
