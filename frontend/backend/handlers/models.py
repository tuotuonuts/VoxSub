"""模型库：列表、安装、卸载、目录、导入（IPC 适配层的一个业务域）。

模型列表的硬件评估、序列化与文件扫描分开；安装/卸载等操作保持既有契约。
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
from ipc_support import _dir_size, _human_size


def _profile_for_cards(failures: list[str]) -> Any:
    from voxsub.hardware import detect_hardware  # noqa: PLC0415
    try:
        return detect_hardware()
    except Exception as error:  # noqa: BLE001 - leave models visible, mark assessment unknown
        failures.append(f"硬件推荐评估失败: {type(error).__name__}: {error}")
        return None


def _rating_for_card(model: Any, profile: Any, failures: list[str]) -> dict[str, object]:
    from voxsub.catalog_cards import card_assessment  # noqa: PLC0415
    if profile is not None:
        try:
            return card_assessment(model, profile)
        except Exception as error:  # noqa: BLE001 - isolate one model's failure
            failures.append(f"{model.id} 推荐评估失败: {type(error).__name__}: {error}")
    return {"level": "unknown", "loadPercent": None, "reason": "暂时无法评估本机配置"}


def _catalog_item(model: Any, size: int, installed: bool, installed_bytes: int,
                  recommendation: dict[str, object]) -> dict[str, Any]:
    return {
        "id": model.id,
        "name": getattr(model, "name", model.id),
        "task": getattr(model, "task", "unknown"),
        "quality": int(getattr(model, "quality_score", 0) or 0),
        "sizeLabel": getattr(model, "size_label", "") or _human_size(size),
        "sizeBytes": size,
        "installedBytes": installed_bytes,
        "installed": installed,
        "builtin": bool(getattr(model, "builtin", False)),
        "runtime": getattr(model, "runtime", "") or "",
        "license": getattr(model, "license", "") or "",
        "languages": getattr(model, "languages", "") or "",
        "description": getattr(model, "description", "") or "",
        "tags": list(getattr(model, "tags", ()) or ()),
        "officialRepo": getattr(model, "official_repo", "") or "",
        "recommendation": recommendation,
        # 硬件支持必须如实呈现，禁止把"未验证"显示成"可用"
        "gpuSupported": bool(getattr(model, "gpu_supported", False)),
        "igpuSupported": bool(getattr(model, "igpu_supported", False)),
        "npuSupported": bool(getattr(model, "npu_supported", False)),
        "minRamGb": float(getattr(model, "min_ram_gb", 0) or 0),
    }


class ModelsHandlers:
    """模型库：列表、安装、卸载、目录、导入。"""

    def _cmd_list_models(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.model_catalog import CATALOG  # noqa: PLC0415

        marketplace = self._marketplace(args)

        items = []
        failures: list[str] = []
        profile = _profile_for_cards(failures)
        for model in CATALOG:
            size = int(getattr(model, "download_bytes", 0) or 0)

            # is_installed 收的是 ModelSpec 对象。先前误传 model.id（字符串）会抛
            # AttributeError，而当时的 except 把它吞成 installed=False —— 界面因此
            # 静默显示"全部未安装"。这里改为：失败要留痕，绝不假装成正常结果。
            try:
                installed = bool(marketplace.is_installed(model))
            except Exception as error:  # noqa: BLE001 - 需上报而不是吞掉
                installed = False
                failures.append(f"{model.id}: {type(error).__name__}: {error}")

            installed_bytes = 0
            if installed:
                try:
                    directory = marketplace.available_model_dir(model)
                    installed_bytes = _dir_size(directory)
                except Exception as error:  # noqa: BLE001
                    failures.append(f"{model.id} 体积统计失败: {error}")

            recommendation = _rating_for_card(model, profile, failures)
            items.append(_catalog_item(model, size, installed, installed_bytes, recommendation))

        for line in failures:
            print(f"[list_models] {line}", file=sys.stderr)

        return {
            "models": items,
            "modelsRoot": str(marketplace.models_dir),
            "lookupRoots": [str(p) for p in marketplace._lookup_roots],
            "diagnostics": failures,
        }

    def _cmd_install_model(self, pipeline: Any, args: dict[str, Any]) -> dict[str, Any]:
        model_id = str(args.get("model_id", ""))
        marketplace = self._marketplace(args)
        spec = self._spec(model_id)

        def _progress(done: int, total: int, stage: str) -> None:
            _event("download", modelId=model_id, completed=done,
                   total=total, stage=str(stage))

        preference = str(args.get("source", "auto"))
        if preference not in {"auto", "global", "china"}:
            raise ValueError("无效下载源，须为 auto / global / china")
        marketplace.install(spec, preference=preference, progress=_progress)
        return {"model_id": model_id}

    def _cmd_uninstall_model(self, args: dict[str, Any]) -> dict[str, Any]:
        model_id = str(args.get("model_id", ""))
        marketplace = self._marketplace(args)
        marketplace.uninstall(self._spec(model_id))
        return {"model_id": model_id}

    def _cmd_model_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        model_id = str(args.get("model_id", ""))
        marketplace = self._marketplace(args)
        spec = self._spec(model_id)
        # 已安装时给真实所在目录（可能在旧存储位置），未安装时给即将写入的位置
        directory = (
            marketplace.available_model_dir(spec)
            if marketplace.is_installed(spec)
            else marketplace.model_dir(spec)
        )
        return {"path": str(directory), "installed": marketplace.is_installed(spec)}

    def _cmd_import_models(self, args: dict[str, Any]) -> dict[str, Any]:
        """把别处的模型并入当前模型目录（Qt 版「迁移已有模型」）。"""
        # migrate_models 定义在 voxsub.model_storage，**不在** model_catalog。
        # 之前写错模块名，打包版实测直接 ImportError：
        #   ImportError: cannot import name 'migrate_models' from 'voxsub.model_catalog'
        from voxsub.model_storage import migrate_models  # noqa: PLC0415

        source = Path(str(args.get("source", "")))
        if not source.is_dir():
            raise FileNotFoundError(f"源目录不存在：{source}")

        destination = Path(
            str(args.get("destination") or self._marketplace(args).models_dir)
        )
        destination.mkdir(parents=True, exist_ok=True)

        result = migrate_models(source, destination)
        return {
            "moved": int(getattr(result, "moved_paths", 0) or 0),
            "skipped": int(getattr(result, "kept_existing_paths", 0) or 0),
            "destination": str(destination),
        }
