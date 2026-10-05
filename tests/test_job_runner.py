"""后台作业执行器 —— 状态机与取消语义的逐条测试。

工作单 §3.3 把"任务状态必须诚实"列为硬要求，所以这里重点钉三件事：

  1. ``cancelling`` 不等于 ``cancelled``：收到取消请求后，只有真正到达
     安全边界才落终态。
  2. 请求超时不是失败、也不是取消：执行器本身没有"客户端超时"这个概念，
     任务的状态只能由任务自己走完。
  3. 只有实际进入终态才解除退出保护（:meth:`JobRunner.has_active_jobs`）。

并发测试一律用 Event/轮询等确定性手段，不用 sleep 猜时间。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "frontend" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import job_runner  # noqa: E402


@pytest.fixture()
def runner():
    made = job_runner.JobRunner()
    yield made
    made.stop(timeout=2.0)


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.005) -> bool:
    """确定性等待：轮询条件而不是猜 sleep 多久。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ------------------------------------------------------------------ 基本状态机

def test_successful_job_walks_queued_running_succeeded(runner):
    seen: list[str] = []
    runner._on_event = lambda kind, payload: seen.append(payload["status"])
    runner.set_executor(lambda command, args, job: {"echo": command})
    runner.start()

    job = runner.submit("ping_work", {})
    assert runner.wait_for_idle(5.0) is True

    assert job.status == job_runner.SUCCEEDED
    assert job.result == {"echo": "ping_work"}
    assert seen == ["queued", "running", "succeeded"]


def test_job_event_uses_one_field_name_for_state(runner):
    """事件里描述状态只允许一个字段名：``status``。

    回归背景：早先发的是 ``action``，而前端 TS 读的是 ``status`` ——
    字段可选，读不到就是 undefined，**不报错**。同一个东西两个名字，
    两边必然漂移，而且坏得很安静。
    """
    payloads: list[dict] = []
    runner._on_event = lambda kind, payload: payloads.append(payload)
    runner.set_executor(lambda command, args, job: "ok")
    runner.start()
    runner.submit("work", {})
    assert runner.wait_for_idle(5.0) is True

    assert payloads, "应当至少发出 queued 事件"
    for payload in payloads:
        assert "status" in payload
        assert "action" not in payload, "不要再发同义字段，避免与 status 漂移"


def test_failure_is_reported_with_identifiable_code(runner):
    def boom(command, args, job):
        raise ValueError("参数不对")

    runner.set_executor(boom)
    runner.start()
    job = runner.submit("bad", {})

    assert runner.wait_for_idle(5.0) is True
    assert job.status == job_runner.FAILED
    assert job.error_code == "ValueError"
    assert "参数不对" in job.error
    assert job.finished is True


def test_worker_survives_a_failing_job(runner):
    """单条任务失败不能打死 worker，后面排队的还要继续跑。"""
    calls: list[str] = []

    def sometimes(command, args, job):
        calls.append(command)
        if command == "boom":
            raise RuntimeError("x")
        return "ok"

    runner.set_executor(sometimes)
    runner.start()
    first = runner.submit("boom", {})
    second = runner.submit("fine", {})

    assert runner.wait_for_idle(5.0) is True
    assert first.status == job_runner.FAILED
    assert second.status == job_runner.SUCCEEDED
    assert calls == ["boom", "fine"]


def test_no_executor_injected_fails_loudly(runner):
    runner.start()
    job = runner.submit("anything", {})
    assert runner.wait_for_idle(5.0) is True
    assert job.status == job_runner.FAILED
    assert "未注入" in job.error


# ------------------------------------------------------------------ 取消语义

def test_cancel_queued_job_never_runs(runner):
    """排队期间被取消的任务根本不执行 —— 不浪费资源，也不产生副作用。"""
    gate = threading.Event()
    ran: list[str] = []

    def blocking(command, args, job):
        ran.append(command)
        gate.wait(timeout=5.0)
        return "done"

    runner.set_executor(blocking)
    runner.start()

    running = runner.submit("first", {})
    assert _wait_until(lambda: running.status == job_runner.RUNNING)
    queued = runner.submit("second", {})

    result = runner.cancel(queued.id)
    assert result["ok"] is True
    assert result["status"] == job_runner.CANCELLING

    gate.set()
    assert runner.wait_for_idle(5.0) is True

    assert queued.status == job_runner.CANCELLED
    assert ran == ["first"], "被取消的排队任务不能真的执行"


def test_cancel_after_worker_check_never_moves_status_back_to_running(runner):
    """取消与 worker 取出任务并发时，状态不能从 cancelling 回退到 running。"""
    check_started = threading.Event()
    allow_work = threading.Event()
    cancel_finished = threading.Event()
    executor_started = threading.Event()
    seen: list[str] = []
    runner._on_event = lambda kind, payload: seen.append(payload["status"])

    class GatedCancelEvent:
        def __init__(self):
            self._event = threading.Event()
            self._first_check = True

        def is_set(self):
            result = self._event.is_set()
            if self._first_check:
                self._first_check = False
                check_started.set()
                # Buggy code checks outside the runner lock, so cancel completes
                # here and the worker subsequently overwrites CANCELLING.
                cancel_finished.wait(timeout=0.2)
            return result

        def set(self):
            self._event.set()

    def execute(command, args, job):
        executor_started.set()
        allow_work.wait(timeout=2.0)
        return "done"

    runner.set_executor(execute)
    job = runner.submit("gated", {})
    job.cancel_event = GatedCancelEvent()
    original_cancel = runner.cancel

    def observed_cancel(job_id):
        result = original_cancel(job_id)
        cancel_finished.set()
        return result

    runner.cancel = observed_cancel
    runner.start()
    assert check_started.wait(timeout=2.0)

    cancel_thread = threading.Thread(target=lambda: runner.cancel(job.id), daemon=True)
    cancel_thread.start()
    cancel_thread.join(timeout=2.0)
    assert not cancel_thread.is_alive(), "取消请求未完成"
    assert executor_started.wait(timeout=2.0)
    allow_work.set()
    assert runner.wait_for_idle(2.0)

    first_cancelling = seen.index(job_runner.CANCELLING)
    assert job_runner.RUNNING not in seen[first_cancelling + 1:], (
        f"任务状态发生回退：{seen}"
    )
    assert job.status == job_runner.CANCELLED


def test_cancel_wins_if_accepted_before_terminal_commit(runner):
    """取消与成功终态提交并发时，先被接受的取消必须赢。"""
    at_terminal_commit = threading.Event()
    allow_terminal_commit = threading.Event()
    runner.set_executor(lambda command, args, job: "completed")
    original_finish = runner._finish

    def pause_before_finish(job, status, *, result=None, error="", error_code=""):
        if status == job_runner.SUCCEEDED:
            at_terminal_commit.set()
            assert allow_terminal_commit.wait(timeout=2.0)
        original_finish(job, status, result=result, error=error, error_code=error_code)

    runner._finish = pause_before_finish
    runner.start()
    job = runner.submit("finish_race", {})
    assert at_terminal_commit.wait(timeout=2.0)

    result = runner.cancel(job.id)
    assert result == {"ok": True, "status": job_runner.CANCELLING}
    allow_terminal_commit.set()
    assert runner.wait_for_idle(2.0)

    assert job.status == job_runner.CANCELLED
    assert job.result is None, "取消获胜时不得保留/暴露成功结果"


def test_cancelling_is_not_cancelled_while_still_running(runner):
    """核心不变式：收到取消请求 ≠ 已取消。还在跑就只能是 cancelling。"""
    release = threading.Event()

    def slow(command, args, job):
        release.wait(timeout=5.0)
        return "late"

    runner.set_executor(slow)
    runner.start()
    job = runner.submit("slow", {})
    assert _wait_until(lambda: job.status == job_runner.RUNNING)

    runner.cancel(job.id)
    assert job.status == job_runner.CANCELLING
    assert job.status != job_runner.CANCELLED
    assert runner.has_active_jobs() is True, "取消中仍必须占着退出保护"

    release.set()
    assert runner.wait_for_idle(5.0) is True
    assert job.status == job_runner.CANCELLED
    assert job.result is None, "取消后必须丢弃结果，不能既取消又算成功"


def test_cancel_after_finish_does_not_rewrite_status(runner):
    """已结束的任务不能被取消请求改写状态 —— 状态是事实，不是愿望。"""
    runner.set_executor(lambda command, args, job: "ok")
    runner.start()
    job = runner.submit("done", {})
    assert runner.wait_for_idle(5.0) is True
    assert job.status == job_runner.SUCCEEDED

    result = runner.cancel(job.id)
    assert result["ok"] is False
    assert result["code"] == "already_finished"
    assert job.status == job_runner.SUCCEEDED


def test_cancel_is_idempotent(runner):
    release = threading.Event()
    runner.set_executor(lambda command, args, job: release.wait(timeout=5.0))
    runner.start()
    job = runner.submit("slow", {})
    assert _wait_until(lambda: job.status == job_runner.RUNNING)

    first = runner.cancel(job.id)
    second = runner.cancel(job.id)
    assert first["ok"] is True and second["ok"] is True
    assert job.status == job_runner.CANCELLING
    release.set()
    assert runner.wait_for_idle(5.0) is True


def test_cancel_unknown_job_raises(runner):
    with pytest.raises(job_runner.UnknownJob):
        runner.cancel("no-such-job")


def test_exception_during_cancelled_job_reports_cancelled_not_failed(runner):
    """取消导致的中断不能伪装成业务失败。"""
    started = threading.Event()
    release = threading.Event()

    def slow(command, args, job):
        started.set()
        release.wait(timeout=5.0)
        raise RuntimeError("被打断了")

    runner.set_executor(slow)
    runner.start()
    job = runner.submit("slow", {})
    assert started.wait(timeout=5.0)

    runner.cancel(job.id)
    release.set()
    assert runner.wait_for_idle(5.0) is True
    assert job.status == job_runner.CANCELLED
    assert job.error_code == "cancelled"


def test_cooperative_cancel_flag_visible_inside_job(runner):
    """任务实现要能在安全边界自己查到"被取消了吗"。"""
    observed: list[bool] = []

    def cooperative(command, args, job):
        observed.append(job_runner.cancel_requested())
        return "ok"

    runner.set_executor(cooperative)
    runner.start()
    runner.submit("peek", {})
    assert runner.wait_for_idle(5.0) is True
    assert observed == [False]

    # worker 之外的线程看不到"当前作业"
    assert job_runner.current_job() is None
    assert job_runner.cancel_requested() is False


# ------------------------------------------------------------------ 超时≠失败

def test_slow_job_past_a_client_timeout_still_succeeds(runner):
    """请求超时不是任务失败：任务比"某个超时值"活得久，也仍然是 running。

    这是工作单 §3.3 明确写死的一条 —— 后端状态不能被前端的等待时限改写。
    """
    release = threading.Event()

    def slow(command, args, job):
        release.wait(timeout=5.0)
        return "终于好了"

    runner.set_executor(slow)
    runner.start()
    job = runner.submit("slow", {})
    assert _wait_until(lambda: job.status == job_runner.RUNNING)

    client_timeout = 0.15
    time.sleep(client_timeout)  # 模拟前端等不住了
    assert job.status == job_runner.RUNNING, "前端超时不能把任务改成失败或取消"
    assert job.status not in job_runner.TERMINAL
    assert runner.has_active_jobs() is True

    release.set()
    assert runner.wait_for_idle(5.0) is True
    assert job.status == job_runner.SUCCEEDED
    assert job.result == "终于好了"


def test_exit_protection_held_until_real_end(runner):
    """只有实际进入终态才解除退出保护。"""
    release = threading.Event()
    runner.set_executor(lambda command, args, job: release.wait(timeout=5.0))
    runner.start()

    assert runner.has_active_jobs() is False
    job = runner.submit("work", {})
    assert _wait_until(lambda: runner.has_active_jobs() is True)
    assert runner.active_job_names() == ["work"]

    release.set()
    assert runner.wait_for_idle(5.0) is True
    assert runner.has_active_jobs() is False
    assert job.status in job_runner.TERMINAL


# ------------------------------------------------------------------ 有界性

def test_queue_is_bounded_and_refuses_clearly(runner):
    """MAX_PENDING counts waiting jobs, not the currently executing one."""
    release = threading.Event()
    entered = threading.Event()

    def execute(command, args, job):
        entered.set()
        return release.wait(timeout=5.0)

    runner.set_executor(execute)
    runner.start()
    runner.submit("first", {})
    try:
        assert entered.wait(timeout=5.0)
        # has_active_jobs() also includes queued jobs: it cannot synchronize
        # this boundary. Wait for execution before filling all pending slots.
        for _ in range(job_runner.MAX_PENDING):
            runner.submit("filler", {})
        with pytest.raises(job_runner.QueueFull):
            runner.submit("overflow", {})
    finally:
        release.set()
    assert runner.wait_for_idle(5.0) is True


def test_completed_history_is_bounded():
    """完成记录有界保留，长时间运行不会把内存吃光。"""
    runner = job_runner.JobRunner()
    runner.set_executor(lambda command, args, job: "ok")
    runner.start()
    try:
        total = job_runner.MAX_HISTORY + 15
        for _ in range(total):
            runner.submit("work", {})
            assert runner.wait_for_idle(5.0) is True
        kept = len(runner.list_jobs(active_only=False))
        assert kept <= job_runner.MAX_HISTORY
    finally:
        runner.stop(timeout=2.0)


def test_live_jobs_are_never_trimmed():
    """裁剪只能丢终态记录，绝不能把还活着的任务丢掉。"""
    release = threading.Event()
    runner = job_runner.JobRunner(max_pending=100, max_history=3)
    runner.set_executor(lambda command, args, job: release.wait(timeout=5.0))
    runner.start()
    try:
        job = runner.submit("long", {})
        assert _wait_until(lambda: job.status == job_runner.RUNNING)

        for _ in range(12):
            runner.submit("filler", {})
        assert runner.get(job.id) is not None, "进行中的任务不能被历史裁剪吃掉"

        release.set()
    finally:
        runner.stop(timeout=2.0)


# ------------------------------------------------------------------ 分类与快照

def test_control_commands_are_not_queued():
    """控制命令集合必须覆盖"必须立刻答复"的那几个。"""
    for name in ("ping", "state", "shutdown", "job_status", "job_list", "cancel_job"):
        assert name in job_runner.CONTROL_COMMANDS
    for name in ("start_migration", "ocr_recognize", "import_models"):
        assert name not in job_runner.CONTROL_COMMANDS


def test_snapshot_excludes_payloads():
    """快照只暴露状态元信息，不带上 args/result 原文 —— 防密钥泄露。"""
    job = job_runner.Job(id="abc", command="start", status=job_runner.RUNNING)
    snapshot = job.snapshot()
    assert set(snapshot) == {"jobId", "command", "status", "sequence"}
    assert snapshot["jobId"] == "abc"


def test_sequence_increases_for_ordering(runner):
    """事件带顺序号，前端才能丢弃迟到/乱序事件。"""
    seen: list[int] = []
    runner._on_event = lambda kind, payload: seen.append(payload["sequence"])
    runner.set_executor(lambda command, args, job: "ok")
    runner.start()
    runner.submit("work", {})
    assert runner.wait_for_idle(5.0) is True
    assert seen == sorted(seen) and len(set(seen)) == len(seen)
