"""主窗（M7 组件清单 #1）：编辑式左右分栏。

- 左栏：模式四卡片（A 麦克风 / B 系统声音 / C 文件 / D OCR，选中高亮）
  + 语言对下拉（中→英 / 英→中）+ 状态灯（待机 / 拾音中 / 推理中，推理中脉冲）
- 右栏：实时字幕流列表（原文 + 译文两行，自动滚动，最新行短暂高亮）
- 底部：胶囊 CTA「开始 / 停止」，内嵌圆形箭头小岛（QPainter 自绘矢量，
  与 FluentIcons 播放语义等价；QFW 的 FluentIcon 无 STOP 对偶图标）

Pipeline 依赖面仅限 DESIGN.md『Pipeline 契约（M6）』，经 pipeline_client.get_pipeline()
获取；UI 测试可显式注入鸭子类型 stub（见 pipeline_client.py）。
"""
from __future__ import annotations

import time
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from voxsub.config_store import ConfigStore
from voxsub.ui.conversation_export import write_conversation_snapshot
from voxsub.ui.file_dialogs import choose_open_file, choose_save_file
from voxsub.ui.i18n import language_manager, retranslate_widget_tree, tr
from voxsub.ui.pipeline_client import get_pipeline
from voxsub.ui.pipeline_configurator import apply_pipeline_config
from voxsub.ui.selection_controls import ToggleSwitch
from voxsub.ui.theme import DESIGN_TOKENS
from voxsub.ui.view_models import ConversationSession
from voxsub.logging_setup import get_logger

logger = get_logger("ui.main_window")

# ---------------------------------------------------------------------------
# 模式元信息 + 纯函数（可单测）
# ---------------------------------------------------------------------------
MODE_INFO: dict[str, dict[str, str]] = {
    "a": {"badge": "A", "title": "麦克风同传", "desc": "选择麦克风，说完一句即生成双语字幕"},
    "b": {"badge": "B", "title": "应用 / 系统声音", "desc": "隔离指定应用，或监听所选输出设备"},
    "c": {"badge": "C", "title": "音视频字幕", "desc": "导入音频或视频，自动提音并导出 SRT"},
    "d": {"badge": "D", "title": "OCR 翻译", "desc": "截图 OCR 或选定屏幕范围实时翻译"},
}
MODE_ORDER = ("a", "b", "c", "d")

# 语言对（value, 显示标签）—— QFW ComboBox 用文本定位，不依赖 itemData
LANG_PAIRS = [
    ("zh-en", "中 → 英"),
    ("en-zh", "英 → 中"),
    ("auto-zh", "自动 → 中文"),
    ("auto-en", "自动 → 英文"),
]
_LANG_LABEL_TO_VALUE = {label: value for value, label in LANG_PAIRS}
_LANG_VALUE_TO_LABEL = {value: label for value, label in LANG_PAIRS}

STATUS_STYLES: dict[str, dict[str, str]] = {
    "idle": {"color": "neutral"},
    "listening": {"color": "accent"},
    "working": {"color": "warning"},
    "success": {"color": "success"},
    "error": {"color": "error"},
}


def _status_kind(status: str) -> str:
    """把带设备名/进度详情的运行状态归一为少量视觉语义。"""
    if status.startswith(("启动失败", "音频设备错误", "识别处理错误", "文件处理失败", "文件不存在")):
        return "error"
    if status.startswith(("完成", "已停止")):
        return "success"
    if status.startswith("拾音中"):
        return "listening"
    if status.startswith(("启动", "正在", "处理中", "翻译中", "推理中")):
        return "working"
    return "idle"


def cycle_mode(current: str) -> str:
    """模式轮换 a → b → c → d → a（托盘 / 快捷键复用；非法值回落 a）。"""
    if current not in MODE_ORDER:
        return "a"
    idx = (MODE_ORDER.index(current) + 1) % len(MODE_ORDER)
    return MODE_ORDER[idx]


# ---------------------------------------------------------------------------
# 模式卡片
# ---------------------------------------------------------------------------
class ModeCard(QFrame):
    """可点击模式卡：badge + 标题 + 描述；selected 高亮（QSS 动态属性驱动）。"""

    clicked = Signal(str)  # 携带模式键 "a"/"b"/"c"/"d"

    def __init__(self, mode: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = mode
        self.setObjectName("modeCard")
        self.setProperty("active", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(64)
        self.setMaximumHeight(68)

        info = MODE_INFO[mode]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(0)
        top = QHBoxLayout()
        top.setSpacing(8)
        badge = QLabel(info["badge"], self)
        badge.setObjectName("modeBadge")
        title = QLabel(info["title"], self)
        title.setObjectName("modeTitle")
        title.setWordWrap(True)
        top.addWidget(badge)
        top.addWidget(title)
        top.addStretch(1)
        desc = QLabel(info["desc"], self)
        desc.setObjectName("modeDesc")
        desc.setWordWrap(True)
        lay.addLayout(top)
        desc.hide()
        self.setToolTip(info["desc"])

    def set_active(self, active: bool) -> None:
        self.setProperty("active", bool(active))
        # 强制重刷 QSS 动态属性选择器
        self.style().unpolish(self)
        self.style().polish(self)

    def is_active(self) -> bool:
        return bool(self.property("active"))

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802 (Qt 命名)
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit(self.mode)
        super().mouseReleaseEvent(ev)


# ---------------------------------------------------------------------------
# 状态灯
# ---------------------------------------------------------------------------
class StatusLight(QWidget):
    """圆点状态灯 + 文本；『推理中』时圆点 240ms OutCubic 脉冲（动效阈值内）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(42)
        self._light_color = DESIGN_TOKENS["dark"]["neutral"]
        self._pulsing = False
        self._pulse_anim: QPropertyAnimation | None = None
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(10)
        self.dot = QLabel("●", self)
        self.dot.setObjectName("statusDot")
        self.dot.setStyleSheet(f"color: {self._light_color}; font-size: 18px;")
        self.dot.setFixedWidth(24)
        self.text = QLabel("待机", self)
        self.text.setObjectName("statusText")
        self.text.setWordWrap(True)
        lay.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.text, 1, Qt.AlignmentFlag.AlignVCenter)

    def set_status(self, status: str, theme_name: str = "dark") -> None:
        """status ∈ {待机, 拾音中, 推理中}；推理中进入脉冲。"""
        self.text.setText(status)
        self.text.setToolTip(status)
        kind = _status_kind({
            "Idle": "待机",
            "Listening": "拾音中",
            "Processing": "推理中",
            "Translating": "翻译中",
        }.get(status, status))
        mapping = STATUS_STYLES[kind]
        color_key = mapping["color"]
        color = DESIGN_TOKENS[theme_name].get(
            color_key, DESIGN_TOKENS["dark"][color_key]
        )
        self._light_color = color
        self.dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        self.set_pulsing(kind == "working")

    def set_pulsing(self, pulsing: bool) -> None:
        if pulsing == self._pulsing:
            return
        self._pulsing = pulsing
        if pulsing:
            # 240ms OutCubic 呼吸脉冲（DESIGN.md 动效阈值 200-280ms / >500ms 禁用）
            anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
            anim.setDuration(240)
            anim.setStartValue(1.0)
            anim.setEndValue(0.35)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setLoopCount(-1)  # 无限往返
            anim.setDirection(QPropertyAnimation.Direction.Forward)
            anim.finished.connect(self._pulse_turn)  # type: ignore[attr-defined]
            self._pulse_anim = anim
            anim.start()
        else:
            if self._pulse_anim is not None:
                self._pulse_anim.stop()
                self._pulse_anim.deleteLater()
                self._pulse_anim = None
            self._opacity_effect.setOpacity(1.0)

    def _pulse_turn(self) -> None:
        """往返动画：finished 时反向（模拟呼吸）。"""
        if self._pulse_anim is None:
            return
        if self._pulse_anim.direction() == QPropertyAnimation.Direction.Forward:
            self._pulse_anim.setDirection(QPropertyAnimation.Direction.Backward)
        else:
            self._pulse_anim.setDirection(QPropertyAnimation.Direction.Forward)


# ---------------------------------------------------------------------------
# 字幕流列表
# ---------------------------------------------------------------------------
_SUBROW_NEWEST_MS = 280  # 最新行高亮时长（动效阈值内）


class SubtitleList(QScrollArea):
    """实时字幕流：原文 + 译文两行；自动滚底；最新行短暂高亮；空状态引导语。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("subtitleScroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget(self)
        self._vbox = QVBoxLayout(self._container)
        self._vbox.setContentsMargins(8, 8, 12, 8)
        self._vbox.setSpacing(10)
        self._vbox.addStretch(1)
        self.setWidget(self._container)

        self._rows: list[QFrame] = []
        self._partial_row: QFrame | None = None
        self._partial_src: QLabel | None = None
        self._partial_dst: QLabel | None = None
        self._empty_hint = QLabel("字幕将显示在这里 —— 选择模式后点击「开始」", self._container)
        self._empty_hint.setObjectName("emptyHint")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._vbox.insertWidget(0, self._empty_hint)
        self._vbox.setStretch(0, 1)

    def add_subtitle(self, src: str, dst: str) -> None:
        """追加一条双语字幕并自动滚底；超出 200 条丢弃最旧（内存滚动）。"""
        self.clear_partial()
        self._empty_hint.hide()
        row = QFrame(self._container)
        row.setObjectName("subRow")
        row.setProperty("newest", True)
        box = QVBoxLayout(row)
        box.setContentsMargins(14, 10, 14, 10)
        box.setSpacing(4)
        src_label = QLabel(src, row)
        src_label.setObjectName("srcText")
        src_label.setWordWrap(True)
        src_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        src_label.setCursor(Qt.CursorShape.IBeamCursor)
        dst_label = QLabel(dst, row)
        dst_label.setObjectName("dstText")
        dst_label.setWordWrap(True)
        dst_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        dst_label.setCursor(Qt.CursorShape.IBeamCursor)
        box.addWidget(src_label)
        box.addWidget(dst_label)
        self._vbox.insertWidget(self._vbox.count() - 1, row)  # stretch 前插入

        # 最新行高亮 280ms 后回落
        def _clear_newest() -> None:
            row.setProperty("newest", False)
            row.style().unpolish(row)
            row.style().polish(row)

        QTimer.singleShot(_SUBROW_NEWEST_MS, _clear_newest)

        self._rows.append(row)
        if len(self._rows) > 200:  # 内存滚动上限
            old = self._rows.pop(0)
            old.setParent(None)
            old.deleteLater()

        # 自动滚底
        QTimer.singleShot(0, self._scroll_to_bottom)

    def set_partial(self, src: str, dst: str | None = None) -> None:
        """原位更新一条双语草稿，不写入会话历史。"""
        src = src.strip()
        if not src:
            self.clear_partial()
            return
        self._empty_hint.hide()
        if self._partial_row is None:
            row = QFrame(self._container)
            row.setObjectName("subRow")
            row.setProperty("partial", True)
            box = QVBoxLayout(row)
            box.setContentsMargins(14, 10, 14, 10)
            box.setSpacing(4)
            src_label = QLabel(src, row)
            src_label.setObjectName("srcText")
            src_label.setWordWrap(True)
            src_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            src_label.setCursor(Qt.CursorShape.IBeamCursor)
            hint = QLabel(dst or tr("识别中…"), row)
            hint.setObjectName("dstText")
            hint.setWordWrap(True)
            hint.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            hint.setCursor(Qt.CursorShape.IBeamCursor)
            box.addWidget(src_label)
            box.addWidget(hint)
            self._vbox.insertWidget(self._vbox.count() - 1, row)
            self._partial_row = row
            self._partial_src = src_label
            self._partial_dst = hint
        elif self._partial_src is not None:
            self._partial_src.setText(src)
            if self._partial_dst is not None:
                self._partial_dst.setText(dst or tr("识别中…"))
        QTimer.singleShot(0, self._scroll_to_bottom)

    def clear_partial(self) -> None:
        row, self._partial_row = self._partial_row, None
        self._partial_src = None
        self._partial_dst = None
        if row is not None:
            row.setParent(None)
            row.deleteLater()

    def count(self) -> int:
        return len(self._rows)

    def set_empty_hint(self, text: str) -> None:
        self._empty_hint.setText(text)

    def clear(self) -> None:
        self.clear_partial()
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._empty_hint.show()

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


# ---------------------------------------------------------------------------
# 胶囊 CTA（开始/停止）
# ---------------------------------------------------------------------------
class CapsuleCTA(QWidget):
    """胶囊按钮：全圆角 accent 胶囊 + 内嵌圆形箭头小岛。

    小岛与文字由 QPainter 自绘（矢量，非 emoji；FluentIcon.PLAY 语义等价）。
    running=False → 播放箭头（开始）；running=True → 停止方块。
    """

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ctaButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(168, 46)
        self._state = "idle"  # idle | starting | running | recording | paused | stopping
        self._record_mode = False
        self._hovered = False
        self._pressed = False

    def set_running(self, running: bool) -> None:
        self._state = "running" if running else "idle"
        self.update()

    def is_running(self) -> bool:
        return self._state != "idle"

    def set_state(self, state: str) -> None:
        self._state = state if state in {
            "idle", "starting", "running", "recording", "paused", "stopping"
        } else "idle"
        self.update()

    def state(self) -> str:
        return self._state

    def set_record_mode(self, enabled: bool) -> None:
        self._record_mode = bool(enabled)
        self.update()

    # -- 事件 ---------------------------------------------------------------
    def enterEvent(self, ev) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            if self.rect().contains(ev.position().toPoint()):
                self.clicked.emit()
        self.update()
        super().mouseReleaseEvent(ev)

    # -- 绘制 ---------------------------------------------------------------
    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        accent = QColor(self._accent())
        accent_deep = QColor(DESIGN_TOKENS["dark"]["accent_deep"])

        # 胶囊底（暂停态用深色底 + accent 描边；运行态用 accent 实底）
        pill = QPainterPath()
        pill.addRoundedRect(1, 1, w - 2, h - 2, h / 2, h / 2)
        active = self._state != "idle"
        if active:
            base = accent_deep if self._pressed else accent
        else:
            base = QColor(
                *self._parse_rgb(DESIGN_TOKENS["dark"]["surface_2"]), 205
            ) if self._pressed else QColor(
                *self._parse_rgb(DESIGN_TOKENS["dark"]["surface_1"]), 235
            )
        p.fillPath(pill, base)
        border = QColor("#14B8A6") if not active else QColor("#0D9488").lighter(120)
        p.setPen(QPen(border if self._hovered else QColor("#14B8A6"), 1.5))
        p.drawPath(pill)

        # 内嵌圆形小岛（左侧）
        r = 14
        cx, cy = 20 + r, h / 2
        p.setBrush(Qt.GlobalColor.white)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))
        # 岛内箭头：开始=右向三角 / 停止=圆角方块
        p.setBrush(QColor("#0D9488"))
        if self._state == "running":  # 停止
            s = 10
            path = QPainterPath()
            path.addRoundedRect(cx - s / 2, cy - s / 2, s, s, 2, 2)
            p.drawPath(path)
        elif self._state == "recording":  # 暂停
            p.drawRoundedRect(int(cx - 6), int(cy - 8), 4, 16, 1, 1)
            p.drawRoundedRect(int(cx + 2), int(cy - 8), 4, 16, 1, 1)
        else:  # 开始 / 继续：播放三角（略右偏视觉居中）
            tri = QPainterPath()
            tri.moveTo(cx - 5, cy - 8)
            tri.lineTo(cx - 5, cy + 8)
            tri.lineTo(cx + 8, cy)
            tri.closeSubpath()
            p.drawPath(tri)

        # 文字
        p.setPen(Qt.GlobalColor.white if active else QColor("#E6FFFFFF"))
        f = QFont("Microsoft YaHei UI", 13, QFont.Weight.DemiBold)
        p.setFont(f)
        text = {
            "starting": tr("正在启动…"),
            "running": tr("停止"),
            "recording": tr("暂停"),
            "paused": tr("继续"),
            "stopping": tr("正在结束…"),
            "idle": tr("开始录音") if self._record_mode else tr("开始"),
        }[self._state]
        p.drawText(
            int(cx + r + 14), 0, int(w - (cx + r + 14) - 12), int(h),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text,
        )
        p.end()

    # -- 颜色工具 -----------------------------------------------------------
    def _accent(self) -> str:
        return DESIGN_TOKENS["dark"]["accent"]

    @staticmethod
    def _parse_rgb(hex_color: str) -> list[int]:
        h = hex_color.lstrip("#")
        return [int(h[i : i + 2], 16) for i in (0, 2, 4)]


class _PipelineBridge(QObject):
    """把 Pipeline 工作线程回调安全地转发到 Qt 主线程。"""

    utterance = Signal(str, str)
    partial = Signal(str)
    draft = Signal(str, str)
    status = Signal(str)
    progress = Signal(int, int, str)
    operation_done = Signal(str, bool, str, bool)
    export_done = Signal(str, bool, str)


# ---------------------------------------------------------------------------
# 主窗
# ---------------------------------------------------------------------------
class MainWindow(QWidget):
    """语幕 VoxSub 主窗（软高级感左右分栏编辑式布局）。"""

    settings_requested = Signal()
    diagnostics_requested = Signal()
    model_hub_requested = Signal()
    running_state_changed = Signal(bool)

    def __init__(
        self,
        store: ConfigStore | None = None,
        pipeline: object | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("rootShell")
        self.setWindowTitle("语幕 VoxSub")
        self.resize(1120, 720)
        self.setMinimumSize(920, 620)
        self._store = store or ConfigStore()
        # Pipeline 依赖面（M6 或 stub）
        self.pipeline = pipeline if pipeline is not None else get_pipeline()

        # -- 当前状态 --------------------------------------------------------
        self._mode = self._store.get("mode", "a")
        self._lang_pair = self._store.get("lang_pair", "zh-en")
        self._overlay = None  # 由 app.py 注入（避免硬依赖，保持可单测）
        self._conversation_session = ConversationSession()
        # Compatibility view retained for integrations that inspect the list.
        self._conversation = self._conversation_session.entries
        self._bridge = _PipelineBridge(self)
        self._pipeline_busy = False
        self._pipeline_worker: threading.Thread | None = None
        self._session_export_busy = False
        self._session_export_worker: threading.Thread | None = None
        self._pending_record_flow = False
        self._file_progress_stage = ""
        self._file_progress_value = 0
        self._page_history: list[QWidget] = []
        self._page_titles: dict[QWidget, str] = {}
        self._embedded_pages: dict[str, QWidget] = {}

        self._build_ui()
        self._wire_pipeline()

        # 初始化状态
        self.set_mode(self._mode)
        self.set_lang_pair(self._lang_pair)
        last_file = str(self._store.get("last_input_file", "") or "")
        if last_file:
            self.file_name_label.setText(Path(last_file).name)
            self.file_name_label.setToolTip(last_file)
        language_manager.language_changed.connect(self._on_language_changed)
        self._on_language_changed(language_manager.language)

    # -- 界面构建 -----------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 主工作区和内置页面层共用一个堆叠场景。打开二级页面时，
        # 页面层覆盖整个客户区并拦截鼠标，主工作区加模糊效果作为背景。
        scene = QWidget(self)
        scene.setObjectName("mainScene")
        scene_stack = QStackedLayout(scene)
        scene_stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._main_content = QWidget(scene)
        self._main_content.setObjectName("mainContent")
        scene_stack.addWidget(self._main_content)
        self._page_layer = QFrame(scene)
        self._page_layer.setObjectName("inAppPageLayer")
        self._page_layer.setVisible(False)
        self._page_layer.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        scene_stack.addWidget(self._page_layer)
        self._page_blur = QGraphicsBlurEffect(self._main_content)
        self._page_blur.setBlurRadius(0)
        self._main_content.setGraphicsEffect(self._page_blur)

        root = QVBoxLayout(self._main_content)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(24)

        # 标题行
        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        brand = QVBoxLayout()
        brand.setSpacing(2)
        eyebrow = QLabel("VOXSUB  /  LIVE TRANSLATION", self)
        eyebrow.setObjectName("eyebrowLabel")
        title = QLabel("语幕", self)
        title.setObjectName("sectionTitle")
        title.setStyleSheet("font-size: 32px; font-weight: 650;")
        brand.addWidget(eyebrow)
        brand.addWidget(title)
        subtitle = QLabel("让对话、会议和视频，落成清晰的双语文幕。", self)
        subtitle.setObjectName("secondaryLabel")
        brand.addWidget(subtitle)
        title_row.addLayout(brand)
        title_row.addStretch(1)
        self.model_hub_btn = QPushButton("模型广场", self)
        self.model_hub_btn.setObjectName("secondaryButton")
        self.model_hub_btn.setMinimumHeight(44)
        self.model_hub_btn.clicked.connect(self.model_hub_requested.emit)
        self.overlay_open_btn = QPushButton("打开浮窗", self)
        self.overlay_open_btn.setObjectName("secondaryButton")
        self.overlay_open_btn.setMinimumHeight(44)
        self.overlay_open_btn.setToolTip("打开字幕浮窗")
        self.overlay_open_btn.clicked.connect(self._open_overlay)
        self.settings_btn = QPushButton("设置", self)
        self.settings_btn.setObjectName("ghostButton")
        self.settings_btn.setMinimumHeight(44)
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        self.diagnostics_btn = QPushButton("诊断与日志", self)
        self.diagnostics_btn.setObjectName("ghostButton")
        self.diagnostics_btn.setMinimumHeight(44)
        self.diagnostics_btn.clicked.connect(self.diagnostics_requested.emit)
        title_row.addWidget(self.model_hub_btn)
        title_row.addWidget(self.overlay_open_btn)
        title_row.addWidget(self.settings_btn)
        title_row.addWidget(self.diagnostics_btn)
        root.addLayout(title_row)

        # 左右分栏
        body = QHBoxLayout()
        body.setSpacing(24)
        body.addWidget(self._build_left_panel(), 0)
        self._workspace_stack = QStackedWidget(self)
        self._workspace_stack.setObjectName("mainWorkspaceStack")
        self._translation_workspace = self._build_right_panel()
        self._workspace_stack.addWidget(self._translation_workspace)
        self._ocr_workspace: QWidget | None = None
        body.addWidget(self._workspace_stack, 1)
        root.addLayout(body, 1)

        # 底部胶囊 CTA 居中
        cta_row = QHBoxLayout()
        cta_row.addStretch(1)
        self.cta = CapsuleCTA(self)
        self.cta.clicked.connect(self._toggle_run)
        cta_row.addWidget(self.cta)
        self.finish_record_btn = QPushButton("结束并保存", self)
        self.finish_record_btn.setObjectName("secondaryButton")
        self.finish_record_btn.setMinimumHeight(44)
        self.finish_record_btn.clicked.connect(self._finish_recording)
        self.finish_record_btn.hide()
        cta_row.addSpacing(8)
        cta_row.addWidget(self.finish_record_btn)
        cta_row.addStretch(1)
        root.addLayout(cta_row)

        page_root = QVBoxLayout(self._page_layer)
        page_root.setContentsMargins(24, 18, 24, 24)
        page_root.setSpacing(12)
        page_header = QFrame(self._page_layer)
        page_header.setObjectName("inAppPageHeader")
        page_header_box = QHBoxLayout(page_header)
        page_header_box.setContentsMargins(0, 0, 0, 0)
        page_header_box.setSpacing(12)
        self._page_back_btn = QPushButton("返回", page_header)
        self._page_back_btn.setObjectName("secondaryButton")
        self._page_back_btn.setMinimumHeight(42)
        self._page_back_btn.clicked.connect(self.close_in_app_page)
        self._page_title_label = QLabel("", page_header)
        self._page_title_label.setObjectName("inAppPageTitle")
        page_header_box.addWidget(self._page_back_btn)
        page_header_box.addWidget(self._page_title_label)
        page_header_box.addStretch(1)
        page_root.addWidget(page_header)

        self._page_surface = QFrame(self._page_layer)
        self._page_surface.setObjectName("inAppPageSurface")
        surface_layout = QVBoxLayout(self._page_surface)
        surface_layout.setContentsMargins(0, 0, 0, 0)
        surface_layout.setSpacing(0)
        self._page_stack = QStackedWidget(self._page_surface)
        self._page_stack.setObjectName("inAppPageStack")
        surface_layout.addWidget(self._page_stack)
        page_root.addWidget(self._page_surface, 1)

        outer.addWidget(scene, 1)

    # -- 内置页面导航 -------------------------------------------------------
    def install_in_app_pages(
        self,
        settings_page: QWidget,
        model_hub_page: QWidget,
        ocr_page: QWidget | None = None,
    ) -> None:
        """Install the regular secondary pages into the main-window shell."""
        self._register_embedded_page("settings", settings_page, "设置")
        self._register_embedded_page("model_hub", model_hub_page, "模型广场")
        if ocr_page is not None:
            prepare = getattr(ocr_page, "set_embedded", None)
            if callable(prepare):
                prepare(True)
            ocr_page.setParent(self._workspace_stack)
            self._workspace_stack.addWidget(ocr_page)
            self._ocr_workspace = ocr_page
            if self._mode == "d":
                self._workspace_stack.setCurrentWidget(ocr_page)
                warmup = getattr(ocr_page, "prepare_live", None)
                if callable(warmup):
                    warmup()
        signal = getattr(settings_page, "model_hub_requested", None)
        if signal is not None:
            signal.connect(self.show_model_hub_page)
        tts_signal = getattr(settings_page, "tts_settings_changed", None)
        if tts_signal is not None:
            tts_signal.connect(self._on_tts_settings_changed)

    def _on_tts_settings_changed(self, enabled: bool,
                                 zh_model_id: str, en_model_id: str) -> None:
        """Apply voice settings immediately, including during a live session."""
        try:
            self.pipeline.set_tts_models({
                "zh": str(zh_model_id),
                "en": str(en_model_id),
            })
            self.pipeline.set_tts(bool(enabled))
        except AttributeError:
            logger.debug("Pipeline 缺少 TTS 动态配置接口")

    def _register_embedded_page(self, key: str, page: QWidget, title: str) -> None:
        prepare = getattr(page, "set_embedded", None)
        if callable(prepare):
            prepare(True)
        page.setParent(self._page_stack)
        self._page_stack.addWidget(page)
        self._embedded_pages[key] = page
        self._page_titles[page] = title

    def _open_in_app_page(self, key: str) -> None:
        page = self._embedded_pages.get(key)
        if page is None:
            logger.warning("内置页面未安装: %s", key)
            return
        current = self._page_stack.currentWidget()
        if current is not page and current is not None and self._page_layer.isVisible():
            self._page_history.append(current)
        self._page_stack.setCurrentWidget(page)
        if key == "settings":
            refresh = getattr(page, "refresh_devices", None)
            if callable(refresh):
                refresh()
        elif key == "model_hub":
            refresh = getattr(page, "refresh", None)
            if callable(refresh):
                refresh()
        self._page_title_label.setText(tr(self._page_titles[page]))
        self._page_back_btn.setText(tr("返回"))
        self._page_layer.setVisible(True)
        self._page_layer.raise_()
        self._page_blur.setBlurRadius(12)
        self._page_layer.setFocus(Qt.FocusReason.OtherFocusReason)

    def show_settings_page(self) -> None:
        self._open_in_app_page("settings")

    def show_model_hub_page(self) -> None:
        self._open_in_app_page("model_hub")

    def show_ocr_page(self) -> None:
        self.set_mode("d")

    def close_in_app_page(self) -> None:
        if not self._page_layer.isVisible():
            return
        current = self._page_stack.currentWidget()
        if self._page_history:
            previous = self._page_history.pop()
            self._page_stack.setCurrentWidget(previous)
            self._page_title_label.setText(tr(self._page_titles[previous]))
            refresh = getattr(previous, "refresh", None)
            if callable(refresh):
                refresh()
            return
        # Returning from the top-level settings page is the embedded
        # equivalent of closing the old settings window: drafts are discarded
        # and cloud credentials are flushed at this boundary.
        prepare = getattr(current, "prepare_for_page_leave", None)
        if callable(prepare):
            prepare()
        self._page_layer.setVisible(False)
        self._page_blur.setBlurRadius(0)
        self._main_content.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self._page_layer.isVisible():
            self.close_in_app_page()
            event.accept()
            return
        super().keyPressEvent(event)

    def _build_left_panel(self) -> QFrame:
        panel = QFrame(self)
        panel.setObjectName("sidePanel")
        panel.setFixedWidth(344)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(8)

        mode_title = QLabel("模式", panel)
        mode_title.setObjectName("sectionTitle")
        lay.addWidget(mode_title)

        self.mode_cards: dict[str, ModeCard] = {}
        mode_grid = QGridLayout()
        mode_grid.setContentsMargins(0, 0, 0, 0)
        mode_grid.setHorizontalSpacing(8)
        mode_grid.setVerticalSpacing(8)
        for m in MODE_ORDER:
            card = ModeCard(m, panel)
            card.clicked.connect(self.set_mode)
            self.mode_cards[m] = card
            index = MODE_ORDER.index(m)
            mode_grid.addWidget(card, index // 2, index % 2)
        lay.addLayout(mode_grid)

        lay.addSpacing(4)
        self.pair_label = QLabel("语言对", panel)
        self.pair_label.setObjectName("sectionTitle")
        lay.addWidget(self.pair_label)
        # QFluentWidgets ComboBox（随 QFW 主题自动着色，见 DESIGN 组件清单 #1）
        from qfluentwidgets import ComboBox as FComboBox

        self.lang_combo: QComboBox = FComboBox(panel)
        self.lang_combo.setObjectName("langCombo")
        self.lang_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        for value, label in LANG_PAIRS:
            self.lang_combo.addItem(label, value)
        self.lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        lay.addWidget(self.lang_combo)

        lay.addSpacing(4)
        self.status_light = StatusLight(panel)
        lay.addWidget(self.status_light)
        self.source_hint = QLabel("设备可在「设置」中选择", panel)
        self.source_hint.setObjectName("secondaryLabel")
        self.source_hint.setWordWrap(True)
        lay.addWidget(self.source_hint)
        lay.addStretch(1)
        return panel

    def _build_right_panel(self) -> QFrame:
        panel = QFrame(self)
        panel.setObjectName("subtitlePanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        head = QHBoxLayout()
        self.workspace_title = QLabel("实时字幕", panel)
        self.workspace_title.setObjectName("sectionTitle")
        head.addWidget(self.workspace_title)
        self.workspace_context = QLabel("原文与译文只保留在本次会话内", panel)
        self.workspace_context.setObjectName("secondaryLabel")
        head.addWidget(self.workspace_context)
        head.addStretch(1)
        # The floating window owns its own controls.  Keep these references as
        # a small compatibility bridge for integrations from earlier builds,
        # but do not place them in the main workspace anymore.
        self.overlay_font_down_btn = QPushButton("A  −", panel)
        self.overlay_font_down_btn.setObjectName("compactGhostButton")
        self.overlay_font_down_btn.setToolTip("减小字幕浮窗字号")
        self.overlay_font_down_btn.setFixedSize(80, 44)
        self.overlay_font_down_btn.clicked.connect(lambda: self._change_overlay_font(-2))
        self.overlay_font_up_btn = QPushButton("A  +", panel)
        self.overlay_font_up_btn.setObjectName("compactGhostButton")
        self.overlay_font_up_btn.setToolTip("增大字幕浮窗字号")
        self.overlay_font_up_btn.setFixedSize(80, 44)
        self.overlay_font_up_btn.clicked.connect(lambda: self._change_overlay_font(+2))
        self.overlay_lock_btn = QPushButton("锁定浮窗", panel)
        self.overlay_lock_btn.setObjectName("ghostButton")
        self.overlay_lock_btn.setFixedHeight(44)
        self.overlay_lock_btn.setToolTip("锁定后鼠标点击会穿过浮窗，作用到下面的软件")
        self.overlay_lock_btn.clicked.connect(self._toggle_overlay_lock)
        for control in (self.overlay_font_down_btn, self.overlay_font_up_btn,
                        self.overlay_lock_btn):
            control.hide()
        self.save_conversation_btn = QPushButton("保存", panel)
        self.save_conversation_btn.setObjectName("ghostButton")
        self.save_conversation_btn.clicked.connect(self._begin_save_conversation)
        self.clear_conversation_btn = QPushButton("清空", panel)
        self.clear_conversation_btn.setObjectName("ghostButton")
        self.clear_conversation_btn.clicked.connect(self.clear_conversation)
        for button in (self.save_conversation_btn, self.clear_conversation_btn):
            button.setMinimumHeight(40)
        lay.addLayout(head)
        action_frame = QFrame(panel)
        action_frame.setObjectName("actionRow")
        action_row = QHBoxLayout(action_frame)
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        session_label = QLabel("会话", action_frame)
        session_label.setObjectName("secondaryLabel")
        action_row.addWidget(session_label)
        session_hint = QLabel("字幕只保留在当前窗口，可随时导出", action_frame)
        session_hint.setObjectName("secondaryLabel")
        action_row.addWidget(session_hint)
        action_row.addStretch(1)
        action_row.addWidget(self.save_conversation_btn)
        action_row.addWidget(self.clear_conversation_btn)
        lay.addWidget(action_frame)

        # C 模式的文件入口属于主要工作区，放在右侧可避免挤压模式导航，
        # 也让“导入 → 处理 → 字幕结果”的层级更自然。
        self.file_panel = QFrame(panel)
        self.file_panel.setObjectName("filePickerCard")
        file_box = QHBoxLayout(self.file_panel)
        file_box.setContentsMargins(20, 16, 16, 16)
        file_box.setSpacing(16)
        file_copy = QVBoxLayout()
        file_copy.setSpacing(4)
        file_head = QLabel("导入音频或视频", self.file_panel)
        file_head.setObjectName("sectionTitle")
        self.file_name_label = QLabel("尚未选择文件 · 将自动提取音频并导出同名 SRT", self.file_panel)
        self.file_name_label.setObjectName("secondaryLabel")
        self.file_name_label.setWordWrap(True)
        file_copy.addWidget(file_head)
        file_copy.addWidget(self.file_name_label)
        self.file_progress_label = QLabel("", self.file_panel)
        self.file_progress_label.setObjectName("secondaryLabel")
        self.file_progress_label.setWordWrap(True)
        self.file_progress_label.hide()
        file_copy.addWidget(self.file_progress_label)
        self.file_progress = QProgressBar(self.file_panel)
        self.file_progress.setObjectName("fileTranslationProgress")
        self.file_progress.setRange(0, 100)
        self.file_progress.setValue(0)
        self.file_progress.setTextVisible(False)
        self.file_progress.hide()
        file_copy.addWidget(self.file_progress)
        self.file_pick_btn = QPushButton("选择文件", self.file_panel)
        self.file_pick_btn.setObjectName("secondaryButton")
        self.file_pick_btn.setMinimumWidth(112)
        self.file_pick_btn.setMinimumHeight(44)
        self.file_pick_btn.clicked.connect(self.select_input_file)
        file_box.addLayout(file_copy, 1)
        file_box.addWidget(self.file_pick_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.file_panel)

        self.record_panel = QFrame(panel)
        self.record_panel.setObjectName("filePickerCard")
        record_box = QHBoxLayout(self.record_panel)
        record_box.setContentsMargins(16, 12, 16, 12)
        record_box.setSpacing(12)
        self.record_switch = ToggleSwitch("同时录音", self.record_panel)
        self.record_switch.setChecked(bool(self._store.get("record_with_translation", False)))
        self.record_switch.setToolTip("翻译麦克风声音的同时保存本地 WAV；暂停期间不会写入录音")
        self.record_switch.toggled.connect(self._on_recording_toggled)
        self.record_status = QLabel(
            ("像手机录音：开始 → 暂停 / 继续 → 结束并保存"
             if self.record_switch.isChecked() else
             "仅生成字幕，不保存麦克风音频"),
            self.record_panel,
        )
        self.record_status.setObjectName("secondaryLabel")
        self.record_status.setWordWrap(True)
        record_box.addWidget(self.record_switch, 0)
        record_box.addWidget(self.record_status, 1)
        lay.addWidget(self.record_panel)

        self.subtitle_list = SubtitleList(panel)
        lay.addWidget(self.subtitle_list, 1)
        return panel

    # -- Pipeline 接线 ------------------------------------------------------
    def _wire_pipeline(self) -> None:
        """订阅 utterance / status 回调（契约见 DESIGN.md M6）。"""
        try:
            # 同线程 emit 直接调用，工作线程 emit 自动排队到 Qt 主线程。
            self._bridge.utterance.connect(self._on_utterance)
            self._bridge.partial.connect(self._on_partial)
            self._bridge.draft.connect(self._on_draft)
            self._bridge.status.connect(self._on_status)
            self._bridge.progress.connect(self._on_file_progress)
            self._bridge.operation_done.connect(self._on_pipeline_operation_done)
            self._bridge.export_done.connect(self._on_export_done)
            self.pipeline.on_utterance(self._bridge.utterance.emit)
            on_draft = getattr(self.pipeline, "on_draft", None)
            if callable(on_draft):
                on_draft(self._bridge.draft.emit)
            else:
                self.pipeline.on_partial(self._bridge.partial.emit)
            self.pipeline.on_status(self._bridge.status.emit)
            on_progress = getattr(self.pipeline, "on_progress", None)
            if callable(on_progress):
                on_progress(self._bridge.progress.emit)
        except AttributeError:
            # 鸭子类型兜底：对象缺方法也不崩壳（设计内行为 → debug）
            logger.debug("Pipeline 缺少 on_utterance/on_status, 跳过订阅 (stub 联调期正常)")

    # -- 状态切换 -----------------------------------------------------------
    def _mode_switch_blocked(self, norm: str) -> bool:
        if self._pipeline_busy and norm != self._mode:
            self._on_status("正在启动或结束任务，请稍候")
            return True
        try:
            if self.pipeline.is_running() and norm != self._mode:
                self._on_status("请先停止当前任务，再切换模式")
                return True
        except AttributeError:
            pass
        return False

    def _set_pipeline_mode(self, mode: str) -> None:
        if mode == "d":
            return
        try:
            self.pipeline.set_mode(mode)
        except AttributeError:
            logger.debug("Pipeline 缺少 set_mode, 跳过 (stub 联调期正常)")

    def _activate_ocr_workspace(self) -> None:
        if self._ocr_workspace is not None:
            self._workspace_stack.setCurrentWidget(self._ocr_workspace)
            warmup = getattr(self._ocr_workspace, "prepare_live", None)
            if callable(warmup):
                warmup()
        self.source_hint.setText(tr("输入：截图、图片或选定屏幕区域"))
        self.status_light.set_status(tr("OCR 待命"))

    def _activate_translation_workspace(self, mode: str) -> None:
        copy = {
            "a": (
                "输入：设置中选择的麦克风",
                "实时字幕",
                "原文与译文只保留在本次会话内",
                "字幕将显示在这里 —— 说完一句后自动生成",
            ),
            "b": (
                "输入：指定应用，或所选系统输出设备",
                "实时字幕",
                "其它应用声音可通过进程隔离排除",
                "字幕将显示在这里 —— 播放目标应用中的内容",
            ),
            "c": (
                "支持 MP4 / MKV / MOV / MP3 / WAV 等常见格式",
                "文件字幕",
                "自动提取音频 · 识别翻译 · 导出 SRT",
                "选择文件后，处理结果与导出位置将显示在这里",
            ),
        }[mode]
        self._workspace_stack.setCurrentWidget(self._translation_workspace)
        self.cta.set_record_mode(mode == "a" and self.record_switch.isChecked())
        self.source_hint.setText(tr(copy[0]))
        self.workspace_title.setText(tr(copy[1]))
        self.workspace_context.setText(tr(copy[2]))
        self.subtitle_list.set_empty_hint(tr(copy[3]))

    def set_mode(self, mode: str) -> None:
        norm = mode if mode in MODE_ORDER else "a"
        if self._mode_switch_blocked(norm):
            return
        self._mode = norm
        for key, card in self.mode_cards.items():
            card.set_active(key == norm)
        self._set_pipeline_mode(norm)
        self._store.set("mode", norm)
        self.file_panel.setVisible(norm == "c")
        if norm != "c":
            self._hide_file_progress()
        self.record_panel.setVisible(norm == "a")
        self.cta.setVisible(norm != "d")
        self.pair_label.setText(tr("翻译方向") if norm == "d" else tr("语言对"))
        if norm == "d":
            self._activate_ocr_workspace()
            return
        self._activate_translation_workspace(norm)

    def current_mode(self) -> str:
        return self._mode

    def cycle_mode(self) -> str:
        """轮换模式（托盘菜单 / 测试复用）。"""
        nxt = cycle_mode(self._mode)
        self.set_mode(nxt)
        return nxt

    def set_lang_pair(self, value: str) -> None:
        if value not in _LANG_VALUE_TO_LABEL:
            value = "zh-en"
        idx = next((i for i, (candidate, _label) in enumerate(LANG_PAIRS)
                    if candidate == value), -1)
        if idx >= 0 and self.lang_combo.currentIndex() != idx:
            self.lang_combo.blockSignals(True)
            self.lang_combo.setCurrentIndex(idx)
            self.lang_combo.blockSignals(False)
        self._lang_pair = value
        self._store.set("lang_pair", value)
        src, dst = value.split("-", 1)
        try:
            self.pipeline.set_langs(src, dst)
        except AttributeError:
            logger.debug("Pipeline 缺少 set_langs")

    def current_lang_pair(self) -> str:
        """当前语言对 value（供托盘 / 测试 / M6 集成读取）。"""
        index = self.lang_combo.currentIndex()
        return LANG_PAIRS[index][0] if 0 <= index < len(LANG_PAIRS) else "zh-en"

    def _on_lang_changed(self, index: int) -> None:
        self.set_lang_pair(self.current_lang_pair())

    def select_input_file(self) -> bool:
        """打开音视频选择器并把路径交给 C 模式 Pipeline。"""
        initial = self._store.get("last_input_file", "")
        initial_dir = str(Path(initial).parent) if initial else ""
        path = choose_open_file(
            self,
            tr("选择要生成字幕的音频或视频",
               "Choose an audio or video file for subtitles"),
            initial_dir,
            [
                tr("音频和视频 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.mp4 *.mkv *.mov *.avi *.webm)",
                   "Audio and video (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.mp4 *.mkv *.mov *.avi *.webm)"),
                tr("所有文件 (*)", "All files (*)"),
            ],
        )
        if not path:
            return False
        self._store.set("last_input_file", path)
        self.file_name_label.setText(Path(path).name)
        self.file_name_label.setToolTip(path)
        try:
            self.pipeline.set_input_file(path)
        except AttributeError:
            logger.debug("Pipeline 缺少 set_input_file")
        return True

    def save_conversation(self, path: str | Path | None = None) -> bool:
        """Synchronously save a conversation when a destination is supplied.

        The visible button uses :meth:`_begin_save_conversation` instead so
        generating and writing a large transcript never blocks the Qt thread.
        """
        if self._pipeline_busy:
            self._on_status("正在结束并整理剩余字幕，完成后再保存")
            return False
        if not self._conversation:
            self._on_status("当前没有可保存的字幕")
            return False
        if path is None:
            return self._begin_save_conversation()
        try:
            out = self._write_conversation_snapshot(tuple(self._conversation), Path(path))
        except OSError as exc:
            logger.exception("保存对话失败")
            self._on_status(f"{tr('保存失败')}: {exc}")
            return False
        self._on_status(f"{tr('对话已保存')} → {out}")
        return True

    @staticmethod
    def _write_conversation_snapshot(
        conversation: tuple[tuple[str, str, int], ...], path: Path
    ) -> Path:
        return write_conversation_snapshot(conversation, path)

    def _begin_save_conversation(self) -> bool:
        """Choose a destination, then export from a worker thread."""
        if self._pipeline_busy:
            self._on_status("正在结束并整理剩余字幕，完成后再保存")
            return False
        if self._session_export_busy:
            return False
        if not self._conversation:
            self._on_status("当前没有可保存的字幕")
            return False
        suggested = f"VoxSub-conversation-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        selected, chosen_filter = choose_save_file(
            self,
            tr("保存当前对话", "Save current session"),
            suggested,
            [
                tr("纯文本 (*.txt)", "Plain text (*.txt)"),
                tr("SRT 字幕 (*.srt)", "SRT subtitles (*.srt)"),
                tr("WebVTT 字幕 (*.vtt)", "WebVTT subtitles (*.vtt)"),
            ],
        )
        if not selected:
            return False
        output = Path(selected)
        if not output.suffix:
            output = output.with_suffix(
                ".srt" if "SRT" in chosen_filter else
                ".vtt" if "WebVTT" in chosen_filter else ".txt"
            )
        snapshot = self._conversation_session.snapshot()
        self._session_export_busy = True
        self.save_conversation_btn.setEnabled(False)
        self.save_conversation_btn.setText(tr("正在保存…"))
        self._on_status(tr("正在后台保存对话…"))

        def _worker() -> None:
            try:
                saved = self._write_conversation_snapshot(snapshot, output)
                success, detail = True, str(saved)
            except Exception as exc:  # noqa: BLE001
                logger.exception("后台保存对话失败")
                success, detail = False, str(exc)
            self._bridge.export_done.emit("conversation", success, detail)

        self._session_export_worker = threading.Thread(
            target=_worker, name="ui-conversation-export", daemon=True
        )
        self._session_export_worker.start()
        return True

    def _on_export_done(self, kind: str, success: bool, detail: str) -> None:
        if kind != "conversation":
            return
        self._session_export_worker = None
        self._session_export_busy = False
        self.save_conversation_btn.setText(tr("保存"))
        self.save_conversation_btn.setEnabled(not self._pipeline_busy)
        if success:
            self._on_status(f"{tr('对话已保存')} → {detail}")
        else:
            self._on_status(f"{tr('保存失败')}: {detail}")

    def clear_conversation(self) -> None:
        self._conversation_session.clear()
        self.subtitle_list.clear()
        if self._overlay is not None:
            try:
                self._overlay.clear_history()
            except AttributeError:
                pass
        self._on_status("当前对话已清空")

    def _change_overlay_font(self, delta: int) -> None:
        if self._overlay is None:
            return
        try:
            self._overlay.change_font_size(delta)
            self._overlay.show()
            self._overlay.raise_()
        except AttributeError:
            logger.debug("字幕浮窗缺少字号接口")

    def _open_overlay(self) -> None:
        """Show the one app-owned overlay; repeated clicks are silent no-ops."""
        overlay = self._overlay
        if overlay is None:
            return
        try:
            if overlay.isVisible():
                return
            show_subtitles = getattr(overlay, "show_subtitles", None)
            if callable(show_subtitles):
                show_subtitles()
            else:
                overlay.show()
                overlay.raise_()
        except AttributeError:
            return

    def _toggle_overlay_lock(self) -> None:
        if self._overlay is None:
            return
        try:
            self._overlay.set_click_through(not self._overlay.is_click_through())
        except AttributeError:
            logger.debug("字幕浮窗缺少锁定接口")

    def _on_overlay_lock_changed(self, locked: bool) -> None:
        self.overlay_lock_btn.setText(tr("解锁浮窗") if locked else tr("锁定浮窗"))
        self.overlay_lock_btn.setToolTip(
            tr("点击解锁，恢复拖动、选中文字和浮窗工具栏")
            if locked else tr("锁定后鼠标点击会穿过浮窗，作用到下面的软件")
        )

    def _apply_pipeline_config(self) -> None:
        """每次启动前从设置读取完整配置，避免设置窗口与 Pipeline 脱节。"""
        cfg = self._store.load()
        try:
            apply_pipeline_config(
                self.pipeline,
                cfg,
                mode=self._mode,
                store=self._store,
            )
        except AttributeError:
            logger.debug("Pipeline 为旧契约，部分配置未接入", exc_info=True)

    # -- 启停 ---------------------------------------------------------------
    def _on_recording_toggled(self, checked: bool) -> None:
        self._store.set("record_with_translation", bool(checked))
        self.cta.set_record_mode(self._mode == "a" and bool(checked))
        self.record_status.setText(
            tr("像手机录音：开始 → 暂停 / 继续 → 结束并保存")
            if checked else tr("仅生成字幕，不保存麦克风音频")
        )

    def _set_pipeline_busy(self, busy: bool) -> None:
        self._pipeline_busy = bool(busy)
        self.cta.setEnabled(not busy)
        self.finish_record_btn.setEnabled(not busy)
        self.save_conversation_btn.setEnabled(not busy and not self._session_export_busy)
        self.clear_conversation_btn.setEnabled(not busy)
        self.lang_combo.setEnabled(not busy)
        self.file_pick_btn.setEnabled(not busy)
        for card in self.mode_cards.values():
            card.setEnabled(not busy)

    def _begin_pipeline_start(self, record_flow: bool) -> None:
        if self._pipeline_busy:
            return
        self._pending_record_flow = bool(record_flow)
        self._set_pipeline_busy(True)
        self.cta.set_state("starting")
        self.record_switch.setEnabled(False)
        if self._mode == "c":
            self._set_file_progress(0, "正在准备音视频")
        self._on_status("启动中…正在加载识别与翻译模型")

        def _worker() -> None:
            try:
                self._apply_pipeline_config()
                self.pipeline.start()
                success = bool(self.pipeline.is_running())
                error = "" if success else "Pipeline 未进入运行状态"
            except Exception as exc:
                logger.exception("后台启动 Pipeline 失败")
                success, error = False, str(exc)
            self._bridge.operation_done.emit("start", success, error, record_flow)

        self._pipeline_worker = threading.Thread(
            target=_worker, name="ui-pipeline-start", daemon=True)
        self._pipeline_worker.start()

    def _begin_pipeline_stop(self, *, finish_recording: bool = False) -> None:
        if self._pipeline_busy:
            return
        self._set_pipeline_busy(True)
        self.cta.set_state("stopping")
        self.finish_record_btn.setEnabled(False)
        self._on_status("正在结束…正在整理剩余音频与字幕")

        def _worker() -> None:
            try:
                self.pipeline.stop()
                success = not bool(self.pipeline.is_running())
                error = "" if success else "Pipeline 未完全停止"
            except Exception as exc:
                logger.exception("后台停止 Pipeline 失败")
                success, error = False, str(exc)
            self._bridge.operation_done.emit(
                "finish" if finish_recording else "stop",
                success,
                error,
                self._pending_record_flow,
            )

        self._pipeline_worker = threading.Thread(
            target=_worker, name="ui-pipeline-stop", daemon=True)
        self._pipeline_worker.start()

    def _on_pipeline_operation_done(
        self, action: str, success: bool, error: str, record_flow: bool
    ) -> None:
        self._pipeline_worker = None
        self._set_pipeline_busy(False)
        if action == "start" and success:
            self.cta.set_state("recording" if record_flow else "running")
            self.record_switch.setEnabled(False)
            self.finish_record_btn.setVisible(record_flow)
            if record_flow:
                self.record_status.setText(tr("正在录音并翻译 · 点击“暂停”可暂时停下"))
            self.running_state_changed.emit(True)
            return

        self.cta.set_state("idle")
        self.finish_record_btn.hide()
        self.finish_record_btn.setEnabled(True)
        self.record_switch.setEnabled(True)
        self.running_state_changed.emit(False)
        if not success:
            label = tr("启动") if action == "start" else tr("结束")
            self._on_status(f"{label}{tr('失败')}: {error}")
            return
        path = getattr(self.pipeline, "last_recording_path", None)
        if path and action == "finish":
            self.record_status.setText(f"{tr('录音已保存')}：{Path(path).name}")
            self.record_status.setToolTip(str(path))

    def _toggle_run(self) -> None:
        if self._pipeline_busy:
            return
        if self._mode == "d":
            self.show_ocr_page()
            return
        try:
            record_flow = self._mode == "a" and self.record_switch.isChecked()
            if self.pipeline.is_running():
                if record_flow:
                    if self.pipeline.is_paused():
                        self.pipeline.resume()
                        self.cta.set_state("recording")
                        self.record_status.setText(tr("正在录音并翻译 · 点击“暂停”可暂时停下"))
                    else:
                        self.pipeline.pause()
                        self.cta.set_state("paused")
                        self.record_status.setText(tr("已暂停 · 点击“继续”恢复，或结束并保存"))
                else:
                    self._begin_pipeline_stop()
            else:
                if self._mode == "c" and not self._store.get("last_input_file", ""):
                    if not self.select_input_file():
                        self._on_status("已取消选择文件")
                        return
                self._begin_pipeline_start(record_flow)
        except Exception as exc:
            logger.exception("主窗启动/停止 Pipeline 失败")
            self.cta.set_running(False)
            self.finish_record_btn.hide()
            self.record_switch.setEnabled(True)
            self._on_status(f"{tr('启动失败')}: {exc}")

    def _finish_recording(self) -> None:
        """Phone-recorder style terminal action: stop, flush and close the WAV."""
        if self._pipeline_busy:
            return
        if self.pipeline.is_running():
            self._begin_pipeline_stop(finish_recording=True)

    def _on_utterance(self, src: str, dst: str) -> None:
        """(原文, 译文) → 字幕流 + 悬浮窗（若注入）。"""
        self._conversation_session.append(src, dst)
        self.subtitle_list.add_subtitle(src, dst)
        if self._overlay is not None:
            try:
                self._overlay.set_subtitles(src, dst)
                if self._mode in ("a", "b") and not self._overlay.isVisible():
                    self._overlay.show()
            except AttributeError:
                logger.debug("字幕浮窗缺少 set_subtitles, 跳过")

    def _on_partial(self, src: str) -> None:
        """兼容旧 Pipeline 的单字段 partial 回调。"""
        self._on_draft(src, "")

    def _on_draft(self, src: str, dst: str) -> None:
        """原位替换当前句的识别和译文草稿。"""
        src = src.strip()
        if not src:
            self.subtitle_list.clear_partial()
            return
        placeholder = (
            tr("译文生成中…", "Translating…")
            if self._store.get("asr_tuning_profile", "auto") == "context"
            else tr("识别中…", "Recognizing…")
        )
        visible_dst = dst.strip() or placeholder
        self.subtitle_list.set_partial(src, visible_dst)
        if self._overlay is not None:
            try:
                if hasattr(self._overlay, "set_partial"):
                    self._overlay.set_partial(src, visible_dst)
                else:
                    self._overlay.set_subtitles(src, visible_dst)
                if self._mode in ("a", "b") and not self._overlay.isVisible():
                    self._overlay.show()
            except AttributeError:
                logger.debug("字幕浮窗缺少临时字幕接口")

    def _on_status(self, text: str) -> None:
        self.status_light.set_status(tr(text), self._current_theme_name())
        terminal = ("已停止", "完成", "启动失败", "音频设备错误", "识别处理错误",
                    "文件处理失败", "文件不存在")
        if text.startswith(terminal) and not self._pipeline_busy:
            if self._mode == "c":
                if text.startswith("完成"):
                    self._set_file_progress(100, "音视频处理完成")
                else:
                    self._hide_file_progress()
            self.cta.set_state("idle")
            self.finish_record_btn.hide()
            self.record_switch.setEnabled(True)
            self.running_state_changed.emit(False)
            path = getattr(self.pipeline, "last_recording_path", None)
            if path and self._mode == "a" and self.record_switch.isChecked():
                self.record_status.setText(f"{tr('录音已保存')}：{Path(path).name}")
                self.record_status.setToolTip(str(path))

    def _set_file_progress(self, completed: int, stage: str) -> None:
        """Present C-mode stage text above a separate, text-free progress bar."""
        if self._mode != "c":
            return
        value = max(0, min(100, int(completed)))
        self._file_progress_stage = stage
        self._file_progress_value = value
        self.file_progress_label.setText(f"{tr(stage)} · {value}%")
        self.file_progress_label.show()
        self.file_progress.setValue(value)
        self.file_progress.show()

    def _hide_file_progress(self) -> None:
        self.file_progress_label.hide()
        self.file_progress.hide()

    def _on_file_progress(self, completed: int, total: int, stage: str) -> None:
        if self._mode != "c":
            return
        value = int(completed / total * 100) if total else 0
        self._set_file_progress(value, stage)

    def _current_theme_name(self) -> str:
        # 尽量用真实配置主题着色状态灯；取不到回落 dark（QSS 主视觉一致）
        try:
            from voxsub.ui.theme import AppTheme, resolve_theme_name

            raw = self._store.get("theme", "system")
            theme = AppTheme(raw) if raw in ("light", "dark", "system") else AppTheme.SYSTEM
            return resolve_theme_name(theme)
        except Exception:
            logger.debug("主题名解析失败, 回落 dark")
            return "dark"

    def _on_language_changed(self, _language: str) -> None:
        retranslate_widget_tree(self)
        self.set_mode(self._mode)
        if self.file_progress.isVisible() and self._file_progress_stage:
            self._set_file_progress(
                self._file_progress_value, self._file_progress_stage)
        self.status_light.set_status(self.status_light.text.text(), self._current_theme_name())
        self.cta.update()

    # -- 外部注入（app.py）--------------------------------------------------
    def attach_overlay(self, overlay: object) -> None:
        """注入字幕浮窗实例（解耦，保持主窗可独立单测）。"""
        self._overlay = overlay
        try:
            overlay.lock_changed.connect(self._on_overlay_lock_changed)
            self._on_overlay_lock_changed(bool(overlay.is_click_through()))
        except AttributeError:
            logger.debug("字幕浮窗为旧接口，锁定状态无法同步")

    def closeEvent(self, ev) -> None:  # noqa: N802
        # 关窗默认隐藏到托盘（退出经由托盘菜单）；应用级退出由 app.py 置 flag
        from PySide6.QtWidgets import QApplication

        inst = QApplication.instance()
        if inst is not None and getattr(inst, "_voxsub_quitting", False):
            ev.accept()
            return
        ev.ignore()
        self.hide()
        self.status_light.text.setText("已最小化到托盘")
        logger.debug("主窗关闭 → 隐藏至托盘 (未退出应用)")
