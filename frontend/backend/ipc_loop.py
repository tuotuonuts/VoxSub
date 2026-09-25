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
from typing import Any, Callable, Iterable

from job_runner import (
    CANCELLED, CONTROL_COMMANDS, FAILED, SUCCEEDED, TERMINAL, Job, JobRunner,
    QueueFull, UnknownJob,
)

#: 单行消息上限。OCR 会传 base64 图片，所以给得比较宽 ——
#: 但仍然有界，防止畸形输入把内存吃光。
MAX_LINE_CHARS = 32 * 1024 * 1024

#: 协议版本。握手要带它，好让前端知道自己对接的是哪一代协议。
PROTOCOL_VERSION = 1

#: 契约校验模式。
#:
#: 为什么默认只警告、不拦：工作单 §3.6 允许分两步走 —— 第一步集中定义 + 一致性
#: 测试（已完成），第二步才是运行时校验。如果这一版契约有偏差就硬拒绝，后果是
#: **用户的功能直接不可用** —— 契约写错不该比没有契约更糟。所以默认把不一致
#: 记成日志、暴露出来，等出站方向零误报之后再翻成 True。
#:
#: 翻成 True 之前要满足：连续跑通全量测试 + 一次打包版冒烟，且出站零告警。
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


class IpcLoop:
    """读循环 + 分派。构造参数全部注入，便于离线测试。"""

    def __init__(
        self,
        service: Any,
        runner: JobRunner,
        emit: Callable[[dict[str, Any]], None],
        *,
        max_line_chars: int = MAX_LINE_CHARS,
        protocol_version: int = PROTOCOL_VERSION,
    ) -> None:
        self._service = service
        self._runner = runner
        self._emit = emit
        self._max_line_chars = int(max_line_chars)
        self._protocol_version = int(protocol_version)
        self._requests: dict[str, _Request] = {}
        self._contract_warned: set[str] = set()
        self._contracts = self._load_contracts()
        # 执行与事件回调都由本对象接管：它知道每条作业对应的原始请求参数，
        # 也知道终态该回给哪个 id。执行器因此保持"只排队、只报状态"的纯粹。
        self._runner._on_event = self._on_job_event  # noqa: SLF001 - 刻意注入
        self._runner.set_executor(self._execute_job)

    # -------------------------------------------------------------- 契约校验

    def _load_contracts(self) -> Any:
        """尽力加载共享契约；加载不到就当没有（**不因此让后端起不来**）。

        打包版里 ``contracts/`` 不在 bundle 内，这里会失败 —— 也就是说运行时的
        契约校验在打包版上是静默关闭的。这是本轮已知且接受的限制：契约的价值
        主要在开发期（一致性测试），而不是给用户装一道可能误伤的门。要让它
        在打包版生效，需要把 ``contracts/`` 一并打进 bundle。
        """
        try:
            from contract_validation import ContractRegistry  # noqa: PLC0415

            return ContractRegistry()
        except Exception:  # noqa: BLE001 - 契约缺失/损坏不能拖垮读循环
            return None

    def _report_contract_problem(self, problem: str) -> None:
        if not problem or problem in self._contract_warned:
            return
        if len(self._contract_warned) >= _CONTRACT_WARN_LIMIT:
            return
        self._contract_warned.add(problem)
        # 经 _event 发（它不校验，见其文档字符串）—— 告警自己不该再触发校验。
        self._event("log", level="WARNING", message=f"契约校验：{problem}")

    def _check_outbound(self, payload: dict[str, Any]) -> str:
        if self._contracts is None:
            return ""
        try:
            self._contracts.validate_outbound(payload)
        except Exception as error:  # noqa: BLE001 - 含 ContractViolation 与加载错误
            return f"出站消息不符合契约（{type(error).__name__}）：{error}"
        return ""

    def _check_inbound(self, command: str, args: dict[str, Any]) -> str:
        if self._contracts is None:
            return ""
        try:
            self._contracts.validate_args(command, args)
        except Exception as error:  # noqa: BLE001
            return f"命令 {command} 的参数不符合契约（{type(error).__name__}）：{error}"
        return ""

    def _send(self, payload: dict[str, Any]) -> None:
        """**唯一的出站口**：先过契约校验，再写 stdout。

        所有应答与事件都从这里出去，所以"有没有校验"这件事不需要每个调用点
        记得 —— 少一个出站口就等于少一处遗忘。
        """
        problem = self._check_outbound(payload)
        if problem:
            self._report_contract_problem(problem)
            if CONTRACT_ENFORCE and "id" in payload:
                payload = {"id": payload["id"], "ok": False, "code": "contract_violation",
                           "error": problem}
        self._emit(payload)

    def _execute_job(self, command: str, _args: Any, job: Job) -> Any:
        """在 worker 线程上执行一条作业。"""
        request = self._requests.get(job.id)
        return self._service.handle(command, request.args if request else {})

    # ------------------------------------------------------------------ 对外

    def serve(self, lines: Iterable[str]) -> None:
        """主读循环。``lines`` 通常是 ``sys.stdin``。

        每一行都独立处理：单条命令失败绝不能杀死进程（原实现的既有约定）。
        """
        for raw in lines:
            try:
                self.handle_line(raw)
            except SystemExit:
                raise
            except BaseException as error:  # noqa: BLE001 - 最后一道保险
                self._event("log", level="ERROR",
                            message=f"处理输入行时异常: {type(error).__name__}: {error}")

    def handle_line(self, raw: str) -> None:
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

    def _parse_line(self, raw: str) -> tuple[Any, str, dict[str, Any]] | None:
        """解析 + 结构校验一行。任一环节不通过就回执并返回 ``None``。

        单独拆出来有两个原因：``handle_line`` 的分支已经顶到复杂度预算，
        再往里加判断就是苟着写；而且"哪些输入算畸形"属于协议层规则，
        单独一个函数才答得清楚。
        """
        if raw is None:
            self._reject(None, "协议消息为空", code="empty_message")
            return None

        text = str(raw)
        if len(text) > self._max_line_chars:
            self._reject(
                None, f"协议消息过大（{len(text)} 字符，上限 {self._max_line_chars}）",
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
            self._send({"id": req_id, "ok": True, "data": data})

    def _submit(self, req_id: Any, command: str, args: dict[str, Any]) -> None:
        wants_async = bool(args.get("async"))

        def register_request(job: Job) -> None:
            # 先建立 jobId → 请求映射，再让 worker 看见队列项。
            # 否则极快任务会用空 args 执行，并在 _complete() 时丢掉终态回复。
            self._requests[job.id] = _Request(
                req_id=req_id, args=args, wants_async=wants_async,
            )
        try:
            job = self._runner.submit(command, args, on_queued=register_request)
        except QueueFull as error:
            self._reject(req_id, str(error), code="queue_full")
            return



        if wants_async:
            # 显式要求异步：立刻回执 jobId，结果靠事件送。
            # 不改变默认（同步）语义，前端可以按自己的节奏迁移过来。
            self._send({"id": req_id, "ok": True,
                        "data": {"jobId": job.id, "accepted": True,
                                 "status": job.status}})

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
                        "jobId": job.id})
        elif job.status == CANCELLED:
            # 取消是明确终态，带可识别错误码，前端据此区分"失败"与"取消"。
            self._send({"id": request.req_id, "ok": False, "jobId": job.id,
                        "code": "cancelled", "error": job.error or "已取消"})
        else:
            self._send({"id": request.req_id, "ok": False, "jobId": job.id,
                        "code": job.error_code or "failed",
                        "error": job.error or "任务失败"})

    def _reject(self, req_id: Any, detail: str, *, code: str = "bad_request") -> None:
        self._event("log", level="WARNING", message=detail)
        self._send({"id": req_id, "ok": False, "code": code, "error": detail})

    def _event(self, kind: str, **fields: Any) -> None:
        """发一个事件。

        **刻意不走 :meth:`_send` 的契约校验**：事件由本对象自己构造，而且校验
        告警本身也要经这条路径发出 —— 再校验一次就递归了。事件形状由
        ``tests/test_contracts.py`` 的覆盖与字段比对负责守住，不靠运行时兜。
        """
        if kind == "log":
            fields.setdefault("ts", _now_iso())
        self._emit({"event": kind, **fields})

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
