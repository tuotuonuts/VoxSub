"""IPC 主读循环 —— 读循环只负责读、校验、分派，不亲自干耗时活。

工作单 §3.3 的两条硬要求决定了这里的形状：

  1. "Python 主读循环保持可响应。耗时任务交给有界 worker/队列。"
     此前所有命令都在 ``for raw in sys.stdin:`` 里同步执行，迁移/OCR 一跑，
     管道里的 ``state``、``stop`` 就全排在后面 —— 界面看起来"卡死"。
     现在：控制命令（ping/state/job_*/cancel_job/shutdown）**在读循环线程上
     立刻执行**，其余命令交给 :class:`job_runner.JobRunner` 的单 worker。
  2. "协议解析要防御：无效 JSON、null、未知命令、错误载荷、缺失成功标记、
     超大消息和子进程异常退出。"
     逐条在 :meth:`IpcLoop.handle_line` 里落实，并有对应测试。

为什么和 :class:`job_runner.JobRunner` 分成两个模块：读循环关心"怎么收、
怎么回"，执行器关心"怎么排队、怎么取消、什么算终态"。两者都能单独测试，
不需要真的起进程。
"""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from job_runner import (
    ActiveJobExists, CANCELLED, CONTROL_COMMANDS, FAILED, SUCCEEDED, TERMINAL, Job, JobRunner,
    QueueFull, UnknownJob,
)

#: 单行消息上限（UTF-8 字节）。OCR 会传 base64 图片，所以给得比较宽 ——
#: 但仍然有界，防止畸形输入把内存吃光。
MAX_LINE_BYTES = 32 * 1024 * 1024

#: 协议版本。握手要带它，好让前端知道自己对接的是哪一代协议。
PROTOCOL_VERSION = 1

#: 契约校验模式。
#:
#: 默认保持兼容模式：已加载的 schema 不匹配会写结构化日志但不拦截，
#: 避免契约漂移直接让用户功能不可用。设置为 True 时，入站 args、出站事件、
#: command result 与异步受理回执都会在边界硬拒绝；启用前须先证明生产调用符合契约。
CONTRACT_ENFORCE = False

#: 同一个契约问题只报一次，避免长任务刷屏。
_CONTRACT_WARN_LIMIT = 50


def _now_iso() -> str:
    """ISO-8601 本地时间（带时区）。

    日志事件必须带它：UI 的 ``LogEntry`` 要求 ``ts``（排序与显示时间）。
    之前有 5 个调用点漏传 —— 契约的 ``knownGaps`` 清单一次就全抓出来了，
    所以现在集中在这里补，而不是要求每个调用点记得传。
    """
    import datetime  # noqa: PLC0415

    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class _Request:
    """一次在途请求：谁发的、参数是什么、要不要立刻回执。"""

    req_id: Any
    args: Any
    wants_async: bool


class _AsyncAckContractRejected(RuntimeError):
    """Strict contract validation rejected the ack before job admission."""


class IpcLoop:
    """读循环 + 分派。构造参数全部注入，便于离线测试。"""

    def __init__(
        self,
        service: Any,
        runner: JobRunner,
        emit: Callable[[dict[str, Any]], None],
        *,
        max_line_bytes: int = MAX_LINE_BYTES,
        protocol_version: int = PROTOCOL_VERSION,
        contracts_dir: str | Path | None = None,
    ) -> None:
        self._service = service
        self._runner = runner
        self._emit = emit
        self._max_line_bytes = int(max_line_bytes)
        self._protocol_version = int(protocol_version)
        self._requests: dict[str, _Request] = {}
        self._contract_warned: set[str] = set()
        self._contracts_dir = contracts_dir
        self._contract_load_error: str | None = None
        self._contracts = self._load_contracts()
        if self._contracts is None:
            self._event(
                "log",
                True,
                level="ERROR",
                message=self._contract_load_error or "IPC 契约不可用；运行时 schema 校验已停用。",
            )
        # 执行与事件回调都由本对象接管：它知道每条作业对应的原始请求参数，
        # 也知道终态该回给哪个 id。执行器因此保持"只排队、只报状态"的纯粹。
        self._runner._on_event = self._on_job_event  # noqa: SLF001 - 刻意注入
        self._runner.set_executor(self._execute_job)

    # -------------------------------------------------------------- 契约校验

    def _load_contracts(self) -> Any:
        """Load the shared contract registry without making optional validation fatal."""
        try:
            from contract_validation import ContractRegistry  # noqa: PLC0415

            if self._contracts_dir is not None:
                return ContractRegistry(self._contracts_dir)
            return ContractRegistry()
        except Exception as error:  # noqa: BLE001 - validation is optional in compatibility mode
            self._contract_load_error = (
                f"IPC 契约不可用（{type(error).__name__}）；运行时 schema 校验已停用。"
            )
            return None

    def _report_contract_problem(self, problem: str) -> None:
        if not problem or problem in self._contract_warned:
            return
        if len(self._contract_warned) >= _CONTRACT_WARN_LIMIT:
            return
        self._contract_warned.add(problem)
        # Use the event emitter but skip re-validating its diagnostic payload.
        self._event(
            "log",
            True,
            level="WARNING",
            message=f"契约校验：{problem}",
        )

    def _check_outbound(
        self,
        payload: dict[str, Any],
        *,
        command: str | None = None,
        async_accepted: bool = False,
    ) -> str:
        if self._contracts is None:
            return "IPC contract registry unavailable" if CONTRACT_ENFORCE else ""
        try:
            self._contracts.validate_outbound(
                payload,
                command=command,
                async_accepted=async_accepted,
            )
        except Exception as error:  # noqa: BLE001 -含 ContractViolation 与加载错误
            return f"出站消息不符合契约（{type(error).__name__}）：{error}"
        return ""

    def _check_request_envelope(self, message: dict[str, Any]) -> str:
        if self._contracts is None:
            return "IPC contract registry unavailable" if CONTRACT_ENFORCE else ""
        try:
            self._contracts.validate_request_envelope(message)
        except Exception as error:  # noqa: BLE001
            return f"入站命令信封不符合契约（{type(error).__name__}）：{error}"
        return ""

    def _check_inbound(self, command: str, args: dict[str, Any]) -> str:
        if self._contracts is None:
            return "IPC contract registry unavailable" if CONTRACT_ENFORCE else ""
        try:
            self._contracts.validate_args(command, args)
        except Exception as error:  # noqa: BLE001
            return f"命令 {command} 的参数不符合契约（{type(error).__name__}）：{error}"
        return ""

    def _send(
        self,
        payload: dict[str, Any],
        *,
        command: str | None = None,
        async_accepted: bool = False,
    ) -> None:
        """Validate and emit one response or event from the production boundary."""
        problem = self._check_outbound(
            payload,
            command=command,
            async_accepted=async_accepted,
        )
        if problem:
            self._report_contract_problem(problem)
            if CONTRACT_ENFORCE:
                if "id" in payload:
                    payload = {
                        "id": payload["id"],
                        "ok": False,
                        "code": "contract_violation",
                        "error": problem,
                    }
                elif "event" in payload:
                    return
        self._emit(payload)

    def _execute_job(self, command: str, _args: Any, job: Job) -> Any:
        """在 worker 线程上执行一条作业。"""
        request = self._requests.get(job.id)
        return self._service.handle(command, request.args if request else {})

    # ------------------------------------------------------------------ 对外

    def serve(self, lines: Iterable[str | bytes]) -> None:
        """主读循环。生产环境使用二进制 stdin，并在分配整行前限制读取长度。

        每一行都独立处理：单条命令失败绝不能杀死进程（原实现的既有约定）。
        可迭代测试夹具仍按逐项处理；文件流则用有界 ``readline``，超限时丢弃
        当前行的剩余部分，避免把其尾部误当成下一条请求。
        """
        readline = getattr(lines, "readline", None)
        if callable(readline):
            while True:
                raw = readline(self._max_line_bytes + 1)
                if raw == b"" or raw == "":
                    return
                if self._stream_chunk_exceeds_limit(raw):
                    self._discard_stream_line_remainder(readline, raw)
                    self._reject(
                        None,
                        f"协议消息过大（至少 {self._max_line_bytes + 1} 字节，上限 {self._max_line_bytes} 字节）",
                        code="message_too_large",
                    )
                    continue
                self._process_input_line(raw)

        for raw in lines:
            self._process_input_line(raw)

    def _stream_chunk_exceeds_limit(self, raw: str | bytes) -> bool:
        if isinstance(raw, bytes):
            return len(raw) > self._max_line_bytes
        if len(raw) > self._max_line_bytes:
            return True
        try:
            return len(raw.encode("utf-8")) > self._max_line_bytes
        except UnicodeEncodeError:
            return False  # handle_line 报 bad_encoding；已读字符数仍有界

    @staticmethod
    def _discard_stream_line_remainder(
        readline: Callable[[int], str | bytes], first_chunk: str | bytes,
    ) -> None:
        newline = b"\n" if isinstance(first_chunk, bytes) else "\n"
        if first_chunk.endswith(newline):
            return
        while True:
            chunk = readline(8192)
            if not chunk or chunk.endswith(newline):
                return

    def _process_input_line(self, raw: str | bytes) -> None:
        try:
            self.handle_line(raw)
        except SystemExit:
            raise
        except BaseException as error:  # noqa: BLE001 - 最后一道保险
            self._event("log", level="ERROR",
                        message=f"处理输入行时异常: {type(error).__name__}: {error}")

    def handle_line(self, raw: str | bytes | None) -> None:
        """处理一行输入。测试直接调它，不需要起进程。"""
        parsed = self._parse_line(raw)
        if parsed is None:
            return  # 解析/结构校验失败，已在 _parse_line 里回执
        req_id, command, args = parsed
        if not self._admissible(req_id, command, args):
            return  # 命令不存在或参数不符合契约，已回执/告警
        if command in CONTROL_COMMANDS:
            self._run_inline(req_id, command, args)
            return
        self._submit(req_id, command, args)

    def _parse_line(self, raw: str | bytes | None) -> tuple[Any, str, dict[str, Any]] | None:
        """解析 + 结构校验一行。任一环节不通过就回执并返回 ``None``。

        单独拆出来有两个原因：``handle_line`` 的分支已经顶到复杂度预算，
        再往里加判断就是苟着写；而且"哪些输入算畸形"属于协议层规则，
        单独一个函数才答得清楚。
        """
        if raw is None:
            self._reject(None, "协议消息为空", code="empty_message")
            return None

        if isinstance(raw, bytes):
            if len(raw) > self._max_line_bytes:
                self._reject(
                    None, f"协议消息过大（{len(raw)} 字节，上限 {self._max_line_bytes} 字节）",
                    code="message_too_large",
                )
                return None
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._reject(None, "协议消息不是有效的 UTF-8", code="bad_encoding")
                return None
        else:
            text = str(raw)
        try:
            size_bytes = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            self._reject(None, "协议消息包含无法编码为 UTF-8 的内容", code="bad_encoding")
            return None
        if size_bytes > self._max_line_bytes:
            self._reject(
                None, f"协议消息过大（{size_bytes} 字节，上限 {self._max_line_bytes} 字节）",
                code="message_too_large",
            )
            return None
        text = text.strip()
        if not text:
            return None

        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            # 保留既有行为：坏行只记日志，不回执（可能连 id 都没有）。
            self._event("log", level="WARNING",
                        message=f"无法解析的输入: {text[:200]}")
            return None
        except (ValueError, TypeError) as error:
            self._event("log", level="WARNING",
                        message=f"无法解析的输入（{type(error).__name__}）: {text[:200]}")
            return None

        return self._split_request(message)

    def _split_request(self, message: Any) -> tuple[Any, str, dict[str, Any]] | None:
        """把已解析的 JSON 拆成 (id, command, args)，不合法就回执并返回 None。"""
        if not isinstance(message, dict):
            self._reject(None, f"协议消息必须是对象，收到 {type(message).__name__}",
                         code="not_an_object")
            return None

        req_id = message.get("id")
        if req_id is None:
            self._reject(None, "协议消息缺少 id", code="missing_id")
            return None

        command = str(message.get("command") or "").strip()
        if not command:
            self._reject(req_id, "协议消息缺少 command", code="missing_command")
            return None

        args = message.get("args")
        if args is not None and not isinstance(args, dict):
            self._reject(req_id, f"args 必须是对象，收到 {type(args).__name__}",
                         code="bad_args")
            return None

        problem = self._check_request_envelope(message)
        if problem:
            self._report_contract_problem(problem)
            if CONTRACT_ENFORCE:
                response_id = req_id if type(req_id) is int else None
                self._reject(response_id, problem, code="contract_violation")
                return None
        return req_id, command, args or {}

    def _admissible(self, req_id: Any, command: str,
                    args: dict[str, Any]) -> bool:
        """命令是否存在 + 参数是否符合共享契约。

        不通过时已经回执（或按 ``CONTRACT_ENFORCE`` 只告警），调用方直接返回。
        """
        if not self._is_known_command(command):
            self._reject(req_id, f"未知命令: {command}", code="unknown_command")
            return False

        problem = self._check_inbound(command, args)
        if problem:
            self._report_contract_problem(problem)
            if CONTRACT_ENFORCE:
                self._reject(req_id, problem, code="contract_violation")
                return False
        return True

    # ------------------------------------------------------------------ 内部

    def _is_known_command(self, command: str) -> bool:
        checker = getattr(self._service, "has_command", None)
        if callable(checker):
            return bool(checker(command))
        return command in {"ping", "shutdown"} or hasattr(
            self._service, f"_cmd_{command}")

    def _run_inline(self, req_id: Any, command: str, args: dict[str, Any]) -> None:
        """控制命令：立刻在读循环线程上执行，绝不排队。"""
        try:
            data = self._service.handle(command, args)
        except Exception as error:  # noqa: BLE001 - 单条命令失败不能杀死进程
            self._send({"id": req_id, "ok": False, "error": f"{type(error).__name__}: {error}"})
            self._event("log", level="ERROR", message=traceback.format_exc())
        else:
            self._send({"id": req_id, "ok": True, "data": data}, command=command)

    def _submit(self, req_id: Any, command: str, args: dict[str, Any]) -> None:
        wants_async = bool(args.get("async"))

        async_ack: dict[str, Any] | None = None

        def register_request(job: Job) -> None:
            nonlocal async_ack
            # Validate before JobRunner makes the task visible to its worker.
            if wants_async:
                async_ack = {
                    "id": req_id,
                    "ok": True,
                    "data": {
                        "jobId": job.id,
                        "accepted": True,
                        "status": job.status,
                    },
                }
                problem = self._check_outbound(async_ack, async_accepted=True)
                if problem:
                    self._report_contract_problem(problem)
                    if CONTRACT_ENFORCE:
                        raise _AsyncAckContractRejected(problem)
            # 先建立 jobId → 请求映射，再让 worker 看见队列项。
            # 否则极快任务会用空 args 执行，并在 _complete() 时丢掉终态回复。
            self._requests[job.id] = _Request(
                req_id=req_id, args=args, wants_async=wants_async,
            )
        try:
            job = self._runner.submit(
                command,
                args,
                on_queued=register_request,
                reject_if_active=(command == "start_migration"),
            )
        except _AsyncAckContractRejected as error:
            self._reject(req_id, str(error), code="contract_violation")
            return
        except ActiveJobExists as error:
            self._reject(
                req_id, str(error), code="active_job_exists",
                job_id=error.job_id,
            )
            return
        except QueueFull as error:
            self._reject(req_id, str(error), code="queue_full")
            return



        if wants_async:
            # The acknowledgement was validated from the pre-enqueue snapshot.
            # Keep that exact state even if the worker finishes before submit returns.
            assert async_ack is not None
            self._send(async_ack, async_accepted=True)

    def _on_job_event(self, kind: str, payload: dict[str, Any]) -> None:
        """worker 线程回调：转发事件；终态时补上对应请求的应答。"""
        job_id = str(payload.get("jobId", ""))
        request = self._requests.get(job_id)
        if (
            request is not None
            and request.wants_async
            and payload.get("command") == "start_migration"
        ):
            client_migration_id = request.args.get("clientMigrationId")
            if isinstance(client_migration_id, str) and 0 < len(client_migration_id) <= 128:
                payload = {**payload, "clientMigrationId": client_migration_id}
                if payload.get("status") == SUCCEEDED:
                    # 迁移报告此前只随同步响应返回。显式异步后必须在终态事件携带报告，
                    # 否则 Renderer 的请求硬上限会丢掉最终结果，退出保护无法收尾。
                    # 不把其他命令的 result 暴露到事件总线上，避免泄出未知敏感结果。
                    job = self._runner.get(job_id)
                    result = job.result if job is not None and isinstance(job.result, dict) else {
                        "done": [],
                        "failed": [{"key": "?", "error": "后端未返回有效迁移报告"}],
                        "elapsedMs": 0,
                        "configUpdates": {},
                        "ok": False,
                    }
                    payload = {**payload, "result": result}
        self._event(kind, **payload)
        # 用 status（状态机的权威字段），不是 action —— 同名两字段是静默漂移的温床。
        if payload.get("status") in TERMINAL:
            self._complete(job_id)

    def _complete(self, job_id: str) -> None:
        request = self._requests.pop(job_id, None)
        if request is None or request.wants_async:
            return  # 异步请求已经回过执了，终态靠 job 事件送达
        job = self._runner.get(job_id)
        if job is None:
            return
        if job.status == SUCCEEDED:
            self._send({"id": request.req_id, "ok": True, "data": job.result,
                        "jobId": job.id}, command=job.command)
        elif job.status == CANCELLED:
            # 取消是明确终态，带可识别错误码，前端据此区分"失败"与"取消"。
            self._send({"id": request.req_id, "ok": False, "jobId": job.id,
                        "code": "cancelled", "error": job.error or "已取消"})
        else:
            self._send({"id": request.req_id, "ok": False, "jobId": job.id,
                        "code": job.error_code or "failed",
                        "error": job.error or "任务失败"})

    def _reject(
        self, req_id: Any, detail: str, *, code: str = "bad_request",
        job_id: str | None = None,
    ) -> None:
        self._event("log", level="WARNING", message=detail)
        response_id = req_id if req_id is None or type(req_id) is int else None
        reply: dict[str, Any] = {
            "id": response_id, "ok": False, "code": code, "error": detail
        }
        if job_id is not None:
            reply["jobId"] = job_id
        self._send(reply)

    def _event(
        self,
        kind: str,
        skip_validation: bool = False,
        /,
        **fields: Any,
    ) -> None:
        """Build an event; validator diagnostics skip only their own re-validation."""
        if kind == "log":
            fields.setdefault("ts", _now_iso())
        payload = {"event": kind, **fields}
        if skip_validation:
            self._emit(payload)
        else:
            self._send(payload)

    def emit_producer_event(self, payload: dict[str, Any]) -> None:
        """Route module-level handler/Pipeline events through this runtime validator."""
        forwarded = dict(payload)
        if forwarded.get("event") == "log":
            forwarded.setdefault("ts", _now_iso())
        self._send(forwarded)

    # ------------------------------------------------------------------ 握手

    def handshake(self, *, version: str, frozen: bool, backend_generation: str = "") -> None:
        """发 ready 事件。

        工作单 §3.6 要求握手能支撑"渲染层重载后重新同步"，因此除了版本，
        还要带协议版本、backend generation、就绪状态和当前活动任务快照。
        """
        self._event(
            "ready",
            version=version,
            frozen=bool(frozen),
            protocolVersion=self._protocol_version,
            backendGeneration=str(backend_generation),
            readiness={"ready": True, "activeJobs": self._runner.active_job_names()},
            session=self._session_snapshot(),
        )

    def _session_snapshot(self) -> Any:
        state = getattr(self._service, "_state_payload", None)
        pipeline = getattr(self._service, "_pipeline", None)
        if callable(state) and pipeline is not None:
            try:
                return state(pipeline)
            except Exception:  # noqa: BLE001 - 握手不能因为状态取不到而失败
                return None
        return None

    # ------------------------------------------------------------------ 取消

    def cancel(self, job_id: str) -> dict[str, Any]:
        """请求取消一个任务；``cancelling`` 不等于 ``cancelled``。"""
        try:
            return self._runner.cancel(job_id)
        except UnknownJob as error:
            return {"ok": False, "code": "unknown_job", "detail": str(error)}

    def job_status(self, job_id: str) -> dict[str, Any]:
        job: Job | None = self._runner.get(job_id)
        if job is None:
            return {"ok": False, "code": "unknown_job", "detail": f"没有这个任务：{job_id}"}
        return {"ok": True, "job": job.snapshot()}
