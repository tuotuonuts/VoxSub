"""Bounded model/IPC metadata. Never store audio, transcripts, prompts or credentials."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from functools import wraps
import json
import contextvars
import threading
import time
import uuid
from typing import Any

RUN_ID = uuid.uuid4().hex[:12]
_PIPELINE_SESSION = contextvars.ContextVar("pipeline_session", default=None)
_REQUEST_ID = contextvars.ContextVar("model_request", default=None)
_LOCK = threading.RLock()
_EVENTS: deque[dict[str, Any]] = deque(maxlen=500)
_ALLOWED = frozenset({"stage", "model", "runtime", "provider", "source", "target", "generation", "duration_ms", "queue_wait_ms", "input_chars", "output_chars", "audio_ms", "error_type", "error_code", "outcome", "command", "fallback", "suspected_language", "request_id", "pipeline_session_id", "sentence_id"})


def record(stage: str, outcome: str, **metadata: Any) -> dict[str, Any]:
    """Accept scalar allowlisted metadata only; errors use type/code, not raw text."""
    event = {"at": datetime.now(timezone.utc).isoformat(), "run_id": RUN_ID,
             "stage": stage, "outcome": outcome}
    if _PIPELINE_SESSION.get():
        event["pipeline_session_id"] = _PIPELINE_SESSION.get()
    if _REQUEST_ID.get():
        event["request_id"] = _REQUEST_ID.get()
    event.update({key: value for key, value in metadata.items()
                  if key in _ALLOWED and isinstance(value, (str, int, float, bool, type(None)))})
    with _LOCK:
        _EVENTS.append(event)
    from voxsub.logging_setup import get_logger
    get_logger("diagnostic_trace").info("MODEL_TRACE %s", json.dumps(event, ensure_ascii=True))
    return event


def snapshot() -> dict[str, Any]:
    with _LOCK:
        events = [dict(event) for event in _EVENTS]
    return {"run_id": RUN_ID, "events": events,
            "failures": sum(e["outcome"] in {"failed", "timeout", "empty", "language_mismatch"} for e in events)}


def traced_command(fn):
    @wraps(fn)
    def wrapped(self, command, args):
        started = time.perf_counter()
        request_id = uuid.uuid4().hex[:12]
        try:
            result = fn(self, command, args)
        except Exception as exc:
            record("ipc", "timeout" if isinstance(exc, TimeoutError) else "failed",
                   command=str(command)[:80], error_type=type(exc).__name__, request_id=request_id,
                   duration_ms=round((time.perf_counter() - started) * 1000, 2))
            raise
        record("ipc", "ok", command=str(command)[:80], request_id=request_id,
               duration_ms=round((time.perf_counter() - started) * 1000, 2))
        return result
    return wrapped


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    code = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
        return str(code)
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int) and not isinstance(errno, bool):
        return "errno:" + str(errno)
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        response = getattr(cause, "response", None)
        code = getattr(cause, "status_code", None) or getattr(response, "status_code", None)
        if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
            return str(code)
    return ""


def error(stage: str, exc: Exception, **metadata: Any) -> None:
    record(stage, "timeout" if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower() else "failed",
           error_type=type(exc).__name__, error_code=_error_code(exc), **metadata)


def model_result(stage: str, text: str | None, *, expected: str, **metadata: Any) -> None:
    from voxsub.language_guard import text_matches_language
    outcome = "ok" if text else "empty"
    if text and expected != "auto" and not text_matches_language(text, expected):
        # Source script uncertainty is retained, not a failed or discarded request.
        outcome = ("language_uncertain" if stage in {"recognition", "file_recognition"}
                   else "language_mismatch")
        if outcome == "language_uncertain":
            metadata["fallback"] = "source_retained"
    record(stage, outcome, output_chars=len(text or ""), **metadata)


def traced_stage(stage: str):
    """Correlate metadata emitted inside one model call, without capturing args/results."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            session_token = _PIPELINE_SESSION.set(getattr(args[0], "_diagnostic_session_id", None) if args else None)
            token = _REQUEST_ID.set(uuid.uuid4().hex[:12])
            started = time.perf_counter()
            record(stage, "started")
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                error(stage, exc, duration_ms=round((time.perf_counter() - started) * 1000, 2))
                raise
            else:
                record(stage, "completed", duration_ms=round((time.perf_counter() - started) * 1000, 2))
                return result
            finally:
                _REQUEST_ID.reset(token)
                _PIPELINE_SESSION.reset(session_token)
        return wrapped
    return decorate
