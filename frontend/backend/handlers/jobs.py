"""后台任务查询与取消（IPC 适配层的一个业务域）。

方法体是从 ipc_server.py **原样搬移**过来的，只改了所在文件；
共享的协议与工具依赖收在 ipc_protocol / ipc_support 里。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ipc_protocol import _event


class JobsHandlers:
    """后台任务查询与取消。"""

    def _cmd_job_list(self, args: dict[str, Any]) -> dict[str, Any]:
        """列出任务。默认只回还活着的，``include_finished=True`` 回最近的历史。"""
        runner = self._job_runner
        if runner is None:
            return {"jobs": [], "active": []}
        jobs = runner.list_jobs(active_only=not bool(args.get("include_finished")))
        return {"jobs": [job.snapshot() for job in jobs],
                "active": runner.active_job_names()}

    def _cmd_job_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """按 jobId 查任务状态。"""
        runner = self._job_runner
        job_id = str(args.get("job_id") or args.get("jobId") or "")
        if runner is None:
            return {"ok": False, "code": "no_runner", "detail": "作业执行器未启用"}
        job = runner.get(job_id)
        if job is None:
            return {"ok": False, "code": "unknown_job", "detail": f"没有这个任务：{job_id}"}
        return {"ok": True, "job": job.snapshot()}

    def _cmd_cancel_job(self, args: dict[str, Any]) -> dict[str, Any]:
        """请求取消。

        返回 ``cancelling`` 表示"已受理"，**不等于**已取消：真正的
        ``cancelled`` 由执行器在安全边界落定，并通过 job 事件送达。
        """
        runner = self._job_runner
        job_id = str(args.get("job_id") or args.get("jobId") or "")
        if runner is None:
            return {"ok": False, "code": "no_runner", "detail": "作业执行器未启用"}
        try:
            return runner.cancel(job_id)
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "code": type(error).__name__, "detail": str(error)}
