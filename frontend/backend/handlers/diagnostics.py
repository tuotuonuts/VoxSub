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


def _check_signature(config: dict[str, Any]) -> tuple:
    keys = ("file_translation_mode", "speech_model_id", "speech_device", "speech_output", "mode", "lang_pair", "stt_provider", "asr_model_id", "translate_tier", "translate_model_id",
            "models_root", "mic_device_id", "loopback_device_id", "capture_process_id", "record_with_translation")
    return tuple((key, config.get(key)) for key in keys)


class DiagnosticsHandlers:
    """诊断：自检、导出、日志、更新日志、设备与硬件档案。"""

    def _cmd_record_overlay_diagnostic(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.logging_setup import get_logger
        states = {key: args.get(key) for key in ("clippingCheck", "materialCheck")}
        if any(value not in ("not_run", "checking", "pass", "fail") for value in states.values()):
            raise ValueError("Invalid native diagnostic state")
        states.update(active=args.get("active") is True, desktopCheck="not_run",
                      fallbackReason=args.get("fallbackReason") if args.get("fallbackReason") in
                      ("native_verification_failed", "native_apply_failed", "transparent_material_incompatible") else None)
        get_logger("overlay_surface").info("OVERLAY_SURFACE %s", json.dumps(states))
        return {"recorded": True}

    def _cmd_run_self_check(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.config_store import ConfigStore
        from voxsub.diagnostic_runtime import quick_checks
        config = dict(ConfigStore().load())
        results = quick_checks(config, self._pipeline)
        self._diagnostic_configuration = _check_signature(config)
        self._diagnostic_results = results
        self._diagnostic_checked_at = time.time()
        return {"results": results, "scope": "quick_read_only", "checked_at": time.time()}

    def _cmd_export_diagnostics(self, args: dict[str, Any]) -> dict[str, Any]:
        """Export latest check snapshot, never rerun smoke tests or include raw content."""
        from voxsub.config_store import ConfigStore
        from voxsub.diagnostics import export_report
        from voxsub.diagnostic_privacy import export_logs, redact
        from voxsub.diagnostic_runtime import diagnostic_snapshot
        path = Path(str(args.get("path", "")))
        config = dict(ConfigStore().load())
        cached_current = getattr(self, "_diagnostic_configuration", None) == _check_signature(config)
        results = getattr(self, "_diagnostic_results", []) if cached_current else []
        snapshot = diagnostic_snapshot(config, self._pipeline,
                                       environment=bool(args.get("environment")) and getattr(self, "_developer_enabled", False))
        snapshot["self_check_at_unix"] = getattr(self, "_diagnostic_checked_at", None) if cached_current else None
        snapshot["self_check_scope"] = "cached_current_configuration" if cached_current else "not_run_or_configuration_changed"
        text = redact(export_report(results)) + "\n" + json.dumps(snapshot, ensure_ascii=False, indent=2)
        log_text = str(args.get("log_text") or "")
        if log_text:
            text += "\nLOG_METADATA\n" + export_logs(log_text) + "\n"
        if path.parent and str(path) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return {"path": str(path), "bytes": len(text.encode("utf-8"))}
        return {"text": text}

    def _cmd_developer_mode(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.logging_setup import stop_diagnostic_session
        enabled = args.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        self._developer_enabled = enabled
        if not enabled:
            stop_diagnostic_session()
        return {"enabled": enabled}

    def _cmd_diagnostic_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.config_store import ConfigStore
        from voxsub.diagnostic_runtime import diagnostic_snapshot
        if not getattr(self, "_developer_enabled", False):
            raise PermissionError("Developer mode is disabled")
        return diagnostic_snapshot(ConfigStore().load(), self._pipeline,
                                   environment=bool(args.get("environment")))

    def _cmd_diagnostic_session(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.logging_setup import start_diagnostic_session, stop_diagnostic_session, diagnostic_session_snapshot
        if not getattr(self, "_developer_enabled", False):
            raise PermissionError("Developer mode is disabled")
        if args.get("action") == "start":
            duration = args.get("seconds", 300)
            if not isinstance(duration, int) or isinstance(duration, bool) or not 60 <= duration <= 1200:
                raise ValueError("Duration must be 60..1200 seconds")
            start_diagnostic_session(duration)
        elif args.get("action") == "stop":
            stop_diagnostic_session()
        elif args.get("action") != "status":
            raise ValueError("Unknown session action")
        return {"session": diagnostic_session_snapshot()}

    def _cmd_recent_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        """最近的日志。

        source="memory"（默认）读本进程缓冲，响应快、只含本次运行；
        source="file" 读磁盘 voxsub.log 的尾部，能拿到历史运行记录——
        排障时用户要的通常是后者（崩溃发生在下次启动之前）。
        """
        from voxsub.diagnostic_trace import RUN_ID
        limit = max(1, min(2000, int(args.get("limit", 200) or 200)))
        source = str(args.get("source", "memory"))

        if source == "file":
            from voxsub.logging_setup import tail_log_file  # noqa: PLC0415

            text = tail_log_file(limit)
            return {"text": text, "source": "file", "lines": len(text.splitlines()), "run_id": RUN_ID}

        return {"logs": self._log_buffer[-limit:], "source": "memory", "run_id": RUN_ID}

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

        from voxsub.hardware_inventory import hardware_inventory  # noqa: PLC0415

        profile = detect_hardware()
        inventory = hardware_inventory()
        cpus = inventory["categories"]["cpu"]["items"]
        cpu = " / ".join(item["Name"] for item in cpus if item["Name"]) or str(profile.cpu_name)
        return {
            "cpu": cpu,
            "physicalCores": int(profile.physical_cores),
            "logicalCores": int(profile.logical_cores),
            "ramGb": float(profile.ram_gb),
            "gpu": str(profile.gpu_name),
            "vramGb": float(profile.vram_gb),
            "gpuProvider": str(profile.gpu_provider),
            "npu": str(profile.npu_name),
            "inventory": inventory,
        }
