"""行为层测试：生产 IPC 的 schema 检查、打包资源定位与兼容模式。"""
from __future__ import annotations

import ast
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "frontend" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import contract_validation  # noqa: E402
import ipc_loop  # noqa: E402
import ipc_protocol  # noqa: E402
import job_runner  # noqa: E402


VALID_STATE = {
    "running": False,
    "paused": False,
    "mode": "a",
    "state": "IDLE",
    "configGeneration": 0,
}


class StateService:
    def __init__(self, result: Any, command: str = "state") -> None:
        self.result = result
        self.command = command
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def has_command(self, command: str) -> bool:
        return command == self.command

    def handle(self, command: str, args: dict[str, Any]) -> Any:
        self.calls.append((command, args))
        return self.result


def make_loop(
    result: Any,
    *,
    contracts_dir: str | Path | None = None,
    command: str = "state",
):
    service = StateService(result, command)
    emitted: list[dict[str, Any]] = []
    options = {} if contracts_dir is None else {"contracts_dir": contracts_dir}
    loop = ipc_loop.IpcLoop(
        service,
        job_runner.JobRunner(),
        emitted.append,
        **options,
    )
    return loop, service, emitted


def events(emitted: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [item for item in emitted if item.get("event") == name]


def replies(emitted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in emitted if "id" in item]


@pytest.mark.parametrize(
    ("payload", "expected_response_id"),
    [
        ({"id": 17, "command": "state", "args": {}, "unexpected": True}, 17),
        ({"id": "bad-id", "command": "state", "args": {}}, None),
    ],
)
def test_strict_mode_rejects_invalid_request_envelope_before_handler(
        monkeypatch, payload, expected_response_id):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, service, emitted = make_loop(VALID_STATE)

    loop.handle_line(json.dumps(payload))

    assert service.calls == []
    response = replies(emitted)[0]
    assert response["id"] == expected_response_id
    assert response["ok"] is False
    assert response["code"] == "contract_violation"


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("invalid_id", ["bad-id", True, 1.5, [], {}])
def test_parse_rejections_do_not_echo_invalid_request_ids(monkeypatch, strict, invalid_id):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", strict)
    registry = contract_validation.ContractRegistry()
    for payload, expected_code in (
        ({"id": invalid_id, "command": ""}, "missing_command"),
        ({"id": invalid_id, "command": "state", "args": []}, "bad_args"),
    ):
        loop, service, emitted = make_loop(VALID_STATE)

        loop.handle_line(json.dumps(payload))

        assert service.calls == []
        response = replies(emitted)[0]
        assert response["id"] is None
        assert response["ok"] is False
        assert response["code"] == expected_code
        for item in emitted:
            registry.validate_outbound(item)


def test_soft_mode_checks_event_schema_but_preserves_existing_delivery():
    loop, _, emitted = make_loop(VALID_STATE)
    loop._event("state", running=True)  # required state fields deliberately absent

    assert events(emitted, "state") == [{"event": "state", "running": True}]
    assert any("契约校验" in item.get("message", "") for item in events(emitted, "log"))


def test_strict_mode_drops_schema_invalid_event_and_reports_it(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, _, emitted = make_loop(VALID_STATE)
    loop._event("state", running=True)

    assert events(emitted, "state") == []
    assert any("契约校验" in item.get("message", "") for item in events(emitted, "log"))


@pytest.mark.parametrize("strict, expected_state_count", [(False, 1), (True, 0)])
def test_shared_event_producer_routes_through_ipc_runtime_validation(
        monkeypatch, strict, expected_state_count):
    """Events from handlers/pipeline must use the same runtime contract boundary."""
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", strict)
    loop, _, emitted = make_loop(VALID_STATE)
    ipc_protocol.set_event_dispatcher(loop.emit_producer_event)
    try:
        ipc_protocol._event("state", running=True)  # Missing required event fields.
    finally:
        ipc_protocol.set_event_dispatcher(None)

    assert len(events(emitted, "state")) == expected_state_count
    diagnostics = events(emitted, "log")
    assert any("契约校验" in item.get("message", "") for item in diagnostics)
    assert all("ts" in item for item in diagnostics)


def test_main_installs_runtime_dispatcher_for_producer_events(monkeypatch):
    """The real composition root must route module-level events through IpcLoop."""
    import io
    import ipc_server

    saved_stdout = sys.stdout
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    monkeypatch.setattr(ipc_server, "FROZEN", False)
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(ipc_server, "_emit", emitted.append)
    monkeypatch.setattr(ipc_protocol, "_emit", emitted.append)
    from voxsub import logging_setup

    def fail_logger(_name):
        raise RuntimeError("isolated startup log sink failure")

    monkeypatch.setattr(logging_setup, "get_logger", fail_logger)
    install_log_sink = ipc_server.BackendService._install_log_sink
    validated: list[dict[str, Any]] = []

    class FakeService:
        _log_sink_installed = False

        def _install_log_sink(self) -> None:
            install_log_sink(self)

        def bind_job_runner(self, _runner) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeRunner:
        def set_executor(self, _executor) -> None:
            pass

        def start(self) -> None:
            pass

        def active_job_names(self) -> list[str]:
            return []

        def stop(self, *, timeout: float) -> None:
            assert timeout == 5.0

    class EventLoop(ipc_loop.IpcLoop):
        def emit_producer_event(self, payload) -> None:
            validated.append(payload)
            super().emit_producer_event(payload)

        def serve(self, stream) -> None:
            ipc_protocol._event("state", running=True)
            super().serve(stream)

    monkeypatch.setattr(ipc_server, "BackendService", FakeService)
    monkeypatch.setattr(ipc_loop, "IpcLoop", EventLoop)
    monkeypatch.setattr(job_runner, "JobRunner", FakeRunner)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", saved_stdout)
    try:
        assert ipc_server.main() == 0
    finally:
        ipc_protocol.set_event_dispatcher(None)

    startup_logs = [item for item in events(emitted, "log") if item["message"] == "日志桥安装失败"]
    assert len(startup_logs) == 1
    assert startup_logs[0] in validated, "startup failure log bypassed the runtime validator"
    contract_validation.ContractRegistry().validate_event(startup_logs[0])
    assert events(emitted, "state") == []
    assert any("契约校验" in item.get("message", "") for item in events(emitted, "log"))


def test_strict_mode_rejects_invalid_command_result_at_real_reply_boundary(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, service, emitted = make_loop({"running": True})
    loop.handle_line(json.dumps({"id": 7, "command": "state"}))

    assert service.calls == [("state", {})]
    response = replies(emitted)[0]
    assert response["ok"] is False
    assert response["code"] == "contract_violation"
    assert "返回值不合法" in response["error"]


def test_strict_mode_accepts_valid_command_result(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, _, emitted = make_loop(VALID_STATE)
    loop.handle_line(json.dumps({"id": 8, "command": "state"}))

    assert replies(emitted) == [{"id": 8, "ok": True, "data": VALID_STATE}]


def test_compatibility_mode_warns_but_preserves_invalid_command_result():
    loop, _, emitted = make_loop({"running": True})
    loop.handle_line(json.dumps({"id": 11, "command": "state", "args": {}}))

    assert replies(emitted)[0]["ok"] is True
    assert any("返回值不合法" in item.get("message", "") for item in events(emitted, "log"))


def test_soft_mode_warns_and_dispatches_schema_invalid_object_args(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", False)
    loop, service, emitted = make_loop(VALID_STATE)
    loop.handle_line(json.dumps({"id": 9, "command": "state", "args": {"unexpected": True}}))

    assert service.calls == [("state", {"unexpected": True})]
    assert replies(emitted)[0]["ok"] is True
    assert any("参数不符合契约" in item.get("message", "") for item in events(emitted, "log"))


def test_strict_mode_rejects_schema_invalid_object_args_before_handler(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, service, emitted = make_loop(VALID_STATE)
    loop.handle_line(json.dumps({"id": 10, "command": "state", "args": {"unexpected": True}}))

    assert service.calls == []
    assert replies(emitted)[0]["ok"] is False
    assert replies(emitted)[0]["code"] == "contract_violation"


def test_strict_mode_fails_closed_and_reports_when_contracts_are_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, service, emitted = make_loop(VALID_STATE, contracts_dir=tmp_path / "missing-contracts")

    assert any("IPC 契约不可用" in item.get("message", "") for item in events(emitted, "log"))
    loop.handle_line(json.dumps({"id": 14, "command": "state", "args": {}}))
    assert service.calls == []
    assert replies(emitted)[0]["code"] == "contract_violation"


def test_contract_registry_uses_pyinstaller_bundle_data_path(monkeypatch, tmp_path):
    bundle_root = tmp_path / "bundle"
    shutil.copytree(ROOT / "contracts", bundle_root / "contracts")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_root), raising=False)

    registry = contract_validation.ContractRegistry()

    assert registry.dir == bundle_root / "contracts"
    assert "state" in registry.command_names
    assert "ready" in registry.event_names


def test_sidecar_pyinstaller_spec_includes_contract_json_directory():
    spec_path = ROOT / "frontend" / "backend" / "ipc_server.spec"
    tree = ast.parse(spec_path.read_text(encoding="utf-8"))
    bundled_contracts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AugAssign) or not isinstance(node.target, ast.Name) or node.target.id != "datas":
            continue
        if not isinstance(node.value, ast.List):
            continue
        for item in node.value.elts:
            if isinstance(item, ast.Tuple) and len(item.elts) == 2:
                try:
                    destination = ast.literal_eval(item.elts[1])
                except (ValueError, TypeError):
                    continue
                if destination == "contracts":
                    source = item.elts[0]
                    if (
                        isinstance(source, ast.Call)
                        and isinstance(source.func, ast.Name)
                        and source.func.id == "str"
                        and source.args
                        and isinstance(source.args[0], ast.BinOp)
                        and isinstance(source.args[0].right, ast.Constant)
                    ):
                        bundled_contracts.append(source.args[0].right.value)

    assert set(bundled_contracts) == {
        "protocol.json",
        "commands.json",
        "events.json",
        "error-codes.json",
    }, "The frozen sidecar must bundle every JSON file loaded by ContractRegistry."


def test_missing_contract_directory_is_reported_not_silent(tmp_path):
    loop, _, emitted = make_loop(VALID_STATE, contracts_dir=tmp_path / "missing-contracts")

    assert loop._contracts is None
    assert any("IPC 契约不可用" in item.get("message", "") for item in events(emitted, "log"))


def test_job_event_result_uses_start_migration_result_contract():
    registry = contract_validation.ContractRegistry()
    valid_event = {
        "event": "job",
        "jobId": "job-valid",
        "command": "start_migration",
        "clientMigrationId": "test-migration-valid",
        "sequence": 1,
        "status": "succeeded",
        "result": {"done": [], "failed": [], "elapsedMs": 0, "configUpdates": {}, "ok": True},
    }
    registry.validate_event(valid_event)
    event = {
        **valid_event,
        "jobId": "job-invalid",
        "clientMigrationId": "test-migration-invalid",
        "result": {"unexpected": True},
    }

    with pytest.raises(contract_validation.ContractViolation, match="返回值不合法"):
        registry.validate_event(event)


def test_strict_mode_drops_invalid_async_migration_result_event(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, _, emitted = make_loop(VALID_STATE)
    loop._event(
        "job",
        jobId="job-2",
        command="start_migration",
        clientMigrationId="test-migration-2",
        sequence=1,
        status="succeeded",
        result={"unexpected": True},
    )

    assert events(emitted, "job") == []
    assert any("返回值不合法" in item.get("message", "") for item in events(emitted, "log"))


def test_strict_mode_emits_contract_valid_fallback_for_missing_migration_result(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, _, emitted = make_loop(VALID_STATE, command="start_migration")
    job_id = "job-missing-migration-result"
    loop._requests[job_id] = ipc_loop._Request(
        req_id=21,
        args={"clientMigrationId": "migration-fallback-test"},
        wants_async=True,
    )
    monkeypatch.setattr(loop._runner, "get", lambda _job_id: SimpleNamespace(result=None))

    loop._on_job_event("job", {
        "jobId": job_id,
        "command": "start_migration",
        "sequence": 1,
        "status": job_runner.SUCCEEDED,
    })

    terminal = events(emitted, "job")
    assert len(terminal) == 1
    assert terminal[0]["result"] == {
        "done": [],
        "failed": [{"key": "?", "error": "后端未返回有效迁移报告"}],
        "elapsedMs": 0,
        "configUpdates": {},
        "ok": False,
    }
    assert job_id not in loop._requests


def test_async_ack_result_has_its_own_small_contract():
    registry = contract_validation.ContractRegistry()
    registry.validate_outbound({
        "id": 1,
        "ok": True,
        "data": {"jobId": "job-1", "accepted": True, "status": "queued"},
    }, async_accepted=True)

    with pytest.raises(contract_validation.ContractViolation):
        registry.validate_outbound({
            "id": 2,
            "ok": True,
            "data": {"jobId": "job-2", "accepted": False, "status": "unknown"},
        }, async_accepted=True)


def test_async_migration_ack_is_validated_at_ipc_boundary(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, service, emitted = make_loop(None, command="start_migration")
    args = {
        "steps": [{"source": "old-models", "target": "new-models", "key": "models"}],
        "async": True,
        "clientMigrationId": "runtime-contract-test",
    }

    loop.handle_line(json.dumps({"id": 12, "command": "start_migration", "args": args}))

    response = replies(emitted)[0]
    assert response["ok"] is True
    assert response["data"]["accepted"] is True
    assert response["data"]["status"] == "queued"
    assert response["data"]["jobId"]
    assert service.calls == []  # unstarted in-memory runner: migration handler never executes
    assert events(emitted, "job")[-1]["clientMigrationId"] == "runtime-contract-test"


def test_async_completion_result_is_validated_against_the_submitted_command(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, _, emitted = make_loop(None, command="start_migration")
    job = job_runner.Job(
        id="migration-completion-test",
        command="start_migration",
        status=job_runner.SUCCEEDED,
        result={"unexpected": True},
    )
    loop._requests[job.id] = ipc_loop._Request(12, {}, False)
    monkeypatch.setattr(loop._runner, "get", lambda _job_id: job)

    loop._complete(job.id)

    assert replies(emitted)[0]["code"] == "contract_violation"


def test_invalid_async_ack_is_rejected_at_ipc_boundary(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, _, emitted = make_loop(None, command="start_migration")
    job = SimpleNamespace(id="bad-ack-job", command="start_migration", status="not-a-job-state")

    def submit(_command, _args, *, on_queued, **_kwargs):
        on_queued(job)
        return job

    monkeypatch.setattr(loop._runner, "submit", submit)
    args = {
        "steps": [{"source": "old-models", "target": "new-models"}],
        "async": True,
        "clientMigrationId": "bad-ack-test",
    }
    loop.handle_line(json.dumps({"id": 13, "command": "start_migration", "args": args}))

    response = replies(emitted)[0]
    assert response["ok"] is False
    assert response["code"] == "contract_violation"


def test_strict_invalid_async_ack_rejects_before_job_is_admitted(monkeypatch):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", True)
    loop, service, emitted = make_loop(None, command="start_migration")
    runner = loop._runner
    runner.start()
    original_check = loop._check_outbound

    def reject_async_ack(payload, *, command=None, async_accepted=False):
        if async_accepted:
            return "forced async acknowledgement contract violation"
        return original_check(
            payload, command=command, async_accepted=async_accepted,
        )

    monkeypatch.setattr(loop, "_check_outbound", reject_async_ack)
    args = {
        "steps": [{"source": "old-models", "target": "new-models", "key": "models"}],
        "async": True,
        "clientMigrationId": "admission-boundary-test",
    }
    try:
        loop.handle_line(json.dumps({"id": 19, "command": "start_migration", "args": args}))

        response = replies(emitted)[0]
        assert response["ok"] is False
        assert response["code"] == "contract_violation"
        assert runner.list_jobs() == [], "a rejected acknowledgement must not leave queued work"
        assert service.calls == []
    finally:
        runner.stop(timeout=2)


@pytest.mark.parametrize("strict", [False, True])
def test_null_request_id_is_rejected_by_both_parser_and_contract(monkeypatch, strict):
    monkeypatch.setattr(ipc_loop, "CONTRACT_ENFORCE", strict)
    loop, service, emitted = make_loop(VALID_STATE)
    request = {"id": None, "command": "state", "args": {}}

    loop.handle_line(json.dumps(request))

    assert service.calls == []
    response = replies(emitted)[0]
    assert response["id"] is None
    assert response["code"] == "missing_id"
    registry = contract_validation.ContractRegistry()
    registry.validate_outbound(response)
    with pytest.raises(contract_validation.ContractViolation):
        registry.validate_request_envelope(request)


def test_null_request_id_error_is_classified_as_a_response():
    registry = contract_validation.ContractRegistry()
    assert registry.validate_outbound({
        "id": None,
        "ok": False,
        "error": "协议消息缺少 id",
        "code": "missing_id",
    }) == "response"
