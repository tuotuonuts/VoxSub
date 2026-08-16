"""诊断页（M7 组件清单 #5，M8 接入点的展示骨架）。

- 读取 voxsub.diagnostics.run_self_check()（M8 实现）；未实现时显示占位说明。
- 结果卡：✅ / ⚠️ / ❌ + 一句话处置（DESIGN.md 明确允许这三字符作为状态符）。
- 一键导出纯文本报告（voxsub.diagnostics.export_report 可用时）。
- 「日志」页签（可观测性改造 P0）：
  - QPlainTextEdit 只读视图：初始调 tail_log_file(300) 灌入最近日志；
  - QTimer(1s) 周期调 drain_events(50)，把新事件按末行指纹去重后 append 到底部并自动滚动；
  - 顶部按钮：「刷新」（重读 tail_log_file）与「导出日志」（tail_log_file 全文写用户路径）；
  - 日志行统一格式 "HH:MM:SS LEVEL [name] message"（文件行去掉日期前缀后与
    内存事件行同一格式，保证指纹去重跨两者生效，避免轮询重复追加）；
  - 日志组件初始化失败时页签内显示占位文案而非崩溃。

日志敏感信息约定：只记录事件级信息（模块/级别的启停、异常摘要），不落盘
API Key、字幕正文、音频数据等用户内容。
"""
from __future__ import annotations

import importlib

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from voxsub.logging_setup import drain_events, get_logger, tail_log_file
from voxsub.ui.theme import DESIGN_TOKENS

logger = get_logger("ui.diagnostics_window")

_STATUS_CHAR = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
_STATUS_COLOR = {"ok": "#34D399", "warn": "#FBBF24", "fail": "#F87171"}

#: 构造函数哨兵：默认自动探测 voxsub.diagnostics
_AUTO = object()

#: 日志页签：初始/刷新读文件末尾行数；轮询每次 drain 事件条数
_LOG_TAIL_LINES = 300
_LOG_POLL_EVENTS = 50


def _fmt_log_line(ev: dict) -> str:
    """"HH:MM:SS LEVEL [name] message" —— LEVEL %-8s 与文件 formatter 对齐, 便于指纹去重。"""
    ts = str(ev.get("ts", ""))
    hhmmss = ts[-8:] if len(ts) >= 8 else ts  # drain 的 ts 可能带日期前缀, 统一取 HH:MM:SS
    return f"{hhmmss} {str(ev.get('level', '')):<8} [{ev.get('name', '')}] {ev.get('message', '')}"


def _strip_file_ts(line: str) -> str:
    """文件行 "2026-08-17 10:23:45 INFO     [name] msg" → "10:23:45 INFO     [name] msg"。"""
    s = line.rstrip("\n")
    if len(s) >= 11 and s[4] == "-" and s[7] == "-" and s[10] == " ":
        return s[11:]
    return s


class DiagnosticsWindow(QWidget):
    """诊断页：自检结果卡（Tab 1）+ 实时日志（Tab 2）。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        diagnostics_module: object = _AUTO,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("diagnosticsWindow")
        self.setWindowTitle("诊断 — 语幕 VoxSub")
        self.resize(680, 560)
        self._results: list[dict] = []
        if diagnostics_module is _AUTO:
            self._load_module()
        else:
            self._module = diagnostics_module  # None → 占位分支（测试注入用）

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("diagnosticsTabs")
        self.tabs.addTab(self._build_selfcheck_tab(), "自检")
        self.tabs.addTab(self._build_log_tab(), "日志")
        root.addWidget(self.tabs, 1)

        self._render()

    # ------------------------------------------------------------------
    # Tab 1：自检（原有骨架内容整体迁入）
    # ------------------------------------------------------------------
    def _build_selfcheck_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        head = QLabel("自检结果", page)
        head.setObjectName("sectionTitle")
        lay.addWidget(head)

        self.scroll = QScrollArea(page)
        self.scroll.setObjectName("subtitleScroll")
        self.scroll.setWidgetResizable(True)
        self._container = QWidget(self.scroll)
        self._vbox = QVBoxLayout(self._container)
        self._vbox.setContentsMargins(4, 4, 12, 4)
        self._vbox.setSpacing(10)
        self._vbox.addStretch(1)
        self.scroll.setWidget(self._container)
        lay.addWidget(self.scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.export_btn = QPushButton("导出报告 (txt)", page)
        self.export_btn.setObjectName("inputBox")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.clicked.connect(self._export_report)
        btn_row.addWidget(self.export_btn)
        lay.addLayout(btn_row)
        return page

    # ------------------------------------------------------------------
    # Tab 2：实时日志（读 logging_setup.drain_events / tail_log_file）
    # ------------------------------------------------------------------
    def _build_log_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(10)
        self._last_seen: str | None = None  # 末行指纹（去重基准）
        self._log_failed = False
        try:
            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)
            self.refresh_log_btn = QPushButton("刷新", page)
            self.refresh_log_btn.setObjectName("inputBox")
            self.refresh_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.refresh_log_btn.clicked.connect(self._refresh_logs)
            btn_row.addWidget(self.refresh_log_btn)
            self.export_log_btn = QPushButton("导出日志", page)
            self.export_log_btn.setObjectName("inputBox")
            self.export_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.export_log_btn.clicked.connect(self._export_logs)
            btn_row.addWidget(self.export_log_btn)
            btn_row.addStretch(1)
            lay.addLayout(btn_row)

            t = DESIGN_TOKENS["dark"]
            self.log_view = QPlainTextEdit(page)
            self.log_view.setObjectName("logView")
            self.log_view.setReadOnly(True)
            self.log_view.setPlaceholderText("暂无日志")
            self.log_view.setStyleSheet(
                f"QPlainTextEdit {{ background-color: {t['surface_1']};"
                f" color: {t['text_primary']}; border: 1px solid {t['border']};"
                f" border-radius: 10px; padding: 8px;"
                f" font-family: {t['font_mono']}; font-size: 12px; }}"
            )
            lay.addWidget(self.log_view, 1)

            # 初始灌入文件末尾（末行作指纹基准，避免轮询重复追加）
            self._refresh_logs()
            # 1s 轮询内存环形队列，增量追加新事件并自动滚动
            self.log_timer = QTimer(self)
            self.log_timer.setInterval(1000)
            self.log_timer.timeout.connect(self._poll_events)
            self.log_timer.start()
        except Exception:
            self._log_failed = True
            logger.exception("日志页签初始化失败, 显示占位")
            place = QLabel("日志页签初始化失败（详见日志文件）", page)
            place.setObjectName("secondaryLabel")
            place.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(place)
        return page

    def _poll_events(self) -> None:
        """drain 内存队列最新事件, 按末行指纹增量追加, 自动滚底。"""
        if self._log_failed:
            return
        try:
            events = drain_events(_LOG_POLL_EVENTS)
        except Exception:
            logger.exception("drain_events 失败")
            return
        lines = [_fmt_log_line(e) for e in events]
        start = 0
        if self._last_seen is not None:
            try:
                start = lines.index(self._last_seen) + 1
            except ValueError:
                # 最旧事件已被环形队列丢弃 → 全量追加（少量重复, 可接受）
                start = 0
        new_lines = lines[start:]
        if not new_lines:
            return
        self._last_seen = lines[-1]
        self.log_view.appendPlainText("\n".join(new_lines))
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _refresh_logs(self) -> None:
        """重读文件末尾, 整体替换视图；用文件末行重设指纹基准。"""
        if self._log_failed:
            return
        try:
            text = tail_log_file(_LOG_TAIL_LINES)
        except Exception:
            logger.exception("tail_log_file 失败")
            return
        lines = [_strip_file_ts(l) for l in text.splitlines()]
        self.log_view.setPlainText("\n".join(lines))
        self._last_seen = lines[-1] if lines else None

    def _export_logs(self) -> None:
        """把日志文件全文导出到用户选择的路径。"""
        if self._log_failed:
            return
        try:
            text = tail_log_file(10**6)  # 取全部
        except Exception as exc:  # noqa: BLE001
            logger.exception("读取日志文件失败")
            text = f"<读取日志失败: {exc}>"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", "voxsub_log.txt", "文本文件 (*.txt)"
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                logger.exception("写日志导出文件失败: %s", path)

    # ------------------------------------------------------------------
    def _load_module(self) -> None:
        """尝试加载 M8 诊断模块；失败则保持占位。"""
        try:
            self._module = importlib.import_module("voxsub.diagnostics")
        except Exception:
            logger.debug("诊断模块不可用, 进入占位模式", exc_info=True)
            self._module = None

    def _render(self) -> None:
        # 清空旧卡
        while self._vbox.count() > 1:
            item = self._vbox.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        if self._module is None or not hasattr(self._module, "run_self_check"):
            self._add_placeholder()
            return

        try:
            self._results = self._module.run_self_check() or []
        except Exception:
            logger.exception("run_self_check 执行失败")
            self._results = [{"check": "自检执行", "status": "fail", "detail": "run_self_check 抛异常"}]
        for item in self._results:
            self._add_result_card(item)

    def _add_placeholder(self) -> None:
        card = QFrame(self._container)
        card.setObjectName("settingsCard")
        card.setStyleSheet(
            "QFrame#settingsCard { background-color: rgba(255,255,255,0.04);"
            " border: 1px dashed rgba(255,255,255,0.12); border-radius: 16px; }"
        )
        box = QVBoxLayout(card)
        box.setContentsMargins(24, 28, 24, 28)
        box.setSpacing(8)
        t = QLabel("诊断模块尚未实现", card)
        t.setObjectName("sectionTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d = QLabel(
            "M8 里程碑将在此接入 voxsub.diagnostics.run_self_check() 的"
            "模型完整性 / ORT providers / ASR·VAD·TTS 冒烟 / 磁盘余量检查结果。",
            card,
        )
        d.setObjectName("secondaryLabel")
        d.setWordWrap(True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(t)
        box.addWidget(d)
        self._vbox.insertWidget(self._vbox.count() - 1, card)

    def _add_result_card(self, item: dict) -> None:
        check = str(item.get("check", "未知检查项"))
        status = str(item.get("status", "warn"))
        detail = str(item.get("detail", ""))
        color = _STATUS_COLOR.get(status, _STATUS_COLOR["warn"])
        card = QFrame(self._container)
        card.setObjectName("settingsCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(20, 14, 20, 14)
        row.setSpacing(12)
        mark = QLabel(_STATUS_CHAR.get(status, "⚠️"), card)
        mark.setStyleSheet(f"font-size: 16px; color: {color};")
        row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        mid = QVBoxLayout()
        name = QLabel(check, card)
        name.setObjectName("sectionTitle")
        det = QLabel(detail, card)
        det.setObjectName("secondaryLabel")
        det.setWordWrap(True)
        mid.addWidget(name)
        mid.addWidget(det)
        row.addLayout(mid, 1)
        self._vbox.insertWidget(self._vbox.count() - 1, card)

    # ------------------------------------------------------------------
    def _export_report(self) -> None:
        """导出一份纯文本报告（M8 export_report 可用时）。"""
        try:
            if self._module is not None and hasattr(self._module, "export_report"):
                text = self._module.export_report()
            else:
                lines = ["语幕 VoxSub 诊断报告（诊断模块未实现，以下为骨架内容）"]
                for item in self._results:
                    lines.append(
                        f"[{item.get('status', 'warn')}] {item.get('check', '')} — {item.get('detail', '')}"
                    )
                text = "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            logger.exception("生成诊断报告失败")
            text = f"导出失败: {exc}"

        path, _ = QFileDialog.getSaveFileName(
            self, "导出诊断报告", "voxsub_diagnostics.txt", "文本文件 (*.txt)"
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                logger.exception("写诊断报告文件失败: %s", path)

    def refresh(self) -> None:
        self._load_module()
        self._render()