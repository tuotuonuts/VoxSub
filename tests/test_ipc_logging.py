"""IPC 日志与录音查询的测试 —— 覆盖用户报告的两个缺陷。

## 缺陷 1：所有日志在界面上都显示成 ERROR

界面日志区唯一能拿到的通路是后端 **stderr**，而 Electron 侧把它一律标成
`level: "error"`。结构化日志事件（带准确级别与时间戳）本该由 `_install_log_sink`
发出，但它把 handler 挂在了**根 logger** 上，而 `logging_setup.setup_logging()`
把 handler 挂在 `"voxsub"` 上并设了 `propagate = False` —— 于是永远收不到
任何记录，`recent_logs(source="memory")` 也恒为空。

这里守住：
  · handler 挂在 "voxsub" 上，能收到 voxsub.* 的记录
  · 事件带 record 的真实级别与时间戳（不是猜的）
  · 内存缓冲同步填充（供 recent_logs(memory)）
  · 摘掉 stderr 控制台 handler（否则同一条日志会到两次），且**保留文件 handler**

## 缺陷 2：`last_recording` 每次结束会话都报假错误

`Pipeline.last_recording_path` 是 @property，而命令里写成了
`pipeline.last_recording_path()` → `None()` → TypeError。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "frontend" / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import ipc_server  # noqa: E402


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("VOXSUB_ROOT", str(Path(__file__).resolve().parents[1]))
    return tmp_path


@pytest.fixture()
def captured_events(monkeypatch):
    """捕获 _emit 写出的协议消息（避免测试往真实 stdout 写）。"""
    events: list[dict] = []
    monkeypatch.setattr(ipc_server, "_emit", lambda payload: events.append(payload))
    return events


# "voxsub" logger 的标准 handler 集合（文件 + stderr 控制台 + 环形缓冲）。
# 第一次进入夹具时记录，之后每个用例前后都恢复到它 ——
# 否则某个用例装过日志桥（会摘掉 stderr handler）之后，后面的用例拿到的
# 初始状态就不一样了，出现"单独跑能过、一起跑就挂"的假失败。
_VOXSUB_BASELINE: list[logging.Handler] | None = None


@pytest.fixture()
def clean_voxsub_logger():
    """让每个用例都在**确定的 handler 状态**下运行，跑完恢复。"""
    global _VOXSUB_BASELINE

    from voxsub.logging_setup import get_logger

    logger = logging.getLogger("voxsub")
    get_logger("ipc")  # 确保 setup_logging 已跑过，标准 handler 已就位

    if _VOXSUB_BASELINE is None:
        _VOXSUB_BASELINE = list(logger.handlers)

    def restore() -> None:
        assert _VOXSUB_BASELINE is not None
        for handler in list(logger.handlers):
            if handler not in _VOXSUB_BASELINE:
                logger.removeHandler(handler)
        for handler in _VOXSUB_BASELINE:
            if handler not in logger.handlers:
                logger.addHandler(handler)

    restore()
    yield logger
    restore()


# --------------------------------------------------------------- 缺陷 1：日志桥


class TestLogBridge:
    def test_bridge_attaches_to_voxsub_logger_not_root(
        self, isolated_config, captured_events, clean_voxsub_logger
    ):
        """handler 必须挂在 "voxsub" 上。

        挂在根 logger 上时永远收不到记录（"voxsub" 的 propagate=False），
        表现是：界面日志区一条结构化日志都没有、recent_logs(memory) 恒为空，
        而所有日志都被 stderr 通路标成 ERROR。
        """
        service = ipc_server.BackendService()
        service._install_log_sink()  # noqa: SLF001

        voxsub_handlers = clean_voxsub_logger.handlers
        bridges = [h for h in voxsub_handlers if h.__class__.__name__ == "_Bridge"]
        assert bridges, f'"voxsub" logger 上没有日志桥，实际 {voxsub_handlers}'

        root_handlers = logging.getLogger().handlers
        root_bridges = [h for h in root_handlers if h.__class__.__name__ == "_Bridge"]
        assert not root_bridges, "日志桥不该挂在根 logger 上（收不到 voxsub 记录）"

    def test_emits_event_with_real_level_and_timestamp(
        self, isolated_config, captured_events, clean_voxsub_logger
    ):
        """事件的 level 必须来自 record，不能是猜的、更不能写死。

        用户报的正是"很多不是错误的日志被识别成 ERROR"。
        """
        service = ipc_server.BackendService()
        service._install_log_sink()  # noqa: SLF001
        captured_events.clear()

        logger = logging.getLogger("voxsub.test_probe")
        logger.info("这是一条信息")
        logger.warning("这是一条告警")

        logs = [e for e in captured_events if e.get("event") == "log"]
        assert len(logs) >= 2, f"应至少收到 2 条日志事件，实际 {logs}"

        levels = [e["level"] for e in logs]
        assert "INFO" in levels, f"INFO 记录的级别应为 INFO，实际 {levels}"
        assert "WARNING" in levels, f"WARNING 记录的级别应为 WARNING，实际 {levels}"
        assert "ERROR" not in levels, f"非错误日志不该标成 ERROR，实际 {levels}"

        for entry in logs:
            assert entry.get("ts"), f"日志事件必须带时间戳，实际 {entry}"
            assert "test_probe" in str(entry.get("message", "")), entry

    def test_memory_buffer_filled_for_recent_logs(
        self, isolated_config, captured_events, clean_voxsub_logger
    ):
        """内存缓冲要同步填充，recent_logs(memory) 才有内容。"""
        service = ipc_server.BackendService()
        service._install_log_sink()  # noqa: SLF001

        logging.getLogger("voxsub.test_probe").info("缓冲探针")
        result = service._cmd_recent_logs({"limit": 50, "source": "memory"})  # noqa: SLF001

        assert result["logs"], "内存缓冲不该为空"
        assert any("缓冲探针" in e["message"] for e in result["logs"]), result["logs"]

    def test_removes_stderr_handler_but_keeps_file_handler(
        self, isolated_config, captured_events, clean_voxsub_logger
    ):
        """摘掉 stderr 控制台 handler（避免重复），但文件 handler 必须保留。

        文件 handler 是 voxsub.log 的写入者；摘掉它就没有历史日志可查了。
        """
        # 先确保 logging_setup 已装好 handler（含 stderr 与文件两个）
        from voxsub.logging_setup import get_logger

        get_logger("ipc")
        before = list(clean_voxsub_logger.handlers)
        assert any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            for h in before
        ), f"前置条件不成立：应存在 stderr handler，实际 {before}"

        service = ipc_server.BackendService()
        service._install_log_sink()  # noqa: SLF001

        after = list(clean_voxsub_logger.handlers)
        stderr_like = [
            h for h in after
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert not stderr_like, f"stderr handler 应被摘掉，实际仍有 {stderr_like}"
        assert any(isinstance(h, logging.FileHandler) for h in after), (
            f"文件 handler 必须保留（voxsub.log 仍要写），实际 {after}"
        )

    def test_install_is_idempotent(
        self, isolated_config, captured_events, clean_voxsub_logger
    ):
        """重复调用不应叠加 handler（否则日志会成倍重复）。"""
        service = ipc_server.BackendService()
        service._install_log_sink()  # noqa: SLF001
        first = len(clean_voxsub_logger.handlers)
        service._install_log_sink()  # noqa: SLF001
        assert len(clean_voxsub_logger.handlers) == first, "重复安装不该新增 handler"


# ------------------------------------------------------- 缺陷 2：last_recording


class TestLastRecording:
    class _FakePipeline:
        """最小替身：last_recording_path 是 property（与真实 Pipeline 一致）。"""

        def __init__(self, path):
            self._path = path

        @property
        def last_recording_path(self):
            return self._path

    def test_returns_none_without_raising(self, isolated_config):
        """没有录音时返回 {"path": None}，不能抛 TypeError。

        真实症状：每次结束会话前端都会查一次，于是日志里刷满
        `last_recording 失败: TypeError: 'NoneType' object is not callable`。
        """
        service = ipc_server.BackendService()
        result = service._cmd_last_recording(self._FakePipeline(None), {})  # noqa: SLF001
        assert result == {"path": None}

    def test_returns_path_when_present(self, isolated_config, tmp_path):
        target = tmp_path / "rec.wav"
        target.write_bytes(b"RIFF")
        service = ipc_server.BackendService()
        result = service._cmd_last_recording(self._FakePipeline(target), {})  # noqa: SLF001
        assert result["path"] == str(target)

    def test_real_pipeline_property_is_readable(self, isolated_config):
        """用真实 Pipeline 验证一遍：属性可读、且不会被当成方法调用。"""
        from voxsub.pipeline import Pipeline

        pipeline = Pipeline()
        assert pipeline.last_recording_path is None

        service = ipc_server.BackendService()
        result = service._cmd_last_recording(pipeline, {})  # noqa: SLF001
        assert result == {"path": None}
