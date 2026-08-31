"""诊断页（M7 组件清单 #5，M8 接入点的展示骨架）。

- 读取 voxsub.diagnostics.run_self_check()（M8 实现）；未实现时显示占位说明。
- 结果卡：✅ / ⚠️ / ❌ + 一句话处置（DESIGN.md 明确允许这三字符作为状态符）。
- 一键导出纯文本报告（voxsub.diagnostics.export_report 可用时）。
- 「日志」页签（可观测性改造 P0）：
  - QPlainTextEdit 只读视图：初始调 tail_log_file(300) 灌入最近日志；
  - QTimer(1s) 周期调 drain_events(50)，把新事件按末行指纹去重后 append 到底部并自动滚动；
- 顶部按钮：「刷新」「导出日志」与「上传日志到 Sentry」（过滤后的手动上传）；
  - 日志行统一格式 "HH:MM:SS LEVEL [name] [session=id] message"（文件行去掉日期前缀后与
    内存事件行同一格式，保证指纹去重跨两者生效，避免轮询重复追加）；
  - 日志组件初始化失败时页签内显示占位文案而非崩溃。

日志敏感信息约定：只记录事件级信息（模块/级别的启停、异常摘要），不落盘
API Key、字幕正文、音频数据等用户内容。
"""
from __future__ import annotations

import importlib
import inspect
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from voxsub.file_io import write_text_atomically
from voxsub.error_reporting import (
    is_error_reporting_enabled,
    preview_log_snapshot_metadata,
    send_diagnostic_report,
    send_log_snapshot,
)
from voxsub.logging_setup import (
    clear_local_logs,
    diagnostic_session_log_snapshot,
    diagnostic_session_snapshot,
    drain_events,
    get_logger,
    start_diagnostic_session,
    stop_diagnostic_session,
    tail_log_file,
)
from voxsub.config_store import ConfigStore
from voxsub.ui.file_dialogs import choose_save_file
from voxsub.ui.i18n import (
    language_manager,
    retranslate_widget_tree,
    tr,
    translate_dynamic,
)
from voxsub.ui.selection_controls import ToggleSwitch

logger = get_logger("ui.diagnostics_window")

_STATUS_CHAR = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
_STATUS_COLOR = {"ok": "#34D399", "warn": "#FBBF24", "fail": "#F87171"}

#: 构造函数哨兵：默认自动探测 voxsub.diagnostics
_AUTO = object()

#: 日志页签：初始/刷新读文件末尾行数；轮询每次 drain 事件条数
_LOG_TAIL_LINES = 300
_LOG_POLL_EVENTS = 50
_DIAGNOSTIC_SESSION_SECONDS = 20 * 60


def _as_non_negative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _format_log_upload_summary(metadata: Mapping[str, Any]) -> str:
    """Format safe upload metadata for the diagnostics page."""
    source = _as_non_negative_int(metadata.get("source_log_lines"))
    uploaded = _as_non_negative_int(metadata.get("uploaded_log_lines"))
    redacted = _as_non_negative_int(metadata.get("privacy_redacted_lines"))
    removed = _as_non_negative_int(metadata.get("privacy_removed_lines"))
    replacements = _as_non_negative_int(metadata.get("privacy_filter_replacements"))
    omitted = _as_non_negative_int(metadata.get("omitted_log_lines"))
    source_first = str(metadata.get("source_first_log_at") or tr("未检测到时间戳"))
    source_last = str(metadata.get("source_last_log_at") or tr("未检测到时间戳"))
    uploaded_first = str(metadata.get("uploaded_first_log_at") or tr("未检测到时间戳"))
    uploaded_last = str(metadata.get("uploaded_last_log_at") or tr("未检测到时间戳"))
    rows = [
        tr("Sentry 日志已上传：{uploaded}/{source} 行").format(
            uploaded=uploaded, source=source),
        tr("来源时间：{first} 至 {last}").format(
            first=source_first, last=source_last),
        tr("上传范围：{first} 至 {last}").format(
            first=uploaded_first, last=uploaded_last),
        tr("已脱敏 {redacted} 行（{replacements} 处），移除 {removed} 行").format(
            redacted=redacted, replacements=replacements, removed=removed),
    ]
    if omitted:
        rows.append(tr("已因大小省略 {omitted} 行（保留最新日志）").format(
            omitted=omitted))
    elif _as_non_negative_int(metadata.get("line_length_limited_lines")):
        rows.append(tr("部分超长日志行已按安全限制截断"))
    return "\n".join(rows)


def _log_upload_is_partial(metadata: object) -> bool:
    """Whether the submitted snapshot intentionally omitted any source lines."""
    return (isinstance(metadata, Mapping) and
            _as_non_negative_int(metadata.get("omitted_log_lines")) > 0)


def _fmt_log_line(ev: dict) -> str:
    """"HH:MM:SS LEVEL [name] [session=id] message" —— 与文件 formatter 对齐。"""
    ts = str(ev.get("ts", ""))
    hhmmss = ts[-8:] if len(ts) >= 8 else ts  # drain 的 ts 可能带日期前缀, 统一取 HH:MM:SS
    session_id = str(ev.get("session_id", "-") or "-")
    return (f"{hhmmss} {str(ev.get('level', '')):<8} [{ev.get('name', '')}] "
            f"[session={session_id}] {ev.get('message', '')}")


def _strip_file_ts(line: str) -> str:
    """文件行 "2026-08-17 10:23:45 INFO     [name] msg" → "10:23:45 INFO     [name] msg"。"""
    s = line.rstrip("\n")
    if len(s) >= 11 and s[4] == "-" and s[7] == "-" and s[10] == " ":
        return s[11:]
    return s


class _ExportBridge(QObject):
    """Deliver worker results back to the diagnostics window's Qt thread."""

    done = Signal(str, bool, object)


class _SelfCheckBridge(QObject):
    """Deliver self-check and repair worker events to the Qt thread."""

    progress = Signal(int, int, str)
    done = Signal(object)


def _call_self_check(module: object, progress) -> list[dict]:
    """Call new progress-aware modules while retaining injected test doubles."""
    run = getattr(module, "run_self_check")
    try:
        parameters = inspect.signature(run).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "progress" in parameters:
        return list(run(progress=progress) or [])
    return list(run() or [])


def _call_repair(module: object, results: tuple[dict, ...], store, progress) -> object:
    """Call a result-aware repair hook while tolerating small test doubles."""
    repair = getattr(module, "repair_self_check")
    try:
        parameters = inspect.signature(repair).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs = {}
    if "store" in parameters:
        kwargs["store"] = store
    if "progress" in parameters:
        kwargs["progress"] = progress
    return repair(results, **kwargs)


def _call_export_report(module: object, results: tuple[dict, ...]) -> str:
    """Export the visible snapshot when the module supports that contract."""
    export = getattr(module, "export_report")
    try:
        parameters = inspect.signature(export).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "results" in parameters:
        return str(export(results=results))
    # Keep compatibility with older/test modules exposing export_report().
    return str(export())


class DiagnosticsWindow(QWidget):
    """诊断页：自检结果卡（Tab 1）+ 实时日志（Tab 2）。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        diagnostics_module: object = _AUTO,
        store: ConfigStore | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("diagnosticsWindow")
        self.setWindowTitle("诊断 — 语幕 VoxSub")
        self.setMinimumSize(640, 520)
        self.resize(760, 640)
        self._results: list[dict] = []
        self._store = store or ConfigStore()
        self._export_bridge = _ExportBridge(self)
        self._export_bridge.done.connect(self._on_export_done)
        self._export_workers: dict[str, threading.Thread] = {}
        self._export_buttons: dict[str, QPushButton] = {}
        self._selfcheck_bridge = _SelfCheckBridge(self)
        self._selfcheck_bridge.progress.connect(self._on_selfcheck_progress)
        self._selfcheck_bridge.done.connect(self._on_selfcheck_done)
        self._selfcheck_worker: threading.Thread | None = None
        self._repair_bridge = _SelfCheckBridge(self)
        self._repair_bridge.progress.connect(self._on_repair_progress)
        self._repair_bridge.done.connect(self._on_repair_done)
        self._repair_worker: threading.Thread | None = None
        self._selfcheck_generation = 0
        self._last_counts = {"ok": 0, "warn": 0, "fail": 0}
        if diagnostics_module is _AUTO:
            self._load_module()
        else:
            self._module = diagnostics_module  # None → 占位分支（测试注入用）

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 22)
        root.setSpacing(16)

        header = QFrame(self)
        header.setObjectName("windowHeader")
        header_box = QVBoxLayout(header)
        header_box.setContentsMargins(2, 0, 2, 0)
        header_box.setSpacing(3)
        eyebrow = QLabel("VOXSUB  /  HEALTH & LOGS", header)
        eyebrow.setObjectName("eyebrowLabel")
        title = QLabel("诊断中心", header)
        title.setObjectName("windowTitleLabel")
        subtitle = QLabel("查看设备、模型和运行时状态；需要排障时直接打开实时日志。", header)
        subtitle.setObjectName("windowSubtitleLabel")
        header_box.addWidget(eyebrow)
        header_box.addWidget(title)
        header_box.addWidget(subtitle)
        root.addWidget(header)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("diagnosticsTabs")
        self.tabs.addTab(self._build_selfcheck_tab(), "自检")
        self.tabs.addTab(self._build_log_tab(), "日志")
        root.addWidget(self.tabs, 1)

        self._render()
        language_manager.language_changed.connect(self._on_language_changed)
        self._on_language_changed(language_manager.language)

    # ------------------------------------------------------------------
    # Tab 1：自检（原有骨架内容整体迁入）
    # ------------------------------------------------------------------
    def _build_selfcheck_tab(self) -> QWidget:
        page = QWidget(self)
        page.setObjectName("diagnosticsPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(12)

        head_row = QHBoxLayout()
        head = QLabel("自检结果", page)
        head.setObjectName("sectionTitle")
        head_row.addWidget(head)
        self.selfcheck_summary = QLabel("正在准备检查…", page)
        self.selfcheck_summary.setObjectName("statusPill")
        head_row.addWidget(self.selfcheck_summary)
        head_row.addStretch(1)
        self.selfcheck_refresh_btn = QPushButton("重新检查", page)
        self.selfcheck_refresh_btn.setObjectName("secondaryButton")
        self.selfcheck_refresh_btn.setMinimumHeight(40)
        self.selfcheck_refresh_btn.clicked.connect(self.refresh)
        head_row.addWidget(self.selfcheck_refresh_btn)
        lay.addLayout(head_row)

        self.selfcheck_progress_label = QLabel("", page)
        self.selfcheck_progress_label.setObjectName("secondaryLabel")
        self.selfcheck_progress_label.setWordWrap(True)
        self.selfcheck_progress_label.hide()
        lay.addWidget(self.selfcheck_progress_label)

        self.selfcheck_progress = QProgressBar(page)
        self.selfcheck_progress.setObjectName("diagnosticsProgress")
        self.selfcheck_progress.setRange(0, 100)
        self.selfcheck_progress.setValue(0)
        self.selfcheck_progress.setTextVisible(False)
        self.selfcheck_progress.hide()
        lay.addWidget(self.selfcheck_progress)

        self.scroll = QScrollArea(page)
        self.scroll.setObjectName("diagnosticsScroll")
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
        self.repair_btn = QPushButton("修复", page)
        self.repair_btn.setObjectName("secondaryButton")
        self.repair_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.repair_btn.clicked.connect(self._repair)
        btn_row.addWidget(self.repair_btn)
        self.export_btn = QPushButton("导出报告 (txt)", page)
        self.export_btn.setObjectName("secondaryButton")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.clicked.connect(self._export_report)
        btn_row.addWidget(self.export_btn)
        self.send_sentry_btn = QPushButton("发送到 Sentry", page)
        self.send_sentry_btn.setObjectName("secondaryButton")
        self.send_sentry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_sentry_btn.setToolTip("发送当前自检报告和本地日志（已过滤隐私信息）")
        self.send_sentry_btn.setEnabled(is_error_reporting_enabled())
        self.send_sentry_btn.clicked.connect(self._send_report_to_sentry)
        btn_row.addWidget(self.send_sentry_btn)
        lay.addLayout(btn_row)
        return page

    # ------------------------------------------------------------------
    # Tab 2：实时日志（读 logging_setup.drain_events / tail_log_file）
    # ------------------------------------------------------------------
    def _build_log_tab(self) -> QWidget:
        page = QWidget(self)
        page.setObjectName("diagnosticsPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 12, 4, 4)
        lay.setSpacing(10)
        self._last_seen: str | None = None  # 末行指纹（去重基准）
        self._log_failed = False
        try:
            toolbar = QFrame(page)
            toolbar.setObjectName("diagnosticToolbar")
            btn_row = QHBoxLayout(toolbar)
            btn_row.setContentsMargins(10, 8, 10, 8)
            btn_row.setSpacing(8)
            self.refresh_log_btn = QPushButton("刷新", page)
            self.refresh_log_btn.setObjectName("secondaryButton")
            self.refresh_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.refresh_log_btn.clicked.connect(self._refresh_logs)
            btn_row.addWidget(self.refresh_log_btn)
            self.export_log_btn = QPushButton("导出日志", page)
            self.export_log_btn.setObjectName("secondaryButton")
            self.export_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.export_log_btn.clicked.connect(self._export_logs)
            btn_row.addWidget(self.export_log_btn)
            self.send_log_sentry_btn = QPushButton("上传日志到 Sentry", page)
            self.send_log_sentry_btn.setObjectName("secondaryButton")
            self.send_log_sentry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.send_log_sentry_btn.setToolTip("手动上传本地日志快照（已过滤隐私信息）")
            self.send_log_sentry_btn.setEnabled(is_error_reporting_enabled())
            self.send_log_sentry_btn.clicked.connect(self._send_logs_to_sentry)
            btn_row.addWidget(self.send_log_sentry_btn)
            self.clear_log_btn = QPushButton("清空视图", page)
            self.clear_log_btn.setObjectName("ghostButton")
            self.clear_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.clear_log_btn.clicked.connect(lambda: self.log_view.clear())
            btn_row.addWidget(self.clear_log_btn)
            self.clear_local_log_btn = QPushButton("清除本机日志", page)
            self.clear_local_log_btn.setObjectName("ghostButton")
            self.clear_local_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.clear_local_log_btn.setToolTip(
                "删除本机历史日志；不会删除模型、配置、用户数据或 Sentry 配置")
            self.clear_local_log_btn.clicked.connect(self._clear_local_logs)
            btn_row.addWidget(self.clear_local_log_btn)
            btn_row.addStretch(1)
            self.log_live_state = QLabel("实时 · 自动跟随", page)
            self.log_live_state.setObjectName("logLiveState")
            btn_row.addWidget(self.log_live_state)
            self.diagnostic_session_state = QLabel(page)
            self.diagnostic_session_state.setObjectName("diagnosticSessionState")
            btn_row.addWidget(self.diagnostic_session_state)
            self.debug_switch = ToggleSwitch("诊断调试", page)
            self.debug_switch.setToolTip(
                "显示音频电平、队列、设备打开与分句等详细事件；20 分钟后自动结束")
            self.debug_switch.setChecked(bool(diagnostic_session_snapshot()))
            self.debug_switch.toggled.connect(self._toggle_debug)
            btn_row.addWidget(self.debug_switch)
            lay.addWidget(toolbar)

            self.log_operation_state = QLabel("", page)
            self.log_operation_state.setObjectName("secondaryLabel")
            self.log_operation_state.setWordWrap(True)
            self.log_operation_state.hide()
            lay.addWidget(self.log_operation_state)

            self.log_view = QPlainTextEdit(page)
            self.log_view.setObjectName("logView")
            self.log_view.setReadOnly(True)
            self.log_view.setPlaceholderText("暂无日志")
            lay.addWidget(self.log_view, 1)

            # 初始灌入文件末尾（末行作指纹基准，避免轮询重复追加）
            self._refresh_logs()
            # 1s 轮询内存环形队列，增量追加新事件并自动滚动
            self.log_timer = QTimer(self)
            self.log_timer.setInterval(1000)
            self.log_timer.timeout.connect(self._poll_events)
            self.log_timer.start()
            self.diagnostic_session_timer = QTimer(self)
            self.diagnostic_session_timer.setInterval(1000)
            self.diagnostic_session_timer.timeout.connect(self._refresh_diagnostic_session_state)
            self.diagnostic_session_timer.start()
            self._refresh_diagnostic_session_state()
        except Exception:
            self._log_failed = True
            logger.exception("日志页签初始化失败, 显示占位")
            place = QLabel("日志页签初始化失败（详见日志文件）", page)
            place.setObjectName("secondaryLabel")
            place.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(place)
        return page

    def _toggle_debug(self, enabled: bool) -> None:
        # ``debug_mode`` used to persist a global DEBUG level.  Keep the key
        # only as a migration target and never let it re-enable verbose logs on
        # the next app launch.
        self._store.set("debug_mode", bool(enabled))
        if enabled:
            start_diagnostic_session(_DIAGNOSTIC_SESSION_SECONDS)
        else:
            stop_diagnostic_session()
        self._refresh_diagnostic_session_state()
        self._poll_events()

    def _refresh_diagnostic_session_state(self) -> None:
        """Reflect expiration without relying on the UI timer for enforcement."""
        metadata = diagnostic_session_snapshot()
        active = metadata is not None
        if hasattr(self, "debug_switch") and self.debug_switch.isChecked() != active:
            self.debug_switch.blockSignals(True)
            self.debug_switch.setChecked(active)
            self.debug_switch.blockSignals(False)
        if not hasattr(self, "diagnostic_session_state"):
            return
        if not active:
            self.diagnostic_session_state.setText(tr("诊断调试未开启"))
            return
        remaining = int(metadata.get("remaining_seconds", 0))
        minutes, seconds = divmod(remaining, 60)
        self.diagnostic_session_state.setText(
            f"{tr('诊断调试剩余')} {minutes:02d}:{seconds:02d}")

    def _clear_local_logs(self) -> None:
        """Ask before clearing only VoxSub's historical local log files."""
        if self._log_failed or "local_log_cleanup" in self._export_workers:
            return
        answer = QMessageBox.question(
            self,
            tr("清除本机日志"),
            tr("这会删除本机历史日志和诊断启动日志，无法恢复。不会删除模型、配置、用户数据或 Sentry 配置。是否继续？"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.clear_local_log_btn.setEnabled(False)
        self.clear_local_log_btn.setText(tr("正在清除…"))

        def _worker() -> None:
            try:
                detail: object = clear_local_logs()
                success = True
            except Exception as exc:  # noqa: BLE001
                logger.exception("后台清除本机日志失败")
                detail, success = str(exc), False
            self._export_bridge.done.emit("local_log_cleanup", success, detail)

        self._export_buttons["local_log_cleanup"] = self.clear_local_log_btn
        self._export_workers["local_log_cleanup"] = threading.Thread(
            target=_worker, name="ui-local-log-cleanup", daemon=True,
        )
        self._export_workers["local_log_cleanup"].start()

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
        """Export the complete log without reading it on the UI thread."""
        if self._log_failed:
            return
        self._start_text_export(
            "logs",
            "导出日志",
            "voxsub_log.txt",
            lambda: tail_log_file(10**6),
        )

    # ------------------------------------------------------------------
    def _load_module(self) -> None:
        """尝试加载 M8 诊断模块；失败则保持占位。"""
        try:
            self._module = importlib.import_module("voxsub.diagnostics")
        except Exception:
            logger.debug("诊断模块不可用, 进入占位模式", exc_info=True)
            self._module = None

    def _render(self) -> None:
        self._clear_result_cards()

        if self._module is None or not hasattr(self._module, "run_self_check"):
            self._results = []
            self._add_placeholder()
            self.selfcheck_summary.setText(tr("未接入检查模块"))
            return

        try:
            self._results = self._module.run_self_check() or []
        except Exception:
            logger.exception("run_self_check 执行失败")
            self._results = [{"check": "自检执行", "status": "fail", "detail": "run_self_check 抛异常"}]
        self._render_cached_results()

    def _clear_result_cards(self) -> None:
        """Remove rendered result cards without rerunning an expensive self-check."""
        while self._vbox.count() > 1:
            item = self._vbox.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _render_cached_results(self) -> None:
        """Render the last self-check in the current UI language."""
        self._clear_result_cards()
        counts = {key: sum(1 for item in self._results if item.get("status") == key)
                  for key in ("ok", "warn", "fail")}
        self._last_counts = counts
        self._set_summary()
        for item in self._results:
            self._add_result_card(item)

    def _set_summary(self) -> None:
        counts = self._last_counts
        self.selfcheck_summary.setText(
            f"{counts['ok']} {tr('正常', 'OK')}  ·  "
            f"{counts['warn']} {tr('注意', 'Warnings')}  ·  "
            f"{counts['fail']} {tr('失败', 'Failed')}"
        )

    def _add_placeholder(self) -> None:
        card = QFrame(self._container)
        card.setObjectName("diagnosticCard")
        card.setProperty("status", "warn")
        box = QVBoxLayout(card)
        box.setContentsMargins(24, 28, 24, 28)
        box.setSpacing(8)
        t = QLabel(tr("诊断模块尚未实现"), card)
        t.setObjectName("sectionTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d = QLabel(
            tr("M8 里程碑将在此接入 voxsub.diagnostics.run_self_check() 的"
               "模型完整性 / ORT providers / ASR·VAD·TTS 冒烟 / 磁盘余量检查结果。",
               "The M8 milestone will connect voxsub.diagnostics.run_self_check() here for model integrity, ORT providers, ASR/VAD/TTS smoke checks, and disk-space results."),
            card,
        )
        d.setObjectName("secondaryLabel")
        d.setWordWrap(True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(t)
        box.addWidget(d)
        self._vbox.insertWidget(self._vbox.count() - 1, card)

    def _add_result_card(self, item: dict) -> None:
        check = tr(str(item.get("check", "未知检查项")))
        status = str(item.get("status", "warn"))
        detail = translate_dynamic(str(item.get("detail", "")))
        color = _STATUS_COLOR.get(status, _STATUS_COLOR["warn"])
        card = QFrame(self._container)
        card.setObjectName("diagnosticCard")
        card.setProperty("status", status if status in _STATUS_COLOR else "warn")
        row = QHBoxLayout(card)
        row.setContentsMargins(20, 14, 20, 14)
        row.setSpacing(12)
        mark = QLabel(_STATUS_CHAR.get(status, "⚠️"), card)
        mark.setObjectName("diagnosticMark")
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
    def _update_sentry_button(self) -> None:
        enabled = is_error_reporting_enabled()
        upload_busy = ("sentry" in self._export_workers or
                       "sentry_logs" in self._export_workers)
        if hasattr(self, "send_sentry_btn"):
            self.send_sentry_btn.setEnabled(
                self._selfcheck_worker is None
                and self._repair_worker is None
                and not upload_busy
                and enabled
            )
        if hasattr(self, "send_log_sentry_btn"):
            self.send_log_sentry_btn.setEnabled(
                not self._log_failed and not upload_busy and enabled)

    def _set_selfcheck_progress(self, value: int, label: str) -> None:
        """Keep stage text and the visual bar on separate, readable rows."""
        bounded = max(0, min(100, int(value)))
        self.selfcheck_progress_label.setText(f"{label} · {bounded}%")
        self.selfcheck_progress_label.show()
        self.selfcheck_progress.setValue(bounded)
        self.selfcheck_progress.show()

    def _set_selfcheck_busy(self, busy: bool, text: str = "") -> None:
        self.selfcheck_refresh_btn.setEnabled(not busy)
        self.repair_btn.setEnabled(not busy and self._repair_worker is None)
        self.export_btn.setEnabled(not busy and "report" not in self._export_workers)
        self._update_sentry_button()
        if busy:
            self._set_selfcheck_progress(0, text or tr("正在检查…"))
        else:
            self.selfcheck_progress_label.hide()
            self.selfcheck_progress.hide()

    def _on_selfcheck_progress(self, completed: int, total: int, label: str) -> None:
        if self._selfcheck_worker is None:
            return
        value = int(completed / total * 100) if total else 0
        self._set_selfcheck_progress(value, label)
        self.selfcheck_summary.setText(label)

    def _on_selfcheck_done(self, results: object) -> None:
        generation = self._selfcheck_generation
        payload = results
        if isinstance(results, dict) and "generation" in results:
            generation = int(results.get("generation", -1))
            payload = results.get("results", [])
        if generation != self._selfcheck_generation:
            logger.debug("忽略过期自检结果: generation=%s current=%s",
                         generation, self._selfcheck_generation)
            return
        self._selfcheck_worker = None
        self._set_selfcheck_busy(False)
        self._results = [dict(item) for item in (payload or [])]
        self._render_cached_results()

    def _start_selfcheck(self) -> None:
        if self._selfcheck_worker is not None:
            return
        if self._module is None or not hasattr(self._module, "run_self_check"):
            self._render()
            return
        module = self._module
        self._selfcheck_generation += 1
        generation = self._selfcheck_generation
        # A running check owns the only visible result set.  Do not leave a
        # previous snapshot on screen while a newer one is being calculated.
        self._results = []
        self._clear_result_cards()
        self._set_selfcheck_busy(True, tr("正在检查…"))

        def _worker() -> None:
            try:
                results = _call_self_check(module, self._selfcheck_bridge.progress.emit)
            except Exception:
                logger.exception("后台 run_self_check 执行失败")
                results = [{"check": "自检执行", "status": "fail", "detail": "run_self_check 抛异常"}]
            self._selfcheck_bridge.done.emit({
                "generation": generation,
                "results": results,
            })

        self._selfcheck_worker = threading.Thread(
            target=_worker, name="ui-self-check", daemon=True
        )
        self._selfcheck_worker.start()

    def _repair(self) -> None:
        """Repair only the targets declared by the latest self-check."""
        if self._repair_worker is not None or self._selfcheck_worker is not None:
            return
        module = self._module
        if module is None:
            self.selfcheck_summary.setText(tr("未接入检查模块"))
            return
        repair = getattr(module, "repair_self_check", None)
        if not callable(repair):
            self.selfcheck_summary.setText(tr("当前检查模块不支持按结果修复"))
            return
        latest_results = tuple(dict(item) for item in self._results)
        if not latest_results:
            self.selfcheck_summary.setText(tr("请先完成一次自检"))
            return
        self.repair_btn.setEnabled(False)
        self.selfcheck_refresh_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.send_sentry_btn.setEnabled(False)
        self._set_selfcheck_progress(0, tr("正在修复…"))

        def _worker() -> None:
            try:
                outcome = _call_repair(
                    module,
                    latest_results,
                    self._store,
                    self._repair_bridge.progress.emit,
                )
                self._repair_bridge.done.emit(outcome or {})
            except Exception as exc:  # noqa: BLE001
                logger.exception("后台自检修复失败")
                self._repair_bridge.done.emit({"repaired": [], "errors": [str(exc)]})

        self._repair_worker = threading.Thread(
            target=_worker, name="ui-self-check-repair", daemon=True
        )
        self._repair_worker.start()

    def _on_repair_progress(self, completed: int, total: int, label: str) -> None:
        if self._repair_worker is None:
            return
        value = int(completed / total * 100) if total else 0
        self._set_selfcheck_progress(value, label)
        self.selfcheck_summary.setText(label)

    def _on_repair_done(self, outcome: object) -> None:
        self._repair_worker = None
        repaired = list((outcome or {}).get("repaired", []))
        errors = list((outcome or {}).get("errors", []))
        self.selfcheck_progress.setValue(100)
        self.selfcheck_progress_label.hide()
        self.repair_btn.setEnabled(self._selfcheck_worker is None)
        self.selfcheck_refresh_btn.setEnabled(self._selfcheck_worker is None)
        self.export_btn.setEnabled("report" not in self._export_workers)
        self._update_sentry_button()
        self.selfcheck_progress.hide()
        if errors:
            self.selfcheck_summary.setText(f"{tr('修复失败')}: {errors[0]}")
        elif repaired:
            self.selfcheck_summary.setText(f"{tr('修复完成')} · {', '.join(repaired)}")
        else:
            self.selfcheck_summary.setText(tr("没有需要修复的项目"))
        self._start_selfcheck()

    def _export_report(self) -> None:
        """Export a report in a worker; self-check work can be expensive."""
        module = self._module
        results = tuple(dict(item) for item in self._results)

        def _build_report() -> str:
            if module is not None and hasattr(module, "export_report"):
                return _call_export_report(module, results)
            lines = [
                "语幕 VoxSub 诊断报告（诊断模块未实现，以下为骨架内容）",
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ]
            for item in results:
                lines.append(
                    f"[{item.get('status', 'warn')}] {item.get('check', '')} — {item.get('detail', '')}"
                )
            return "\n".join(lines)

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        self._start_text_export(
            "report",
            "导出诊断报告",
            f"voxsub_diagnostics_{timestamp}.txt",
            _build_report,
        )

    def _send_report_to_sentry(self) -> None:
        """Upload the latest visible self-check and local logs off the UI thread."""
        if ("sentry" in self._export_workers or
                "sentry_logs" in self._export_workers or
                not is_error_reporting_enabled()):
            self.selfcheck_summary.setText(tr("Sentry 未配置"))
            return
        module = self._module
        results = tuple(dict(item) for item in self._results)
        if not results:
            self.selfcheck_summary.setText(tr("请先完成一次自检"))
            return
        self.send_sentry_btn.setEnabled(False)
        self.send_sentry_btn.setText(tr("正在发送…"))

        def _worker() -> None:
            try:
                report = "语幕 VoxSub 诊断报告\n" + "\n".join(
                    f"[{item.get('status', 'warn')}] {item.get('check', '')}: "
                    f"{item.get('detail', '')}" for item in results
                )
                if module is not None and hasattr(module, "export_report"):
                    try:
                        parameters = inspect.signature(module.export_report).parameters
                    except (TypeError, ValueError):
                        parameters = {}
                    if "results" in parameters:
                        report = _call_export_report(module, results)
                logs, session_metadata = diagnostic_session_log_snapshot()
                if session_metadata is None:
                    logs = tail_log_file(10**6)
                upload_kwargs = {"trigger": "diagnostics_page"}
                if session_metadata is not None:
                    upload_kwargs["session_metadata"] = session_metadata
                upload_metadata = preview_log_snapshot_metadata(
                    logs, session_metadata=session_metadata)
                success = bool(send_diagnostic_report(report, logs, **upload_kwargs))
                detail: object = upload_metadata if success else "upload-failed"
            except Exception as exc:  # noqa: BLE001
                logger.exception("后台发送 Sentry 诊断报告失败")
                success, detail = False, str(exc)
            self._export_bridge.done.emit("sentry", success, detail)

        self._export_buttons["sentry"] = self.send_sentry_btn
        self._export_workers["sentry"] = threading.Thread(
            target=_worker, name="ui-sentry-report", daemon=True,
        )
        self._export_workers["sentry"].start()

    def _send_logs_to_sentry(self) -> None:
        """Upload a filtered local-log snapshot without requiring a self-check."""
        if (self._log_failed or "sentry" in self._export_workers or
                "sentry_logs" in self._export_workers or
                not is_error_reporting_enabled()):
            if hasattr(self, "log_live_state"):
                self.log_live_state.setText(tr("Sentry 未配置"))
            return
        self.send_log_sentry_btn.setEnabled(False)
        self.send_log_sentry_btn.setText(tr("正在上传日志…"))

        def _worker() -> None:
            try:
                logs, session_metadata = diagnostic_session_log_snapshot()
                if session_metadata is None:
                    logs = tail_log_file(10**6)
                upload_kwargs = {"trigger": "diagnostics_logs_tab"}
                if session_metadata is not None:
                    upload_kwargs["session_metadata"] = session_metadata
                upload_metadata = preview_log_snapshot_metadata(
                    logs, session_metadata=session_metadata)
                success = bool(send_log_snapshot(logs, **upload_kwargs))
                detail: object = upload_metadata if success else "upload-failed"
            except Exception as exc:  # noqa: BLE001
                logger.exception("后台发送 Sentry 日志失败")
                success, detail = False, str(exc)
            self._export_bridge.done.emit("sentry_logs", success, detail)

        self._export_buttons["sentry_logs"] = self.send_log_sentry_btn
        self._export_workers["sentry_logs"] = threading.Thread(
            target=_worker, name="ui-sentry-log-upload", daemon=True,
        )
        self._export_workers["sentry_logs"].start()

    def _start_text_export(self, kind: str, title: str, suggested_name: str,
                           build_text) -> None:
        """Choose a path synchronously, then build and write data in a worker."""
        if kind in self._export_workers:
            return
        path, _ = choose_save_file(
            self, title, suggested_name, ["文本文件 (*.txt)"], default_suffix="txt"
        )
        if not path:
            return
        output = Path(path)
        if not output.suffix:
            output = output.with_suffix(".txt")
        button = self.export_log_btn if kind == "logs" else self.export_btn
        self._export_buttons[kind] = button
        button.setEnabled(False)
        button.setText(tr("正在导出…"))

        def _worker() -> None:
            try:
                text = str(build_text())
                written = write_text_atomically(output, text, encoding="utf-8")
                success, detail = True, str(written)
            except Exception as exc:  # noqa: BLE001
                logger.exception("后台导出失败: kind=%s", kind)
                success, detail = False, str(exc)
            self._export_bridge.done.emit(kind, success, detail)

        worker = threading.Thread(target=_worker, name=f"ui-{kind}-export", daemon=True)
        self._export_workers[kind] = worker
        worker.start()

    def _on_export_done(self, kind: str, success: bool, detail: object) -> None:
        self._export_workers.pop(kind, None)
        button = self._export_buttons.pop(kind, None)
        if button is not None:
            button.setEnabled(True)
            if kind == "logs":
                button.setText(tr("导出日志"))
            elif kind == "sentry":
                button.setText(tr("发送到 Sentry"))
                self._update_sentry_button()
            elif kind == "sentry_logs":
                button.setText(tr("上传日志到 Sentry"))
                self._update_sentry_button()
            elif kind == "local_log_cleanup":
                button.setText(tr("清除本机日志"))
            else:
                button.setText(tr("导出报告 (txt)"))
        if success:
            if kind == "logs":
                self.log_live_state.setText(f"{tr('日志已导出')} · {Path(str(detail)).name}")
            elif kind == "sentry":
                self.selfcheck_summary.setText(
                    tr("诊断报告已发送（日志为部分上传）")
                    if _log_upload_is_partial(detail)
                    else tr("诊断报告已发送"))
                self._show_log_upload_summary(detail)
            elif kind == "sentry_logs":
                self.log_live_state.setText(
                    tr("日志已部分上传到 Sentry")
                    if _log_upload_is_partial(detail)
                    else tr("日志已发送到 Sentry"))
                self._show_log_upload_summary(detail)
            elif kind == "local_log_cleanup":
                self.log_view.clear()
                self._last_seen = None
                self.log_live_state.setText(tr("本机日志已清除"))
                self._show_log_cleanup_summary(detail)
            else:
                self.selfcheck_summary.setText(f"{tr('报告已导出')} · {Path(detail).name}")
        else:
            if kind == "logs":
                self.log_live_state.setText(tr("日志导出失败"))
            elif kind == "sentry":
                self.selfcheck_summary.setText(tr("诊断报告发送失败"))
            elif kind == "sentry_logs":
                self.log_live_state.setText(tr("日志发送失败"))
            elif kind == "local_log_cleanup":
                self.log_live_state.setText(tr("本机日志清除失败"))
            else:
                self.selfcheck_summary.setText(tr("报告导出失败"))

    def _show_log_upload_summary(self, detail: object) -> None:
        """Render the exact safe range/count metadata returned by an upload."""
        if not isinstance(detail, Mapping) or not hasattr(self, "log_operation_state"):
            return
        self.log_operation_state.setText(_format_log_upload_summary(detail))
        self.log_operation_state.show()

    def _show_log_cleanup_summary(self, detail: object) -> None:
        """Show cleanup counts without exposing any local file path."""
        if not isinstance(detail, Mapping) or not hasattr(self, "log_operation_state"):
            return
        cleared = _as_non_negative_int(detail.get("cleared_files"))
        failed = _as_non_negative_int(detail.get("failed_files"))
        text = tr("已清除 {cleared} 个本机日志文件").format(cleared=cleared)
        if failed:
            text += "\n" + tr("仍有 {failed} 个日志文件未能清除，请关闭占用程序后再试").format(
                failed=failed)
        self.log_operation_state.setText(text)
        self.log_operation_state.show()

    def refresh(self) -> None:
        # The module is imported once during construction.  Re-importing it on
        # every click can invalidate injected implementations and adds avoidable
        # latency before the actual background check even starts.
        if self._module is None:
            self._load_module()
        self._start_selfcheck()

    def _on_language_changed(self, _language: str) -> None:
        retranslate_widget_tree(self)
        if not hasattr(self, "selfcheck_summary"):
            return
        self._clear_result_cards()
        if self._module is None or not hasattr(self._module, "run_self_check"):
            self._add_placeholder()
            self.selfcheck_summary.setText(tr("未接入检查模块"))
            return
        self._render_cached_results()

    def closeEvent(self, event) -> None:  # noqa: N802
        timer = getattr(self, "log_timer", None)
        if timer is not None:
            timer.stop()
        session_timer = getattr(self, "diagnostic_session_timer", None)
        if session_timer is not None:
            session_timer.stop()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        # Settings can be changed while this window is hidden; refresh the
        # button state when it becomes visible again.
        self._update_sentry_button()
        super().showEvent(event)
