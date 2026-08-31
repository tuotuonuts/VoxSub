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
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _log_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "logs"


#: 内存环形日志事件缓冲 (诊断页/调试读取); 上限 2000 条
_EVENT_QUEUE: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=2000)
_QUEUE_LOCK = threading.Lock()
_HANDLERS_INITIALIZED = False
_INIT_LOCK = threading.Lock()
_DIAGNOSTIC_SESSION_LOCK = threading.RLock()
_DIAGNOSTIC_SESSION: "DiagnosticSession | None" = None
_DIAGNOSTIC_SESSION_DEFAULT_SECONDS = 20 * 60


@dataclass(frozen=True)
class DiagnosticSession:
    """A bounded verbose-logging interval created by the diagnostics UI."""

    session_id: str
    started_at: datetime
    expires_at: datetime
    previous_level: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _expire_diagnostic_session_locked(now: datetime) -> DiagnosticSession | None:
    """Return the active session, restoring the prior level once it expires.

    This intentionally does not log: it is also called from logging filters and
    must never cause a recursive logging call.
    """
    global _DIAGNOSTIC_SESSION
    session = _DIAGNOSTIC_SESSION
    if session is None or now < session.expires_at:
        return session
    logging.getLogger("voxsub").setLevel(session.previous_level)
    _DIAGNOSTIC_SESSION = None
    return None


def _active_diagnostic_session(*, announce_expiry: bool = True) -> DiagnosticSession | None:
    expired = False
    with _DIAGNOSTIC_SESSION_LOCK:
        before = _DIAGNOSTIC_SESSION
        session = _expire_diagnostic_session_locked(_utc_now())
        expired = before is not None and session is None
    if expired and announce_expiry:
        root = logging.getLogger("voxsub")
        root.info("诊断调试会话已自动结束，已恢复日志级别=%s",
                  logging.getLevelName(root.level))
    return session


class _DiagnosticSessionFilter(logging.Filter):
    """Annotate every local record with its bounded diagnostic-session ID."""

    def filter(self, record: logging.LogRecord) -> bool:
        session = _active_diagnostic_session(announce_expiry=False)
        record.diagnostic_session_id = session.session_id if session else "-"
        return True


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
                "%(asctime)s %(levelname)-8s [%(name)s] [session=%(diagnostic_session_id)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"))
            file_handler.addFilter(_DiagnosticSessionFilter())
            root.addHandler(file_handler)
        except OSError as exc:
            # 日志文件被旧实例/外部工具锁住时，仍保留内存实时日志，不能让
            # import 阶段或整个 GUI 因“记录日志失败”而启动失败。
            file_error = exc

        if log_to_console:
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter(
                "%(levelname)-8s [%(name)s] [session=%(diagnostic_session_id)s] %(message)s"))
            console.addFilter(_DiagnosticSessionFilter())
            root.addHandler(console)

        ring = _RingBufferHandler()
        ring.setLevel(logging.DEBUG)
        ring.addFilter(_DiagnosticSessionFilter())
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
    """Compatibility wrapper for the former persistent debug-mode control.

    User-facing callers now receive a bounded diagnostic session instead of an
    easy-to-forget application-wide DEBUG setting.  The environment variable
    ``VOXSUB_LOG=DEBUG`` remains available for intentional developer runs.
    """
    if enabled:
        start_diagnostic_session()
    else:
        stop_diagnostic_session()


def is_debug_mode() -> bool:
    """返回当前是否处于 DEBUG 日志级别。"""
    setup_logging()
    _active_diagnostic_session()
    return logging.getLogger("voxsub").isEnabledFor(logging.DEBUG)


def start_diagnostic_session(
    duration_seconds: int = _DIAGNOSTIC_SESSION_DEFAULT_SECONDS,
) -> dict[str, Any]:
    """Enable verbose local logging for a bounded, privacy-safe interval.

    Starting a new session replaces a prior active session and preserves the
    logger level that existed before the first session.  No user content is
    kept in the session metadata; the random ID only joins local logs, Sentry
    events, and explicit diagnostic uploads.
    """
    global _DIAGNOSTIC_SESSION
    setup_logging()
    try:
        requested = int(duration_seconds)
    except (TypeError, ValueError):
        requested = _DIAGNOSTIC_SESSION_DEFAULT_SECONDS
    duration = max(60, min(requested, 2 * 60 * 60))
    now = _utc_now()
    root = logging.getLogger("voxsub")
    with _DIAGNOSTIC_SESSION_LOCK:
        previous = _expire_diagnostic_session_locked(now)
        previous_level = previous.previous_level if previous else root.level
        _DIAGNOSTIC_SESSION = DiagnosticSession(
            session_id=uuid.uuid4().hex[:12],
            started_at=now,
            expires_at=now + timedelta(seconds=duration),
            previous_level=previous_level,
        )
        root.setLevel(logging.DEBUG)
    metadata = diagnostic_session_snapshot()
    root.info("诊断调试会话已开启: id=%s duration_sec=%s",
              metadata["session_id"] if metadata else "-", duration)
    return metadata or {}


def stop_diagnostic_session() -> bool:
    """Stop the active diagnostic session and restore the previous log level."""
    global _DIAGNOSTIC_SESSION
    setup_logging()
    with _DIAGNOSTIC_SESSION_LOCK:
        session = _expire_diagnostic_session_locked(_utc_now())
        if session is None:
            return False
        _DIAGNOSTIC_SESSION = None
        root = logging.getLogger("voxsub")
        root.setLevel(session.previous_level)
    root.info("诊断调试会话已结束: id=%s，已恢复日志级别=%s",
              session.session_id, logging.getLevelName(root.level))
    return True


def diagnostic_session_snapshot() -> dict[str, Any] | None:
    """Return safe metadata for the currently active diagnostic session."""
    session = _active_diagnostic_session()
    if session is None:
        return None
    remaining = max(0, int((session.expires_at - _utc_now()).total_seconds()))
    return {
        "session_id": session.session_id,
        "started_at": _format_timestamp(session.started_at),
        "expires_at": _format_timestamp(session.expires_at),
        "remaining_seconds": remaining,
    }


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
            "session_id": getattr(r, "diagnostic_session_id", "-"),
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


def _same_file_path(left: str | Path, right: Path) -> bool:
    """Compare file paths without requiring either path to exist."""
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right)))


def _truncate_active_log_file(path: Path) -> tuple[int, int]:
    """Empty the live log in place so its active handler remains usable."""
    root = logging.getLogger("voxsub")
    matched_handler = False
    for handler in root.handlers:
        if not isinstance(handler, logging.FileHandler):
            continue
        filename = getattr(handler, "baseFilename", "")
        if not filename or not _same_file_path(filename, path):
            continue
        matched_handler = True
        try:
            handler.acquire()
            stream = handler.stream
            if stream is None:
                return 0, 1
            stream.flush()
            stream.seek(0)
            stream.truncate()
            stream.seek(0, 2)
            return 1, 0
        except OSError:
            return 0, 1
        finally:
            handler.release()
    if not matched_handler and path.is_file():
        try:
            path.write_text("", encoding="utf-8")
            return 1, 0
        except OSError:
            return 0, 1
    return 0, 0


def clear_local_logs() -> dict[str, int]:
    """Clear VoxSub's on-disk logs and the in-memory log view buffer.

    Model files, configuration, user data, Sentry credentials, downloaded
    tools, and exported reports are intentionally outside this operation. The
    current log is truncated in place so a running application can continue to
    write it; historical rotations and source-test ``.log`` files are removed.
    """
    setup_logging()
    log_dir = _log_dir()
    cleared, failed = _truncate_active_log_file(log_dir / "voxsub.log")

    candidates: list[Path] = []
    try:
        candidates.extend(path for path in log_dir.glob("voxsub.log.*") if path.is_file())
    except OSError:
        failed += 1

    diagnostics_dir = log_dir.parent / "diagnostics"
    try:
        candidates.extend(path for path in diagnostics_dir.rglob("*.log") if path.is_file())
    except OSError:
        failed += 1

    for path in candidates:
        try:
            path.unlink()
            cleared += 1
        except OSError:
            failed += 1

    # The diagnostics page is backed by this queue as well as the log file.
    # Drop the previous session's in-memory entries so cleared logs do not
    # immediately reappear in the view.
    with _QUEUE_LOCK:
        while True:
            try:
                _EVENT_QUEUE.get_nowait()
            except queue.Empty:
                break
    return {"cleared_files": cleared, "failed_files": failed}


def diagnostic_session_log_snapshot() -> tuple[str, dict[str, Any] | None]:
    """Return only the active session's on-disk logs plus bounded metadata.

    The current rotating file is intentionally used instead of a second debug
    file.  This keeps local diagnostics simple while preventing manual Sentry
    uploads from sweeping unrelated historical activity when a session exists.
    """
    metadata = diagnostic_session_snapshot()
    if metadata is None:
        return "", None
    marker = f"[session={metadata['session_id']}]"
    lines = [line for line in tail_log_file(10**6).splitlines() if marker in line]
    timestamps = [line[:19] for line in lines if len(line) >= 19 and line[4:5] == "-"]
    metadata = dict(metadata)
    metadata.update({
        "line_count": len(lines),
        "first_log_at": timestamps[0] if timestamps else "",
        "last_log_at": timestamps[-1] if timestamps else "",
    })
    return ("\n".join(lines) + ("\n" if lines else ""), metadata)


__all__ = [
    "DiagnosticSession",
    "clear_local_logs",
    "diagnostic_session_log_snapshot",
    "diagnostic_session_snapshot",
    "drain_events",
    "get_logger",
    "is_debug_mode",
    "set_debug_mode",
    "setup_logging",
    "start_diagnostic_session",
    "stop_diagnostic_session",
    "tail_log_file",
]
