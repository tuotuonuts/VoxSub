"""语幕 VoxSub —— 统一日志基建 + 诊断页「日志」页签 测试（可观测性改造 P0）。

覆盖：
- logging_setup：setup_logging 幂等（不重复挂 handler） / drain_events 能取到
  刚 logger.info 的事件（字段 ts/level/name/message 齐备） / tail_log_file 行为
- diagnostics_window「日志」页签 offscreen 冒烟：QPlainTextEdit 存在、只读、
  可填充文本不崩；轮询增量追加（指纹去重）；刷新 / 导出按钮存在；timer 配置正确

运行: cd VoxSub && unset PYTHONPATH PYTHONHOME && .venv/Scripts/python.exe -m pytest tests/test_logging_ui.py -v

注意：本文件不触碰真实 %LOCALAPPDATA% 日志文件的写入（tail 相关用例通过
monkeypatch logging_setup._log_dir 指向 tmp 目录）；日志基建的全局初始化由
其它模块 import 时自然发生（幂等，与用例顺序无关）。
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

# 无头运行：必须在任何 Qt 构造前设置（QApplication 读取该变量）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

import voxsub.logging_setup as logging_setup  # noqa: E402
from voxsub.logging_setup import drain_events, get_logger, setup_logging, tail_log_file  # noqa: E402


# ---------------------------------------------------------------------------
# 共享 QApplication（模块级单例，offscreen）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _wait_until(qapp, predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    qapp.processEvents()
    assert predicate(), "timed out waiting for asynchronous export"


# ===========================================================================
# 1. logging_setup 单测
# ===========================================================================
class TestSetupLogging:
    def test_setup_logging_is_idempotent(self):
        """重复 setup_logging 不重复挂 handler（幂等）。"""
        before = list(logging.getLogger("voxsub").handlers)
        setup_logging()  # 已初始化则应直接返回
        setup_logging("DEBUG", log_to_console=True)
        after = list(logging.getLogger("voxsub").handlers)
        assert logging_setup._HANDLERS_INITIALIZED  # noqa: SLF001
        assert len(after) == len(before)
        assert after == before  # 同一批 handler 对象, 无重复挂载

    def test_drain_events_contains_just_logged_info(self):
        """drain_events 能取到刚 logger.info 的事件, 字段齐备。"""
        lg = get_logger("test.drain")
        lg.info("drain 冒烟标记 %s", "msg-42")
        evs = drain_events(200)
        hit = [e for e in evs if e["name"] == "voxsub.test.drain" and "msg-42" in e["message"]]
        assert hit, "刚 logger.info 的事件应能从 drain_events 取到"
        last = hit[-1]
        assert last["level"] == "INFO"
        assert last["ts"]
        assert last["message"] == "drain 冒烟标记 msg-42"

    def test_drain_events_respects_limit(self):
        """drain_events(limit) 至多返回 limit 条。"""
        lg = get_logger("test.drain")
        for i in range(5):
            lg.warning("drain 限流测试 %d", i)
        evs = drain_events(3)
        assert len(evs) <= 3
        assert evs[-1]["message"].endswith("4")  # 最新一条在最末

    def test_get_logger_scoped_under_voxsub(self):
        assert get_logger("ui.xyz").name == "voxsub.ui.xyz"


class TestTailLogFile:
    def test_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(logging_setup, "_log_dir", lambda: tmp_path / "no_such_dir")
        assert tail_log_file(10) == ""

    def test_reads_tail_lines(self, monkeypatch, tmp_path):
        (tmp_path / "voxsub.log").write_text(
            "line1\nline2\nline3\n", encoding="utf-8"
        )
        monkeypatch.setattr(logging_setup, "_log_dir", lambda: tmp_path)
        assert tail_log_file(2) == "line2\nline3\n"
        assert tail_log_file(99) == "line1\nline2\nline3\n"

    def test_unreadable_file_returns_message(self, monkeypatch, tmp_path):
        # 用目录替代文件 → open 抛 OSError → 返回提示文案而非抛异常
        monkeypatch.setattr(logging_setup, "_log_dir", lambda: tmp_path)
        (tmp_path / "voxsub.log").mkdir()
        assert tail_log_file(5).startswith("<读取日志失败")


# ===========================================================================
# 2. 诊断页「日志」页签 offscreen 冒烟
# ===========================================================================
class TestDiagnosticsLogTab:
    def test_log_tab_widgets_and_fill(self, qapp):
        from PySide6.QtWidgets import QPlainTextEdit, QPushButton

        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        dw = DiagnosticsWindow(diagnostics_module=None)  # 跳过真实自检, 聚焦日志页签
        try:
            assert dw.tabs.count() == 2
            assert dw.tabs.tabText(0) == "自检"
            assert dw.tabs.tabText(1) == "日志"
            edits = dw.findChildren(QPlainTextEdit)
            assert len(edits) == 1
            view = edits[0]
            assert view.isReadOnly()
            view.setPlainText("塞一段测试文本")  # 可填充, 不崩
            assert view.toPlainText() == "塞一段测试文本"
            btns = {b.text() for b in dw.findChildren(QPushButton)}
            assert {"刷新", "导出日志", "上传日志到 Sentry", "导出报告 (txt)"} <= btns
            # 1s 轮询 timer 已启动
            assert dw.log_timer.isActive()
            assert dw.log_timer.interval() == 1000
        finally:
            dw.close()
            dw.deleteLater()

    def test_poll_appends_new_events(self, qapp):
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        dw = DiagnosticsWindow(diagnostics_module=None)
        try:
            get_logger("test.diag").info("实时日志标记-1")
            dw._poll_events()  # noqa: SLF001
            assert "实时日志标记-1" in dw.log_view.toPlainText()
            assert dw._last_seen is not None  # noqa: SLF001

            get_logger("test.diag").info("实时日志标记-2")
            dw._poll_events()  # noqa: SLF001
            assert "实时日志标记-2" in dw.log_view.toPlainText()
            assert "实时日志标记-1" in dw.log_view.toPlainText()
        finally:
            dw.close()
            dw.deleteLater()

    def test_refresh_reloads_and_keeps_placeholders(self, qapp):
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        dw = DiagnosticsWindow(diagnostics_module=None)
        try:
            get_logger("test.diag").info("刷新前标记")
            dw._refresh_logs()  # noqa: SLF001 —— 重读文件尾部, 不崩
            assert isinstance(dw._last_seen, str) or dw._last_seen is None  # noqa: SLF001
        finally:
            dw.close()
            dw.deleteLater()

    def test_format_helper_line_shape(self):
        """行格式: "HH:MM:SS LEVEL [name] message"。"""
        from voxsub.ui.diagnostics_window import _fmt_log_line, _strip_file_ts

        line = _fmt_log_line({"ts": "2026-08-17 10:23:45", "level": "INFO", "name": "voxsub.a", "message": "hi"})
        assert line.startswith("10:23:45 INFO")
        assert "[voxsub.a]" in line and line.endswith("hi")
        assert _strip_file_ts("2026-08-17 10:23:45 INFO     [voxsub.a] hi").startswith("10:23:45")
        assert _strip_file_ts("普通无前缀行") == "普通无前缀行"

    def test_debug_switch_changes_runtime_level(self, qapp, tmp_path):
        from voxsub.ui.config_store import ConfigStore
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        store = ConfigStore(tmp_path / "config.json")
        dw = DiagnosticsWindow(diagnostics_module=None, store=store)
        try:
            dw.debug_switch.setChecked(True)
            assert store.get("debug_mode") is True
            assert logging.getLogger("voxsub").isEnabledFor(logging.DEBUG)
            dw.debug_switch.setChecked(False)
            assert not logging.getLogger("voxsub").isEnabledFor(logging.DEBUG)
        finally:
            dw.close()
            dw.deleteLater()

    def test_log_export_does_not_block_the_ui_thread(self, qapp, tmp_path, monkeypatch):
        import voxsub.ui.diagnostics_window as diagnostics_window
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        output = tmp_path / "voxsub_log.txt"
        dw = DiagnosticsWindow(diagnostics_module=None)
        try:
            monkeypatch.setattr(
                diagnostics_window,
                "choose_save_file",
                lambda *a, **k: (str(output), "文本文件 (*.txt)"),
            )

            def _slow_tail(_lines):
                time.sleep(0.15)
                return "line one\nline two\n"

            monkeypatch.setattr(diagnostics_window, "tail_log_file", _slow_tail)
            started = time.monotonic()
            dw._export_logs()  # noqa: SLF001
            assert time.monotonic() - started < 0.08
            assert not dw.export_log_btn.isEnabled()
            _wait_until(qapp, lambda: "logs" not in dw._export_workers)  # noqa: SLF001
            assert output.read_text(encoding="utf-8") == "line one\nline two\n"
            assert dw.export_log_btn.isEnabled()
        finally:
            dw.close()
            dw.deleteLater()

    def test_manual_log_upload_uses_filtered_sentry_path(self, qapp, monkeypatch):
        import voxsub.ui.diagnostics_window as diagnostics_window
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        captured = {}
        monkeypatch.setattr(diagnostics_window, "is_error_reporting_enabled",
                            lambda: True)
        monkeypatch.setattr(diagnostics_window, "tail_log_file",
                            lambda _lines: "ERROR local log")
        monkeypatch.setattr(
            diagnostics_window,
            "send_log_snapshot",
            lambda logs, trigger="": captured.update(logs=logs, trigger=trigger) or True,
        )
        dw = DiagnosticsWindow(diagnostics_module=None)
        try:
            dw._send_logs_to_sentry()  # noqa: SLF001
            _wait_until(qapp, lambda: "sentry_logs" not in dw._export_workers)  # noqa: SLF001
            assert captured["trigger"] == "diagnostics_logs_tab"
            assert captured["logs"] == "ERROR local log"
            assert dw.send_log_sentry_btn.isEnabled()
            assert "Sentry" in dw.log_live_state.text()
        finally:
            dw.close()
            dw.deleteLater()

    def test_report_export_does_not_block_the_ui_thread(self, qapp, tmp_path, monkeypatch):
        import voxsub.ui.diagnostics_window as diagnostics_window
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        class _SlowReport:
            @staticmethod
            def export_report() -> str:
                time.sleep(0.15)
                return "report body"

        output = tmp_path / "voxsub_diagnostics.txt"
        dw = DiagnosticsWindow(diagnostics_module=_SlowReport())
        try:
            monkeypatch.setattr(
                diagnostics_window,
                "choose_save_file",
                lambda *a, **k: (str(output), "文本文件 (*.txt)"),
            )
            started = time.monotonic()
            dw._export_report()  # noqa: SLF001
            assert time.monotonic() - started < 0.08
            assert not dw.export_btn.isEnabled()
            _wait_until(qapp, lambda: "report" not in dw._export_workers)  # noqa: SLF001
            assert output.read_text(encoding="utf-8") == "report body"
            assert dw.export_btn.isEnabled()
        finally:
            dw.close()
            dw.deleteLater()

    def test_selfcheck_refresh_runs_off_ui_thread_with_progress(self, qapp):
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        calls = {"count": 0}

        class _SlowDiag:
            @staticmethod
            def run_self_check(progress=None):
                calls["count"] += 1
                if calls["count"] > 1:
                    if progress:
                        progress(0, 2, "phase one")
                    time.sleep(0.15)
                    if progress:
                        progress(2, 2, "phase two")
                return [{"check": "模型完整性", "status": "ok", "detail": "ready"}]

        dw = DiagnosticsWindow(diagnostics_module=_SlowDiag())
        try:
            started = time.monotonic()
            dw.refresh()
            assert time.monotonic() - started < 0.08
            assert not dw.selfcheck_refresh_btn.isEnabled()
            assert not dw.selfcheck_progress.isHidden()
            assert not dw.selfcheck_progress_label.isHidden()
            assert not dw.selfcheck_progress.isTextVisible()
            _wait_until(qapp, lambda: dw._selfcheck_worker is None)  # noqa: SLF001
            assert dw.selfcheck_progress.isHidden()
            assert dw.selfcheck_progress_label.isHidden()
            assert dw.selfcheck_refresh_btn.isEnabled()
            assert dw.selfcheck_progress.value() == 100
        finally:
            dw.close()
            dw.deleteLater()

    def test_new_selfcheck_replaces_old_snapshot_immediately(self, qapp):
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        release = threading.Event()
        calls = {"count": 0}

        class _Diag:
            @staticmethod
            def run_self_check(progress=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    return [{"check": "旧结果", "status": "fail", "detail": "旧问题"}]
                release.wait(2)
                return [{"check": "新结果", "status": "ok", "detail": "已恢复"}]

        dw = DiagnosticsWindow(diagnostics_module=_Diag())
        try:
            assert dw._results[0]["check"] == "旧结果"  # noqa: SLF001
            dw.refresh()
            assert dw._results == []  # noqa: SLF001
            assert not any("旧问题" in label.text() for label in dw.findChildren(type(dw.selfcheck_summary)))
            release.set()
            _wait_until(qapp, lambda: dw._selfcheck_worker is None)  # noqa: SLF001
            assert [item["check"] for item in dw._results] == ["新结果"]  # noqa: SLF001
        finally:
            release.set()
            dw.close()
            dw.deleteLater()

    def test_repair_receives_latest_selfcheck_snapshot(self, qapp):
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        captured = {}
        calls = {"count": 0}

        class _Diag:
            @staticmethod
            def run_self_check(progress=None):
                calls["count"] += 1
                return [{
                    "check": "ASR 冒烟",
                    "status": "fail",
                    "detail": "ASR 模型损坏",
                    "repair": {"kind": "models", "model_ids": ["asr-target"]},
                }]

            @staticmethod
            def repair_self_check(results, store=None, progress=None):
                captured["results"] = results
                return {"repaired": ["asr-target"], "errors": []}

        dw = DiagnosticsWindow(diagnostics_module=_Diag())
        try:
            expected = tuple(dict(item) for item in dw._results)  # noqa: SLF001
            dw._repair()  # noqa: SLF001
            _wait_until(qapp, lambda: "results" in captured)
            assert captured["results"] == expected
            _wait_until(qapp, lambda: dw._selfcheck_worker is None)  # noqa: SLF001
            assert calls["count"] >= 2  # 修复完成后仍按既有体验自动复检
        finally:
            dw.close()
            dw.deleteLater()

    def test_report_filename_has_timestamp(self, qapp, tmp_path, monkeypatch):
        import voxsub.ui.diagnostics_window as diagnostics_window
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        output = tmp_path / "report.txt"
        captured = {}

        class _Diag:
            @staticmethod
            def run_self_check():
                return []

            @staticmethod
            def export_report():
                return "report body"

        def _choose(*args, **kwargs):
            captured["suggested"] = args[2]
            return str(output), "文本文件 (*.txt)"

        dw = DiagnosticsWindow(diagnostics_module=_Diag())
        try:
            monkeypatch.setattr(diagnostics_window, "choose_save_file", _choose)
            dw._export_report()  # noqa: SLF001
            _wait_until(qapp, lambda: "report" not in dw._export_workers)  # noqa: SLF001
            assert re.fullmatch(r"voxsub_diagnostics_\d{14}\.txt", captured["suggested"])
        finally:
            dw.close()
            dw.deleteLater()

    def test_report_export_uses_latest_snapshot(self, qapp, tmp_path, monkeypatch):
        import voxsub.ui.diagnostics_window as diagnostics_window
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        output = tmp_path / "report.txt"
        captured = {}

        class _Diag:
            @staticmethod
            def run_self_check():
                return [{"check": "最新", "status": "fail", "detail": "当前问题"}]

            @staticmethod
            def export_report(results=None):
                captured["results"] = results
                return "snapshot report"

        monkeypatch.setattr(diagnostics_window, "choose_save_file",
                            lambda *args, **kwargs: (str(output), "文本文件 (*.txt)"))
        dw = DiagnosticsWindow(diagnostics_module=_Diag())
        try:
            expected = tuple(dict(item) for item in dw._results)  # noqa: SLF001
            dw._export_report()  # noqa: SLF001
            _wait_until(qapp, lambda: output.exists() and "results" in captured)
            assert captured["results"] == expected
            assert output.read_text(encoding="utf-8") == "snapshot report"
        finally:
            dw.close()
            dw.deleteLater()

    def test_send_report_to_sentry_uses_latest_snapshot_and_logs(
            self, qapp, monkeypatch):
        import voxsub.ui.diagnostics_window as diagnostics_window
        from voxsub.ui.diagnostics_window import DiagnosticsWindow

        captured = {}

        class _Diag:
            @staticmethod
            def run_self_check():
                return [{"check": "最新", "status": "fail", "detail": "当前问题"}]

            @staticmethod
            def export_report(results=None):
                captured["results"] = results
                return "snapshot report"

        monkeypatch.setattr(diagnostics_window, "is_error_reporting_enabled",
                            lambda: True)
        monkeypatch.setattr(diagnostics_window, "tail_log_file",
                            lambda _lines: "INFO local log")
        monkeypatch.setattr(
            diagnostics_window,
            "send_diagnostic_report",
            lambda report, logs, trigger="": captured.update(
                report=report, logs=logs, trigger=trigger) or True,
        )
        dw = DiagnosticsWindow(diagnostics_module=_Diag())
        try:
            expected = tuple(dict(item) for item in dw._results)  # noqa: SLF001
            dw._send_report_to_sentry()  # noqa: SLF001
            _wait_until(qapp, lambda: captured.get("report") == "snapshot report")
            assert captured["results"] == expected
            assert captured["logs"] == "INFO local log"
            assert captured["trigger"] == "diagnostics_page"
        finally:
            dw.close()
            dw.deleteLater()
