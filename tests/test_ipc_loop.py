"""IPC 读循环 —— 协议防御与"控制命令不排队"的测试。

工作单 §3.3 要求"主读循环保持可响应"，§3.6 要求协议解析要能扛住
无效 JSON、null、未知命令、错误载荷、超大消息。这里逐条落成测试。

为什么值得单独测：以前所有命令都在读循环里同步执行，迁移一跑，
界面上的"停止/状态"就全被堵住 —— 这是"看起来卡死"的根因。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "frontend" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import ipc_loop  # noqa: E402
import job_runner  # noqa: E402


class FakeService:
    """最小可用的服务替身：记录调用、可控地变慢、可选地炸。

    ``job_*`` 三个命令按真实 BackendService 的语义转发给执行器 ——
    这样读循环与执行器的接线才算真的被测到，而不是各测各的。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._commands = {"ping", "state", "echo", "slow", "start_migration", "job_list",
                          "job_status", "cancel_job", "shutdown", "explode"}
        self.release = threading.Event()
        self.started = threading.Event()
        self.runner: Any = None

    def has_command(self, command: str) -> bool:
        return command in self._commands

    def handle(self, command: str, args: Any) -> Any:
        args = args or {}
        self.calls.append((command, args))
        if command == "explode":
            raise ValueError("内部错误")
        if command == "slow":
            self.started.set()
            self.release.wait(timeout=5.0)
            return {"slept": True}
        if command == "state":
            return {"running": False}
        if command == "job_status":
            job_id = str(args.get("job_id") or args.get("jobId") or "")
            job = self.runner.get(job_id) if self.runner else None
            if job is None:
                return {"ok": False, "code": "unknown_job"}
            return {"ok": True, "job": job.snapshot()}
        if command == "job_list":
            if self.runner is None:
                return {"jobs": [], "active": []}
            active_only = not bool(args.get("include_finished"))
            return {"jobs": [item.snapshot()
                             for item in self.runner.list_jobs(active_only=active_only)],
                    "active": self.runner.active_job_names()}
        if command == "cancel_job":
            job_id = str(args.get("job_id") or args.get("jobId") or "")
            try:
                return self.runner.cancel(job_id)
            except job_runner.UnknownJob:
                return {"ok": False, "code": "unknown_job"}
        return {"echo": command, "args": args}


@pytest.fixture()
def loop():
    service = FakeService()
    runner = job_runner.JobRunner()
    emitted: list[dict[str, Any]] = []
    made = ipc_loop.IpcLoop(service, runner, emitted.append)
    service.runner = runner
    runner.start()
    made.service = service  # type: ignore[attr-defined]
    made.runner = runner  # type: ignore[attr-defined]
    made.emitted = emitted  # type: ignore[attr-defined]
    yield made
    runner.stop(timeout=2.0)


def _replies(emitted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in emitted if "id" in item]


def _events(emitted: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [item for item in emitted if item.get("event") == kind]


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# --------------------------------------------------------------- 正常路径

def test_control_command_answered_inline(loop):
    loop.handle_line(json.dumps({"id": 1, "command": "ping"}))
    replies = _replies(loop.emitted)
    assert replies == [{"id": 1, "ok": True, "data": {"echo": "ping", "args": {}}}]


def test_queued_command_replies_with_same_id(loop):
    loop.handle_line(json.dumps({"id": 7, "command": "echo", "args": {"a": 1}}))
    assert _wait_until(lambda: _replies(loop.emitted))
    reply = _replies(loop.emitted)[0]
    assert reply["id"] == 7
    assert reply["ok"] is True
    assert reply["data"] == {"echo": "echo", "args": {"a": 1}}
    assert reply["jobId"]


def test_request_is_registered_before_worker_can_complete(loop, monkeypatch):
    """极快 worker 不能在 IPC 登记 request 前吞掉参数或终态回复。"""
    handled = threading.Event()
    original_handle = loop.service.handle
    original_submit = loop.runner.submit

    def observed_handle(command, args):
        result = original_handle(command, args)
        handled.set()
        return result

    def submit_then_wait(command, args, **kwargs):
        job = original_submit(command, args, **kwargs)
        assert handled.wait(timeout=2.0), "worker 未在 submit 返回前执行"
        return job

    monkeypatch.setattr(loop.service, "handle", observed_handle)
    monkeypatch.setattr(loop.runner, "submit", submit_then_wait)
    expected = {"nonce": "request-registered-first"}
    loop.handle_line(json.dumps({"id": 71, "command": "echo", "args": expected}))

    replies = [reply for reply in _replies(loop.emitted) if reply["id"] == 71]
    assert len(replies) == 1, "终态应答不能在 request 登记竞态中丢失"
    assert replies[0]["ok"] is True
    assert replies[0]["data"]["args"] == expected
    assert loop.runner.has_active_jobs() is False
    assert loop._requests == {}


def test_async_request_gets_immediate_receipt(loop):
    """``args.async`` 时立刻回 jobId，结果靠事件送（不改变默认同步语义）。"""
    loop.handle_line(json.dumps({"id": 3, "command": "echo", "args": {"async": True}}))
    immediate = _replies(loop.emitted)
    assert len(immediate) == 1
    assert immediate[0]["ok"] is True
    assert immediate[0]["data"]["accepted"] is True
    assert immediate[0]["data"]["jobId"]


def test_async_migration_terminal_event_carries_safe_result(loop):
    """异步迁移使用 jobId 跟踪；成功终态必须携带迁移报告以免 30 分钟硬超时丢结果。"""
    loop.handle_line(json.dumps({
        "id": 4, "command": "start_migration", "args": {
            "async": True, "steps": [], "clientMigrationId": "mig-test-4",
        },
    }))
    assert _wait_until(lambda: any(item.get("id") == 4 for item in _replies(loop.emitted)))
    receipt = next(item for item in _replies(loop.emitted) if item["id"] == 4)
    assert receipt["data"]["accepted"] is True
    job_id = receipt["data"]["jobId"]
    assert _wait_until(lambda: any(
        event.get("jobId") == job_id and event.get("status") == "succeeded"
        for event in _events(loop.emitted, "job")
    ))
    succeeded = next(event for event in _events(loop.emitted, "job")
                     if event.get("jobId") == job_id and event.get("status") == "succeeded")
    assert succeeded["result"]["echo"] == "start_migration"
    assert succeeded["clientMigrationId"] == "mig-test-4"


def test_async_non_migration_job_does_not_expose_result(loop):
    """通用 job 事件不泄漏任意命令结果；仅迁移报告走异步终态。"""
    loop.handle_line(json.dumps({"id": 5, "command": "echo", "args": {"async": True, "clientMigrationId": "do-not-leak"}}))
    assert _wait_until(lambda: any(item.get("id") == 5 for item in _replies(loop.emitted)))
    receipt = next(item for item in _replies(loop.emitted) if item["id"] == 5)
    job_id = receipt["data"]["jobId"]
    assert _wait_until(lambda: any(
        event.get("jobId") == job_id and event.get("status") == "succeeded"
        for event in _events(loop.emitted, "job")
    ))
    succeeded = next(event for event in _events(loop.emitted, "job")
                     if event.get("jobId") == job_id and event.get("status") == "succeeded")
    assert "result" not in succeeded
    assert "clientMigrationId" not in succeeded


def test_async_migration_without_client_id_does_not_publish_result(loop):
    """没有客户端关联 ID 的旧式异步调用不广播不可关联的迁移报告。"""
    loop.handle_line(json.dumps({"id": 6, "command": "start_migration", "args": {"async": True, "steps": []}}))
    assert _wait_until(lambda: any(item.get("id") == 6 for item in _replies(loop.emitted)))
    receipt = next(item for item in _replies(loop.emitted) if item["id"] == 6)
    job_id = receipt["data"]["jobId"]
    assert _wait_until(lambda: any(
        event.get("jobId") == job_id and event.get("status") == "succeeded"
        for event in _events(loop.emitted, "job")
    ))
    succeeded = next(event for event in _events(loop.emitted, "job")
                     if event.get("jobId") == job_id and event.get("status") == "succeeded")
    assert "result" not in succeeded
    assert "clientMigrationId" not in succeeded


def test_async_migration_success_without_snapshot_emits_safe_failure_report(loop, monkeypatch):
    """任务快照异常缺失时仍结束保护并明确返回失败报告，而不是发出无效事件。"""
    monkeypatch.setattr(loop.runner, "get", lambda _job_id: None)
    loop.handle_line(json.dumps({
        "id": 7, "command": "start_migration", "args": {
            "async": True, "steps": [], "clientMigrationId": "mig-no-snapshot",
        },
    }))
    assert _wait_until(lambda: any(item.get("id") == 7 for item in _replies(loop.emitted)))
    receipt = next(item for item in _replies(loop.emitted) if item["id"] == 7)
    job_id = receipt["data"]["jobId"]
    assert _wait_until(lambda: any(
        event.get("jobId") == job_id and event.get("status") == "succeeded"
        for event in _events(loop.emitted, "job")
    ))
    succeeded = next(event for event in _events(loop.emitted, "job")
                     if event.get("jobId") == job_id and event.get("status") == "succeeded")
    assert succeeded["clientMigrationId"] == "mig-no-snapshot"
    assert succeeded["result"]["ok"] is False
    assert "有效迁移报告" in succeeded["result"]["failed"][0]["error"]


def test_job_failure_is_reported_as_not_ok(loop):
    loop.handle_line(json.dumps({"id": 9, "command": "explode"}))
    assert _wait_until(lambda: _replies(loop.emitted))
    reply = _replies(loop.emitted)[0]
    assert reply["ok"] is False
    assert "内部错误" in reply["error"]


def test_job_events_carry_job_id_and_sequence(loop):
    loop.handle_line(json.dumps({"id": 4, "command": "echo"}))
    assert _wait_until(lambda: not loop.runner.has_active_jobs())
    events = _events(loop.emitted, "job")
    assert [item["status"] for item in events] == ["queued", "running", "succeeded"]
    assert all(item["jobId"] for item in events)
    assert [item["sequence"] for item in events] == [1, 2, 3]


def test_log_events_always_carry_a_timestamp(loop):
    """UI 的 LogEntry 要求 ts —— 漏传会让日志时间列永远空白。"""
    loop.handle_line("这不是 JSON")
    logs = _events(loop.emitted, "log")
    assert logs, "坏输入必须留下一条日志"
    assert logs[0]["ts"], "日志事件必须带 ts"


def test_inbound_rejection_replies_carry_a_code(loop):
    """拒绝要带可识别错误码，前端据此区分"协议错"和"命令失败"。"""
    loop.handle_line(json.dumps({"id": 1, "command": "definitely_not_a_command"}))
    reply = _replies(loop.emitted)[0]
    assert reply["ok"] is False
    assert reply["code"] == "unknown_command"


# --------------------------------------------------------------- 响应性

def test_control_commands_do_not_queue_behind_long_work(loop):
    """核心回归：迁移这类长任务在跑时，状态查询必须立刻答复。"""
    loop.handle_line(json.dumps({"id": 1, "command": "slow"}))
    assert loop.service.started.wait(timeout=5.0), "长任务应已开始执行"

    started = time.monotonic()
    loop.handle_line(json.dumps({"id": 2, "command": "state"}))
    elapsed = time.monotonic() - started

    assert elapsed < 0.3, f"控制命令被长任务堵住了（耗时 {elapsed:.2f}s）"
    state_replies = [item for item in _replies(loop.emitted) if item["id"] == 2]
    assert state_replies and state_replies[0]["ok"] is True

    loop.service.release.set()
    assert _wait_until(lambda: not loop.runner.has_active_jobs())


def test_cancel_is_accepted_while_long_work_runs(loop):
    """"停止和取消不排在普通耗时任务后面" —— 取消也必须立刻受理。"""
    loop.handle_line(json.dumps({"id": 1, "command": "slow", "args": {"async": True}}))
    assert loop.service.started.wait(timeout=5.0)
    job_id = _replies(loop.emitted)[0]["data"]["jobId"]

    started = time.monotonic()
    loop.handle_line(json.dumps({"id": 2, "command": "cancel_job",
                                 "args": {"job_id": job_id}}))
    elapsed = time.monotonic() - started
    assert elapsed < 0.3

    reply = [item for item in _replies(loop.emitted) if item["id"] == 2][0]
    assert reply["ok"] is True
    assert reply["data"]["status"] == job_runner.CANCELLING

    loop.service.release.set()
    assert _wait_until(lambda: not loop.runner.has_active_jobs())
    assert loop.runner.get(job_id).status == job_runner.CANCELLED


def test_commands_run_in_order_on_single_worker(loop):
    """单 worker 保序：先提交的先执行（迁移与 OCR 不能被并发/乱序）。"""
    for index in range(4):
        loop.handle_line(json.dumps({"id": index, "command": "echo"}))
    assert _wait_until(lambda: len(_replies(loop.emitted)) == 4)
    assert [item["id"] for item in _replies(loop.emitted)] == [0, 1, 2, 3]


# --------------------------------------------------------------- 协议防御

def test_invalid_json_is_logged_not_crashing(loop):
    loop.handle_line("这不是 JSON")
    assert _replies(loop.emitted) == []
    warnings = _events(loop.emitted, "log")
    assert warnings and "无法解析的输入" in warnings[0]["message"]


@pytest.mark.parametrize("payload", ["null", "[]", '"a string"', "42", "true"])
def test_non_object_payloads_are_rejected(loop, payload):
    loop.handle_line(payload)
    replies = _replies(loop.emitted)
    assert replies and replies[0]["ok"] is False
    assert replies[0]["code"] == "not_an_object"


def test_missing_id_is_rejected(loop):
    loop.handle_line(json.dumps({"command": "ping"}))
    replies = _replies(loop.emitted)
    assert replies and replies[0]["code"] == "missing_id"


def test_missing_command_is_rejected(loop):
    loop.handle_line(json.dumps({"id": 5}))
    replies = _replies(loop.emitted)
    assert replies and replies[0]["code"] == "missing_command"


def test_unknown_command_is_rejected_before_queueing(loop):
    """未知命令要在排队前拒绝 —— 否则调用方白等一场注定失败的作业。"""
    loop.handle_line(json.dumps({"id": 6, "command": "definitely_not_a_command"}))
    replies = _replies(loop.emitted)
    assert replies and replies[0]["code"] == "unknown_command"
    assert loop.runner.list_jobs(active_only=False) == []


@pytest.mark.parametrize("args", [[1, 2], "text", 3])
def test_wrong_args_type_is_rejected(loop, args):
    loop.handle_line(json.dumps({"id": 8, "command": "echo", "args": args}))
    replies = _replies(loop.emitted)
    assert replies and replies[0]["code"] == "bad_args"


def test_null_args_is_treated_as_empty(loop):
    loop.handle_line(json.dumps({"id": 2, "command": "ping", "args": None}))
    assert _replies(loop.emitted)[0]["ok"] is True


def test_oversized_message_is_rejected(loop):
    """超大消息要有界拒绝，不能把内存吃光。"""
    loop._max_line_chars = 200
    loop.handle_line(json.dumps({"id": 1, "command": "echo",
                                 "args": {"blob": "x" * 500}}))
    replies = _replies(loop.emitted)
    assert replies and replies[0]["code"] == "message_too_large"


def test_blank_lines_are_ignored(loop):
    loop.handle_line("")
    loop.handle_line("   \n")
    assert loop.emitted == []


def test_none_line_is_rejected_not_crashed(loop):
    loop.handle_line(None)
    replies = _replies(loop.emitted)
    assert replies and replies[0]["code"] == "empty_message"


def test_service_exception_does_not_kill_the_loop(loop):
    """单条命令失败绝不能杀死进程 —— 既有约定，必须保住。"""
    loop.handle_line(json.dumps({"id": 1, "command": "explode"}))
    loop.handle_line(json.dumps({"id": 2, "command": "ping"}))
    assert _wait_until(lambda: len(_replies(loop.emitted)) == 2)

    by_id = {item["id"]: item for item in _replies(loop.emitted)}
    assert by_id[1]["ok"] is False
    assert by_id[2]["ok"] is True, "上一条命令失败不能影响后面的命令"


def test_serve_processes_a_whole_batch(loop):
    lines = [json.dumps({"id": index, "command": "echo"}) for index in range(3)]
    loop.serve(lines)
    assert loop.runner.wait_for_idle(5.0) is True
    assert [item["id"] for item in _replies(loop.emitted)] == [0, 1, 2]


# --------------------------------------------------------------- 作业查询

def test_job_status_and_list(loop):
    loop.handle_line(json.dumps({"id": 1, "command": "echo", "args": {"async": True}}))
    job_id = _replies(loop.emitted)[0]["data"]["jobId"]
    assert _wait_until(lambda: not loop.runner.has_active_jobs())

    loop.handle_line(json.dumps({"id": 2, "command": "job_status",
                                 "args": {"job_id": job_id}}))
    status = [item for item in _replies(loop.emitted) if item["id"] == 2][0]
    assert status["ok"] is True
    assert status["data"]["job"]["status"] == job_runner.SUCCEEDED

    loop.handle_line(json.dumps({"id": 3, "command": "job_status",
                                 "args": {"job_id": "nope"}}))
    missing = [item for item in _replies(loop.emitted) if item["id"] == 3][0]
    assert missing["data"]["code"] == "unknown_job"

    loop.handle_line(json.dumps({"id": 4, "command": "job_list",
                                 "args": {"include_finished": True}}))
    listed = [item for item in _replies(loop.emitted) if item["id"] == 4][0]
    assert listed["ok"] is True
    assert any(item["jobId"] == job_id for item in listed["data"]["jobs"])


def test_cancel_unknown_job_is_reported(loop):
    loop.handle_line(json.dumps({"id": 1, "command": "cancel_job",
                                 "args": {"job_id": "ghost"}}))
    reply = _replies(loop.emitted)[0]
    # 命令本身执行成功（读循环没出错），但业务结果是"拒绝"：不存在的任务
    # 不能因为一句取消就凭空变成已取消。
    assert reply["ok"] is True
    assert reply["data"]["ok"] is False
    assert reply["data"]["code"] == "unknown_job"


# --------------------------------------------------------------- 握手

def test_handshake_declares_protocol_version_and_snapshot(loop):
    """握手要支持"渲染层重载后重新同步"（§3.6）：版本 + 代际 + 就绪 + 任务快照。"""
    loop.handshake(version="9.9.9", frozen=False, backend_generation="1234")
    ready = _events(loop.emitted, "ready")
    assert len(ready) == 1
    payload = ready[0]
    assert payload["version"] == "9.9.9"
    assert payload["protocolVersion"] == ipc_loop.PROTOCOL_VERSION
    assert payload["backendGeneration"] == "1234"
    assert payload["readiness"]["ready"] is True
    assert payload["readiness"]["activeJobs"] == []


def test_handshake_survives_state_lookup_failure(loop):
    """握手不能因为状态取不到而失败。"""
    def boom(_pipeline):  # pragma: no cover - 只作为异常源
        raise RuntimeError("状态不可用")

    loop._service._state_payload = boom
    loop._service._pipeline = object()
    loop.handshake(version="1.0", frozen=True)
    assert _events(loop.emitted, "ready")[0]["session"] is None
