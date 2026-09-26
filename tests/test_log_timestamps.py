"""Absolute log time, tested without GUI/devices or user data."""
import logging
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend" / "backend"))
import ipc_protocol
import ipc_server


def test_session_snapshot_retains_full_timestamp(monkeypatch):
    import voxsub.logging_setup as logs
    monkeypatch.setattr(logs, "diagnostic_session_snapshot", lambda: {"session_id": "test"})
    original = "2026-09-26T17:25:20.775+00:00 INFO [session=test] original\n"
    monkeypatch.setattr(logs, "tail_log_file", lambda _: original)
    text, meta = logs.diagnostic_session_log_snapshot()
    assert text == original
    assert meta["first_log_at"] == "2026-09-26T17:25:20.775+00:00"
    assert meta["last_log_at"] == meta["first_log_at"]


def test_all_clock_sources_are_absolute_milliseconds():
    import ipc_loop
    for clock in (ipc_protocol._now_iso, ipc_loop._now_iso):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(?:Z|[+-]\d{2}:\d{2})", clock())


def test_job_producer_carries_its_own_timestamp():
    import job_runner
    events = []
    runner = job_runner.JobRunner(on_event=lambda kind, payload: events.append(payload))
    try:
        runner.submit("fixture", {})
        assert events and "ts" in events[0]
        assert datetime.fromisoformat(events[0]["ts"]).tzinfo is not None
    finally:
        runner.stop()


def test_file_formatter_and_ring_keep_record_instant(monkeypatch):
    import voxsub.logging_setup as logs
    logs.setup_logging()
    record = logging.LogRecord("voxsub.fixture", logging.INFO, __file__, 1, "original", (), None)
    record.created = datetime(2026, 9, 26, 17, 25, 20, 775000, tzinfo=timezone.utc).timestamp()
    record.diagnostic_session_id = "-"
    record.asctime = "old formatter contamination"
    logger = logging.getLogger("voxsub")
    file_handler = next(h for h in logger.handlers if isinstance(h, logging.FileHandler))
    assert file_handler.format(record).startswith("2026-09-26T17:25:20.775+00:00 INFO")
    monkeypatch.setattr(logs, "_EVENT_QUEUE", __import__("queue").Queue(maxsize=3))
    logs._EVENT_QUEUE.put(record)
    assert logs.drain_events()[0]["ts"] == "2026-09-26T17:25:20.775+00:00"



def test_bridge_uses_record_creation_time(monkeypatch):
    events = []
    monkeypatch.setattr(ipc_protocol, "_emit", events.append)
    logger = logging.getLogger("voxsub")
    before = list(logger.handlers)
    service = ipc_server.BackendService()
    try:
        service._install_log_sink()
        bridge = next(h for h in logger.handlers if getattr(h, "_outer", None) is service)
        record = logging.LogRecord("voxsub.fixture", logging.INFO, __file__, 1, "original", (), None)
        record.created = datetime(2026, 9, 26, 17, 25, 20, 775000, tzinfo=timezone.utc).timestamp()
        bridge.emit(record)
        emitted = next(e for e in events if e.get("event") == "log")
        assert emitted["ts"] == "2026-09-26T17:25:20.775+00:00"
        assert emitted["message"] == "voxsub.fixture: original"
    finally:
        for h in list(logger.handlers):
            if h not in before:
                logger.removeHandler(h)
                h.close()
        for h in before:
            if h not in logger.handlers:
                logger.addHandler(h)
