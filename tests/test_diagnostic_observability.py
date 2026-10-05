"""Silent isolated tests for quick-check truth, trace privacy and developer lifecycle."""
from __future__ import annotations
import json
import logging
from pathlib import Path
import queue
import sys
from types import SimpleNamespace

import pytest

from voxsub.diagnostic_privacy import export_logs, redact, safe_snapshot
from voxsub import diagnostic_trace as trace
from voxsub.diagnostic_runtime import pipeline_snapshot, quick_checks
from voxsub.log_budget import BudgetLogHandler
from voxsub.config_store import APP_CONFIG_SCHEMA, ConfigStore
from voxsub.diagnostics import export_report

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend" / "backend"))
from ipc_server import BackendService

@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    result = BackendService()
    monkeypatch.setattr(result, "ensure_pipeline", lambda: pytest.fail("diagnostics must not load pipeline"))
    return result


def test_empty_and_unverified_are_not_passed():
    assert "未检查" in export_report([])
    assert "全部通过" not in export_report([])
    report = export_report([{"check": "device", "status": "not_run", "detail": "unverified"}])
    assert "检查范围有限" in report
    assert "0 ok" in report
    mixed = export_report([{ "check": "model", "status": "fail", "detail": "missing" }, { "check": "device", "status": "not_run", "detail": "unverified" }])
    assert "存在 1 项失败" in mixed and "[NOT_RUN]" in mixed


def test_quick_check_no_model_load(service, monkeypatch):
    import voxsub.diagnostic_runtime as module
    monkeypatch.setattr(module, "_selected_model_check", lambda label, model, cloud: {"check": label, "status": "not_run", "detail": model})
    result = service.handle("run_self_check", {})
    assert result["scope"] == "quick_read_only"
    assert len(result["results"]) >= 5
    assert service._pipeline is None
    assert any(i["check"] == "实际推理设备" and i["status"] == "not_run" for i in result["results"])


def test_export_no_rerun_no_body_no_credentials(service, monkeypatch):
    import voxsub.diagnostics as module
    monkeypatch.setattr(module, "run_self_check", lambda: pytest.fail("must not recheck during export"))
    service._diagnostic_results = []
    text = service.handle("export_diagnostics", {"log_text": "INFO secret transcript: hello my password is xyz\nERROR api_key=sk-abc123"})["text"]
    for secret in ("hello my password", "xyz", "sk-abc123"):
        assert secret not in text
    assert "未检查" in text and "privacy boundary" in text
    assert "checked_at" in text and "version" in text


def test_trace_allowlist_and_bound():
    trace._EVENTS.clear()
    for i in range(520):
        trace.record("translation", "ok", source="en", target="zh", output_chars=i,
                     transcript="private body", api_key="secret", raw_audio=[1, 2])
    result = trace.snapshot()
    assert len(result["events"]) == 500
    assert "private body" not in json.dumps(result) and "secret" not in json.dumps(result)


def test_errors_classified_without_body():
    trace._EVENTS.clear()
    trace.error("translation", TimeoutError("private transcript and key"), source="en", target="zh")
    event = trace.snapshot()["events"][-1]
    assert event["outcome"] == "timeout" and event["error_type"] == "TimeoutError"
    assert "private transcript" not in json.dumps(event)


def test_language_mismatch_is_metadata_only():
    trace._EVENTS.clear()
    trace.model_result("translation", "This is an English sentence.", expected="zh", source="en", target="zh")
    event = trace.snapshot()["events"][-1]
    assert event["outcome"] == "language_mismatch"
    assert "English sentence" not in json.dumps(event)


def test_safe_export_trace_drops_unknown_fields():
    line = 'INFO MODEL_TRACE ' + json.dumps({"stage": "translation", "outcome": "timeout", "error_type": "TimeoutError", "text": "private body", "api_key": "token"})
    exported = export_logs(line)
    assert "private body" not in exported and "token" not in exported
    assert "TimeoutError" in exported

@pytest.mark.parametrize("text", ["api_key=sk-abc123", "Authorization: Bearer abc123", r"C:\Users\Alice\Documents\private.wav", "https://server.example/v1?token=secret"])
def test_redaction(text):
    result = redact(text)
    assert "abc123" not in result and "Alice" not in result and "server.example" not in result


def test_snapshot_removes_unique_identifiers():
    result = safe_snapshot({"serialNumber": "private", "username": "Alice", "cpu": "CPU model", "driver": "1.2.3"})
    assert result == {"cpu": "CPU model", "driver": "1.2.3"}


def test_runtime_snapshot_does_not_call_recording_property():
    fake = SimpleNamespace(state="idle", _queue=queue.Queue(maxsize=8), recording_state={"recordingEnabled": False}, config_generation=2)
    result = pipeline_snapshot(fake)
    assert result["recording"]["recordingEnabled"] is False
    assert result["queues"]["capture"]["capacity"] == 8
    assert result["inference_verified"] is False
    assert pipeline_snapshot(None)["state"] == "not_loaded"


def test_developer_gate_and_close_restores_logs(service):
    from voxsub.logging_setup import diagnostic_session_snapshot
    with pytest.raises(PermissionError):
        service.handle("diagnostic_snapshot", {})
    assert service.handle("developer_mode", {"enabled": True}) == {"enabled": True}
    assert service.handle("diagnostic_session", {"action": "start", "seconds": 60})["session"]
    assert service.handle("diagnostic_snapshot", {})["pipeline"]["state"] == "not_loaded"
    assert service.handle("developer_mode", {"enabled": False}) == {"enabled": False}
    assert diagnostic_session_snapshot() is None
    assert BackendService()._developer_enabled is False

@pytest.mark.parametrize("seconds", [0, 59, 1201, True, "300"])
def test_verbose_session_duration_is_bounded(service, seconds):
    service.handle("developer_mode", {"enabled": True})
    with pytest.raises(ValueError):
        service.handle("diagnostic_session", {"action": "start", "seconds": seconds})


def test_log_budget_config_range():
    assert APP_CONFIG_SCHEMA.normalize("log_limit_mb", 1) == 10
    assert APP_CONFIG_SCHEMA.normalize("log_limit_mb", 100000) == 10240
    assert APP_CONFIG_SCHEMA.normalize("log_limit_unit", "invalid") == "MB"


def test_log_budget_shrink_is_bounded_and_preserves_other_files(tmp_path):
    path = tmp_path / "voxsub.log"
    path.write_bytes(b"x" * 12 * 1024 * 1024)
    unrelated = tmp_path / "user-report.log"
    unrelated.write_text("preserve")
    handler = BudgetLogHandler(str(path), 50)
    try:
        handler.set_budget(10)
        handler.emit(logging.makeLogRecord({"msg": "hello", "levelno": logging.INFO, "levelname": "INFO"}))
        handler.flush()
        assert sum(p.stat().st_size for p in tmp_path.glob("voxsub.log*")) <= 10 * 1024 * 1024
        assert unrelated.read_text() == "preserve"
    finally:
        handler.close()


def test_log_budget_caps_large_messages(tmp_path):
    handler = BudgetLogHandler(str(tmp_path / "voxsub.log"), 10)
    try:
        handler.emit(logging.makeLogRecord({"msg": "x" * 2 * 1024 * 1024, "levelno": logging.INFO}))
        handler.flush()
        assert (tmp_path / "voxsub.log").stat().st_size <= 17000
    finally:
        handler.close()


def test_late_expiry_cannot_stop_new_verbose_session():
    from voxsub import logging_setup as logs
    try:
        first = logs.start_diagnostic_session(60)
        second = logs.start_diagnostic_session(60)
        logs._expire_session_timer(first["session_id"])
        assert logs.diagnostic_session_snapshot()["session_id"] == second["session_id"]
        logs._expire_session_timer(second["session_id"])
        assert logs.diagnostic_session_snapshot() is None
    finally:
        logs.stop_diagnostic_session()


def test_wrapped_http_error_keeps_status_without_body():
    cause = RuntimeError("secret server body")
    cause.response = SimpleNamespace(status_code=429)
    exc = RuntimeError("private transcript")
    exc.__cause__ = cause
    trace.error("translation", exc)
    event = trace.snapshot()["events"][-1]
    assert event["error_code"] == "429"
    assert "private transcript" not in json.dumps(event)


def test_export_rejects_stale_check_after_config_change(service, monkeypatch):
    import voxsub.diagnostic_runtime as module
    monkeypatch.setattr(module, "quick_checks", lambda config, pipe: [{"check": "old", "status": "ok", "detail": "old-config-only"}])
    service.handle("run_self_check", {})
    assert "old-config-only" in service.handle("export_diagnostics", {})["text"]
    ConfigStore().update({"lang_pair": "en-zh"})
    exported = service.handle("export_diagnostics", {})["text"]
    assert "old-config-only" not in exported
    assert "not_run_or_configuration_changed" in exported


def test_multibyte_logs_obey_total_budget(tmp_path):
    handler = BudgetLogHandler(str(tmp_path / "voxsub.log"), 10)
    try:
        record = logging.makeLogRecord({"msg": "中文" * 8192, "levelno": logging.INFO})
        for _ in range(240):
            handler.emit(record)
        handler.flush()
        assert sum(p.stat().st_size for p in tmp_path.glob("voxsub.log*")) <= 10 * 1024 * 1024
    finally:
        handler.close()


def test_model_events_share_request_and_session_without_results():
    trace._EVENTS.clear()
    class Fake:
        _diagnostic_session_id = "abcdef123456"
        @trace.traced_stage("recognition")
        def decode(self):
            trace.model_result("recognition", "private sentence", expected="en", source="en", target="zh")
            return "private sentence"
    assert Fake().decode() == "private sentence"
    events = trace.snapshot()["events"]
    assert len({e["request_id"] for e in events}) == 1
    assert {e["pipeline_session_id"] for e in events} == {"abcdef123456"}
    assert "private sentence" not in json.dumps(events)


def test_uncertain_recognition_is_retained_not_reported_as_translation_failure():
    trace._EVENTS.clear()
    trace.model_result("recognition", "混合语言正文", expected="en", source="en", target="zh")
    result = trace.snapshot()
    event = result["events"][-1]
    assert event["outcome"] == "language_uncertain"
    assert event["fallback"] == "source_retained"
    assert result["failures"] == 0
    assert "混合语言正文" not in json.dumps(result, ensure_ascii=False)

@pytest.mark.parametrize("path", [r"\\private-server\private-share\models\weights.bin", "/mnt/private-user/models/weights.bin", "/home/private-user/private-weights.bin"])
def test_private_network_and_portable_paths_are_redacted(path):
    assert "private" not in redact(path)
    assert "[PATH]" in redact(path)


def test_snapshot_redacts_credential_key_aliases_but_retains_counts():
    value = safe_snapshot({"apiKey": "private-key", "access_token": "private-access", "Authorization": "private-bearer",
                           "token": "private-token", "token_count": 37, "nested": [{"refreshToken": "private-refresh", "model": "public-model"}]})
    assert "private" not in json.dumps(value)
    assert value["token_count"] == 37 and value["nested"][0]["model"] == "public-model"


def test_diagnostic_export_uses_atomic_writer_without_destroying_previous_report(service, tmp_path, monkeypatch):
    from handlers import diagnostics
    report = tmp_path / "existing-report.txt"
    report.write_text("previous complete report", encoding="utf-8")
    calls = []
    def denied(path, text, **kwargs):
        calls.append((path, text))
        raise PermissionError("synthetic atomic commit failure")
    monkeypatch.setattr(diagnostics, "write_text_atomically", denied)
    with pytest.raises(PermissionError):
        service._cmd_export_diagnostics({"path": str(report)})
    assert len(calls) == 1 and "checked_at" in calls[0][1]
    assert report.read_text(encoding="utf-8") == "previous complete report"
