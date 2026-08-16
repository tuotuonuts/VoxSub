"""主窗（M7 组件清单 #1）：编辑式左右分栏。

- 左栏：模式三卡片（A 麦克风同传 / B 系统声音字幕 / C 文件字幕，选中高亮）
  + 语言对下拉（中→英 / 英→中）+ 状态灯（待机 / 拾音中 / 推理中，推理中脉冲）
- 右栏：实时字幕流列表（原文 + 译文两行，自动滚动，最新行短暂高亮）
- 底部：胶囊 CTA「开始 / 停止」，内嵌圆形箭头小岛（QPainter 自绘矢量，
  与 FluentIcons 播放语义等价；QFW 的 FluentIcon 无 STOP 对偶图标）

Pipeline 依赖面仅限 DESIGN.md『Pipeline 契约（M6）』，经 pipeline_client.get_pipeline()
获取；M6 未实现时由鸭子类型 stub 顶替（见 pipeline_client.py）。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from voxsub.ui.config_store import ConfigStore
from voxsub.ui.pipeline_client import get_pipeline
from voxsub.ui.theme import DESIGN_TOKENS
from voxsub.logging_setup import get_logger

logger = get_logger("ui.main_window")

# ---------------------------------------------------------------------------
# 模式元信息 + 纯函数（可单测）
# ---------------------------------------------------------------------------
MODE_INFO: dict[str, dict[str, str]] = {
    "a": {"badge": "A", "title": "麦克风同传", "desc": "说一句翻一句，实时双语文幕"},
    "b": {"badge": "B", "title": "系统声音字幕", "desc": "为系统播放的声音生成字幕"},
    "c": {"badge": "C", "title": "文件字幕", "desc": "导入音视频，导出双语字幕"},
}
MODE_ORDER = ("a", "b", "c")

# 语言对（value, 显示标签）—— QFW ComboBox 用文本定位，不依赖 itemData
LANG_PAIRS = [("zh-en", "中 → 英"), ("en-zh", "英 → 中")]
_LANG_LABEL_TO_VALUE = {label: value for value, label in LANG_PAIRS}
_LANG_VALUE_TO_LABEL = {value: label for value, label in LANG_PAIRS}

STATUS_STYLES: dict[str, dict[str, str]] = {
    # 状态 → (灯色 token, 文本)
    "待机": {"color": "neutral"},
    "拾音中": {"color": "accent"},
    "推理中": {"color": "warning"},
}


def cycle_mode(current: str) -> str:
    """模式轮换 a → b → c → a（托盘 / 快捷键复用；非法值回落 a）。"""
    if current not in MODE_ORDER:
        return "a"
    idx = (MODE_ORDER.index(current) + 1) % len(MODE_ORDER)
    return MODE_ORDER[idx]


# ---------------------------------------------------------------------------
# 模式卡片
# ---------------------------------------------------------------------------
class ModeCard(QFrame):
    """可点击模式卡：badge + 标题 + 描述；selected 高亮（QSS 动态属性驱动）。"""

    clicked = Signal(str)  # 携带模式键 "a"/"b"/"c"

    def __init__(self, mode: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = mode
        self.setObjectName("modeCard")
        self.setProperty("active", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(92)

        info = MODE_INFO[mode]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        badge = QLabel(info["badge"], self)
        badge.setObjectName("modeBadge")
        title = QLabel(info["title"], self)
        title.setObjectName("modeTitle")
        top.addWidget(badge)
        top.addWidget(title)
        top.addStretch(1)
        desc = QLabel(info["desc"], self)
        desc.setObjectName("modeDesc")
        desc.setWordWrap(True)
        lay.addLayout(top)
        lay.addWidget(desc)

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
        self.setFixedHeight(48)
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
        lay.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.text, 1, Qt.AlignmentFlag.AlignVCenter)

    def set_status(self, status: str, theme_name: str = "dark") -> None:
        """status ∈ {待机, 拾音中, 推理中}；推理中进入脉冲。"""
        self.text.setText(status)
        mapping = STATUS_STYLES.get(status, STATUS_STYLES["待机"])
        color_key = mapping["color"]
        color = DESIGN_TOKENS[theme_name].get(
            color_key, DESIGN_TOKENS["dark"][color_key]
        )
        self._light_color = color
        self.dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        self.set_pulsing(status == "推理中")

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
        self._empty_hint = QLabel("字幕将显示在这里 —— 选择模式后点击「开始」", self._container)
        self._empty_hint.setObjectName("emptyHint")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._vbox.insertWidget(0, self._empty_hint)
        self._vbox.setStretch(0, 1)

    def add_subtitle(self, src: str, dst: str) -> None:
        """追加一条双语字幕并自动滚底；超出 200 条丢弃最旧（内存滚动）。"""
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
        dst_label = QLabel(dst, row)
        dst_label.setObjectName("dstText")
        dst_label.setWordWrap(True)
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

    def count(self) -> int:
        return len(self._rows)

    def clear(self) -> None:
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
        self._running = False
        self._hovered = False
        self._pressed = False

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        self.update()

    def is_running(self) -> bool:
        return self._running

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
        if self._running:
            base = accent_deep if self._pressed else accent
        else:
            base = QColor(
                *self._parse_rgb(DESIGN_TOKENS["dark"]["surface_2"]), 205
            ) if self._pressed else QColor(
                *self._parse_rgb(DESIGN_TOKENS["dark"]["surface_1"]), 235
            )
        p.fillPath(pill, base)
        border = QColor("#14B8A6") if not self._running else QColor("#0D9488").lighter(120)
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
        if self._running:  # 停止
            s = 10
            path = QPainterPath()
            path.addRoundedRect(cx - s / 2, cy - s / 2, s, s, 2, 2)
            p.drawPath(path)
        else:  # 播放三角（略右偏视觉居中）
            tri = QPainterPath()
            tri.moveTo(cx - 5, cy - 8)
            tri.lineTo(cx - 5, cy + 8)
            tri.lineTo(cx + 8, cy)
            tri.closeSubpath()
            p.drawPath(tri)

        # 文字
        p.setPen(Qt.GlobalColor.white if self._running else QColor("#E6FFFFFF"))
        f = QFont("Microsoft YaHei UI", 13, QFont.Weight.DemiBold)
        p.setFont(f)
        text = "停止" if self._running else "开始"
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


# ---------------------------------------------------------------------------
# 主窗
# ---------------------------------------------------------------------------
class MainWindow(QWidget):
    """语幕 VoxSub 主窗（软高级感左右分栏编辑式布局）。"""

    def __init__(
        self,
        store: ConfigStore | None = None,
        pipeline: object | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("rootShell")
        self.setWindowTitle("语幕 VoxSub")
        self.resize(980, 640)
        self._store = store or ConfigStore()
        # Pipeline 依赖面（M6 或 stub）
        self.pipeline = pipeline if pipeline is not None else get_pipeline()

        # -- 当前状态 --------------------------------------------------------
        self._mode = self._store.get("mode", "a")
        self._lang_pair = self._store.get("lang_pair", "zh-en")
        self._overlay = None  # 由 app.py 注入（避免硬依赖，保持可单测）

        self._build_ui()
        self._wire_pipeline()

        # 初始化状态
        self.set_mode(self._mode)
        self.set_lang_pair(self._lang_pair)

    # -- 界面构建 -----------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(16)

        # 标题行
        title_row = QHBoxLayout()
        title = QLabel("语幕", self)
        title.setObjectName("sectionTitle")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        subtitle = QLabel("实时翻译字幕 VoxSub", self)
        subtitle.setObjectName("secondaryLabel")
        title_row.addWidget(title)
        title_row.addWidget(subtitle)
        title_row.addStretch(1)
        root.addLayout(title_row)

        # 左右分栏
        body = QHBoxLayout()
        body.setSpacing(20)
        body.addWidget(self._build_left_panel(), 0)
        body.addWidget(self._build_right_panel(), 1)
        root.addLayout(body, 1)

        # 底部胶囊 CTA 居中
        cta_row = QHBoxLayout()
        cta_row.addStretch(1)
        self.cta = CapsuleCTA(self)
        self.cta.clicked.connect(self._toggle_run)
        cta_row.addWidget(self.cta)
        cta_row.addStretch(1)
        root.addLayout(cta_row)

    def _build_left_panel(self) -> QFrame:
        panel = QFrame(self)
        panel.setObjectName("sidePanel")
        panel.setFixedWidth(300)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(14)

        mode_title = QLabel("模式", panel)
        mode_title.setObjectName("sectionTitle")
        lay.addWidget(mode_title)

        self.mode_cards: dict[str, ModeCard] = {}
        for m in MODE_ORDER:
            card = ModeCard(m, panel)
            card.clicked.connect(self.set_mode)
            self.mode_cards[m] = card
            lay.addWidget(card)

        lay.addSpacing(4)
        pair_label = QLabel("语言对", panel)
        pair_label.setObjectName("sectionTitle")
        lay.addWidget(pair_label)
        # QFluentWidgets ComboBox（随 QFW 主题自动着色，见 DESIGN 组件清单 #1）
        from qfluentwidgets import ComboBox as FComboBox

        self.lang_combo: QComboBox = FComboBox(panel)
        self.lang_combo.setObjectName("langCombo")
        self.lang_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        for value, label in LANG_PAIRS:
            self.lang_combo.addItem(label)
        self.lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        lay.addWidget(self.lang_combo)

        lay.addSpacing(8)
        self.status_light = StatusLight(panel)
        lay.addWidget(self.status_light)
        lay.addStretch(1)
        return panel

    def _build_right_panel(self) -> QFrame:
        panel = QFrame(self)
        panel.setObjectName("subtitlePanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel("实时字幕", panel)
        t.setObjectName("sectionTitle")
        head.addWidget(t)
        head.addStretch(1)
        lay.addLayout(head)
        self.subtitle_list = SubtitleList(panel)
        lay.addWidget(self.subtitle_list, 1)
        return panel

    # -- Pipeline 接线 ------------------------------------------------------
    def _wire_pipeline(self) -> None:
        """订阅 utterance / status 回调（契约见 DESIGN.md M6）。"""
        try:
            self.pipeline.on_utterance(self._on_utterance)
            self.pipeline.on_status(self._on_status)
        except AttributeError:
            # 鸭子类型兜底：对象缺方法也不崩壳（设计内行为 → debug）
            logger.debug("Pipeline 缺少 on_utterance/on_status, 跳过订阅 (stub 联调期正常)")

    # -- 状态切换 -----------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        norm = mode if mode in MODE_ORDER else "a"
        self._mode = norm
        for key, card in self.mode_cards.items():
            card.set_active(key == norm)
        try:
            self.pipeline.set_mode(norm)
        except AttributeError:
            logger.debug("Pipeline 缺少 set_mode, 跳过 (stub 联调期正常)")
        self._store.set("mode", norm)

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
        label = _LANG_VALUE_TO_LABEL[value]
        idx = self.lang_combo.findText(label)
        if idx >= 0 and self.lang_combo.currentIndex() != idx:
            self.lang_combo.blockSignals(True)
            self.lang_combo.setCurrentIndex(idx)
            self.lang_combo.blockSignals(False)
        self._lang_pair = value
        self._store.set("lang_pair", value)

    def current_lang_pair(self) -> str:
        """当前语言对 value（供托盘 / 测试 / M6 集成读取）。"""
        label = self.lang_combo.currentText()
        return _LANG_LABEL_TO_VALUE.get(label, "zh-en")

    def _on_lang_changed(self, index: int) -> None:
        self.set_lang_pair(self.current_lang_pair())

    # -- 启停 ---------------------------------------------------------------
    def _toggle_run(self) -> None:
        try:
            if self.pipeline.is_running():
                self.pipeline.stop()
                self.cta.set_running(False)
            else:
                self.pipeline.start()
                self.cta.set_running(True)
        except AttributeError:
            logger.debug("Pipeline 缺少 start/stop/is_running, 跳过 (stub 联调期正常)")

    def _on_utterance(self, src: str, dst: str) -> None:
        """(原文, 译文) → 字幕流 + 悬浮窗（若注入）。"""
        self.subtitle_list.add_subtitle(src, dst)
        if self._overlay is not None:
            try:
                self._overlay.set_subtitles(src, dst)
            except AttributeError:
                logger.debug("字幕浮窗缺少 set_subtitles, 跳过")

    def _on_status(self, text: str) -> None:
        self.status_light.set_status(text, self._current_theme_name())

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

    # -- 外部注入（app.py）--------------------------------------------------
    def attach_overlay(self, overlay: object) -> None:
        """注入字幕浮窗实例（解耦，保持主窗可独立单测）。"""
        self._overlay = overlay

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