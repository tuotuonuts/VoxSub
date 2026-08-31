"""Optional Sentry error reporting with a strict privacy boundary.

Sentry is deliberately a side-channel to the existing local logging system:
missing SDK/DSN, an invalid DSN, or a network failure must never affect the
application.  Events are limited to diagnostics metadata and exception
types; user content and credentials are filtered before transport.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from voxsub import __version__
from voxsub.config_store import ConfigStore
from voxsub.logging_setup import diagnostic_session_snapshot, get_logger

logger = get_logger("error_reporting")

_VALID_ENVIRONMENTS = frozenset({"development", "testing", "production"})
_SAFE_BUILD_RE = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\r\n,;|\"']+")
_UNIX_PRIVATE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])/(?:Users|home|private|var/folders|tmp|opt)/[^\r\n,;|\"']+",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret|dsn)"
    r"\s*[:=]\s*[^\s,;]+"
)
_CONTENT_RE = re.compile(
    r"(?i)\b(transcri(?:ption|pt)?|recognized?|recognition|subtitle|translation|"
    r"source[_-]?text|target[_-]?text|prompt|audio|pcm|wave|content|text)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_PATH_KEY_RE = re.compile(r"(?i)^(?:path|file|filename|directory|folder|cwd|home)$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|authorization|password|secret|dsn|cookie|"
    r"audio|pcm|wave|subtitle|transcri|recogn|translation|prompt|content|source[_-]?text|"
    r"target[_-]?text|filename|filepath|directory|folder|cwd)"
)

_ORIGINAL_SYS_EXCEPTHOOK = None
_ORIGINAL_THREADING_EXCEPTHOOK = None
_SENTRY_HANDLER: logging.Handler | None = None
_SDK: Any = None
_CONTEXT: "RuntimeContext | None" = None
_PENDING_HARDWARE_ERROR: BaseException | None = None
_INITIALIZED = False
_HOOK_GUARD = threading.local()
_STATE_LOCK = threading.RLock()
_DIAGNOSTIC_MAX_CHARS = 120_000
_SAFE_SESSION_ID_RE = re.compile(r"^[a-f0-9]{8,32}$", re.IGNORECASE)
_SAFE_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:T| )\d{2}:\d{2}:\d{2}(?:\+00:00)?$")
_LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
_LOG_UPLOAD_RETENTION = "latest_complete_lines"


@dataclass(frozen=True)
class RuntimeContext:
    """Stable, non-content metadata shared by logs and Sentry events."""

    version: str
    build_id: str
    environment: str
    release: str
    os_name: str
    os_version: str
    python_version: str
    cpu: str
    memory_gb: float | None
    gpu: str
    gpu_provider: str
    npu: str
    npu_provider: str
    npu_driver: str
    inference_backend: str


def _local_settings() -> Mapping[str, Any]:
    """Read optional telemetry settings without making startup fragile."""
    try:
        data = ConfigStore().load()
        return data if isinstance(data, Mapping) else {}
    except Exception:
        logger.debug("读取本地 Sentry 配置失败", exc_info=True)
        return {}


def _setting(name: str) -> str:
    value = _local_settings().get(name, "")
    return value.strip() if isinstance(value, str) else ""


def _environment() -> str:
    for raw in (
        os.environ.get("VOXSUB_ENVIRONMENT", ""),
        os.environ.get("VOXSUB_ENV", ""),
        _setting("sentry_environment"),
    ):
        value = raw.strip().casefold()
        if value in _VALID_ENVIRONMENTS:
            return value
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return "testing"
    return "production" if getattr(sys, "frozen", False) else "development"


def _build_id() -> str:
    raw = (os.environ.get("VOXSUB_BUILD") or
           os.environ.get("VOXSUB_BUILD_ID") or
           _setting("sentry_build") or "source").strip()
    safe = _SAFE_BUILD_RE.sub("-", raw).strip("-._")
    return (safe or "source")[:64]


def _memory_gb() -> float | None:
    try:
        import psutil

        total = float(psutil.virtual_memory().total) / (1024 ** 3)
        return round(total, 2) if total > 0 else None
    except Exception:
        return None


def _hardware_context() -> dict[str, str]:
    """Read the existing hardware profile without making startup fragile."""
    global _PENDING_HARDWARE_ERROR
    try:
        from voxsub.hardware import detect_hardware

        profile = detect_hardware()
        return {
            "cpu": profile.cpu_name,
            "gpu": profile.gpu_name,
            "gpu_provider": profile.gpu_provider,
            "npu": profile.npu_name,
            "npu_provider": profile.npu_provider,
            "npu_driver": profile.npu_driver_version,
            "inference_backend": ";".join(filter(None, (
                profile.gpu_provider,
                profile.npu_provider,
                profile.integrated_gpu_provider,
                "CPU",
            ))),
        }
    except Exception as exc:
        # Hardware probing is best effort.  The exception itself is retained in
        # the local log; no telemetry setup failure may block application start.
        logger.warning("错误报告硬件上下文收集失败", exc_info=True)
        _PENDING_HARDWARE_ERROR = exc
        return {
            "cpu": platform.processor() or platform.machine() or "unknown",
            "gpu": "",
            "gpu_provider": "",
            "npu": "",
            "npu_provider": "",
            "npu_driver": "",
            "inference_backend": "unknown",
        }


def runtime_context() -> RuntimeContext:
    """Return cached metadata, initializing it without enabling Sentry."""
    global _CONTEXT
    with _STATE_LOCK:
        if _CONTEXT is None:
            build_id = _build_id()
            environment = _environment()
            hardware = _hardware_context()
            _CONTEXT = RuntimeContext(
                version=__version__,
                build_id=build_id,
                environment=environment,
                release=f"voxsub@{__version__}+{build_id}",
                os_name=platform.system() or "unknown",
                os_version=platform.version() or platform.release() or "unknown",
                python_version=platform.python_version(),
                memory_gb=_memory_gb(),
                **hardware,
            )
        return _CONTEXT


def _sanitize_string_with_stats(value: str) -> tuple[str, int, bool]:
    """Sanitize one free-form value and report non-sensitive filter counts."""
    text = str(value)
    replacements = 0
    text, count = _WINDOWS_PATH_RE.subn("<private-path>", text)
    replacements += count
    text, count = _UNIX_PRIVATE_PATH_RE.subn("<private-path>", text)
    replacements += count
    text, count = _SECRET_RE.subn(
        lambda match: f"{match.group(1)}=<filtered>", text)
    replacements += count
    text, count = _CONTENT_RE.subn(
        lambda match: f"{match.group(1)}=<filtered>", text)
    replacements += count
    truncated = len(text) > 2000
    return text[:2000], replacements, truncated


def _sanitize_string(value: str) -> str:
    return _sanitize_string_with_stats(value)[0]


def _sanitize_value(value: Any, key: str = "") -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return "<filtered>"
    if isinstance(value, str):
        if _PATH_KEY_RE.match(key):
            return "<private-path>"
        return _sanitize_string(value)
    if isinstance(value, Mapping):
        return {str(k): _sanitize_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, key) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_string(repr(value))


def sanitize_event(event: Mapping[str, Any], hint: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a transport-safe copy suitable for Sentry's ``before_send``."""
    cleaned = _sanitize_value(dict(event))
    if not isinstance(cleaned, dict):  # pragma: no cover - defensive contract
        return {}
    # Sentry's request/user/server fields can contain hostnames, headers,
    # cookies, or query strings that are not needed for diagnosing VoxSub.
    for key in ("request", "user", "server_name"):
        cleaned.pop(key, None)
    exception = cleaned.get("exception")
    if isinstance(exception, dict):
        for item in exception.get("values", ()):
            if isinstance(item, dict) and "value" in item:
                # Exception messages can contain recognition text or user
                # filenames.  Keep the exception type for grouping, but never
                # transmit the free-form message itself.
                item["value"] = "<filtered exception message>"
    return cleaned


def _load_sdk() -> Any:
    try:
        import sentry_sdk

        return sentry_sdk
    except ImportError:
        return None


def _sentry_dsn() -> str:
    """Resolve DSN with process environment taking precedence over config."""
    return (os.environ.get("VOXSUB_SENTRY_DSN") or
            _setting("sentry_dsn")).strip()


def _context_payload(context: RuntimeContext) -> dict[str, Any]:
    payload = asdict(context)
    # Sentry tags must remain short and scalar; detailed values are attached as
    # a context while the same summary is emitted to the local log.
    return payload


def _set_sdk_context(sdk: Any, context: RuntimeContext) -> None:
    try:
        sdk.set_tag("voxsub_version", context.version)
        sdk.set_tag("voxsub_build", context.build_id)
        sdk.set_tag("voxsub_environment", context.environment)
        sdk.set_context("voxsub_runtime", _context_payload(context))
    except Exception:
        # Context enrichment is optional and must never interfere with startup.
        pass


def _area_for_logger(name: str) -> str:
    lowered = name.casefold()
    if any(token in lowered for token in ("model", "asr", "tts", "ocr", "translate")):
        return "model_or_inference"
    if any(token in lowered for token in ("hardware", "router")):
        return "device_detection"
    if "thread" in lowered or lowered.endswith("worker"):
        return "background_thread"
    return "runtime"


def _safe_session_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Allow only generated diagnostic-session fields into telemetry."""
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, Any] = {}
    session_id = value.get("session_id")
    if isinstance(session_id, str) and _SAFE_SESSION_ID_RE.fullmatch(session_id):
        safe["session_id"] = session_id
    for key in ("started_at", "expires_at", "first_log_at", "last_log_at"):
        timestamp = value.get(key)
        if isinstance(timestamp, str) and _SAFE_TIMESTAMP_RE.fullmatch(timestamp):
            safe[key] = timestamp
    for key in ("remaining_seconds", "line_count"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool):
            safe[key] = max(0, min(number, 2_000_000))
    return safe


def _set_session_scope(scope: Any, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Join Sentry events with the current local diagnostic session, if any."""
    safe = _safe_session_metadata(
        metadata if metadata is not None else diagnostic_session_snapshot())
    if not safe:
        return safe
    try:
        scope.set_tag("diagnostic_session", safe["session_id"])
        scope.set_context("voxsub_diagnostic_session", safe)
    except Exception:
        pass
    return safe


def _set_scope_context(scope: Any, key: str, value: Mapping[str, Any]) -> None:
    """Set context when supported by the SDK/fake scope in use."""
    setter = getattr(scope, "set_context", None)
    if callable(setter):
        try:
            setter(key, dict(value))
        except Exception:
            pass


def capture_exception(
    exc: BaseException,
    *,
    area: str = "runtime",
    logger_name: str = "",
) -> Any:
    """Capture one exception if Sentry is active; never raise to the caller."""
    sdk = _SDK
    if sdk is None or not isinstance(exc, BaseException):
        return None
    try:
        with sdk.push_scope() as scope:
            scope.set_tag("error_area", area)
            if logger_name:
                scope.set_tag("error_logger", logger_name)
            _set_session_scope(scope)
            return sdk.capture_exception(exc)
    except Exception:
        return None


def capture_message(message: str, *, level: str = "error", area: str = "runtime") -> Any:
    """Capture a category marker without exposing the original log message."""
    sdk = _SDK
    if sdk is None:
        return None
    try:
        with sdk.push_scope() as scope:
            scope.set_tag("error_area", area)
            _set_session_scope(scope)
            # The local log retains ``message`` for diagnosis.  Sentry only
            # needs a stable marker because arbitrary log text may contain
            # subtitle, recognition, audio or user-file content.
            del message
            return sdk.capture_message(f"VoxSub diagnostic event ({area})", level=level)
    except Exception:
        return None


def is_error_reporting_enabled() -> bool:
    """Return whether a usable Sentry client is active in this process."""
    return _SDK is not None


def _log_timestamp_range(lines: list[str]) -> tuple[str, str]:
    """Return first/last standard log timestamp without copying log content."""
    timestamps = [match.group(1) for line in lines
                  if (match := _LOG_TIMESTAMP_RE.match(line))]
    return (timestamps[0], timestamps[-1]) if timestamps else ("", "")


def _sanitize_diagnostic_text_with_stats(
    value: str,
) -> tuple[str, dict[str, int | str | bool]]:
    """Sanitize an attachment and retain newest complete lines within its cap.

    The cap is intentionally applied after privacy filtering. Selecting from
    the end makes a manual upload useful for the issue currently under
    investigation, while retaining whole lines keeps Sentry records readable.
    """
    rendered_lines: list[str] = []
    replacements = 0
    filtered_lines = 0
    redacted_lines = 0
    line_length_limited_lines = 0
    source_lines = str(value).splitlines()
    for line in source_lines:
        cleaned, count, line_truncated = _sanitize_string_with_stats(line)
        rendered_lines.append(cleaned)
        replacements += count
        if count or line_truncated:
            filtered_lines += 1
        if count:
            redacted_lines += 1
        if line_truncated:
            line_length_limited_lines += 1

    # Work backwards so an oversized attachment retains the newest contiguous
    # records. The delimiter is counted to ensure the cap never creates a
    # partial first line.
    retained_reversed: list[str] = []
    rendered_length = 0
    for line in reversed(rendered_lines):
        line_length = len(line) + (1 if retained_reversed else 0)
        if rendered_length + line_length > _DIAGNOSTIC_MAX_CHARS:
            break
        retained_reversed.append(line)
        rendered_length += line_length
    retained_lines = list(reversed(retained_reversed))
    omitted_lines = len(rendered_lines) - len(retained_lines)
    rendered = "\n".join(retained_lines)
    source_first, source_last = _log_timestamp_range(source_lines)
    uploaded_first, uploaded_last = _log_timestamp_range(retained_lines)
    truncation_reason = ""
    if omitted_lines and line_length_limited_lines:
        truncation_reason = "attachment_size_and_line_length_limits"
    elif omitted_lines:
        truncation_reason = "attachment_size_limit"
    elif line_length_limited_lines:
        truncation_reason = "line_length_limit"
    return rendered, {
        "source_lines": len(source_lines),
        "uploaded_lines": len(retained_lines),
        "filtered_lines": filtered_lines,
        "privacy_redacted_lines": redacted_lines,
        # Lines are redacted in place rather than silently removed. Keep this
        # explicit so an upload can account for every source log line.
        "privacy_removed_lines": 0,
        "line_length_limited_lines": line_length_limited_lines,
        "privacy_filter_replacements": replacements,
        "omitted_lines": omitted_lines,
        "attachment_char_limit": _DIAGNOSTIC_MAX_CHARS,
        "retention": _LOG_UPLOAD_RETENTION,
        "source_first_log_at": source_first,
        "source_last_log_at": source_last,
        "uploaded_first_log_at": uploaded_first,
        "uploaded_last_log_at": uploaded_last,
        "truncation_reason": truncation_reason,
        "truncated": bool(omitted_lines or line_length_limited_lines),
    }


def _sanitize_diagnostic_text(value: str) -> str:
    """Backward-compatible text-only diagnostic sanitizer."""
    return _sanitize_diagnostic_text_with_stats(value)[0]


def _upload_metadata(
    session_metadata: Mapping[str, Any] | None,
    log_stats: Mapping[str, int | str | bool],
) -> dict[str, Any]:
    """Build a compact, non-content Sentry context for a diagnostic upload."""
    metadata = _safe_session_metadata(session_metadata)
    metadata.update({
        "source_log_lines": int(log_stats.get("source_lines", 0)),
        "uploaded_log_lines": int(log_stats.get("uploaded_lines", 0)),
        "omitted_log_lines": int(log_stats.get("omitted_lines", 0)),
        "privacy_filtered_lines": int(log_stats.get("filtered_lines", 0)),
        "privacy_redacted_lines": int(log_stats.get("privacy_redacted_lines", 0)),
        "privacy_removed_lines": int(log_stats.get("privacy_removed_lines", 0)),
        "line_length_limited_lines": int(
            log_stats.get("line_length_limited_lines", 0)),
        "privacy_filter_replacements": int(
            log_stats.get("privacy_filter_replacements", 0)),
        "attachment_char_limit": int(log_stats.get("attachment_char_limit", 0)),
        "log_retention": str(log_stats.get("retention", "")),
        "source_first_log_at": str(log_stats.get("source_first_log_at", "")),
        "source_last_log_at": str(log_stats.get("source_last_log_at", "")),
        "uploaded_first_log_at": str(log_stats.get("uploaded_first_log_at", "")),
        "uploaded_last_log_at": str(log_stats.get("uploaded_last_log_at", "")),
        "log_truncation_reason": str(log_stats.get("truncation_reason", "")),
        "log_truncated": bool(log_stats.get("truncated", False)),
    })
    return metadata


def preview_log_snapshot_metadata(
    logs: str,
    *,
    session_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the safe metadata that a manual log upload will include.

    The UI can therefore tell the user exactly which range was retained
    without exposing raw or filtered log content outside this module.
    """
    _clean_logs, log_stats = _sanitize_diagnostic_text_with_stats(logs)
    return _upload_metadata(session_metadata, log_stats)


def _metadata_header(metadata: Mapping[str, Any]) -> str:
    """Make an attachment self-describing without adding user content."""
    if not metadata:
        return ""
    ordered = (
        "session_id", "started_at", "expires_at", "first_log_at", "last_log_at",
        "line_count", "source_log_lines", "uploaded_log_lines", "omitted_log_lines",
        "source_first_log_at", "source_last_log_at", "uploaded_first_log_at",
        "uploaded_last_log_at", "privacy_filtered_lines", "privacy_redacted_lines",
        "privacy_removed_lines", "line_length_limited_lines",
        "privacy_filter_replacements", "attachment_char_limit", "log_retention",
        "log_truncation_reason", "log_truncated",
    )
    rows = [f"# {key}: {metadata[key]}" for key in ordered if key in metadata]
    return "# VoxSub diagnostic upload\n" + "\n".join(rows) + "\n\n"


def send_diagnostic_report(
    report: str,
    logs: str,
    *,
    trigger: str = "manual",
    session_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Upload a filtered self-check report and local log snapshot.

    This is intentionally explicit and best-effort.  Attachments are preferred
    so the full bounded snapshots remain available without putting user text in
    event fields; a context fallback keeps compatibility with small SDK fakes.
    """
    sdk = _SDK
    if sdk is None:
        return False
    clean_report, report_stats = _sanitize_diagnostic_text_with_stats(report)
    clean_logs, log_stats = _sanitize_diagnostic_text_with_stats(logs)
    upload_metadata = _upload_metadata(session_metadata, log_stats)
    upload_metadata.update({
        "uploaded_report_lines": int(report_stats["uploaded_lines"]),
        "report_privacy_filter_replacements": int(
            report_stats["privacy_filter_replacements"]),
    })
    log_attachment = _metadata_header(upload_metadata) + clean_logs
    try:
        with sdk.push_scope() as scope:
            scope.set_tag("error_area", "diagnostics")
            scope.set_tag("diagnostic_trigger", _SAFE_BUILD_RE.sub("-", str(trigger))[:32])
            _set_session_scope(scope, upload_metadata)
            _set_scope_context(scope, "voxsub_diagnostic_upload", upload_metadata)
            add_attachment = getattr(scope, "add_attachment", None)
            if callable(add_attachment):
                add_attachment(
                    bytes=clean_report.encode("utf-8"),
                    filename="voxsub-self-check.txt",
                    content_type="text/plain",
                )
                add_attachment(
                    bytes=log_attachment.encode("utf-8"),
                    filename="voxsub-log.txt",
                    content_type="text/plain",
                )
            else:
                _set_scope_context(scope, "voxsub_diagnostics", {
                    "report": clean_report,
                    "logs": log_attachment,
                })
            return bool(sdk.capture_message("VoxSub diagnostic report", level="info"))
    except Exception:
        logger.warning("Sentry 诊断报告上传失败，保留本地日志", exc_info=True)
        return False


def send_log_snapshot(
    logs: str,
    *,
    trigger: str = "manual_log_upload",
    session_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Upload one filtered local-log snapshot without requiring a self-check.

    This is an explicit user action from the Diagnostics log tab.  The local
    logging path remains authoritative: missing Sentry configuration or a
    transport failure returns ``False`` and never affects the running app.
    """
    sdk = _SDK
    if sdk is None:
        return False
    clean_logs, log_stats = _sanitize_diagnostic_text_with_stats(logs)
    upload_metadata = _upload_metadata(session_metadata, log_stats)
    log_attachment = _metadata_header(upload_metadata) + clean_logs
    try:
        with sdk.push_scope() as scope:
            scope.set_tag("error_area", "diagnostics")
            scope.set_tag("diagnostic_trigger", _SAFE_BUILD_RE.sub(
                "-", str(trigger))[:32])
            _set_session_scope(scope, upload_metadata)
            _set_scope_context(scope, "voxsub_diagnostic_upload", upload_metadata)
            add_attachment = getattr(scope, "add_attachment", None)
            if callable(add_attachment):
                add_attachment(
                    bytes=log_attachment.encode("utf-8"),
                    filename="voxsub-log.txt",
                    content_type="text/plain",
                )
            else:
                _set_scope_context(scope, "voxsub_diagnostics", {"logs": log_attachment})
            return bool(sdk.capture_message(
                "VoxSub diagnostic log upload", level="info"))
    except Exception:
        logger.warning("Sentry 日志上传失败，保留本地日志", exc_info=True)
        return False


class SentryLogHandler(logging.Handler):
    """Forward only exceptional log records to Sentry."""

    def emit(self, record: logging.LogRecord) -> None:
        if _SDK is None or record.name.startswith("voxsub.error_reporting"):
            return
        # Warning records without a traceback are usually expected fallbacks;
        # exception-bearing warnings still represent model/device failures.
        if record.levelno < logging.ERROR and not record.exc_info:
            return
        try:
            if record.exc_info and record.exc_info[1] is not None:
                capture_exception(
                    record.exc_info[1], area=_area_for_logger(record.name),
                    logger_name=record.name,
                )
            else:
                capture_message(
                    record.getMessage(), area=_area_for_logger(record.name),
                )
        except Exception:
            # A telemetry transport must never break logging or the application.
            return


def _install_exception_hooks() -> None:
    global _ORIGINAL_SYS_EXCEPTHOOK, _ORIGINAL_THREADING_EXCEPTHOOK
    global _PENDING_HARDWARE_ERROR
    if _ORIGINAL_SYS_EXCEPTHOOK is None:
        _ORIGINAL_SYS_EXCEPTHOOK = sys.excepthook

        def _sys_hook(exc_type, exc_value, exc_traceback) -> None:
            if not getattr(_HOOK_GUARD, "active", False):
                _HOOK_GUARD.active = True
                try:
                    if isinstance(exc_value, BaseException):
                        capture_exception(exc_value, area="unhandled_exception")
                finally:
                    _HOOK_GUARD.active = False
            _ORIGINAL_SYS_EXCEPTHOOK(exc_type, exc_value, exc_traceback)

        sys.excepthook = _sys_hook

    if hasattr(threading, "excepthook") and _ORIGINAL_THREADING_EXCEPTHOOK is None:
        _ORIGINAL_THREADING_EXCEPTHOOK = threading.excepthook

        def _thread_hook(args) -> None:
            thread = getattr(args, "thread", None)
            name = getattr(thread, "name", "unknown")
            exc = getattr(args, "exc_value", None)
            if isinstance(exc, BaseException):
                capture_exception(exc, area="background_thread",
                                  logger_name=f"thread:{name}")
            _ORIGINAL_THREADING_EXCEPTHOOK(args)

        threading.excepthook = _thread_hook


def initialize_error_reporting() -> RuntimeContext:
    """Initialize optional Sentry and return the shared runtime context."""
    global _INITIALIZED, _SDK, _SENTRY_HANDLER, _PENDING_HARDWARE_ERROR
    with _STATE_LOCK:
        context = runtime_context()
        if _INITIALIZED:
            return context
        _INITIALIZED = True
        dsn = _sentry_dsn()
        sdk = _load_sdk() if dsn else None
        if sdk is None:
            if dsn:
                logger.warning("Sentry SDK 未安装，继续使用本地日志")
            else:
                logger.info("Sentry 未配置 DSN，错误仅写入本地日志")
            return context
        try:
            sdk.init(
                dsn=dsn,
                environment=context.environment,
                release=context.release,
                send_default_pii=False,
                include_local_variables=False,
                before_send=sanitize_event,
                default_integrations=False,
            )
            _SDK = sdk
            _set_sdk_context(sdk, context)
            _SENTRY_HANDLER = SentryLogHandler(level=logging.WARNING)
            logging.getLogger("voxsub").addHandler(_SENTRY_HANDLER)
            _install_exception_hooks()
            if _PENDING_HARDWARE_ERROR is not None:
                capture_exception(_PENDING_HARDWARE_ERROR, area="device_detection")
                _PENDING_HARDWARE_ERROR = None
            logger.info(
                "Sentry 已启用: version=%s build=%s environment=%s release=%s",
                context.version, context.build_id, context.environment, context.release,
            )
        except Exception:
            _SDK = None
            logger.warning("Sentry 初始化失败，继续使用本地日志", exc_info=True)
        return context


def shutdown_error_reporting(timeout: float = 2.0) -> None:
    """Best-effort flush during normal application shutdown."""
    sdk = _SDK
    if sdk is None:
        return
    try:
        sdk.flush(timeout=max(0.0, float(timeout)))
    except Exception:
        pass


def reload_error_reporting() -> RuntimeContext:
    """Reload Sentry settings after the user edits local configuration."""
    global _INITIALIZED, _SDK, _SENTRY_HANDLER, _CONTEXT
    global _ORIGINAL_SYS_EXCEPTHOOK, _ORIGINAL_THREADING_EXCEPTHOOK
    with _STATE_LOCK:
        sdk = _SDK
        if sdk is not None:
            try:
                sdk.flush(timeout=2.0)
            except Exception:
                pass
        if _SENTRY_HANDLER is not None:
            logging.getLogger("voxsub").removeHandler(_SENTRY_HANDLER)
        if _ORIGINAL_SYS_EXCEPTHOOK is not None:
            sys.excepthook = _ORIGINAL_SYS_EXCEPTHOOK
        if (_ORIGINAL_THREADING_EXCEPTHOOK is not None and
                hasattr(threading, "excepthook")):
            threading.excepthook = _ORIGINAL_THREADING_EXCEPTHOOK
        _INITIALIZED = False
        _SDK = None
        _SENTRY_HANDLER = None
        _CONTEXT = None
        _PENDING_HARDWARE_ERROR = None
        _ORIGINAL_SYS_EXCEPTHOOK = None
        _ORIGINAL_THREADING_EXCEPTHOOK = None
        return initialize_error_reporting()


def _reset_for_tests() -> None:
    """Reset process-global state for isolated unit tests."""
    global _INITIALIZED, _SDK, _SENTRY_HANDLER, _CONTEXT, _PENDING_HARDWARE_ERROR
    global _ORIGINAL_SYS_EXCEPTHOOK, _ORIGINAL_THREADING_EXCEPTHOOK
    with _STATE_LOCK:
        if _SENTRY_HANDLER is not None:
            logging.getLogger("voxsub").removeHandler(_SENTRY_HANDLER)
        if _ORIGINAL_SYS_EXCEPTHOOK is not None:
            sys.excepthook = _ORIGINAL_SYS_EXCEPTHOOK
        if (_ORIGINAL_THREADING_EXCEPTHOOK is not None and
                hasattr(threading, "excepthook")):
            threading.excepthook = _ORIGINAL_THREADING_EXCEPTHOOK
        _INITIALIZED = False
        _SDK = None
        _SENTRY_HANDLER = None
        _CONTEXT = None
        _PENDING_HARDWARE_ERROR = None
        _ORIGINAL_SYS_EXCEPTHOOK = None
        _ORIGINAL_THREADING_EXCEPTHOOK = None


__all__ = [
    "RuntimeContext",
    "SentryLogHandler",
    "capture_exception",
    "capture_message",
    "is_error_reporting_enabled",
    "initialize_error_reporting",
    "preview_log_snapshot_metadata",
    "reload_error_reporting",
    "runtime_context",
    "sanitize_event",
    "send_diagnostic_report",
    "send_log_snapshot",
    "shutdown_error_reporting",
]
