"""voxsub.logging_setup —— 统一日志基建 (大项目可观测性, P0)。

语幕首版会有大量真机 bug, debug 必须能拿到完整现场。设计:
- 单入口 get_logger(name) 获取各模块 logger (模块内 `logger = get_logger(__name__)`)
- 落盘: %LOCALAPPDATA%\\VoxSub\\logs\\voxsub.log, 单文件 5MB 轮转, 保留 5 个
- 同步到控制台 (开发期) + 事件总线 (UI 诊断页"实时日志"页签可看)
- 捕获工具: logger.exception() 自动带 traceback, 供第一现场排查
- 级别: 默认 INFO; VOXSUB_LOG 环境变量可覆盖 (DEBUG 排查用)

UI 侧通过 subscribe() 订阅日志事件流, 无需轮询磁盘。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import threading
from pathlib import Path


def _log_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "logs"


#: 内存环形日志事件缓冲 (诊断页/调试读取); 上限 2000 条
_EVENT_QUEUE: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=2000)
_QUEUE_LOCK = threading.Lock()
_HANDLERS_INITIALIZED = False
_INIT_LOCK = threading.Lock()


class _RingBufferHandler(logging.Handler):
    """把日志记录送进内存环形队列 (UI 诊断页 + 事后 dump)。

    避免直接打印/写文件重复, 仅作事件分发用。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _QUEUE_LOCK.acquire()
            try:
                if _EVENT_QUEUE.full():
                    _EVENT_QUEUE.get_nowait()  # 丢弃最旧
                _EVENT_QUEUE.put_nowait(record)
            finally:
                _QUEUE_LOCK.release()
        except Exception:
            pass  # 日志基建绝不允许让应用崩


def setup_logging(level: str | None = None, log_to_console: bool = True) -> None:
    """初始化根 logger (幂等)。

    Args:
        level: 覆盖日志级别 ("DEBUG"|"INFO"|"WARNING"); None 则读 VOXSUB_LOG 或默认 INFO。
        log_to_console: 是否同步到 stderr (开发便捷; 打包发布版可关)。
    """
    global _HANDLERS_INITIALIZED
    with _INIT_LOCK:
        if _HANDLERS_INITIALIZED:
            return
        root = logging.getLogger("voxsub")
        chosen = (level or os.environ.get("VOXSUB_LOG") or "INFO").upper()
        root.setLevel(getattr(logging, chosen, logging.INFO))
        root.propagate = False  # 不让事件泄漏到第三方库的根 logger

        log_path = _log_dir() / "voxsub.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_error: OSError | None = None
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_path), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"))
            root.addHandler(file_handler)
        except OSError as exc:
            # 日志文件被旧实例/外部工具锁住时，仍保留内存实时日志，不能让
            # import 阶段或整个 GUI 因“记录日志失败”而启动失败。
            file_error = exc

        if log_to_console:
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter(
                "%(levelname)-8s [%(name)s] %(message)s"))
            root.addHandler(console)

        ring = _RingBufferHandler()
        ring.setLevel(logging.DEBUG)
        root.addHandler(ring)

        _HANDLERS_INITIALIZED = True
        root.info("日志系统初始化: 级别=%s 文件=%s", chosen, log_path)
        if file_error is not None:
            root.warning("日志文件暂不可写，已切换为应用内实时日志: %s", file_error)


def get_logger(name: str) -> logging.Logger:
    """获取语幕 logger (在使用前 setup_logging 一次; 未见则兜底初始化)。"""
    setup_logging()
    return logging.getLogger(f"voxsub.{name}")


def set_debug_mode(enabled: bool) -> None:
    """运行时切换应用日志级别，供内置调试控制台使用。

    ``setup_logging`` 只负责一次性安装 handlers；调试模式必须允许用户在应用
    已运行时开关，因此这里直接调整 ``voxsub`` 根 logger 的级别。内存事件流和
    文件日志共用该级别，关闭后恢复 INFO。
    """
    setup_logging()
    root = logging.getLogger("voxsub")
    level = logging.DEBUG if enabled else logging.INFO
    root.setLevel(level)
    root.log(logging.INFO, "内置调试模式%s，日志级别=%s",
             "已开启" if enabled else "已关闭", logging.getLevelName(level))


def is_debug_mode() -> bool:
    """返回当前是否处于 DEBUG 日志级别。"""
    setup_logging()
    return logging.getLogger("voxsub").isEnabledFor(logging.DEBUG)


def drain_events(limit: int = 200) -> list[dict]:
    """读取最近日志事件 (诊断页/调试), 返回 [{ts, level, name, message}]。

    非破坏性拉取新事件 + 拦截: 用于 UI 实时日志页签或崩溃前 dump。
    """
    out: list[dict] = []
    with _QUEUE_LOCK:
        items = list(_EVENT_QUEUE.queue)
    for r in items[-limit:]:
        ts = (r.asctime if hasattr(r, "asctime")
              else logging.Formatter().formatTime(r, "%H:%M:%S"))
        out.append({
            "ts": ts,
            "level": r.levelname,
            "name": r.name,
            "message": r.getMessage(),
        })
    return out


def tail_log_file(lines: int = 200) -> str:
    """读日志文件末尾 (严重问题/崩溃后排查)。"""
    p = _log_dir() / "voxsub.log"
    if not p.exists():
        return ""
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError as exc:
        return f"<读取日志失败: {exc}>"
