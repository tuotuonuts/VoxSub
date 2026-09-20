"""IPC 协议层：stdout 的**唯一**写入点、事件发射、进程退出。

为什么单独一层：读循环线程（控制命令）与作业 worker 线程都会写 stdout，
必须串行化；而且这一层被 ipc_server 与各 handler 共同使用，放在入口文件里
会形成循环 import。ipc_server 会再导出这些名字，既有引用点不受影响。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

#: 真正的 stdout。**必须在 ipc_server 把 sys.stdout 换成 stderr 之前**捕获，
#: 否则协议输出会混进诊断流。
_PROTOCOL_OUT = sys.stdout

#: 保护 stdout 写入。多线程写非串行化会把两行 JSON 交错在一起。
_PROTOCOL_LOCK = threading.Lock()


def _emit(payload: dict[str, Any]) -> None:
    # 加锁的理由：读循环线程（控制命令）和作业 worker 线程会同时写 stdout。
    # 没有锁就可能把两行 JSON 交错在一起，前端收到半截 JSON 只能丢包。
    with _PROTOCOL_LOCK:
        try:
            _PROTOCOL_OUT.write(json.dumps(payload, ensure_ascii=False) + "\n")
            _PROTOCOL_OUT.flush()
        except (BrokenPipeError, ValueError):
            raise SystemExit(0) from None


def _event(kind: str, **fields: Any) -> None:
    _emit({"event": kind, **fields})


def _cancel_requested() -> bool:
    """当前作业是否被请求取消。

    长任务实现用它在**安全边界**检查取消：迁移在每步之间查，OCR 这类
    无法立即打断的原生调用则不查 —— 由作业执行器在结果返回后丢弃，
    并落 ``cancelled`` 终态（不谎报、不提前解除退出保护）。
    """
    try:
        import job_runner  # noqa: PLC0415
    except ImportError:
        return False
    return job_runner.cancel_requested()


def _now_iso() -> str:
    import datetime  # noqa: PLC0415

    return datetime.datetime.now().isoformat(timespec="seconds")


def _exit_process() -> None:
    """在计时器线程里结束进程（lambda 里不能 raise）。"""
    raise SystemExit(0)

