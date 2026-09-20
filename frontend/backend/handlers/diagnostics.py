"""诊断：自检、导出、日志、更新日志、设备与硬件档案（IPC 适配层的一个业务域）。

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


class DiagnosticsHandlers:
    """诊断：自检、导出、日志、更新日志、设备与硬件档案。"""

    def _cmd_run_self_check(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.diagnostics import run_self_check  # noqa: PLC0415

        results = []
        for item in run_self_check():
            results.append({
                "check": str(item.get("check", "")),
                "status": str(item.get("status", "")),
                "detail": str(item.get("detail", "")),
            })
        return {"results": results}

    def _cmd_export_diagnostics(self, args: dict[str, Any]) -> dict[str, Any]:
        """导出诊断报告；可附带日志文本（诊断页「导出日志」）。"""
        from voxsub.diagnostics import export_report  # noqa: PLC0415

        path = Path(str(args.get("path", "")))
        text = export_report()

        # 日志导出复用同一入口：把日志附在报告之后，而不是另建命令
        log_text = str(args.get("log_text") or "")
        if log_text:
            text = f"{text}\n\n{'=' * 60}\n日志快照\n{'=' * 60}\n{log_text}\n"

        if path.parent and str(path) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return {"path": str(path), "bytes": len(text.encode("utf-8"))}
        return {"text": text}

    def _cmd_recent_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        """最近的日志。

        source="memory"（默认）读本进程缓冲，响应快、只含本次运行；
        source="file" 读磁盘 voxsub.log 的尾部，能拿到历史运行记录——
        排障时用户要的通常是后者（崩溃发生在下次启动之前）。
        """
        limit = int(args.get("limit", 200) or 200)
        source = str(args.get("source", "memory"))

        if source == "file":
            from voxsub.logging_setup import tail_log_file  # noqa: PLC0415

            text = tail_log_file(limit)
            return {"text": text, "source": "file", "lines": len(text.splitlines())}

        return {"logs": self._log_buffer[-limit:], "source": "memory"}

    def _cmd_clear_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        """清除本机日志文件（保留模型、配置、凭据、已导出报告）。

        与 Qt 版「清除本机日志」语义一致：活动日志就地截断（应用可继续写），
        历史轮转文件删除。
        """
        from voxsub.logging_setup import clear_local_logs  # noqa: PLC0415

        result = clear_local_logs()
        self._log_buffer.clear()
        return dict(result) if isinstance(result, dict) else {"cleared": True}

    def _cmd_log_path(self, args: dict[str, Any]) -> dict[str, Any]:
        """日志文件位置，供界面「打开日志所在文件夹」。"""
        try:
            from voxsub.logging_setup import _log_dir  # noqa: PLC0415

            return {"path": str(_log_dir())}
        except (ImportError, AttributeError):
            local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            return {"path": str(Path(local) / "VoxSub" / "logs")}

    def _cmd_release_notes(self, args: dict[str, Any]) -> dict[str, Any]:
        """更新日志（默认只回最近一版；include_history=True 回全部）。"""
        # 从 voxsub.release_notes 导入，而不是 voxsub.ui.release_notes ——
        # 后者在导入时就需要 PySide6，会让"打包时不带 Qt"直接失败。
        from voxsub.release_notes import RELEASE_HISTORY  # noqa: PLC0415

        english = str(args.get("language", "zh")).startswith("en")
        include_history = bool(args.get("include_history", False))
        notes = list(RELEASE_HISTORY)
        if not include_history:
            notes = notes[:1]

        items = []
        for note in notes:
            title = note.title_en if english else note.title_zh
            body_items = note.items_en if english else note.items_zh
            items.append({
                "version": note.version,
                "title": title,
                "body": "\n".join(f"· {line}" for line in body_items),
            })
        return {"notes": items, "total": len(RELEASE_HISTORY)}

    def _cmd_list_devices(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.router import enumerate_devices  # noqa: PLC0415

        devices = []
        for device in enumerate_devices():
            devices.append({
                "provider": str(device.provider),
                "name": str(device.name),
                "kind": str(getattr(device, "kind", "")),
                "scoreMs": getattr(device, "score_ms", None),
            })
        return {"devices": devices}

    def _cmd_hardware_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.hardware import detect_hardware  # noqa: PLC0415

        profile = detect_hardware()
        return {
            "cpu": str(profile.cpu_name),
            "physicalCores": int(profile.physical_cores),
            "logicalCores": int(profile.logical_cores),
            "ramGb": float(profile.ram_gb),
            "gpu": str(profile.gpu_name),
            "vramGb": float(profile.vram_gb),
            "gpuProvider": str(profile.gpu_provider),
            "npu": str(profile.npu_name),
        }
