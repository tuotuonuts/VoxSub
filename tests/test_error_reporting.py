"""Optional Sentry integration and privacy-boundary tests."""
from __future__ import annotations

import logging
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import voxsub.error_reporting as error_reporting


class _Scope:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.attachments: list[dict[str, object]] = []

    def set_tag(self, key: str, value: str) -> None:
        self.tags[key] = value

    def add_attachment(self, **kwargs) -> None:
        self.attachments.append(kwargs)


class _FakeSentry:
    def __init__(self, *, init_error: Exception | None = None,
                 capture_error: Exception | None = None) -> None:
        self.init_error = init_error
        self.capture_error = capture_error
        self.init_kwargs: dict[str, object] = {}
        self.exceptions: list[BaseException] = []
        self.messages: list[tuple[str, str]] = []
        self.tags: dict[str, str] = {}
        self.contexts: dict[str, dict] = {}
        self.flush_calls: list[float] = []
        self.scopes: list[_Scope] = []

    def init(self, **kwargs) -> None:
        if self.init_error:
            raise self.init_error
        self.init_kwargs = kwargs

    def set_tag(self, key: str, value: str) -> None:
        self.tags[key] = value

    def set_context(self, key: str, value: dict) -> None:
        self.contexts[key] = value

    @contextmanager
    def push_scope(self):
        scope = _Scope()
        self.scopes.append(scope)
        yield scope

    def capture_exception(self, exc: BaseException) -> str:
        if self.capture_error:
            raise self.capture_error
        self.exceptions.append(exc)
        return "event-id"

    def capture_message(self, message: str, level: str = "error") -> str:
        if self.capture_error:
            raise self.capture_error
        self.messages.append((message, level))
        return "message-id"

    def flush(self, timeout: float) -> None:
        self.flush_calls.append(timeout)


@pytest.fixture(autouse=True)
def _reset_reporting(monkeypatch):
    error_reporting._reset_for_tests()  # noqa: SLF001
    monkeypatch.delenv("VOXSUB_SENTRY_DSN", raising=False)
    monkeypatch.delenv("VOXSUB_ENVIRONMENT", raising=False)
    monkeypatch.delenv("VOXSUB_ENV", raising=False)
    monkeypatch.delenv("VOXSUB_BUILD", raising=False)
    monkeypatch.delenv("VOXSUB_BUILD_ID", raising=False)
    monkeypatch.setattr(error_reporting, "_hardware_context", lambda: {
        "cpu": "test-cpu", "gpu": "", "gpu_provider": "",
        "npu": "", "npu_provider": "", "npu_driver": "",
        "inference_backend": "CPU",
    })
    monkeypatch.setattr(error_reporting, "_memory_gb", lambda: 16.0)
    yield
    error_reporting._reset_for_tests()  # noqa: SLF001


def test_sanitize_event_removes_credentials_content_paths_and_request() -> None:
    clean = error_reporting.sanitize_event({
        "message": (
            "api_key=sk-test text='机密字幕' "
            "path=C:\\Users\\Alice\\secret\\clip.wav"
        ),
        "request": {"headers": {"Authorization": "Bearer secret"}},
        "user": {"id": "alice"},
        "server_name": "private-host",
        "extra": {
            "audio": "raw pcm",
            "filename": "C:\\Users\\Alice\\clip.wav",
            "safe_count": 3,
        },
    })

    rendered = repr(clean)
    assert "sk-test" not in rendered
    assert "机密字幕" not in rendered
    assert "C:\\Users\\Alice" not in rendered
    assert "Authorization" not in rendered
    assert "request" not in clean
    assert "user" not in clean
    assert "server_name" not in clean
    assert clean["extra"]["audio"] == "<filtered>"
    assert clean["extra"]["safe_count"] == 3

def test_sanitize_event_removes_free_form_exception_text() -> None:
    clean = error_reporting.sanitize_event({
        "exception": {"values": [{
            "type": "RuntimeError",
            "value": "识别文本和用户文件.wav",
        }]},
    })
    value = clean["exception"]["values"][0]["value"]
    assert value == "<filtered exception message>"
    assert "用户文件" not in repr(clean)


def test_no_dsn_keeps_local_logging_and_does_not_import_sdk(monkeypatch) -> None:
    monkeypatch.setattr(error_reporting, "_load_sdk",
                        lambda: pytest.fail("SDK should not load without DSN"))
    context = error_reporting.initialize_error_reporting()

    assert context.environment == "testing"
    assert error_reporting._SDK is None  # noqa: SLF001
    assert error_reporting._SENTRY_HANDLER is None  # noqa: SLF001
    assert logging.getLogger("voxsub").handlers


def test_local_config_provides_dsn_environment_and_build(monkeypatch) -> None:
    fake = _FakeSentry()
    monkeypatch.setattr(error_reporting, "_local_settings", lambda: {
        "sentry_dsn": "https://public@example.invalid/1",
        "sentry_environment": "testing",
        "sentry_build": "desktop-7",
    })
    monkeypatch.setattr(error_reporting, "_load_sdk", lambda: fake)

    context = error_reporting.initialize_error_reporting()

    assert context.environment == "testing"
    assert context.build_id == "desktop-7"
    assert fake.init_kwargs["dsn"] == "https://public@example.invalid/1"


def test_reload_error_reporting_applies_config_without_leaking_hooks(monkeypatch) -> None:
    fake = _FakeSentry()
    monkeypatch.setenv("VOXSUB_SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setattr(error_reporting, "_load_sdk", lambda: fake)
    error_reporting.initialize_error_reporting()
    assert error_reporting.is_error_reporting_enabled()

    monkeypatch.delenv("VOXSUB_SENTRY_DSN", raising=False)
    context = error_reporting.reload_error_reporting()

    assert context.environment == "testing"
    assert not error_reporting.is_error_reporting_enabled()
    assert error_reporting._SENTRY_HANDLER is None  # noqa: SLF001


def test_environment_overrides_invalid_local_environment(monkeypatch) -> None:
    monkeypatch.setenv("VOXSUB_ENVIRONMENT", "production")
    monkeypatch.setattr(error_reporting, "_local_settings", lambda: {
        "sentry_environment": "invalid",
    })
    assert error_reporting._environment() == "production"  # noqa: SLF001


def test_sdk_init_failure_falls_back_to_local_logging(monkeypatch) -> None:
    fake = _FakeSentry(init_error=RuntimeError("offline"))
    monkeypatch.setenv("VOXSUB_SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setattr(error_reporting, "_load_sdk", lambda: fake)

    context = error_reporting.initialize_error_reporting()

    assert context.environment == "testing"
    assert error_reporting._SDK is None  # noqa: SLF001
    assert error_reporting._SENTRY_HANDLER is None  # noqa: SLF001


def test_exception_hook_and_log_handler_capture_without_propagating(monkeypatch) -> None:
    fake = _FakeSentry()
    monkeypatch.setenv("VOXSUB_SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setenv("VOXSUB_ENVIRONMENT", "testing")
    monkeypatch.setenv("VOXSUB_BUILD", "ci-42")
    monkeypatch.setattr(error_reporting, "_load_sdk", lambda: fake)
    original_hook = sys.excepthook
    seen: list[str] = []
    monkeypatch.setattr(sys, "excepthook",
                        lambda _type, value, _tb: seen.append(str(value)))

    context = error_reporting.initialize_error_reporting()
    assert context.environment == "testing"
    assert context.build_id == "ci-42"
    assert fake.init_kwargs["send_default_pii"] is False
    assert fake.init_kwargs["include_local_variables"] is False

    try:
        raise RuntimeError("model load failed at C:\\Users\\Alice\\model.onnx")
    except RuntimeError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
        logging.getLogger("voxsub.test.error").error(
            "api_key=secret text='hidden'", exc_info=True)

    assert seen
    assert len(fake.exceptions) >= 2
    assert original_hook is not sys.excepthook


def test_background_thread_exception_hook_capture(monkeypatch) -> None:
    fake = _FakeSentry()
    monkeypatch.setenv("VOXSUB_SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setattr(error_reporting, "_load_sdk", lambda: fake)
    seen: list[str] = []
    monkeypatch.setattr(threading, "excepthook",
                        lambda args: seen.append(args.thread.name))
    error_reporting.initialize_error_reporting()

    exc = RuntimeError("worker failed")
    args = SimpleNamespace(thread=SimpleNamespace(name="model-worker"),
                           exc_value=exc, exc_type=type(exc), exc_traceback=None)
    threading.excepthook(args)

    assert seen == ["model-worker"]
    assert fake.exceptions and fake.exceptions[-1] is exc


def test_capture_failures_are_swallowed_and_flush_is_best_effort(monkeypatch) -> None:
    fake = _FakeSentry(capture_error=RuntimeError("network down"))
    monkeypatch.setenv("VOXSUB_SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setattr(error_reporting, "_load_sdk", lambda: fake)
    error_reporting.initialize_error_reporting()

    assert error_reporting.capture_exception(RuntimeError("boom")) is None
    assert error_reporting.capture_message("failure") is None
    error_reporting.shutdown_error_reporting(timeout=1.5)
    assert fake.flush_calls == [1.5]


def test_send_diagnostic_report_attaches_filtered_snapshot(monkeypatch) -> None:
    fake = _FakeSentry()
    monkeypatch.setenv("VOXSUB_SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setattr(error_reporting, "_load_sdk", lambda: fake)
    error_reporting.initialize_error_reporting()

    assert error_reporting.send_diagnostic_report(
        "ASR fail path=C:\\Users\\Alice\\model.onnx",
        "ERROR api_key=secret recognized='机密字幕'",
    )
    scope = fake.scopes[-1]
    assert len(scope.attachments) == 2
    rendered = b"\n".join(item["bytes"] for item in scope.attachments)
    assert b"secret" not in rendered
    assert "机密字幕".encode() not in rendered
    assert b"C:\\Users\\Alice" not in rendered


def test_send_diagnostic_report_without_sentry_is_noop() -> None:
    assert not error_reporting.is_error_reporting_enabled()
    assert not error_reporting.send_diagnostic_report("report", "logs")
