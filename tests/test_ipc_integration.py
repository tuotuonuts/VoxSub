"""跨进程集成测试：真起 sidecar，真走 NDJSON（工作单 §3.9 L3）。

前面几个单元测试都在进程内替换了 reader/emit；这一层不替换任何东西 ——
真子进程、真管道、真 `main()`。它能抓到只有"真跑起来"才会暴露的问题：

  · `main()` 的接线（IpcLoop + JobRunner）是否真的在位；
  · 握手字段是否真的发到了线上；
  · **控制命令是否真能插在慢命令前面**（单元测试只能证明分派逻辑，
    证明不了当真有一条 2 秒的命令占着 worker 时读循环还能回话）。

安全与纪律：
  · 只跑只读命令，不迁移、不下载、不删除；
  · `CREATE_NO_WINDOW` / 不抢焦点；进程在 finally 里必被回收；
  · 配置隔离到 tmp_path（`LOCALAPPDATA`），绝不碰真实用户配置。

标记为 `integration`，不进默认门禁之外的路径。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR = REPO_ROOT / "frontend" / "backend" / "ipc_server.py"

pytestmark = pytest.mark.integration

#: 几条只读命令。一起排进 worker，制造"确实有活儿在队列里"的现场 ——
#: 单靠一条命令太看机器脸色（同一台机器上 run_self_check 冷的时候 2.9 秒、
#: 热的时候不到 1 秒），凑够几条才不至于让这条断言变成空转。
QUEUED_READ_ONLY_COMMANDS = ("run_self_check", "list_models", "list_capture_targets")

#: 控制命令必须在这么久之内得到答复（真机实测约 0.24 秒，留足余量）。
CONTROL_REPLY_BUDGET_SECONDS = 1.5


class SidecarHarness:
    """起一个真 sidecar，按 id 收发消息。"""

    def __init__(self, tmp_path: Path) -> None:
        env = dict(os.environ)
        # 配置/日志/台账全部隔离到临时目录，别碰真实用户数据。
        env["LOCALAPPDATA"] = str(tmp_path / "AppData")
        env["VOXSUB_ROOT"] = str(REPO_ROOT)
        env.pop("PYTHONPATH", None)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [sys.executable, str(SIDECAR)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=str(REPO_ROOT), bufsize=1,
            env=env, creationflags=flags,
        )
        self._inbox: "queue.Queue[dict]" = queue.Queue()
        #: 每条消息**到达**的时刻（monotonic）。判断"谁先答话"必须用到达时刻，
        #: 不能用"我什么时候去取它" —— 后者在消息已入队时恒为 0。
        self.arrivals: dict[object, float] = {}
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            arrived = time.monotonic()
            if "id" in message:
                self.arrivals[message["id"]] = arrived
            self._inbox.put(message)

    def await_arrivals(self, req_ids, timeout: float = 30.0) -> bool:
        """等到这些 id 的应答都到达。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(req_id in self.arrivals for req_id in req_ids):
                return True
            time.sleep(0.02)
        return all(req_id in self.arrivals for req_id in req_ids)

    def send(self, req_id: int, command: str, args: dict | None = None) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps({"id": req_id, "command": command, "args": args or {}}) + "\n")
        self.process.stdin.flush()

    def next_message(self, timeout: float = 10.0) -> dict | None:
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def await_reply(self, req_id: int, timeout: float = 20.0) -> tuple[dict | None, float]:
        """等到某个 id 的应答，返回 (消息, 从调用开始的秒数)。"""
        started = time.monotonic()
        deadline = started + timeout
        while time.monotonic() < deadline:
            message = self.next_message(timeout=max(0.05, deadline - time.monotonic()))
            if message is None:
                break
            if message.get("id") == req_id:
                return message, time.monotonic() - started
        return None, time.monotonic() - started

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.send(9999, "shutdown")
                time.sleep(0.3)
        except Exception:  # noqa: BLE001 - 收尾不该掩盖测试结果
            pass
        finally:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=10)


@pytest.fixture()
def sidecar(tmp_path):
    if not SIDECAR.is_file():
        pytest.skip(f"找不到 sidecar：{SIDECAR}")
    harness = SidecarHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def _handshake(harness: SidecarHarness) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        message = harness.next_message(timeout=5)
        if message is None:
            break
        if message.get("event") == "ready":
            return message
    pytest.fail("sidecar 没有发出 ready 握手")


# ------------------------------------------------------------------ 握手

def test_handshake_declares_protocol_and_readiness(sidecar):
    """§3.6：握手要能支撑"渲染层重载后重新同步"。"""
    ready = _handshake(sidecar)
    assert ready["version"]
    assert ready["protocolVersion"] >= 1
    assert ready["backendGeneration"], "要能区分后端代次（重载后判断是否换了进程）"
    assert ready["readiness"]["ready"] is True
    assert isinstance(ready["readiness"]["activeJobs"], list)
    assert "session" in ready


def test_ping_round_trips(sidecar):
    _handshake(sidecar)
    sidecar.send(1, "ping")
    reply, elapsed = sidecar.await_reply(1)
    assert reply is not None and reply["ok"] is True
    assert reply["data"]["version"]
    assert elapsed < CONTROL_REPLY_BUDGET_SECONDS


# ------------------------------------------------------------------ 响应性

def test_control_command_preempts_queued_work(sidecar):
    """**本层最重要的一条**：worker 手里有活儿、队列里还排着活儿的时候，
    读循环仍然能立刻答复控制命令。

    进程内单测只能证明分派逻辑；只有真起进程、真走管道，才能证明
    "停止/查询不会排在耗时任务后面"。
    """
    _handshake(sidecar)
    started = time.monotonic()
    for index, command in enumerate(QUEUED_READ_ONLY_COMMANDS, start=1):
        sidecar.send(index, command)
    control_id = len(QUEUED_READ_ONLY_COMMANDS) + 1
    sidecar.send(control_id, "state")  # 排在最后发，必须最先答

    wanted = list(range(1, control_id + 1))
    assert sidecar.await_arrivals(wanted, timeout=40), "有命令一直没回话"

    control_at = sidecar.arrivals[control_id]
    last_queued_at = max(sidecar.arrivals[index] for index in range(1, control_id))

    assert control_at - started < CONTROL_REPLY_BUDGET_SECONDS, (
        f"state 等了 {control_at - started:.2f}s，说明它排在了队列后面"
    )
    if last_queued_at - started >= 0.5:
        # 队列确实有厚度时才做"先答谁"的强断言。
        assert control_at < last_queued_at, (
            "控制命令比队列里的普通命令还晚答 —— 它排进了队列，"
            "这正是缺陷 #5（读循环被长任务堵住）的表现"
        )

    # 队列在这台机器上太快时（隔离配置下模型目录是空的，几条只读命令总共
    # 不到半秒），"先答谁"不能作为证据。**强证明在进程内那一条**：
    # tests/test_ipc_loop.py::test_control_commands_do_not_queue_behind_long_work
    # 用一条可控的慢命令确定性地验证插队；这里负责证明真实接线跑得通。


def test_commands_still_run_in_order_on_the_worker(sidecar):
    """插队只针对控制命令；普通命令必须保序（单 worker 是刻意的）。"""
    _handshake(sidecar)
    for index in (1, 2, 3):
        sidecar.send(index, "get_config")
    for index in (1, 2, 3):
        reply, _ = sidecar.await_reply(index)
        assert reply is not None and reply["ok"] is True, f"第 {index} 条没回话"


# ------------------------------------------------------------------ 协议防御

def test_unknown_command_is_rejected_with_a_code(sidecar):
    _handshake(sidecar)
    sidecar.send(7, "definitely_not_a_command")
    reply, _ = sidecar.await_reply(7)
    assert reply is not None
    assert reply["ok"] is False
    assert reply["code"] == "unknown_command"


def test_malformed_line_does_not_kill_the_process(sidecar):
    """坏行只记日志；后面的命令必须照常工作。"""
    _handshake(sidecar)
    assert sidecar.process.stdin is not None
    sidecar.process.stdin.write("这不是 JSON\n")
    sidecar.process.stdin.write(json.dumps([1, 2, 3]) + "\n")
    sidecar.process.stdin.flush()

    sidecar.send(8, "ping")
    reply, _ = sidecar.await_reply(8)
    assert reply is not None and reply["ok"] is True, "坏输入把后端弄死了"


def test_shutdown_exits_the_process_when_stdin_closes(sidecar):
    """退出路径：收到 shutdown 且**输入管道关闭**之后，进程必须真的退出。

    为什么必须关管道：``shutdown`` 的处理是"收好资源 + 起一个 0.2s 定时器让
    进程自己走"。定时器在子线程里，子线程里的 ``SystemExit`` **不会**终止整个
    进程 —— 真正让进程退出的信号是 stdin 到达 EOF（Electron 正是这么做的：
    发完 shutdown 就关管道）。所以只发 shutdown、不关管道，进程留着是对的，
    这不是 bug；把它写成"必须退出"会掩盖真实的退出机制。
    """
    _handshake(sidecar)
    sidecar.send(5, "shutdown")
    sidecar.await_arrivals([5], timeout=5)

    # Electron 侧的等价动作：关掉输入管道
    assert sidecar.process.stdin is not None
    sidecar.process.stdin.close()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and sidecar.process.poll() is None:
        time.sleep(0.1)
    assert sidecar.process.poll() is not None, (
        "输入管道关闭后进程仍然不退 —— main() 的收尾（runner.stop + service.close）"
        "可能卡住了"
    )
