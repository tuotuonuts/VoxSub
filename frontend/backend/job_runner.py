"""有界作业执行器 —— 把耗时工作移出主读循环，并给出诚实的任务状态。

为什么需要它（工作单 §3.3）：

  · 主读循环必须保持可响应。此前所有命令都在读循环里**同步执行**，
    于是"迁移跑 3 分钟"期间 `state` / `stop` / 取消全部排在后面，
    管道里的控制命令要等迁移结束才被读到。
  · 耗时任务需要 ``jobId``、进度事件和明确的终态。
  · **请求超时不是任务失败，也不是任务已取消。** 状态只能由后端说了算，
    前端超时只是"这次没等到回话"，不能反过来改写任务状态。

状态语义（本模块是唯一权威定义）：

    queued ──► running ──► succeeded
                    │      failed
                    └──► cancelling ──► cancelled

判定规则：

  · ``cancelling`` **不等于** ``cancelled``。原生推理/复制卷无法立刻打断时，
    只标记"取消中"，等真正到达安全边界才落 ``cancelled``。
  · 只有实际进入终态才解除退出保护。
  · 取消必须能"插队"：它由读循环线程直接处理，不排进作业队列。

实现取舍：

  · **单 worker 串行执行。** 工作单要求"OCR 首先采用单 worker"、
    "迁移与相关目录破坏性操作互斥"，串行是最省心的正确起点；
    等确有多路并行需求再显式声明并发能力。
  · **队列有界**（``MAX_PENDING``）。满了就明确拒绝，而不是无限堆积 ——
    无界执行器会把积压藏起来。
  · **完成记录有界保留**（``MAX_HISTORY``），避免内存无界增长。
  · 不引入 asyncio，延续现有线程模型。
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

#: 待执行队列上限。超过就拒绝新任务（明确错误码），不无限堆积。
MAX_PENDING = 16

#: 已完成任务保留条数。超出后丢最旧的。
MAX_HISTORY = 50

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLING = "cancelling"
CANCELLED = "cancelled"

#: 终态。进入其中之一才算"任务真的结束了"。
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})

#: 仍然占用资源、需要计入退出保护的状态。
ACTIVE = frozenset({QUEUED, RUNNING, CANCELLING})

#: 这些命令**永远**在读循环线程上同步执行，不排进作业队列。
#: 依据（§3.3）：参数校验、状态查询、任务提交要快速返回；
#: "停止和取消不排在普通耗时任务后面"。
CONTROL_COMMANDS = frozenset({
    "ping",
    "state",
    "shutdown",
    "job_status",
    "job_list",
    "cancel_job",
})


class QueueFull(RuntimeError):
    """待执行队列已满，拒绝接收新任务。"""


class UnknownJob(KeyError):
    """引用了不存在的 jobId。"""


#: 当前 worker 正在执行的任务，供命令实现协作式检查取消。
_local = threading.local()


def current_job() -> "Job | None":
    """返回当前 worker 线程正在执行的任务（不在 worker 线程则为 None）。"""
    return getattr(_local, "job", None)


def cancel_requested() -> bool:
    """当前任务是否已被请求取消。

    给长任务实现用：在安全边界（例如迁移的每一步之间）检查这个值，
    就能做到"取消不排在耗时任务后面"且不破坏数据。
    """
    job = current_job()
    return bool(job and job.cancel_event.is_set())


@dataclass
class Job:
    """一条后台任务。"""

    id: str
    command: str
    status: str = QUEUED
    sequence: int = 0
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str = ""
    error_code: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _seq_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def next_sequence(self) -> int:
        """事件顺序号。让前端能丢弃迟到/乱序的事件。"""
        with self._seq_lock:
            self.sequence += 1
            return self.sequence

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL

    def snapshot(self) -> dict[str, Any]:
        """对外可见的任务快照。**不包含** args / result 原文，避免泄露密钥。"""
        payload: dict[str, Any] = {
            "jobId": self.id,
            "command": self.command,
            "status": self.status,
            "sequence": self.sequence,
        }
        if self.error:
            payload["error"] = self.error
        if self.error_code:
            payload["code"] = self.error_code
        if self.started_at is not None and self.finished_at is not None:
            payload["elapsedMs"] = int((self.finished_at - self.started_at) * 1000)
        return payload


class JobRunner:
    """单 worker 的有界作业执行器。

    ``execute`` 的签名是 ``execute(command: str, args: Any, job: Job) -> Any``，
    抛出的异常会被捕获并转成 ``failed`` 终态（不会杀死 worker 线程）。
    """

    def __init__(
        self,
        execute: Callable[[str, Any, Job], Any] | None = None,
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        max_pending: int = MAX_PENDING,
        max_history: int = MAX_HISTORY,
    ) -> None:
        self._execute = execute
        self._on_event = on_event
        self._max_pending = max(1, int(max_pending))
        self._max_history = max(1, int(max_history))

        self._lock = threading.RLock()
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._pending: deque[str] = deque()
        self._wake = threading.Event()
        self._stopping = False
        self._worker: threading.Thread | None = None

    def set_executor(self, execute: Callable[[str, Any, Job], Any]) -> None:
        """注入执行函数。

        ``execute`` 留成可后置注入，是因为真正干活的入口在
        :class:`ipc_loop.IpcLoop` —— 它知道每条作业对应的原始请求参数。
        这样执行器本身仍然可以脱离协议层单独测试。
        """
        self._execute = execute

    # -------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """启动 worker 线程（幂等）。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stopping = False
            self._worker = threading.Thread(
                target=self._run, name="voxsub-job-worker", daemon=True,
            )
            self._worker.start()

    def stop(self, timeout: float = 2.0) -> None:
        """停止 worker。**不**打断正在执行的任务 —— 只阻止后续任务。"""
        with self._lock:
            self._stopping = True
            self._wake.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)

    # -------------------------------------------------------------- 提交/查询

    def submit(
        self,
        command: str,
        args: Any,
        *,
        on_queued: Callable[[Job], None] | None = None,
    ) -> Job:
        """提交一个任务，立即返回 ``Job``（此时状态为 queued 或 running）。

        ``on_queued`` 在任务对 worker 可见之前调用，供协议层先登记请求关联；
        回调应保持轻量且不得重入此 runner。
        """
        job = Job(id=uuid.uuid4().hex, command=str(command))
        with self._lock:
            if len(self._pending) >= self._max_pending:
                raise QueueFull(
                    f"后台任务队列已满（{self._max_pending} 个待执行），请稍后再试"
                )
            if on_queued is not None:
                on_queued(job)
            self._jobs[job.id] = job
            self._trim_history_locked()
            self._pending.append(job.id)
        self._wake.set()
        self._emit("job", job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(str(job_id or ""))

    def list_jobs(self, *, active_only: bool = False) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if active_only:
            jobs = [job for job in jobs if job.status in ACTIVE]
        return jobs

    def has_active_jobs(self) -> bool:
        """是否仍有未结束的任务 —— 退出保护只看这个。"""
        return any(job.status in ACTIVE for job in self.list_jobs())

    def active_job_names(self) -> list[str]:
        return [job.command for job in self.list_jobs(active_only=True)]

    # -------------------------------------------------------------- 取消

    def cancel(self, job_id: str) -> dict[str, Any]:
        """请求取消。

        ``cancelling`` 只是"收到取消请求"，不等于已取消：真正的
        ``cancelled`` 由 worker 在安全边界落定。已经结束的任务不能被改写。
        """
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if job is None:
                raise UnknownJob(f"没有这个任务：{job_id}")
            if job.status in TERMINAL:
                return {"ok": False, "status": job.status,
                        "code": "already_finished",
                        "detail": f"任务已结束（{job.status}），无法取消"}
            job.cancel_event.set()
            if job.status != CANCELLING:
                job.status = CANCELLING
                self._emit("job", job)
            return {"ok": True, "status": CANCELLING}

    # -------------------------------------------------------------- 内部

    def _trim_history_locked(self) -> None:
        """丢掉最旧的**终态**记录，保持有界。绝不丢还活着的任务。"""
        if len(self._jobs) <= self._max_history:
            return
        for job_id in list(self._jobs.keys()):
            if len(self._jobs) <= self._max_history:
                break
            job = self._jobs[job_id]
            if job.status in TERMINAL:
                del self._jobs[job_id]

    def _next_pending(self) -> Job | None:
        with self._lock:
            while self._pending:
                job = self._jobs.get(self._pending.popleft())
                if job is not None:
                    return job
            return None

    def _emit(self, kind: str, job: Job, **fields: Any) -> None:
        if self._on_event is None:
            return
        # 线上字段只有 `status`（状态机的权威词汇），不另发一个 `action` 同义词 ——
        # 两个名字描述同一件事时，两边迟早会漂移，而 UI 读错名字是**静默**的
        # （TS 里字段可选，读不到就是 undefined，不报错）。这条规则是契约
        # `uiFieldDrift` 清单里唯一被标为"最该先修"的一条。
        payload: dict[str, Any] = {
            "jobId": job.id,
            "command": job.command,
            "status": job.status,
            "sequence": job.next_sequence(),
        }
        payload.update({key: value for key, value in fields.items() if value != ""})
        try:
            self._on_event(kind, payload)
        except Exception:  # noqa: BLE001 - 事件发不出去不能影响任务本身
            pass

    def _run(self) -> None:
        while True:
            job = self._next_pending()
            if job is None:
                if self._stopping:
                    return
                self._wake.wait(timeout=0.25)
                self._wake.clear()
                continue

            # 取消与"正式开始"必须在同一把锁下仲裁：
            # 若取消先取得锁，任务直接取消、不调用 executor；若 worker 先取得锁，
            # 状态先成为 running，之后的取消就是普通的协作式运行中取消，不能回退状态。
            with self._lock:
                cancelled_before_start = job.cancel_event.is_set()
                if not cancelled_before_start:
                    job.status = RUNNING
                    job.started_at = time.monotonic()
            if cancelled_before_start:
                self._finish(job, CANCELLED, error="已取消", error_code="cancelled")
                continue
            self._emit("job", job)

            _local.job = job
            try:
                if self._execute is None:
                    raise RuntimeError("作业执行器未注入 execute，无法执行任务")
                result = self._execute(job.command, None, job)
            except BaseException as error:  # noqa: BLE001 - worker 不能被单任务打死
                if job.cancel_event.is_set():
                    # 取消导致的异常：落 cancelled，不伪装成业务失败。
                    self._finish(job, CANCELLED, error="已取消", error_code="cancelled")
                else:
                    self._finish(job, FAILED, error=f"{type(error).__name__}: {error}",
                                 error_code=type(error).__name__)
            else:
                # 终态与 cancel() 必须由 _finish 在同一把锁下仲裁；否则取消可以
                # 落在“最后一次检查后、成功提交前”并被成功状态覆盖。
                self._finish(job, SUCCEEDED, result=result)
            finally:
                _local.job = None

    def _finish(self, job: Job, status: str, *, result: Any = None, error: str = "",
                error_code: str = "") -> None:
        with self._lock:
            if job.status in TERMINAL:
                return
            # cancel() and the terminal commit share this lock. If cancellation
            # was accepted first, neither success nor failure may overwrite it.
            if status != CANCELLED and job.cancel_event.is_set():
                status = CANCELLED
                result = None
                error = "已取消"
                error_code = "cancelled"
            job.status = status
            job.finished_at = time.monotonic()
            job.result = result if status == SUCCEEDED else None
            if error:
                job.error = error
            if error_code:
                job.error_code = error_code
            self._trim_history_locked()
        self._emit("job", job, error=job.error, code=job.error_code)

    # -------------------------------------------------------------- 测试辅助

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """等到没有活着的任务。仅测试/退出协调使用。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.has_active_jobs():
                return True
            time.sleep(0.01)
        return False

    def iter_jobs(self) -> Iterable[Job]:
        return self.list_jobs()
